"""Official publication is a gated action, distinct from diagnostic exports."""

from datetime import datetime, timezone
from pathlib import Path
from ..domain.reference import RegistryPath
from ..geo.linear import path_geometry
from ..storage.files import digest, write_json, sha256, read_json
from .evidence import validate_evidence


def validate_registry(paths, graph, evidence, config):
    """Validate published scope for every declared date/slot, never just boolean readiness."""
    from shapely.geometry import shape, Point
    from ..geo.crs import project
    from .gates import (
        applicable,
        calibration_gate,
        source_contract_readiness,
        method_artifact_valid,
    )
    from datetime import datetime, time
    from zoneinfo import ZoneInfo

    reasons = []
    keys = set()
    geometries = set()
    evidence_ids = set()
    try:
        validate_evidence(
            [{k: v for k, v in row.items() if not k.startswith("_")} for row in evidence]
        )
    except (ValueError, TypeError):
        reasons.append("EVIDENCE_INVALID")
    if not paths:
        reasons.append("EMPTY_REGISTRY")
    if not config.study.dates:
        reasons.append("STUDY_DATES_REQUIRED")
    for path in paths:
        key = (path.registry_version, path.segment_id, path.direction_id)
        if key in keys:
            reasons.append("DUPLICATE_REGISTRY_KEY")
        keys.add(key)
        if path.graph_version != graph.version:
            reasons.append("GRAPH_VERSION_MISMATCH")
            continue
        try:
            line = path_geometry(graph, path.edges)
            declared = project(shape(path.geometry), graph.crs)
            if (
                declared.geom_type != "LineString"
                or not declared.is_valid
                or not line.is_simple
                or abs(line.length - path.length_m) > 1e-4
                or abs(declared.length - line.length) > 1e-4
                or declared.hausdorff_distance(line) > 1e-4
                or Point(declared.coords[0]).distance(Point(line.coords[0])) > 1e-4
                or Point(declared.coords[-1]).distance(Point(line.coords[-1])) > 1e-4
            ):
                reasons.append("REGISTRY_GEOMETRY_INVALID")
            signature = digest(list(line.coords))
            reverse = digest(list(reversed(line.coords)))
            if signature in geometries or reverse in geometries:
                reasons.append("DUPLICATE_OR_REVERSED_GEOMETRY")
            geometries.add(signature)
        except (ValueError, KeyError, TypeError):
            reasons.append("REGISTRY_GEOMETRY_INVALID")
        if path.approval_status != "approved" or not path.approved_by or path.approved_at is None:
            reasons.append("REVIEW_MISSING")
        scoped_records = [r for r in evidence if r.get("evidence_id") in path.evidence_ids]
        for day in config.study.dates:
            for slot in config.study.slots:
                when = datetime.combine(
                    day, time.fromisoformat(slot), ZoneInfo(config.study.timezone)
                )
                for kind in (
                    "legal_direction",
                    "map_review",
                    "matching_validation",
                    "source_direction_method",
                ):
                    selected, failures = applicable(scoped_records, kind, path, when, config)
                    passed = []
                    for item, payload in selected:
                        if (
                            kind == "legal_direction"
                            and payload.get("legal_direction_status") != "supported"
                        ):
                            continue
                        if (
                            kind == "map_review"
                            and payload.get("geometry_sha256") != path.geometry_sha256
                        ):
                            continue
                        if kind == "source_direction_method" and not method_artifact_valid(
                            item, payload
                        ):
                            continue
                        if kind == "matching_validation":
                            if (
                                payload.get("target_method") != config.sources.target_method
                                or not payload.get("validation_passed")
                                or payload.get("geometry_sha256") != path.geometry_sha256
                                or not payload.get("association_stable")
                            ):
                                continue
                            if (
                                payload.get("source_direction_coverage", 0)
                                < config.policy.minimum_coverage
                                or payload.get("target_valid_coverage", 0)
                                < config.policy.minimum_coverage
                            ):
                                continue
                            hashes = payload.get("input_raw_hashes", {})
                            try:
                                if not hashes or any(
                                    sha256(Path(p)) != h for p, h in hashes.items()
                                ):
                                    continue
                            except OSError:
                                continue
                        passed.append(item)
                    if not passed:
                        reasons.extend([kind.upper() + "_EVIDENCE_MISSING"] + failures)
                    evidence_ids.update(item.evidence_id for item in passed)
                calibration = calibration_gate(scoped_records, path, when, config)
                if not calibration["ready"]:
                    reasons.extend(calibration["reasons"])
                evidence_ids.update(calibration["evidence_ids"])
        if (
            path.legal_direction_status != "supported"
            or path.source_direction_status != "supported"
        ):
            reasons.append("DIRECTION_UNVERIFIED")
    source = source_contract_readiness(config)
    if not source["ready"]:
        reasons.extend(source["reasons"])
    return {
        "valid": not reasons,
        "reasons": sorted(set(reasons)),
        "records": len(paths),
        "validated_dates": [d.isoformat() for d in config.study.dates],
        "evidence_ids": sorted(evidence_ids),
        "source_contract": source,
    }


def publish_registry(paths, graph, evidence, config):
    result = validate_registry(paths, graph, evidence, config)
    if not result["valid"]:
        return result
    evidence_clean = [{k: v for k, v in item.items() if not k.startswith("_")} for item in evidence]
    version = (
        "registry_"
        + digest(
            {
                "paths": [p.model_dump(mode="json") for p in paths],
                "evidence": evidence_clean,
                "config_hash": config.hash,
            }
        )[:20]
    )
    published = [p.model_copy(update={"registry_version": version}) for p in paths]
    folder = config.output("registry/" + version)
    folder.mkdir(parents=True, exist_ok=False)
    write_json(
        folder / "paths.json", [p.model_dump(mode="json") for p in published], immutable=True
    )
    write_json(folder / "evidence.json", evidence_clean, immutable=True)

    write_json(
        folder / "manifest.json",
        {
            "version": version,
            "validation": result,
            "published_at": datetime.now(timezone.utc).isoformat(),
            "files": {name: sha256(folder / name) for name in ("paths.json", "evidence.json")},
            "config_hash": config.hash,
            "graph_version": graph.version,
            "graph_artifact": str(graph.artifact_root) if hasattr(graph, "artifact_root") else None,
            "target_method": config.sources.target_method,
        },
        immutable=True,
    )
    write_json(config.output("registry/current.json"), {"version": version, "path": str(folder)})
    return {**result, "path": str(folder), "version": version}


def read_paths(path: Path):
    from ..storage.files import read_json

    return [RegistryPath.model_validate(item) for item in read_json(path)]


def validate_published_registry(folder, plan, config):
    """Revalidate hashed bundle and scoped evidence before any official HTTP call."""
    reasons = []
    if folder is None:
        return {"valid": False, "reasons": ["OFFICIAL_REGISTRY_ARTIFACT_MISSING"]}
    try:
        folder = Path(folder).resolve()
        manifest = read_json(folder / "manifest.json")
        if not manifest.get("files") or any(
            sha256(folder / name) != checksum for name, checksum in manifest["files"].items()
        ):
            return {"valid": False, "reasons": ["REGISTRY_ARTIFACT_HASH_MISMATCH"]}
        paths = read_paths(folder / "paths.json")
        if manifest.get("target_method") != config.sources.target_method:
            reasons.append("REGISTRY_TARGET_METHOD_MISMATCH")
        if (
            manifest["version"] != plan.registry_version
            or digest([p.model_dump(mode="json") for p in paths]) != plan.registry_sha256
        ):
            reasons.append("PLAN_REGISTRY_MISMATCH")
        if manifest["graph_version"] != plan.graph_version:
            reasons.append("PLAN_GRAPH_MISMATCH")
        from ..reference.graph import load_graph
        from .gates import read_evidence

        graph = load_graph(Path(manifest["graph_artifact"]))
        result = validate_registry(paths, graph, read_evidence(folder / "evidence.json"), config)
        reasons.extend(result["reasons"])
    except (OSError, ValueError, TypeError, KeyError):
        reasons.append("REGISTRY_ARTIFACT_INVALID")
    return {"valid": not reasons, "reasons": sorted(set(reasons))}
