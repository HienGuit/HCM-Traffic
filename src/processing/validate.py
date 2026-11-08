"""Independent expected-key, support, target, evidence and profile invariants."""

import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from ..domain.processing import Interval, Observation, TARGET_METHOD, OUTPUT_SCHEMA
from ..storage.files import read_json, sha256, digest
from .target import compute_target, valid_relative


def natural_key(row):
    instant = datetime.fromisoformat(str(row["scheduled_at"]).replace("Z", "+00:00"))
    if instant.tzinfo is None:
        raise ValueError("Naive observation timestamp")
    return (
        row["registry_version"],
        row["segment_id"],
        row["direction_id"],
        instant.astimezone(timezone.utc).isoformat(),
    )


def close(first, second, tolerance=1e-8):
    if first is None or second is None:
        return first is second
    return (
        isinstance(first, (int, float))
        and isinstance(second, (int, float))
        and math.isfinite(first)
        and math.isfinite(second)
        and abs(first - second) <= tolerance
    )


def verify_measurement(
    row,
    interval_records,
    feature_map,
    source_ready,
    minimum_coverage,
    validated_evidence_ids,
    associations,
    policy=None,
):
    errors = []
    intervals = [
        Interval.model_validate({k: v for k, v in record.items() if k in Interval.model_fields})
        for record in interval_records
    ]
    whole = row["segment_length_m"]
    if not intervals:
        if row.get("ci") is not None or row.get("ci_raw") is not None:
            errors.append("TARGET_WITHOUT_INTERVAL_SUPPORT")
        if any(
            row.get(k) not in (None, 0, 0.0)
            for k in (
                "target_valid_coverage",
                "absolute_speed_coverage",
                "geometry_coverage",
                "accepted_direction_coverage",
            )
        ):
            errors.append("COVERAGE_WITHOUT_INTERVAL_SUPPORT")
        return errors
    ordered = sorted(intervals, key=lambda i: i.start_m)
    if (
        abs(ordered[0].start_m) > 1e-7
        or abs(ordered[-1].end_m - whole) > 1e-6
        or any(abs(a.end_m - b.start_m) > 1e-6 for a, b in zip(ordered, ordered[1:]))
    ):
        errors.append("REGISTRY_PARTITION_GAP_OR_OVERLAP")
    for interval in intervals:
        if not interval.target_valid and not interval.speed_valid:
            continue
        physical = (
            interval.status == "accepted"
            and interval.footprint_status == "matched"
            and interval.source_direction == "supported"
            and interval.legal_direction == "supported"
            and interval.time_aligned
            and interval.closure is False
        )
        if not physical:
            errors.append("TARGET_INTERVAL_GATE_INVALID")
        automatic = bool(interval.association_ids) and all(
            associations.get(a, {}).get("method_id") == "directed_openlr_occurrence_v2"
            for a in interval.association_ids
        )
        if not automatic and (
            not interval.evidence_ids
            or not set(interval.evidence_ids) <= set(validated_evidence_ids)
        ):
            errors.append("INTERVAL_EVIDENCE_INVALID")
        if not interval.association_ids:
            errors.append("INTERVAL_ASSOCIATION_MISSING")
        for association_id in interval.association_ids:
            record = associations.get(association_id)
            if (
                not record
                or record.get("status") != "supported"
                or record.get("segment_id") != row["segment_id"]
                or record.get("snapshot_id") != row.get("source_snapshot_id")
            ):
                errors.append("ASSOCIATION_EVIDENCE_MISMATCH")
        if not interval.contributors:
            errors.append("TARGET_CONTRIBUTOR_MISSING")
        for identity in interval.contributors:
            feature = feature_map.get(identity)
            if not feature or feature.get("road_closure") is not False:
                errors.append("RAW_INTERVAL_CLOSURE_MISMATCH")
        for enabled, field, value, identities, status in (
            (interval.target_valid, "relative_speed", interval.relative_speed,
             interval.relative_contributors, interval.relative_status),
            (interval.speed_valid, "absolute_speed", interval.current,
             interval.absolute_contributors, interval.absolute_status),
        ):
            if not enabled:
                continue
            selected = interval.contributors if status == "legacy" else identities
            if not selected or not set(selected) <= set(interval.contributors):
                errors.append("MEASUREMENT_CONTRIBUTORS_INVALID")
            if status not in ("legacy", "consistent", "resolved"):
                errors.append("MEASUREMENT_STATUS_INVALID")
            resolved_relative = field == 'relative_speed' and status == 'resolved'
            for identity in selected:
                feature = feature_map.get(identity, {})
                if (not resolved_relative and not close(feature.get(field), value)) or field in feature.get("invalid_fields", {}):
                    errors.append("RAW_INTERVAL_" + field.upper() + "_MISMATCH")
            if status != "legacy" and not resolved_relative:
                source_ids = interval.selected_contributors if interval.congestion_priority_resolution else interval.contributors
                raw_values = {
                    f[field] for identity in source_ids
                    if (f := feature_map.get(identity))
                    and f.get(field) is not None
                    and field not in f.get("invalid_fields", {})
                    and (valid_relative(f[field]) if field == "relative_speed" else isinstance(f[field], (int,float)) and not isinstance(f[field],bool) and math.isfinite(f[field]) and f[field]>=0)
                }
                if raw_values != {value}:
                    errors.append("UNRESOLVED_MEASUREMENT_CONFLICT")
        if interval.measurement_method == 'independent_interval_policy_v2':
            from ..matching.intervals import resolve_relative
            from ..domain.processing import FeatureOccurrence
            fmap={identity:FeatureOccurrence.model_validate(feature_map[identity]) for identity in interval.contributors}
            resolution=resolve_relative(interval.contributors,fmap,policy)
            if (not close(resolution['value'],interval.relative_speed)
                or resolution['method']!=interval.relative_resolution_method
                or resolution['status']!=interval.relative_status
                or set(resolution['contributors'])!=set(interval.relative_contributors)
                or set(resolution['selected'])!=set(interval.selected_contributors)):
                errors.append('RELATIVE_POLICY_RESOLUTION_MISMATCH')
    # Recompute every published number; coverage uses the whole fixed registry denominator.
    context_reasons = [
        r
        for r in row.get("quality_reasons", [])
        if r in ("CLOSED", "CLOSURE_UNKNOWN")
    ]
    result = compute_target(
        intervals,
        contract_ready=source_ready,
        whole_path_length=whole,
        minimum_coverage=minimum_coverage,
        quality_reasons=context_reasons,
        ci_quality_reasons=[r for r in row.get('ci_reasons',[]) if r in ('TRAFFIC_TIME_MISALIGNED','SOURCE_VERSION_CONFLICT','SOURCE_CONTEXT_CONFLICT')],
        speed_quality_reasons=[r for r in row.get('speed_reasons',[]) if r in ('TRAFFIC_TIME_MISALIGNED','SOURCE_VERSION_CONFLICT','SOURCE_CONTEXT_CONFLICT')],
    )
    for field in (
        "ci_raw",
        "target_valid_coverage",
        "absolute_speed_coverage",
        "current_speed_kmh",
    ):
        if not close(row.get(field), result[field]):
            errors.append("RECOMPUTED_" + field.upper() + "_MISMATCH")
    if row.get("ci") is not None and not close(row["ci"], result["ci"]):
        errors.append("RECOMPUTED_CI_MISMATCH")
    if result["ci"] is not None and row.get("ci") is None and not row.get("quality_reasons"):
        errors.append("CI_NULL_WITHOUT_REASON")
    if row.get("freeflow_speed_kmh") is not None or row.get("paired_speed_coverage") is not None:
        errors.append("LEGACY_MEASUREMENT_IN_NEW_METHOD")
    return errors


def validate_dataset(
    rows,
    expected_key_set,
    *,
    interval_records=(),
    feature_records=(),
    source_ready=False,
    minimum_coverage=0.95,
    validated_evidence_ids=(),
    association_records=(),
    policy=None,
):
    errors = []
    keys = set()
    feature_map = {f["occurrence_id"]: f for f in feature_records}
    association_map = {
        r["association_id"]: r for r in association_records if r.get("association_id")
    }
    if not isinstance(expected_key_set, set):
        return {"valid": False, "errors": ["EXPECTED_KEY_SET_REQUIRED"], "rows": len(rows)}
    for row in rows:
        try:
            Observation.model_validate(row)
            key = natural_key(row)
        except (ValueError, KeyError, TypeError):
            errors.append("OBSERVATION_SCHEMA_INVALID")
            continue
        if key in keys:
            errors.append("DUPLICATE_KEY")
        keys.add(key)
        if row["observation_id"] != digest([TARGET_METHOD, *key]):
            errors.append("OBSERVATION_ID_MISMATCH")
        if row["target_method"] != TARGET_METHOD or row["schema_version"] != OUTPUT_SCHEMA:
            errors.append("TARGET_METHOD_OR_SCHEMA_MISMATCH")
        for field in (
            "geometry_coverage",
            "target_valid_coverage",
            "absolute_speed_coverage",
            "accepted_direction_coverage",
            "lanes_known_coverage",
        ):
            if row.get(field) is not None and not -1e-8 <= row[field] <= 1 + 1e-8:
                errors.append("COVERAGE_OUT_OF_RANGE")
        if row["freeflow_speed_kmh"] is not None or row["paired_speed_coverage"] is not None:
            errors.append("LEGACY_MEASUREMENT_IN_NEW_METHOD")
        records = [
            r
            for r in interval_records
            if r["segment_id"] == row["segment_id"]
            and r.get("direction_id", row["direction_id"]) == row["direction_id"]
            and r["snapshot_id"] == row.get("source_snapshot_id")
        ]
        errors.extend(
            verify_measurement(
                row,
                records,
                feature_map,
                source_ready,
                minimum_coverage,
                validated_evidence_ids,
                association_map,
                policy,
            )
        )
        if row["ci_ready"] != (row["ci"] is not None):
            errors.append("CI_PROFILE_INVALID")
        if row["ci"] is not None and (
            not 0 <= row["ci"] <= 1
            or row["target_valid_coverage"] is None
            or row["target_valid_coverage"] + 1e-9 < minimum_coverage
            or row["road_closure"] is not False
        ):
            errors.append("CI_GATE_INVALID")
        required = (
            "precipitation_mm",
            "weather_code",
            "road_type",
            "lanes_mean",
            "junction_count",
            "signal_count",
        )
        model = (
            row["ci_ready"]
            and all(row[k] is not None for k in required)
            and "COVARIATE_CONFLICT" not in row["quality_reasons"]
        )
        if model != row["model_ready"]:
            errors.append("MODEL_COVARIATE_MEMBERSHIP_INVALID")
        if row["needs_review"] != bool(row["quality_reasons"]):
            errors.append("REVIEW_MEMBERSHIP_INVALID")
        if row.get("precipitation_mm") is not None and row["precipitation_mm"] < 0:
            errors.append("PRECIPITATION_INVALID")
        if row.get("weather_code") is not None:
            from ..sources.weather import WMO_CODES

            if (
                row["weather_code"] not in WMO_CODES
                or row["weather_valid_at"] is None
                or not row.get("precipitation_interval_seconds")
            ):
                errors.append("WEATHER_METADATA_INVALID")
    if keys != expected_key_set:
        errors.append("EXPECTED_KEY_SET_MISMATCH")
    return {
        "valid": not errors,
        "errors": sorted(set(errors)),
        "rows": len(rows),
        "expected": len(expected_key_set),
        "missing_keys": [list(k) for k in sorted(expected_key_set - keys)],
        "unexpected_keys": [list(k) for k in sorted(keys - expected_key_set)],
    }


def validate_run(folder: Path):
    manifest = read_json(folder / "manifest.json")
    if manifest.get("status") != "passed" or not manifest.get("validation", {}).get("valid"):
        return {"valid": False, "errors": ["PROCESSING_RUN_NOT_PASSED"], "rows": 0}
    if (
        manifest.get("output_schema") != OUTPUT_SCHEMA
        or manifest.get("target_method") != TARGET_METHOD
    ):
        return {
            "valid": False,
            "errors": ["LEGACY_METHOD_RUN_REQUIRES_EXPLICIT_MIGRATION"],
            "rows": 0,
        }
    errors = []
    for name, checksum in manifest["checksums"].items():
        path = (folder / name).resolve()
        if (
            not path.is_relative_to(folder.resolve())
            or not path.is_file()
            or sha256(path) != checksum
        ):
            errors.append("ARTIFACT_HASH_MISMATCH:" + name)
    if errors:
        return {"valid": False, "errors": errors, "rows": 0}
    with (folder / "outputs/all_observations.csv").open(encoding="utf-8-sig", newline="") as stream:
        strings = list(csv.DictReader(stream))
    rows = []
    for record in strings:
        data = {
            k: (None if v == "" else json.loads(v) if k in ("quality_reasons","ci_reasons","speed_reasons") else v)
            for k, v in record.items()
        }
        try:
            rows.append(Observation.model_validate(data).model_dump(mode="json"))
        except ValueError:
            errors.append("OBSERVATION_SCHEMA_INVALID")

    def audit(name):
        return [
            json.loads(line)
            for line in (folder / f"audit/{name}.jsonl").read_text(encoding="utf-8").splitlines()
            if line
        ]

    intervals = audit("traffic_intervals")
    features = audit("feature_occurrences")
    associations = audit("association_decisions")
    from ..config import ResolvedConfig, Study, Policy, Sources, Budget
    from ..domain.reference import RegistryPath
    from ..domain.processing import FeatureOccurrence, Footprint, DecodedLocation
    from ..preparation.gates import (
        source_contract_readiness,
        read_evidence,
        artifact,
        direction_gate,
        calibration_gate,
        legal_gate,
    )

    raw_config = read_json(folder / "resolved_config.json")
    config = ResolvedConfig(
        Study.model_validate(raw_config["study"]),
        Policy.model_validate(raw_config["policy"]),
        Sources.model_validate(raw_config["sources"]),
        Budget.model_validate(raw_config["budget"]),
    )
    source = source_contract_readiness(config)
    if source != manifest["source_contract_gate"]:
        errors.append("SOURCE_CONTRACT_GATE_CHANGED")
    records = read_evidence(config.study.evidence_file)
    verified_ids = set()
    for record in records:
        item, payload, failures = artifact(record)
        if not failures:
            verified_ids.add(item.evidence_id)
    if not set(manifest["validated_evidence_ids"]) <= verified_ids:
        errors.append("EVIDENCE_ARTIFACT_CHANGED_OR_INVALID")
    paths = {
        (p["segment_id"], p["direction_id"]): RegistryPath.model_validate(p)
        for p in manifest["registry_paths"]
    }
    feature_map = {f["occurrence_id"]: FeatureOccurrence.model_validate(f) for f in features}
    footprint_map = {
        (r["segment_id"], r.get("direction_id", "FORWARD"), r["occurrence_id"]): r
        for r in audit("feature_footprints")
    }
    location_map = {
        (r["code"], r["profile"]): DecodedLocation.model_validate(r)
        for r in audit("decoded_locations")
    }
    if any(
        d.get("method_id") == "directed_openlr_occurrence_v2" and d["status"] == "supported"
        for d in associations
    ):
        from ..reference.graph import load_graph

        graph = load_graph(Path(manifest["graph_artifact"]))
    for decision in associations:
        if decision["status"] != "supported":
            continue
        path = paths[(decision["segment_id"], decision["direction_id"])]
        feature = feature_map[decision["occurrence_id"]]
        raw = footprint_map[(path.segment_id, path.direction_id, feature.occurrence_id)]
        footprint = Footprint.model_validate(
            {k: v for k, v in raw.items() if k in Footprint.model_fields}
        )
        location = location_map[(feature.openlr, "baseline")]
        when = datetime.fromisoformat(decision["observed_at"])
        if decision.get("method_id") == "directed_openlr_occurrence_v2":
            from ..matching.direction import automatic_direction, legal_direction, profiles_agree, profile_consensus

            proof = automatic_direction(feature, footprint, path, location, graph)
            common = profile_consensus(footprint,location,location_map.get((feature.openlr,'stricter')),path,graph,required='stricter' in config.policy.decoder_profiles)
            if common.pieces != footprint.pieces:
                errors.append('PROFILE_SUPPORT_OUTSIDE_INTERSECTION')
            if not profiles_agree(
                location, location_map.get((feature.openlr, "stricter")), path, graph
            ):
                proof["status"] = "unverified"
            if (
                proof["status"] != "supported"
                or proof["association_id"] != decision["association_id"]
            ):
                errors.append("SOURCE_DIRECTION_PROOF_INVALID")
            legal = legal_direction(path, graph)
            for interval in intervals:
                if interval.get("target_valid") and decision["association_id"] in interval.get(
                    "association_ids", []
                ):
                    if any(
                        s["status"] != "supported"
                        and s["start_m"] < interval["end_m"]
                        and s["end_m"] > interval["start_m"]
                        for s in legal["segments"]
                    ):
                        errors.append("LEGAL_PROOF_INVALID")
            continue
        proof = direction_gate(feature, footprint, path, location, records, when, config)
        if proof["status"] != "supported" or proof["association_id"] != decision["association_id"]:
            errors.append("SOURCE_DIRECTION_PROOF_INVALID")
        if (
            not calibration_gate(records, path, when, config)["ready"]
            or legal_gate(records, path, when, config)["status"] != "supported"
        ):
            if any(
                i.get("target_valid") and decision["association_id"] in i.get("association_ids", [])
                for i in intervals
            ):
                errors.append("CALIBRATION_OR_LEGAL_PROOF_INVALID")
    if manifest.get("scientific_release") and (
        config.study.provisional
        or not manifest.get("scientific_release_eligible")
        or not source["ready"]
    ):
        errors.append("SCIENTIFIC_RELEASE_GATE_INVALID")
    from .dataset import expected_keys

    derived_expected = expected_keys(list(paths.values()), config)
    if manifest.get('scheduled_scope') is not None:
        derived_expected={k for k in derived_expected if k[-1] in manifest['scheduled_scope']}
    if {tuple(k) for k in manifest["expected_keys"]} != derived_expected:
        errors.append("EXPECTED_KEYS_MANIFEST_MISMATCH")
    validation = validate_dataset(
        rows,
        derived_expected,
        interval_records=intervals,
        feature_records=features,
        source_ready=manifest["source_contract_gate"]["ready"],
        minimum_coverage=read_json(folder / "resolved_config.json")["policy"]["minimum_coverage"],
        validated_evidence_ids=manifest["validated_evidence_ids"],
        association_records=associations,
        policy=config.policy,
    )
    errors.extend(validation["errors"])
    for subset in ("ci_ready", "model_ready", "needs_review"):
        with (folder / f"outputs/{subset}.csv").open(encoding="utf-8-sig", newline="") as stream:
            actual = list(csv.DictReader(stream))
        if actual != [r for r in strings if r[subset] == "true"]:
            errors.append("SUBSET_MISMATCH:" + subset)
    # Pilot diagnostics are checked too, even when the scheduled table is empty.
    feature_map = {f["occurrence_id"]: f for f in features}
    association_map = {r["association_id"]: r for r in associations if r.get("association_id")}
    for row in audit("pilot_observations"):
        selected = [
            i
            for i in intervals
            if i["segment_id"] == row["segment_id"]
            and i.get("direction_id", row["direction_id"]) == row["direction_id"]
            and i["snapshot_id"] == row["source_snapshot_id"]
        ]
        errors.extend(
            verify_measurement(
                row,
                selected,
                feature_map,
                manifest["source_contract_gate"]["ready"],
                read_json(folder / "resolved_config.json")["policy"]["minimum_coverage"],
                manifest["validated_evidence_ids"],
                association_map,
            )
        )
    return {"valid": not errors, "errors": sorted(set(errors)), "rows": len(rows)}
