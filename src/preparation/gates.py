"""Artifact-backed source, calibration and scoped association evidence gates.

Configuration booleans are never proof. Reading evidence is offline and validates
the artifact bytes, reviewer/method, effective time, vehicle and association scope.
"""

from pathlib import Path
import math
from shapely.geometry import shape, box
from ..domain.reference import ReviewEvidence
from ..domain.processing import TARGET_METHOD
from ..storage.files import read_json, sha256, digest


def read_evidence(path: Path | None) -> list[dict]:
    if path is None:
        return []
    value = read_json(path)
    rows = value.get("records", []) if isinstance(value, dict) else value
    return [dict(row, _base=str(path.resolve().parent)) for row in rows]


def artifact(record: dict) -> tuple[ReviewEvidence | None, dict | None, list[str]]:
    """Return structured evidence plus verified local artifact, never URL fetches."""
    try:
        data = {k: v for k, v in record.items() if not k.startswith("_")}
        evidence = ReviewEvidence.model_validate(data)
        errors = []
        if evidence.status != "approved":
            errors.append("EVIDENCE_NOT_APPROVED")
        if not evidence.reviewer.strip() or not evidence.method_id.strip():
            errors.append("EVIDENCE_REVIEWER_METHOD_MISSING")
        if len(evidence.daily_time_window) != 2:
            errors.append("EVIDENCE_TIME_WINDOW_INVALID")
        from datetime import time

        times = [time.fromisoformat(t) for t in evidence.daily_time_window]
        if len(times) == 2 and times[0] > times[1]:
            errors.append("EVIDENCE_TIME_WINDOW_INVALID")
        path = Path(evidence.artifact_path or evidence.source_url_or_path)
        if not path.is_absolute():
            path = Path(record.get("_base", ".")) / path
        path = path.resolve()
        if not path.is_file() or sha256(path) != evidence.source_sha256:
            errors.append("EVIDENCE_HASH_MISMATCH")
        if errors:
            return evidence, None, errors
        payload = read_json(path)
        if not isinstance(payload, dict):
            return evidence, None, ["EVIDENCE_ARTIFACT_SCHEMA"]
        return evidence, payload, []
    except (ValueError, TypeError, KeyError, OSError):
        return None, None, ["EVIDENCE_INVALID"]


def scoped(record, path, when, vehicle, graph_version, policy_hash, product=None):
    evidence, payload, reasons = artifact(record)
    if reasons:
        return evidence, payload, reasons
    if when.tzinfo is None:
        return evidence, payload, ["EVIDENCE_TIME_NAIVE"]
    from zoneinfo import ZoneInfo

    local = when.astimezone(ZoneInfo("Asia/Ho_Chi_Minh"))
    if not evidence.effective_from <= when or (
        evidence.effective_to is not None and when > evidence.effective_to
    ):
        reasons.append("EVIDENCE_EXPIRED_OR_NOT_EFFECTIVE")
    if evidence.vehicle_scope != vehicle:
        reasons.append("EVIDENCE_VEHICLE_SCOPE")
    if (
        not evidence.daily_time_window[0]
        <= local.strftime("%H:%M")
        <= evidence.daily_time_window[1]
    ):
        reasons.append("EVIDENCE_DAILY_TIME_SCOPE")
    if evidence.graph_version != graph_version:
        reasons.append("EVIDENCE_GRAPH_SCOPE")
    if evidence.matching_policy_hash != policy_hash:
        reasons.append("EVIDENCE_POLICY_SCOPE")
    if product and evidence.product != product:
        reasons.append("EVIDENCE_PRODUCT_SCOPE")
    if path.direction_id not in evidence.direction_scope:
        reasons.append("EVIDENCE_DIRECTION_SCOPE")
    if path.segment_id not in evidence.spatial_scope:
        bounds = payload.get("region_bbox")
        if (
            "*" not in evidence.spatial_scope
            or not bounds
            or not box(*bounds).covers(shape(path.geometry))
        ):
            reasons.append("EVIDENCE_SPATIAL_SCOPE")
    return evidence, payload, reasons


def applicable(records, kind, path, when, config):
    selected = []
    failures = []
    for record in records:
        if record.get("evidence_type") != kind:
            continue
        evidence, payload, reasons = scoped(
            record,
            path,
            when,
            config.study.vehicle_scope,
            path.graph_version,
            digest(config.policy.model_dump(mode="json")),
            config.sources.traffic_product if kind.startswith("source_direction") else None,
        )
        if reasons:
            failures.extend(reasons)
        else:
            selected.append((evidence, payload))
    return selected, sorted(set(failures))


def source_contract_readiness(config) -> dict:
    """Versioned product/schema contract checked once, without row-level approvals."""
    source = config.sources
    reasons = []
    expected = {
        "api_version": "2",
        "traffic_product": "orbis_vector_flow_v2",
        "target_method": TARGET_METHOD,
        "relative_speed_semantics": "fraction_of_freeflow",
        "absolute_speed_unit": "km/h",
        "source_direction_rule": "directed_openlr_occurrence_v2",
        "closure_semantics": "presence_true_absent_false",
    }
    if not source.version or any(getattr(source, k) != v for k, v in expected.items()):
        reasons.append("SOURCE_CONTRACT_SCOPE_OR_SEMANTICS")
    if not all(
        field in source.attributes for field in ("openlr", "relative_speed", "road_closure")
    ):
        reasons.append("SOURCE_REQUIRED_ATTRIBUTES_MISSING")
    path = config.study.source_evidence_file
    if path is not None:
        try:
            record = read_json(path)
            if (
                record.get("product") != source.traffic_product
                or record.get("api_version") != source.api_version
            ):
                reasons.append("SOURCE_CONTRACT_SCOPE")
            for document in record.get("documentation", []):
                if sha256(Path(document["artifact_path"])) != document["sha256"]:
                    reasons.append("SOURCE_DOCUMENT_HASH_MISMATCH")
        except (OSError, ValueError, KeyError, TypeError):
            reasons.append("SOURCE_CONTRACT_ARTIFACT_INVALID")
    return {
        "ready": not reasons,
        "reasons": sorted(set(reasons)),
        "method_id": TARGET_METHOD,
        "contract_version": source.version,
        "contract_sha256": digest(source.model_dump(mode="json")),
        "source_direction_rule": source.source_direction_rule,
        "evidence_sha256": sha256(path) if path and path.is_file() else None,
        "legacy_flags_used": False,
    }


def calibration_gate(records, path, when, config) -> dict:
    selected, failures = applicable(records, "matching_calibration", path, when, config)
    for evidence, payload in selected:
        limits = payload.get("limits", {})
        if evidence.method_id != "footprint_dp_v2" or not payload.get("validation_passed"):
            continue
        values = [limits.get("residual_m"), limits.get("step_error_m")]
        if not all(
            isinstance(v, (float, int)) and not isinstance(v, bool) and math.isfinite(v) and v > 0
            for v in values
        ):
            continue
        if not payload.get("validation_case_hashes"):
            continue
        case_files = payload["validation_case_hashes"]
        try:
            if not isinstance(case_files, dict) or any(
                sha256(Path(case["artifact_path"])) != case["sha256"]
                for case in case_files.values()
            ):
                continue
        except (OSError, KeyError, TypeError):
            continue
        return {
            "ready": True,
            "evidence_ids": [evidence.evidence_id],
            "limits": limits,
            "reasons": [],
        }
    return {"ready": False, "evidence_ids": [], "reasons": ["FOOTPRINT_UNCALIBRATED"] + failures}


def association_identity(feature, footprint, path, location) -> str:
    return digest(
        {
            "occurrence_id": feature.occurrence_id,
            "resource_id": feature.resource_id,
            "openlr": feature.openlr,
            "graph": path.graph_version,
            "geometry_sha256": path.geometry_sha256,
            "segment": path.segment_id,
            "direction": path.direction_id,
            "decoded_edges": location.edges,
            "start_m": location.start_m,
            "end_m": location.end_m,
            "pieces": footprint.pieces,
            "decoder_dependencies": location.dependencies,
        }
    )


def method_artifact_valid(evidence, payload):
    if evidence.method_id != "directed_openlr_occurrence_v1":
        return False
    required = {
        "reversed_vertices",
        "opposite_direction",
        "parallel_road",
        "multipart",
        "tile_duplicate",
    }
    cases = payload.get("validation_cases", {})
    valid_cases = True
    for name in required:
        case = cases.get(name, {})
        try:
            if not case.get("passed") or sha256(Path(case["artifact_path"])) != case["sha256"]:
                valid_cases = False
        except (OSError, KeyError):
            valid_cases = False
    references = payload.get("direction_reference_evidence", [])
    references_valid = bool(references)
    for reference in references:
        try:
            if (
                sha256(Path(reference["artifact_path"])) != reference["sha256"]
                or reference.get("assertion") != "source speed follows directed OpenLR support"
            ):
                references_valid = False
        except (OSError, KeyError, TypeError):
            references_valid = False
    if (
        not valid_cases
        or not payload.get("supports_source_travel_direction")
        or not references_valid
    ):
        return False
    if payload.get("association_rule") != "directed_openlr_occurrence_v1":
        return False
    return True


def direction_gate(feature, footprint, path, location, records, when, config) -> dict:
    association = association_identity(feature, footprint, path, location)
    result = {
        "association_id": association,
        "status": "unverified",
        "evidence_ids": [],
        "reasons": ["SOURCE_DIRECTION_METHOD_UNAPPROVED"],
        "method_id": "directed_openlr_occurrence_v1",
    }
    if (
        location.status != "decoded"
        or footprint.status not in ("matched", "provisional")
        or not footprint.pieces
    ):
        result["reasons"] = ["ASSOCIATION_EVIDENCE_MISMATCH"]
        return result
    if any(
        p["path_edge_index"] < 0
        or p["path_edge_index"] >= len(location.edges)
        or location.edges[p["path_edge_index"]] != p["edge_id"]
        for p in footprint.pieces
    ):
        result["reasons"] = ["ASSOCIATION_EVIDENCE_MISMATCH"]
        return result
    manual, _ = applicable(records, "source_direction", path, when, config)
    for evidence, payload in manual:
        if (
            association in evidence.association_ids
            and feature.occurrence_id in evidence.occurrence_ids
            and payload.get("association_id") == association
        ):
            result.update(status="supported", evidence_ids=[evidence.evidence_id], reasons=[])
            return result
    methods, failures = applicable(records, "source_direction_method", path, when, config)
    for evidence, payload in methods:
        if not method_artifact_valid(evidence, payload):
            continue
        # Reference/footprint correspondence is checked for every new occurrence;
        # the reviewed method evidence can cover an entire explicit region/time scope.
        if any(p["edge_id"] not in {e.edge_id for e in path.edges} for p in footprint.pieces):
            # Locations can extend past the registry: require only the overlapping pieces.
            overlap = [
                p for p in footprint.pieces if p["edge_id"] in {e.edge_id for e in path.edges}
            ]
            if not overlap:
                continue
        result.update(status="supported", evidence_ids=[evidence.evidence_id], reasons=[])
        return result
    result["reasons"] += failures
    return result


def legal_gate(records, path, when, config):
    selected, failures = applicable(records, "legal_direction", path, when, config)
    for evidence, payload in selected:
        if payload.get("legal_direction_status") == "supported":
            return {"status": "supported", "evidence_ids": [evidence.evidence_id], "reasons": []}
    return {"status": "unverified", "evidence_ids": [], "reasons": ["LEGAL_UNKNOWN"] + failures}
