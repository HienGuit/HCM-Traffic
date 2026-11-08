"""Length-weighted relative-speed decline; no inferred freeflow or cross-pairing."""

import math
from ..domain.processing import Interval, TARGET_METHOD
from ..geo.support import length


def valid_relative(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= 1
    )


def weighted_relative(intervals: list[Interval]) -> float | None:
    """Mean of 1-relative over disjoint positive metric intervals; reject overlap."""
    selected = sorted([i for i in intervals if i.end_m > i.start_m], key=lambda i: i.start_m)
    if any(a.end_m > b.start_m + 1e-8 for a, b in zip(selected, selected[1:])):
        raise ValueError("Overlapping target intervals must be resolved before aggregation")
    if not selected:
        return None
    if any(not valid_relative(i.relative_speed) for i in selected):
        raise ValueError("Relative speed must be finite in [0,1]")
    denominator = sum(i.end_m - i.start_m for i in selected)
    return sum((i.end_m - i.start_m) * (1 - i.relative_speed) for i in selected) / denominator


def compute_target(
    intervals: list[Interval],
    *,
    contract_ready: bool,
    whole_path_length: float | None = None,
    minimum_coverage: float = 0.95,
    quality_reasons=(),
    ci_quality_reasons=(),
    speed_quality_reasons=(),
) -> dict:
    """New target requires relative speed, not absolute/freeflow. Coverage uses full path."""
    if whole_path_length is None or not math.isfinite(whole_path_length) or whole_path_length <= 0:
        raise ValueError("Explicit positive whole registry path length required")
    if any(
        i.start_m < 0 or i.end_m < i.start_m or i.end_m > whole_path_length + 1e-6
        for i in intervals
    ):
        raise ValueError("Interval outside registry path")
    ordered = sorted(intervals, key=lambda i: i.start_m)
    if any(a.end_m > b.start_m + 1e-8 for a, b in zip(ordered, ordered[1:])):
        raise ValueError("Unresolved interval overlap")
    reasons = list(quality_reasons) + list(ci_quality_reasons)

    def physical(i):
        return (
            i.status == "accepted"
            and i.source_direction == "supported"
            and i.legal_direction == "supported"
            and i.time_aligned
            and i.closure is False
            and i.overlap_class
            not in ("direction_conflict", "context_conflict", "closure_conflict")
            and (i.measurement_method != "legacy_joint_v1" or i.overlap_class != "payload_conflict")
        )

    accepted = [
        i for i in intervals if i.target_valid and physical(i) and i.relative_status in ("legacy", "consistent", "resolved") and valid_relative(i.relative_speed)
    ]
    coverage = length([(i.start_m, i.end_m) for i in accepted]) / whole_path_length
    diagnostic = [
        i
        for i in intervals
        if i.status not in ("gap", "conflict")
        and i.relative_status in ("legacy", "consistent", "resolved")
        and i.closure is False
        and valid_relative(i.relative_speed)
    ]
    diagnostic_ci = weighted_relative(diagnostic)
    ci_raw = weighted_relative(accepted) if contract_ready else None
    if not contract_ready:
        reasons.append("SOURCE_NOT_READY")
    if coverage + 1e-9 < minimum_coverage:
        reasons.append("TARGET_COVERAGE_LOW")
    if any(i.closure is True and i.contributors for i in intervals):
        reasons.append("CLOSED")
    if any(i.closure is None and i.contributors for i in intervals):
        reasons.append("CLOSURE_UNKNOWN")
    if not accepted and any(
        i.relative_status == "legacy"
        and
        i.contributors and i.status != "conflict" and not valid_relative(i.relative_speed)
        for i in intervals
    ):
        reasons.append(
            "RELATIVE_SPEED_MISSING"
            if all(i.relative_speed is None for i in intervals)
            else "RELATIVE_SPEED_INVALID"
        )
    speed_intervals = [
        i
        for i in intervals
        if i.speed_valid
        and i.absolute_status in ("legacy", "consistent")
        and physical(i)
        and i.current is not None
        and math.isfinite(i.current)
        and i.current >= 0
    ]
    speed_coverage = length([(i.start_m, i.end_m) for i in speed_intervals]) / whole_path_length
    speed_reasons = list(quality_reasons) + list(speed_quality_reasons)
    if not contract_ready:
        speed_reasons.append("SOURCE_NOT_READY")
    if speed_coverage + 1e-9 < minimum_coverage:
        speed_reasons.append("ABSOLUTE_SPEED_COVERAGE_LOW")
    speed_reasons.extend(r for r in reasons if r in ("CLOSED", "CLOSURE_UNKNOWN"))
    if coverage + 1e-9 < minimum_coverage:
        reasons.extend(r for i in intervals for r in i.relative_reasons)
    if speed_coverage + 1e-9 < minimum_coverage:
        speed_reasons.extend(r for i in intervals for r in i.absolute_reasons)
    current = None
    if (
        contract_ready
        and speed_coverage + 1e-9 >= minimum_coverage
        and speed_intervals
        and not speed_reasons
    ):
        support = sum(i.end_m - i.start_m for i in speed_intervals)
        current = (
            0.0
            if any(i.current == 0 for i in speed_intervals)
            else support / sum((i.end_m - i.start_m) / i.current for i in speed_intervals)
        )
    blockers = sorted(set(reasons))
    return {
        "target_method": TARGET_METHOD,
        "current_speed_kmh": current,
        "freeflow_speed_kmh": None,
        "ci_raw": ci_raw,
        "ci": ci_raw if ci_raw is not None and not blockers else None,
        "target_valid_coverage": coverage,
        "absolute_speed_coverage": speed_coverage,
        "paired_speed_coverage": None,
        "diagnostic_ci": diagnostic_ci,
        "diagnostic_geometry_coverage": length([(i.start_m, i.end_m) for i in diagnostic])
        / whole_path_length,
        "reasons": sorted(set(blockers + speed_reasons)),
        "ci_reasons": blockers,
        "speed_reasons": sorted(set(speed_reasons)),
        "ci_policy_resolution_used": any(i.relative_resolution_method in ("relative_midrange_v1", "prefer_nonunity_on_large_conflict_v1") for i in accepted),
    }
