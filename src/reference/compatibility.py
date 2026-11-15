"""Explicit, all-or-nothing path rebinding with portable graph witnesses."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from shapely.geometry import shape, Point

from ..domain.reference import RegistryPath, DirectedEdge
from ..geo.linear import path_geometry
from ..geo.crs import project
from ..storage.files import read_json, write_json, write_csv, sha256, digest
from ..storage.paths import safe_output
from .graph import Graph
from .semantic import checked_semantics, SEMANTIC_METHOD

COMPATIBILITY_METHOD = "graph_path_compatibility_v1"
BUNDLE_CONTRACT = "candidate_runtime_v2"
OPTIONAL_ARTIFACTS = frozenset({
    "shortlist.json", "static.json", "candidates.geojson",
    "candidate_assessment.csv", "review.html", "capture_plan_local.json",
})


def candidate_bundle(folder):
    folder = Path(folder).resolve()
    manifest = read_json(folder / "manifest.json")
    artifacts = manifest.get("artifacts")
    if not artifacts or "paths.json" not in artifacts:
        raise ValueError("CANDIDATE_ARTIFACT_MANIFEST_REQUIRED")
    contract = manifest.get("bundle_contract")
    if contract not in (None, BUNDLE_CONTRACT):
        raise ValueError("CANDIDATE_BUNDLE_CONTRACT_UNSUPPORTED")
    for name, checksum in artifacts.items():
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise ValueError("CANDIDATE_ARTIFACT_PATH_INVALID")
        path = (folder / name).resolve()
        if not path.is_relative_to(folder):
            raise ValueError("CANDIDATE_ARTIFACT_PATH_INVALID")
        if not path.exists() and contract == BUNDLE_CONTRACT and name in OPTIONAL_ARTIFACTS:
            continue
        if not path.is_file() or sha256(path) != checksum:
            raise ValueError("CANDIDATE_ARTIFACT_HASH_MISMATCH")
    paths = [RegistryPath.model_validate(p) for p in read_json(folder / "paths.json")]
    if not paths or len({(p.segment_id, p.direction_id) for p in paths}) != len(paths):
        raise ValueError("EMPTY_OR_DUPLICATE_CANDIDATES")
    return manifest, paths


def descriptor_edges(folder, edge_ids):
    with closing(
        sqlite3.connect((folder / "graph.sqlite").resolve().as_uri() + "?mode=ro", uri=True)
    ) as db:
        result = {}
        for edge_id in sorted(edge_ids):
            row = db.execute("SELECT payload FROM edges WHERE id=?", (edge_id,)).fetchone()
            if row is None:
                raise ValueError("PATH_EDGE_MISSING: " + edge_id)
            result[edge_id] = json.loads(row[0])
    return result


def small_graph(meta, edges):
    nodes = {}
    for e in edges.values():
        nodes[e["start"]] = [*e["coords"][0], {}]
        nodes[e["end"]] = [*e["coords"][-1], {}]
    return Graph(meta["version"], [DirectedEdge(**p) for p in edges.values()], nodes, meta["crs"])


def verify_paths(paths, graph):
    for path in paths:
        if path.graph_version != graph.version:
            raise ValueError("PATH_GRAPH_VERSION_MISMATCH")
        line = path_geometry(graph, path.edges)
        if abs(line.length - path.length_m) > 1e-6:
            raise ValueError("PATH_LENGTH_MISMATCH: " + path.segment_id)
        stored = project(shape(path.geometry), graph.crs)
        if (
            stored.hausdorff_distance(line) > 1e-6
            or Point(stored.coords[0]).distance(Point(line.coords[0])) > 1e-6
            or Point(stored.coords[-1]).distance(Point(line.coords[-1])) > 1e-6
        ):
            raise ValueError("PATH_GEOMETRY_OR_DIRECTION_MISMATCH: " + path.segment_id)
    return True


def compatibility_export(graph_folder, candidates, output, config):
    graph_folder, candidates, output = map(
        lambda p: Path(p).resolve(), (graph_folder, candidates, output)
    )
    # Output is a QA/project artifact, or a new file in the candidate data subtree.
    allowed = config.study.project_files_root.resolve()
    if output.is_relative_to(config.study.data_root.resolve() / "reference/candidates"):
        allowed = config.study.data_root.resolve() / "reference/candidates"
    safe_output(
        output,
        allowed,
        (
            config.study.osm_pbf,
            config.study.project_root / "src",
            config.study.project_root / "config",
        ),
    )
    manifest, paths = candidate_bundle(candidates)
    meta, semantic = checked_semantics(graph_folder)
    if manifest["graph_version"] != meta["version"]:
        raise ValueError("CANDIDATE_GRAPH_MISMATCH")
    edges = descriptor_edges(graph_folder, {e.edge_id for p in paths for e in p.edges})
    verify_paths(paths, small_graph(meta, edges))
    payload = {
        "method": COMPATIBILITY_METHOD,
        "semantic_method": SEMANTIC_METHOD,
        "semantic_sha256": semantic,
        "graph_version": meta["version"],
        "crs": meta["crs"],
        "source_graph_manifest_sha256": sha256(graph_folder / "manifest.json"),
        "source_candidate_manifest_sha256": sha256(candidates / "manifest.json"),
        "source_paths_sha256": sha256(candidates / "paths.json"),
        "paths": [p.model_dump(mode="json") for p in paths],
        "edges": edges,
    }
    document = {"payload": payload, "sha256": digest(payload)}
    write_json(output, document, immutable=True)
    return {
        "status": "complete",
        "compatibility": str(output),
        "semantic_sha256": semantic,
        "paths": len(paths),
    }


def rebind_paths(graph_folder, candidates, compatibility, config):
    graph_folder, candidates = Path(graph_folder).resolve(), Path(candidates).resolve()
    audit = (
        config.study.project_files_root
        / "reference_rebind"
        / digest(
            [
                sha256(Path(compatibility)),
                sha256(graph_folder / "manifest.json"),
                sha256(candidates / "manifest.json"),
            ]
        )[:20]
    )
    audit = safe_output(audit, config.study.project_files_root, (config.study.osm_pbf,))
    audit.mkdir(parents=True, exist_ok=True)
    try:
        return _rebind(graph_folder, candidates, Path(compatibility), config, audit)
    except Exception as exc:
        write_json(
            audit / "failure.json",
            {"status": "failed", "reason": str(exc), "error_type": type(exc).__name__},
        )
        raise


def _rebind(graph_folder, candidates, compatibility, config, audit):
    manifest, paths = candidate_bundle(candidates)
    document = read_json(compatibility)
    witness = document["payload"]
    if (
        document["sha256"] != digest(witness)
        or witness["method"] != COMPATIBILITY_METHOD
        or witness["semantic_method"] != SEMANTIC_METHOD
    ):
        raise ValueError("COMPATIBILITY_HASH_OR_METHOD_MISMATCH")
    if witness["source_candidate_manifest_sha256"] != sha256(
        candidates / "manifest.json"
    ) or witness["source_paths_sha256"] != sha256(candidates / "paths.json"):
        raise ValueError("COMPATIBILITY_CANDIDATE_MISMATCH")
    if witness["paths"] != [p.model_dump(mode="json") for p in paths]:
        raise ValueError("COMPATIBILITY_PATH_MISMATCH")
    source_meta = {"version": witness["graph_version"], "crs": witness["crs"]}
    source = small_graph(source_meta, witness["edges"])
    verify_paths(paths, source)
    meta, semantic = checked_semantics(graph_folder)
    if semantic != witness["semantic_sha256"]:
        raise ValueError("GRAPH_SEMANTICS_DIFFER")
    mapping, rows = {}, []
    for old_id, old in witness["edges"].items():
        key = f"{old['way_id']}:{old['first_index']}:{old['last_index']}:{old['traversal']}"
        new_id = meta["version"] + ":" + key
        new = descriptor_edges(graph_folder, {new_id})[new_id]
        if {k: v for k, v in old.items() if k != "id"} != {
            k: v for k, v in new.items() if k != "id"
        }:
            raise ValueError("EDGE_DESCRIPTOR_MISMATCH: " + old_id)
        mapping[old_id] = new_id
        rows.append(
            {
                "old_edge_id": old_id,
                "new_edge_id": new_id,
                "status": "equivalent",
                "length_m": new["length"],
            }
        )
    if len(set(mapping.values())) != len(mapping):
        raise ValueError("AMBIGUOUS_EDGE_CORRESPONDENCE")
    version = (
        "rebind_"
        + digest(
            [sha256(candidates / "manifest.json"), meta["version"], semantic, COMPATIBILITY_METHOD]
        )[:20]
    )
    rebound = [
        p.model_copy(
            update={
                "registry_version": version,
                "graph_version": meta["version"],
                "edges": [e.model_copy(update={"edge_id": mapping[e.edge_id]}) for e in p.edges],
            }
        )
        for p in paths
    ]
    new_edges = descriptor_edges(graph_folder, set(mapping.values()))
    target = small_graph(meta, new_edges)
    verify_paths(rebound, target)
    for old, new in zip(paths, rebound):
        if not path_geometry(source, old.edges).equals_exact(
            path_geometry(target, new.edges), 1e-6
        ):
            raise ValueError("TRIMMED_GEOMETRY_MISMATCH")
        if old.geometry != new.geometry or old.geometry_sha256 != new.geometry_sha256:
            raise ValueError("REGISTRY_GEOMETRY_CHANGED")
    destination = config.candidate_output(version)
    if destination.exists():
        raise FileExistsError("Candidate version already exists: " + str(destination))
    destination.mkdir(parents=True)

    def remap(value):
        if isinstance(value, list):
            return [remap(x) for x in value]
        if isinstance(value, dict):
            return {mapping.get(k, k): remap(v) for k, v in value.items()}
        if isinstance(value, str):
            return mapping.get(value, value)
        return value

    artifacts = {}
    for name in manifest["artifacts"]:
        if not (candidates / name).is_file():
            continue  # candidate_bundle already rejected missing required inputs.
        if name in ("paths.json", "shortlist.json", "static.json", "candidates.geojson"):
            data = read_json(candidates / name)
            if name in ("paths.json", "shortlist.json"):
                lookup = {
                    (p.segment_id, p.direction_id): p.model_dump(mode="json") for p in rebound
                }
                data = [lookup[(p["segment_id"], p["direction_id"])] for p in data]
            else:
                data = remap(data)
            write_json(destination / name, data, immutable=True)
        elif name == "candidate_assessment.csv":
            import csv
            from ..preparation.assessment import write_assessment_csv

            with (candidates / name).open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                write_assessment_csv(destination / name, list(reader), reader.fieldnames)
        else:
            from ..storage.files import atomic_bytes

            atomic_bytes(destination / name, (candidates / name).read_bytes(), immutable=True)
        artifacts[name] = sha256(destination / name)
    if "review.html" in artifacts:
        from ..preparation.evidence import review_map

        review_map(destination / "review.html", rebound, config)
        artifacts["review.html"] = sha256(destination / "review.html")
    updated = dict(
        manifest,
        candidate_version=version,
        graph_version=meta["version"],
        artifacts=artifacts,
        graph_manifest_sha256=sha256(graph_folder / "manifest.json"),
        migration={
            "method": COMPATIBILITY_METHOD,
            "source_candidate_manifest_sha256": sha256(candidates / "manifest.json"),
            "semantic_sha256": semantic,
            "static_provenance": "same graph semantics; source methods/counts retained",
        },
    )
    write_json(destination / "manifest.json", updated, immutable=True)
    # A companion file avoids a circular hash between manifest and witness.
    portable = dict(
        witness,
        graph_version=meta["version"],
        crs=meta["crs"],
        source_graph_manifest_sha256=sha256(graph_folder / "manifest.json"),
        source_candidate_manifest_sha256=sha256(destination / "manifest.json"),
        source_paths_sha256=sha256(destination / "paths.json"),
        paths=[p.model_dump(mode="json") for p in rebound],
        edges=new_edges,
    )
    write_json(
        destination / "compatibility.json",
        {"payload": portable, "sha256": digest(portable)},
        immutable=True,
    )
    write_csv(audit / "correspondence.csv", rows)
    report = {
        "status": "complete",
        "candidate_version": version,
        "candidates": str(destination),
        "paths": len(rebound),
        "graph": str(graph_folder),
        "graph_version": meta["version"],
        "semantic_sha256": semantic,
        "audit": str(audit),
        "source_preserved": True,
        "approved_by_migration": False,
    }
    write_json(audit / "acceptance.json", report)
    return report


def portable_export(graph_folder, candidates, output, config):
    """Create a minimal transferable version without editing historical inputs."""
    import shutil

    candidates, output = Path(candidates).resolve(), Path(output).resolve()
    manifest, paths = candidate_bundle(candidates)
    safe_output(output, config.study.project_root / "data/reference/candidates",
                (config.study.osm_pbf, config.study.project_root / "src", config.study.project_root / "config"))
    if output.exists():
        raise FileExistsError(output)
    # Unknown artifacts remain mandatory: never silently discard future inputs.
    names = [name for name in manifest["artifacts"] if name not in OPTIONAL_ARTIFACTS]
    output.mkdir(parents=True)
    for name in names:
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(candidates / name, target)
    updated = dict(manifest, candidate_version=output.name, bundle_contract=BUNDLE_CONTRACT,
                   portable_source_manifest_sha256=sha256(candidates / "manifest.json"))
    write_json(output / "manifest.json", updated, immutable=True)
    result = compatibility_export(graph_folder, output, output / "compatibility.json", config)
    candidate_bundle(output)
    return dict(result, candidates=str(output), candidate_version=output.name,
                required_files=["paths.json", "manifest.json"],
                compatibility_required="only when paths require graph rebinding")
