"""Two weather variables; source interval and timestamps are mandatory metadata."""

from datetime import datetime, timedelta, timezone
import json
from .mvt import number

WMO_VERSION = "open_meteo_2026_11_v1"
WMO_CODES = {
    0,
    1,
    2,
    3,
    45,
    48,
    51,
    53,
    55,
    56,
    57,
    61,
    63,
    65,
    66,
    67,
    71,
    73,
    75,
    77,
    80,
    81,
    82,
    85,
    86,
    95,
    96,
    97,
    99,
}


def request_spec(resource):
    return {"url": resource.url, "params": resource.params, "headers": {}}


def parse_weather(body: bytes, resource, received_at: str):
    data = json.loads(body)
    current = data.get("current", {})
    units = data.get("current_units", {})
    reasons = []
    valid_at = None
    if current.get("time") and data.get("utc_offset_seconds") == 0:
        valid_at = datetime.fromisoformat(current["time"])
        if valid_at.tzinfo is None:
            valid_at = valid_at.replace(tzinfo=timezone.utc)
    interval = current.get("interval")
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval <= 0:
        interval = None
    precipitation = number(current.get("precipitation"))
    if precipitation is not None and precipitation < 0:
        precipitation = None
    code = current.get("weather_code")
    if isinstance(code, bool) or not isinstance(code, int) or code not in WMO_CODES:
        code = None
        reasons.append("WEATHER_CODE_UNSUPPORTED")
    if units.get("precipitation") != "mm" or units.get("weather_code") != "wmo code":
        precipitation = None
        code = None
    if not valid_at or interval is None or precipitation is None or code is None:
        reasons.append("WEATHER_MISSING")
    return {
        "resource_id": resource.resource_id,
        "segments": resource.segments,
        "requested_point": resource.point,
        "returned_point": [data.get("longitude"), data.get("latitude")],
        "valid_at": valid_at.isoformat() if valid_at else None,
        "received_at": received_at,
        "precipitation_mm": precipitation,
        "weather_code": code,
        "interval_seconds": interval,
        "window_start": (valid_at - timedelta(seconds=interval)).isoformat()
        if valid_at and interval
        else None,
        "window_end": valid_at.isoformat() if valid_at else None,
        "units": units,
        "wmo_version": WMO_VERSION,
        "reasons": reasons,
    }
