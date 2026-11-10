"""T0â€“T7 orchestration. No HTTP client or live backfill dependency."""

from datetime import datetime, timezone
from pathlib import Path
import uuid
from ..storage.files import digest, write_json, read_json, sha256
from ..reference.static import static_features
from ..matching.direction import automatic_direction, legal_direction, profiles_agree, profile_consensus, METHOD
from ..matching.decode import decode_location
from ..matching.map_reader import OSMMapReader
from ..matching.footprint import associate_footprint
from ..matching.intervals import partition_registry, compute_coverage
from ..matching.diagnostics import sensitivity, compare_associations
from ..geo.linear import path_geometry
from ..geo.crs import project
from shapely.geometry import LineString
from .inputs import load_inventory, network_off
from .normalize import normalize_snapshot
from .quality import assess_quality
from .target import compute_target
from .covariates import align_weather
from .dataset import canonical_rows, expected_keys
from .validate import validate_dataset
from .export import export_run
from ..domain.reference import RegistryPath
from ..domain.processing import Footprint, OUTPUT_SCHEMA, TARGET_METHOD
from ..reference.graph import Graph
from ..config import ResolvedConfig
from ..preparation.gates import (
    read_evidence,
    source_contract_readiness,
    calibration_gate,
)
from ..preparation.registry import validate_registry


def process_inventory(
    inventory_path: Path,
    paths: list[RegistryPath],
    graph: Graph,
    config: ResolvedConfig,
    *,
    force: bool = False,
    update_pointer: bool = True,
    scheduled_scope: set[str] | None = None,
) -> dict:
    """Offline T0â€“T7 run; preserve failed artifacts and never publish a scientific pass."""
    run_artifact = []
    with network_off():
        try:
            return _process(inventory_path, paths, graph, config, force, run_artifact, update_pointer, scheduled_scope)
        except Exception as exc:
            if run_artifact:
                write_json(
                    run_artifact[0] / "manifest.json",
                    {
                        "status": "failed",
                        "scientific_release": False,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "checksums": {},
                    },
                    immutable=True,
                )
            raise


def _process(inventory_path, paths, graph, config, force, run_artifact, update_pointer=True, scheduled_scope=None):
    inventory, snapshots, quarantine, hashes = load_inventory(inventory_path)
    evidence = read_evidence(config.study.evidence_file)
    source_gate = source_contract_readiness(config)
    registry_gate = validate_registry(paths, graph, evidence, config)
    for p in (config.study.evidence_file, config.study.source_evidence_file):
        if p and p.is_file():
            hashes[str(p)] = sha256(p)
    for record in evidence:
        from ..preparation.gates import artifact

        item, payload, failures = artifact(record)
        if not failures:
            artifact_path = Path(item.artifact_path or item.source_url_or_path)
            if not artifact_path.is_absolute():
                artifact_path = Path(record.get("_base", ".")) / artifact_path
            hashes[str(artifact_path.resolve())] = sha256(artifact_path)

    from ..provenance import source_fingerprint

    fingerprint = digest(
        {
            "inputs": hashes,
            "registry": [p.model_dump(mode="json") for p in paths],
            "graph": graph.version,
            "config": config.hash,
            "source": source_fingerprint(config.study.project_root),
            "scheduled_scope": sorted(scheduled_scope) if scheduled_scope is not None else None,
        }
    )
    pointer = config.output("processed/diagnostic_current.json")
    if not force and pointer.is_file():
        previous = read_json(pointer)
        if previous["fingerprint"] == fingerprint:
            from .validate import validate_run

            from ..storage.relocate import resolve_artifact
            previous_path = resolve_artifact(previous["path"], config.study.relocation_file)
            check = validate_run(previous_path)
            if check["valid"]:
                return {"run": str(previous_path), "cache_hit": True, "validation": check}
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    folder = config.output("processed/" + run_id)
    folder.mkdir(parents=True, exist_ok=False)
    run_artifact.append(folder)
    audit = {
        k: []
        for k in (
            "feature_occurrences",
            "decoded_locations",
            "feature_footprints",
            "traffic_intervals",
            "weather_alignment",
            "static_features",
            "pilot_observations",
            "quarantine",
            "diagnostics",
            "errors",
            "association_decisions",
            "target_aggregates",
        )
    }
    audit["quarantine"] = quarantine
    normalized = []
    errors_by_snapshot = {}
    plans_by_snapshot = {}
    all_codes = set()
    priority_codes = set()
    corridors = [
        path_geometry(graph, p.edges).buffer(config.policy.diagnostic_search_m) for p in paths
    ]
    reader = OSMMapReader(graph)
    for _, manifest, plan, selected in snapshots:
        if (
            plan.graph_version != graph.version
            or manifest.registry_version != plan.registry_version
        ):
            raise ValueError("Snapshot/reference/registry version mismatch")
        features, weather, receipts, errors = normalize_snapshot(manifest, plan, selected)
        errors_by_snapshot[manifest.snapshot_id] = errors
        plans_by_snapshot[manifest.snapshot_id] = plan
        normalized.append((manifest, features, weather, receipts))
        audit["feature_occurrences"].extend(f.model_dump(mode="json") for f in features)
        audit["errors"].extend(errors)
        all_codes.update(f.openlr for f in features if f.openlr)
        for feature in features:
            if feature.openlr and any(
                project(LineString(component), graph.crs).intersects(corridor)
                for component in feature.components
                for corridor in corridors
            ):
                priority_codes.add(feature.openlr)
    selected_codes = sorted(priority_codes)[: config.policy.max_replay_codes]
    locations = {}
    evaluations = 0
    hits = 0
    for code in selected_codes:
        for profile in config.policy.decoder_profiles:
            location, hit = decode_location(
                code, graph, profile, config.output("cache/locations"), reader
            )
            locations[(code, profile)] = location
            hits += hit
            evaluations += not hit
            audit["decoded_locations"].append(location.model_dump(mode="json"))
    for code in selected_codes:
        baseline = locations[(code, "baseline")]
        stricter = locations.get((code, "stricter"))
        audit["diagnostics"].append(
            {
                "openlr": code,
                "profile_comparison": {
                    "baseline_status": baseline.status,
                    "stricter_status": stricter.status if stricter else "not_run",
                    "same_directed_path_and_offsets": bool(
                        stricter
                        and baseline.status == stricter.status == "decoded"
                        and baseline.edges == stricter.edges
                        and abs(baseline.start_m - stricter.start_m) < 1e-6
                        and abs(baseline.end_m - stricter.end_m) < 1e-6
                    ),
                    "direction_verified": False,
                },
            }
        )
    static = {
        (p.segment_id, p.direction_id): static_features(p, graph, config.policy) for p in paths
    }
    audit["static_features"] = list(static.values())
    measurements = {}
    validated_evidence_ids = set()
    for manifest, features, weather, receipts in normalized:
        feature_map = {f.occurrence_id: f for f in features}
        observed = manifest.scheduled_at or manifest.started_at
        for path in paths:
            calibration = calibration_gate(evidence, path, observed, config)
            legal = legal_direction(path, graph)
            validated_evidence_ids.update(calibration["evidence_ids"] + legal["evidence_ids"])
            footprints = []
            decisions = {}
            alternates = []
            edge_ids = {e.edge_id for e in path.edges}
            for feature in features:
                location = locations.get((feature.openlr, "baseline"))
                if (
                    location
                    and location.status == "decoded"
                    and edge_ids.intersection(location.edges)
                ):
                    footprint = associate_footprint(
                        feature, location, graph, config.policy, calibration
                    )
                    check = locations.get((feature.openlr, "stricter"))
                    footprint = profile_consensus(footprint, location, check, path, graph,
                        required="stricter" in config.policy.decoder_profiles)
                    decision = automatic_direction(feature, footprint, path, location, graph)
                    if "DECODER_PROFILE_DISAGREEMENT" in footprint.reasons:
                        decision['reasons']=['DECODER_PROFILE_DISAGREEMENT']
                else:
                    reason = (
                        "OPENLR_MISSING"
                        if not feature.openlr
                        else "REPLAY_BUDGET_NOT_EVALUATED"
                        if location is None
                        else "DECODE_FAILED"
                        if location.status != "decoded"
                        else "NO_DIRECTED_PATH_CORRESPONDENCE"
                    )
                    footprint = Footprint(
                        occurrence_id=feature.occurrence_id,
                        status="not_evaluated" if location is None else "failed",
                        reasons=[reason],
                    )
                    decision = {
                        "association_id": None,
                        "status": "unverified",
                        "evidence_ids": [],
                        "reasons": [reason],
                    }
                footprints.append(footprint)
                decisions[feature.occurrence_id] = decision
                validated_evidence_ids.update(decision["evidence_ids"])
                audit["association_decisions"].append(
                    {
                        "segment_id": path.segment_id,
                        "direction_id": path.direction_id,
                        "snapshot_id": manifest.snapshot_id,
                        "occurrence_id": feature.occurrence_id,
                        "observed_at": observed.isoformat(),
                        **decision,
                    }
                )
                audit["feature_footprints"].append(
                    {
                        "segment_id": path.segment_id,
                        "direction_id": path.direction_id,
                        "snapshot_id": manifest.snapshot_id,
                        **footprint.model_dump(mode="json"),
                    }
                )
                strict = locations.get((feature.openlr, "stricter"))
                if (
                    config.policy.run_sensitivity
                    and strict
                    and strict.status == "decoded"
                    and edge_ids.intersection(strict.edges)
                ):
                    alternate_policy = config.policy.model_copy(
                        update=config.policy.association_profiles.get("constrained", {})
                    )
                    alternate = associate_footprint(feature, strict, graph, alternate_policy)
                    alternates.append(alternate)
            intervals, correspondence = partition_registry(
                footprints, path, feature_map, graph, decisions, receipts, config.policy
            )
            quality = assess_quality(intervals, path, receipts, config.policy, legal=legal)
            coverage = compute_coverage(intervals, path.length_m, raw_support=correspondence)
            target = compute_target(
                intervals,
                contract_ready=source_gate["ready"],
                whole_path_length=path.length_m,
                minimum_coverage=config.policy.minimum_coverage,
                quality_reasons=quality["reasons"],
                ci_quality_reasons=quality["ci_reasons"],
                speed_quality_reasons=quality["speed_reasons"],
            )
            alternate_intervals, _ = partition_registry(
                alternates, path, feature_map, graph, decisions, receipts, config.policy
            )
            weather_row = align_weather(weather, path.segment_id, observed, config.policy)
            static_row = static[(path.segment_id, path.direction_id)]
            reasons = target["reasons"] + weather_row["reasons"]
            relevant_resources = {
                r.resource_id
                for r in plans_by_snapshot[manifest.snapshot_id].resources
                if path.segment_id in r.segments
                or (manifest.collection_kind == "pilot" and r.provider == "traffic")
            }
            if any(
                e["resource"] in relevant_resources
                for e in errors_by_snapshot[manifest.snapshot_id]
            ):
                reasons.append("SOURCE_SCHEMA_INVALID")
            if any(
                e["snapshot"] == manifest.snapshot_id and e["resource"] in relevant_resources
                for e in quarantine
            ):
                reasons.append("RAW_HASH_MISMATCH")
            if static_row["lanes_mean"] is None:
                reasons.append("LANES_MISSING")
            if manifest.status == "partial":
                reasons.append("PARTIAL_CAPTURE")
            contributing = [i for i in intervals if i.contributors]
            closure = (
                True
                if any(i.closure is True for i in contributing)
                else False
                if contributing and all(i.closure is False for i in contributing)
                else None
            )
            result = {
                "schema_version": OUTPUT_SCHEMA,
                "calibration_status": "evaluated" if calibration["ready"] else "not_evaluated",
                "review_status": path.approval_status,
                "technical_status": "passed" if target["ci"] is not None else "incomplete",
                "direction_method": METHOD,
                "target_method": TARGET_METHOD,
                "current_speed_kmh": target["current_speed_kmh"],
                "freeflow_speed_kmh": None,
                "ci_raw": target["ci_raw"],
                "ci": target["ci"],
                "ci_policy_resolution_used": target["ci_policy_resolution_used"],
                "ci_reasons": target["ci_reasons"],
                "speed_reasons": target["speed_reasons"],
                "processing_policy_sha256": digest(config.policy.model_dump(mode="json")),
                "footprint_method": "directed_component_consensus_v2",
                "interval_method": "independent_interval_policy_v2",
                "target_valid_coverage": target["target_valid_coverage"],
                "absolute_speed_coverage": target["absolute_speed_coverage"],
                "paired_speed_coverage": None,
                "accepted_direction_coverage": coverage["source_direction_coverage"],
                "geometry_coverage": coverage["geometry_coverage"],
                "geometry_present_coverage": coverage["geometry_present_coverage"],
                "geometry_association_coverage": coverage["geometry_association_coverage"],
                "road_closure": closure,
                "closure_status": "closed"
                if closure is True
                else "clear"
                if closure is False
                else "unknown",
                "precipitation_mm": weather_row.get("precipitation_mm"),
                "weather_code": weather_row.get("weather_code"),
                "precipitation_interval_seconds": weather_row.get("interval_seconds"),
                "weather_valid_at": weather_row.get("valid_at"),
                "precipitation_window_start": weather_row.get("window_start"),
                "precipitation_window_end": weather_row.get("window_end"),
                "weather_grid_distance_m": weather_row.get("grid_distance_m"),
                "weather_gap_s": weather_row.get("gap_s"),
                **{
                    k: static_row[k]
                    for k in (
                        "road_type",
                        "oneway",
                        "lanes_mean",
                        "lanes_min",
                        "lanes_max",
                        "lanes_known_coverage",
                        "junction_count",
                        "signal_count",
                    )
                },
                "quality_reasons": sorted(set(reasons)),
                "source_snapshot_id": manifest.snapshot_id,
                "static_feature_version": digest(static_row)[:20],
            }
            if manifest.collection_kind == "scheduled" and manifest.scheduled_at:
                if manifest.registry_version != path.registry_version:
                    raise ValueError("Scheduled snapshot registry mismatch; no implicit migration")
                key = (path.segment_id, path.direction_id, manifest.scheduled_at.isoformat())
                if key in measurements:
                    raise ValueError("Duplicate scheduled snapshot key")
                measurements[key] = result
            else:
                audit["pilot_observations"].append(
                    {
                        "registry_version": path.registry_version,
                        "segment_id": path.segment_id,
                        "direction_id": path.direction_id,
                        "segment_length_m": path.length_m,
                        "collection_kind": manifest.collection_kind,
                        "scheduled_at": None,
                        "observed_at": observed.isoformat(),
                        **result,
                    }
                )
            audit["traffic_intervals"].extend(
                {
                    "registry_version": path.registry_version,
                    "segment_id": path.segment_id,
                    "direction_id": path.direction_id,
                    "scheduled_at": manifest.scheduled_at.isoformat()
                    if manifest.scheduled_at
                    else None,
                    "snapshot_id": manifest.snapshot_id,
                    **i.model_dump(mode="json"),
                }
                for i in intervals
            )
            audit["target_aggregates"].append(
                {
                    "segment_id": path.segment_id,
                    "direction_id": path.direction_id,
                    "snapshot_id": manifest.snapshot_id,
                    "diagnostic_only": True,
                    **target,
                }
            )
            audit["weather_alignment"].append(
                {
                    "segment_id": path.segment_id,
                    "direction_id": path.direction_id,
                    "snapshot_id": manifest.snapshot_id,
                    **weather_row,
                }
            )
            audit["diagnostics"].append(
                {
                    "segment_id": path.segment_id,
                    "snapshot_id": manifest.snapshot_id,
                    "coverage": coverage,
                    "quality": quality,
                    "source_contract_gate": source_gate,
                    "calibration_gate": calibration,
                    "legal_gate": legal,
                    "correspondence": correspondence,
                    "sampling": sensitivity(intervals, path.length_m)
                    if config.policy.run_sensitivity
                    else {"status": "NOT_RUN"},
                    "association_sensitivity": compare_associations(
                        intervals, alternate_intervals, path.length_m
                    )
                    if config.policy.run_sensitivity
                    else {"status": "NOT_RUN"},
                    "association_profiles": {
                        "baseline_decoder": "baseline",
                        "alternate_decoder": "stricter",
                        "baseline_projection": {},
                        "alternate_projection": config.policy.association_profiles.get(
                            "constrained", {}
                        ),
                    },
                }
            )
    rows = canonical_rows(paths, measurements, config, run_id)
    expected = expected_keys(paths, config)
    if scheduled_scope is not None:
        from .validate import natural_key
        rows=[r for r in rows if natural_key(r)[-1] in scheduled_scope]
        expected={k for k in expected if k[-1] in scheduled_scope}
    validation = validate_dataset(
        rows,
        expected,
        interval_records=audit["traffic_intervals"],
        feature_records=audit["feature_occurrences"],
        source_ready=source_gate["ready"],
        minimum_coverage=config.policy.minimum_coverage,
        validated_evidence_ids=validated_evidence_ids,
        association_records=audit["association_decisions"],
        policy=config.policy,
    )
    metadata = {
        "run_id": run_id,
        "scheduled_scope": sorted(scheduled_scope) if scheduled_scope is not None else None,
        "output_schema": OUTPUT_SCHEMA,
        "target_method": TARGET_METHOD,
        "inventory_path": str(inventory_path.resolve()),
        "source_contract_gate": source_gate,
        "registry_gate": registry_gate,
        "validated_evidence_ids": sorted(validated_evidence_ids),
        "expected_keys": [list(k) for k in sorted(expected)],
        "registry_paths": [p.model_dump(mode="json") for p in paths],
        "scientific_release_eligible": bool(
            not config.study.provisional
            and registry_gate["valid"]
            and source_gate["ready"]
            and inventory.get("collection_kind") == "scheduled"
            and rows
            and any(r["ci_ready"] for r in rows)
        ),
        "fingerprint": fingerprint,
        "input_hashes": hashes,
        "graph_version": graph.version,
        "config_hash": config.hash,
        "source_fingerprint": source_fingerprint(config.study.project_root),
        "lock_sha256": sha256(config.study.project_root / "requirements.txt"),
        "network": "off",
        "decode_evaluations": evaluations,
        "decode_cache_hits": hits,
        "unique_codes_total": len(all_codes),
        "unique_codes_selected": len(selected_codes),
        "codes_not_evaluated": len(all_codes) - len(selected_codes),
        "code_selection": "selected candidate corridor intersection, then lexical bounded evaluation; not road assignment",
        "graph_artifact": str(graph.artifact_root) if hasattr(graph, "artifact_root") else None,
        "resolved_osm_pbf": str(config.study.osm_pbf.resolve()),
        "valid_traffic_resources": len({f.resource_id for _, fs, _, _ in normalized for f in fs}),
        "collection_kind": inventory.get("collection_kind"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "footprint_method": "directed_component_consensus_v2",
        "profile_method": "profile_support_intersection_v1",
        "interval_method": "independent_interval_policy_v2",
        "policy_sha256": digest(config.policy.model_dump(mode="json")),
    }
    write_json(folder / "input_manifest.json", {"hashes": hashes, "quarantine": quarantine})
    return export_run(folder, rows, audit, metadata, validation, config, update_pointer=update_pointer)
