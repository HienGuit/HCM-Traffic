"""Offline sampling diagnostics; interval union remains the primary coverage."""


def sensitivity(intervals, length):
    variants = []
    for step in (5, 10, 20):
        for phase in (0, step / 2):
            points = []
            x = phase
            while x < length:
                found = next((i for i in intervals if i.start_m <= x < i.end_m), None)
                points.append(
                    {
                        "m": x,
                        "status": found.status if found else "gap",
                        "contributors": found.contributors if found else [],
                    }
                )
                x += step
            variants.append({"step_m": step, "phase_m": phase, "samples": points})
    coverage = (
        sum(i.end_m - i.start_m for i in intervals if i.status not in ("gap", "conflict")) / length
    )
    return {
        "kind": "sampling_diagnostic_only_not_association_sensitivity",
        "variants": variants,
        "interval_geometry_coverage": coverage,
        "thresholds": {str(t): coverage >= t for t in (0.90, 0.95, 0.98)},
        "accuracy_claim": False,
    }


def compare_associations(baseline, alternate, length_m):
    """Compare actual contributor assignments on a shared atomic metre partition."""
    from ..geo.support import atomic

    def signature(rows, a, b):
        return tuple(
            sorted(
                (i.status, tuple(sorted(i.contributors)))
                for i in rows
                if i.start_m < b and i.end_m > a
            )
        )

    changed = []
    for a, b in atomic(
        [[(i.start_m, i.end_m) for i in rows] for rows in (baseline, alternate)], length_m
    ):
        first, second = signature(baseline, a, b), signature(alternate, a, b)
        if first != second:
            changed.append({"start_m": a, "end_m": b, "baseline": first, "alternate": second})

    def coverage(rows):
        return (
            sum(i.end_m - i.start_m for i in rows if i.status not in ("gap", "conflict")) / length_m
        )

    return {
        "kind": "actual_decoder_and_projection_profile_sensitivity",
        "changed_assignment_m": sum(r["end_m"] - r["start_m"] for r in changed),
        "changed_intervals": changed,
        "baseline_geometry_coverage": coverage(baseline),
        "alternate_geometry_coverage": coverage(alternate),
        "threshold_pass_changed": {
            str(t): (coverage(baseline) >= t) != (coverage(alternate) >= t)
            for t in (0.9, 0.95, 0.98)
        },
        "direction_verified": False,
    }
