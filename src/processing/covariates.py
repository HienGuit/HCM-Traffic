from datetime import datetime
from pyproj import Geod


def align_weather(records, segment_id, scheduled_at, policy):
    matches = [r for r in records if segment_id in r["segments"]]
    if not matches:
        return {"precipitation_mm": None, "weather_code": None, "reasons": ["WEATHER_MISSING"]}
    # Selection uses the explicit segment-resource mapping; no nearby-grid fallback.
    record = matches[0]
    reasons = list(record["reasons"])
    gap = None
    distance = None
    if record["valid_at"]:
        gap = (scheduled_at - datetime.fromisoformat(record["valid_at"])).total_seconds()
        if gap > policy.weather_age_s or gap < -policy.weather_future_s:
            reasons.append("WEATHER_STALE")
    returned = record["returned_point"]
    if all(isinstance(v, (int, float)) for v in returned) and record["requested_point"]:
        distance = Geod(ellps="WGS84").inv(*record["requested_point"], *returned)[2]
        if distance > policy.weather_grid_m:
            reasons.append("WEATHER_GRID_FAR")
    else:
        reasons.append("WEATHER_MISSING")
    return {
        **record,
        "gap_s": gap,
        "grid_distance_m": distance,
        "precipitation_mm": None if reasons else record["precipitation_mm"],
        "weather_code": None if reasons else record["weather_code"],
        "reasons": sorted(set(reasons)),
    }
