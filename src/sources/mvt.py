"""Stored MVT -> occurrences, preserving extent, components and absent semantics."""

import math
import mapbox_vector_tile
from mapbox_vector_tile.Mapbox import vector_tile_pb2
from google.protobuf.message import DecodeError
from ..domain.processing import FeatureOccurrence
from ..geo.tiles import tile_lonlat
from ..storage.files import digest


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def decode_tile(body: bytes, tile: tuple[int, int, int], snapshot: str, resource: str):
    """Decode y-down tile coordinates to WGS84. No freeflow/confidence inference."""
    if not body:
        raise ValueError("SOURCE_SCHEMA_INVALID: empty tile")
    try:
        wire = vector_tile_pb2.tile()
        wire.ParseFromString(body)
        decoded = mapbox_vector_tile.decode(body, default_options={"y_coord_down": True})
    except DecodeError as exc:
        raise ValueError("SOURCE_SCHEMA_INVALID: malformed protobuf") from exc
    z, x, y = tile
    occurrences = []
    source_ids = {
        layer.name: [feature.id if feature.HasField("id") else None for feature in layer.features]
        for layer in wire.layers
    }
    if decoded and set(decoded) != {"Traffic flow"}:
        raise ValueError("SOURCE_SCHEMA_INVALID: unexpected Orbis flow layers")
    for layer_name, layer in decoded.items():
        extent = layer.get("extent")
        if not isinstance(extent, int) or extent <= 0:
            raise ValueError("SOURCE_SCHEMA_INVALID: invalid extent")
        for index, feature in enumerate(layer.get("features", [])):
            geometry = feature.get("geometry", {})
            kind = geometry.get("type")
            if kind not in ("LineString", "MultiLineString"):
                continue
            raw = geometry["coordinates"]
            raw_components = [raw] if kind == "LineString" else raw
            components = [
                [tile_lonlat(x + px / extent, y + py / extent, z) for px, py in points]
                for points in raw_components
                if len(points) >= 2
            ]
            tags = feature.get("properties", {})
            if not isinstance(tags, dict):
                raise ValueError("SOURCE_SCHEMA_INVALID: tags")
            invalid_fields = {}
            reasons = []
            relative = number(tags.get("relative_speed"))
            if "relative_speed" not in tags:
                reasons.append("RELATIVE_SPEED_MISSING")
            elif relative is None or not 0 <= relative <= 1:
                reasons.append("RELATIVE_SPEED_INVALID")
                invalid_fields["relative_speed"] = repr(tags["relative_speed"])
            absolute = number(tags.get("absolute_speed"))
            if "absolute_speed" in tags and (absolute is None or absolute < 0):
                invalid_fields["absolute_speed"] = repr(tags["absolute_speed"])
                absolute = None
            tags = {
                k: (None if isinstance(v, float) and not math.isfinite(v) else v)
                for k, v in tags.items()
            }
            if "openlr" in tags and not isinstance(tags["openlr"], str):
                raise ValueError("SOURCE_SCHEMA_INVALID: non-string OpenLR")
            closure = tags.get("road_closure", False)
            if not isinstance(closure, bool):
                if closure in (0, 1):
                    closure = bool(closure)
                else:
                    closure = None
            identity = digest(
                [
                    "orbis_vector_flow_v2",
                    snapshot,
                    resource,
                    layer_name,
                    index,
                    components,
                    tags.get("openlr"),
                ]
            )
            occurrences.append(
                FeatureOccurrence(
                    occurrence_id=identity,
                    resource_id=resource,
                    layer=layer_name,
                    feature_index=index,
                    source_feature_id=source_ids[layer_name][index],
                    components=components,
                    tags=tags,
                    openlr=tags.get("openlr"),
                    absolute_speed=absolute,
                    relative_speed=relative,
                    road_closure=closure,
                    quantization_m=40075016.686
                    * math.cos(math.radians(components[0][0][1]))
                    / (2**z * extent)
                    if components
                    else 0,
                    reasons=reasons,
                    invalid_fields=invalid_fields,
                )
            )
    return occurrences
