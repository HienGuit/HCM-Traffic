from ..sources.mvt import decode_tile
from ..sources.weather import parse_weather


def normalize_snapshot(manifest, plan, selected):
    features = []
    weather = []
    errors = []
    receipts = {}
    for resource in plan.resources:
        if resource.resource_id not in selected:
            continue
        attempt, path = selected[resource.resource_id]
        try:
            if resource.provider == "traffic":
                batch = decode_tile(
                    path.read_bytes(), resource.tile, manifest.snapshot_id, resource.resource_id
                )
                features.extend(batch)
                for feature in batch:
                    receipts[feature.occurrence_id] = {
                        "requested_at": attempt.requested_at.isoformat(),
                        "received_at": attempt.received_at.isoformat(),
                        "source_version": attempt.source_version,
                        "snapshot_id": manifest.snapshot_id,
                        "body_sha256": attempt.body_sha256,
                    }
            else:
                weather.append(
                    parse_weather(path.read_bytes(), resource, attempt.received_at.isoformat())
                )
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            errors.append(
                {
                    "resource": resource.resource_id,
                    "reason": "SOURCE_SCHEMA_INVALID",
                    "error_type": type(exc).__name__,
                    "detail": str(exc)[:300],
                }
            )
    return features, weather, receipts, errors
