"""Scheduled canonical rows and profile flags derive from one table."""

from datetime import datetime, time
from zoneinfo import ZoneInfo
from ..storage.files import digest

from ..domain.processing import Observation, OUTPUT_SCHEMA, TARGET_METHOD

FIELDS = list(Observation.model_fields)


def observation(path, scheduled, run_id, config, measurement=None):
    key = [
        TARGET_METHOD,
        path.registry_version,
        path.segment_id,
        path.direction_id,
        scheduled.astimezone(ZoneInfo("UTC")).isoformat(),
    ]
    local = scheduled.astimezone(ZoneInfo(config.study.timezone))
    row = {field: None for field in FIELDS}
    row.update(
        observation_id=digest(key),
        schema_version=OUTPUT_SCHEMA,
        registry_version=path.registry_version,
        segment_id=path.segment_id,
        direction_id=path.direction_id,
        scheduled_at=scheduled.isoformat(),
        local_date=local.date().isoformat(),
        time_slot=local.strftime("%H:%M"),
        day_of_week=local.isoweekday(),
        day_type="weekend" if local.isoweekday() > 5 else "weekday",
        road_name=path.road_name,
        segment_length_m=path.length_m,
        processing_run_id=run_id,
        target_method=config.sources.target_method,
        speed_method="harmonic_current_on_own_support_v1",
        speed_source_product=config.sources.traffic_product,
        calibration_status="not_evaluated",
        review_status=path.approval_status,
        technical_status="missed",
        direction_method=config.sources.source_direction_rule,
        ci_policy_resolution_used=False,
        ci_reasons=[],
        speed_reasons=[],
        ci_ready=False,
        model_ready=False,
        needs_review=True,
    )
    if measurement is None:
        row.update(
            ci_status="missed",
            model_status="target_unavailable",
            quality_reasons=["MISSING_CAPTURE"],
        )
        return Observation.model_validate(row).model_dump(mode="json")
    row.update({k: v for k, v in measurement.items() if k in row})
    reasons = row["quality_reasons"] or []
    row["ci_ready"] = row["ci"] is not None
    required = [
        "precipitation_mm",
        "weather_code",
        "road_type",
        "lanes_mean",
        "junction_count",
        "signal_count",
    ]
    row["model_ready"] = (
        row["ci_ready"]
        and all(row[k] is not None for k in required)
        and "COVARIATE_CONFLICT" not in reasons
    )
    row["needs_review"] = bool(reasons)
    row["ci_status"] = (
        "valid"
        if row["ci_ready"]
        else "closed"
        if "CLOSED" in reasons
        else "conflict"
        if any("CONFLICT" in r for r in reasons)
        else "unverified"
        if "DIRECTION_UNVERIFIED" in reasons
        else "missing_input"
    )
    row["model_status"] = (
        "ready"
        if row["model_ready"]
        else "target_unavailable"
        if not row["ci_ready"]
        else "missing_covariates"
    )
    return Observation.model_validate(row).model_dump(mode="json")


def canonical_rows(paths, measurements, config, run_id):
    rows = []
    for day in config.study.dates:
        for slot in config.study.slots:
            scheduled = datetime.combine(
                day, time.fromisoformat(slot), ZoneInfo(config.study.timezone)
            )
            for path in paths:
                key = (
                    path.segment_id,
                    path.direction_id,
                    scheduled.astimezone(ZoneInfo("UTC")).isoformat(),
                )
                rows.append(observation(path, scheduled, run_id, config, measurements.get(key)))
    return sorted(
        rows,
        key=lambda r: (
            r["registry_version"],
            r["segment_id"],
            r["direction_id"],
            r["scheduled_at"],
        ),
    )


def expected_keys(paths, config):
    """Exact expected natural keys in UTC, never just N*slots*dates cardinality."""
    return {
        (
            p.registry_version,
            p.segment_id,
            p.direction_id,
            datetime.combine(day, time.fromisoformat(slot), ZoneInfo(config.study.timezone))
            .astimezone(ZoneInfo("UTC"))
            .isoformat(),
        )
        for p in paths
        for day in config.study.dates
        for slot in config.study.slots
    }
