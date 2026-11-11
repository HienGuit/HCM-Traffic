"""Full scalar table and exact 15-column interim projection; payloads stay sidecar."""

from datetime import datetime
from zoneinfo import ZoneInfo
from ..storage.files import write_csv, write_json, write_jsonl, sha256, digest

INTERIM_FIELDS = [
    "observation_id",
    "road_name",
    "direction_label",
    "local_date",
    "day_of_week",
    "time_slot",
    "segment_length_m",
    "road_type",
    "lanes_mean",
    "junction_count",
    "signal_count",
    "current_speed_kmh",
    "precipitation_mm",
    "weather_code",
    "ci",
]


def enrich_rows(rows, pilots, metadata, audit, config):
    paths = {(p["segment_id"], p["direction_id"]): p for p in metadata["registry_paths"]}
    statics = {(s["segment_id"], s["direction_id"]): s for s in audit["static_features"]}
    weather = {
        (s["segment_id"], s["direction_id"], s["snapshot_id"]): s
        for s in audit["weather_alignment"]
    }
    full, sidecars = [], []
    for source in [*rows, *pilots]:
        row = dict(source)
        path = paths[(row["segment_id"], row["direction_id"])]
        static = statics[(row["segment_id"], row["direction_id"])]
        instant = row.get("scheduled_at") or row.get("observed_at")
        local = datetime.fromisoformat(instant).astimezone(ZoneInfo(config.study.timezone))
        scheduled = row.get("scheduled_at") is not None
        point = path["geometry"]["coordinates"]
        label = "{:.7f},{:.7f} -> {:.7f},{:.7f}".format(*point[0], *point[-1])
        identity = row.get("observation_id") or digest(
            [
                config.sources.target_method,
                path["registry_version"],
                row["segment_id"],
                row["direction_id"],
                row.get("source_snapshot_id"),
                instant,
            ]
        )
        static_id = digest(
            {
                "graph": path["graph_version"],
                "edges": path["edges"],
                "junction_method": static["junction_method"],
                "signal_method": static["signal_method"],
            }
        )
        row.update(
            observation_id=identity,
            physical_segment_id=row["segment_id"],
            segment_direction_id=row["segment_id"] + "__" + row["direction_id"],
            static_path_id=static_id,
            road_name=path["road_name"],
            direction_label=label,
            local_date=local.date().isoformat(),
            day_of_week=local.isoweekday(),
            time_slot=local.strftime("%H:%M"),
            time_slot_kind="scheduled" if scheduled else "actual_pilot",
            collection_kind="scheduled" if scheduled else "pilot",
            graph_version=path["graph_version"],
            calibration_status=row.get("calibration_status", "not_evaluated"),
            review_status=path["approval_status"],
            direction_method=config.sources.source_direction_rule,
            source_contract_version=config.sources.version,
            speed_method="harmonic_current_on_own_support_v1",
            scientific_release=False,
        )
        row.update(
            {
                k: v
                for k, v in static.items()
                if not isinstance(v, (dict, list, tuple))
                and k not in ("segment_id", "direction_id")
            }
        )
        w = weather.get((row["segment_id"], row["direction_id"], row.get("source_snapshot_id")), {})
        row.update(
            precipitation_window_start=w.get("window_start"),
            precipitation_window_end=w.get("window_end"),
            weather_grid_distance_m=w.get("grid_distance_m"),
            weather_gap_s=w.get("gap_s"),
        )
        row["quality_reason_codes"] = ";".join(row.get("quality_reasons", []))
        row["signal_reason_codes"] = ";".join(static.get("signal_reasons", []))
        payload = {k: v for k, v in row.items() if isinstance(v, (dict, list, tuple))}
        payload.update({k: v for k, v in static.items() if isinstance(v, (dict, list, tuple))})
        sidecars.append({"observation_id": identity, "static_path_id": static_id, **payload})
        full.append({k: v for k, v in row.items() if not isinstance(v, (dict, list, tuple))})
    return full, sidecars


def export_feature_tables(rows, audit, metadata, config):
    full, sidecars = enrich_rows(rows, audit["pilot_observations"], metadata, audit, config)
    run = metadata["run_id"]
    full_path = config.output("interim/" + run + "/tables/observations_full.csv")
    interim_path = config.output("interim/" + run + "/tables/observations.csv")
    fields = sorted({key for row in full for key in row})
    write_csv(full_path, full, fields)
    write_csv(
        interim_path, [{k: row.get(k) for k in INTERIM_FIELDS} for row in full], INTERIM_FIELDS
    )
    sidecar_path = full_path.parent / "sidecar.jsonl"
    write_jsonl(sidecar_path, sidecars)
    manifest = {
        "artifact_type": "derived_full_table",
        "schema_version": "full_features_v1",
        "rows": len(full),
        "full": str(full_path),
        "interim": str(interim_path),
        "sidecar": str(sidecar_path),
        "interim_columns": INTERIM_FIELDS,
        "source_fingerprint": metadata["source_fingerprint"],
        "sha256": {str(p): sha256(p) for p in (full_path, interim_path, sidecar_path)},
    }
    write_json(full_path.parent / "manifest.json", manifest, immutable=True)
    return manifest
