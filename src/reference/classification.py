"""OSM semantics and OpenLR mapping adapter.

FRC/FOW mapping data adapted from TomTom osm_tag_mapper revision
d4b00037fa07072079f50e9bd34809b4a413903f, Copyright 2025 TomTom Global
Content B.V., Apache-2.0 (see bundled NOTICE). Attribution is approximate,
not a claim of parity with TomTom's internal road attribution.
"""

MAPPER_VERSION = "osm_tag_mapper_d4b00037_equivalent_v1"
FRC_MAP = {
    "motorway": 0,
    "motorway_link": 1,
    "trunk": 1,
    "trunk_link": 1,
    "primary": 2,
    "primary_link": 2,
    "secondary": 3,
    "secondary_link": 3,
    "tertiary": 4,
    "tertiary_link": 4,
    "unclassified": 6,
    "residential": 7,
    "service": 7,
    "living_street": 7,
    "road": 7,
    "track": 7,
    "yes": 5,
}
NON_MOTOR = {"footway", "path", "steps", "pedestrian", "cycleway", "bridleway"}


def frc_fow(tags: dict) -> tuple[int, int]:
    highway = tags.get("highway")
    frc = FRC_MAP.get(highway, FRC_MAP.get(tags.get("construction"), 7))
    if tags.get("railway"):
        fow = 0
    elif highway != "motorway" and tags.get("dual_carriageway") in ("yes", "true", "1"):
        fow = 2
    elif tags.get("junction") == "roundabout":
        fow = 4
    elif tags.get("junction") == "circular" or highway in (
        "pedestrian",
        "service",
        "path",
        "footway",
        "turning_circle",
    ):
        fow = 7
    elif highway == "motorway":
        fow = 1
    elif str(highway).endswith("_link"):
        fow = 6
    else:
        fow = 3 if highway in FRC_MAP else 0
    return frc, fow


def directions(tags: dict) -> tuple[tuple[int, ...], str]:
    """Legal traversal supported by ordinary tags; conditional/access stays unknown."""
    value = tags.get("oneway")
    if value in ("-1", "reverse"):
        traversals = (-1,)
    elif value in ("yes", "1", "true") or (value is None and tags.get("junction") == "roundabout"):
        traversals = (1,)
    elif value in (None, "no", "0", "false"):
        traversals = (1, -1)
    else:
        return (1, -1), "unverified"
    unknown = (
        any("conditional" in k for k in tags)
        or tags.get("access") in ("private", "destination", "customers")
        or tags.get("motor_vehicle") in ("private", "destination")
    )
    return traversals, "unverified" if unknown else "supported"


def directional_lanes(tags: dict, traversal: int) -> int | None:
    """OSM directional profile; never divide two-way total lanes by two."""
    key = "lanes:forward" if traversal == 1 else "lanes:backward"
    value = tags.get(key)
    allowed, _ = directions(tags)
    if value is None and len(allowed) == 1 and traversal in allowed:
        value = tags.get("lanes")
    try:
        result = int(value)
        return result if 0 < result < 30 else None
    except (TypeError, ValueError):
        return None
