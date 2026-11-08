"""Study exclusions in metric CRS and topology-aware structural intersection checks."""

import unicodedata
from shapely.geometry import shape
from shapely.strtree import STRtree
from ..geo.crs import project
from ..geo.support import union


def road_key(name):
    value = unicodedata.normalize("NFC", str(name)).strip().casefold()
    return value[6:] if value.startswith("\u0111\u01b0\u1eddng ") else value


def active_structure(value):
    return value is not None and str(value).strip().lower() not in ("", "no", "0", "false")


def structure_reason(tags):
    if active_structure(tags.get("bridge")):
        return "OSM_BRIDGE"
    if active_structure(tags.get("tunnel")):
        return "OSM_TUNNEL"
    if tags.get("junction") in ("roundabout", "circular"):
        return "OSM_ROUNDABOUT"
    if tags.get("highway", "").endswith("_link"):
        return "OSM_RAMP"
    return None


def exclusion_geometry(feature, crs):
    geometry = project(shape(feature["geometry"]), crs)
    kind = geometry.geom_type
    props = feature["properties"]
    if kind in ("Polygon", "MultiPolygon"):
        return geometry  # Input already contains the complete study envelope.
    radius = props.get("buffer_m")
    if radius is None or radius < 0 or (kind == "Point" and radius <= 0):
        raise ValueError("Explicit positive radius required for point exclusions")
    return geometry.buffer(radius) if radius else geometry


def direction_applies(scope, direction):
    if scope == "both":
        return True
    return direction in (scope if isinstance(scope, list) else [scope])


def line_intervals(line, geometry):
    """Exact geometric intersection, including middle sections; no sampled tests."""
    cut = line.intersection(geometry)
    parts = list(cut.geoms) if hasattr(cut, "geoms") else [cut]
    intervals = []
    for part in parts:
        if part.geom_type == "LineString" and part.length > 1e-8:
            from shapely.geometry import Point

            offsets = [line.project(Point(p)) for p in part.coords]
            intervals.append((min(offsets), max(offsets)))
    return union(intervals)


def subtract_intervals(support, excluded):
    result = list(support)
    for low, high in union(excluded):
        pieces = []
        for a, b in result:
            if high <= a or low >= b:
                pieces.append((a, b))
            else:
                if a < low:
                    pieces.append((a, low))
                if high < b:
                    pieces.append((high, b))
        result = pieces
    return result


class CandidateFilter:
    def __init__(self, features, graph):
        self.graph = graph
        self.features = features
        self.geometries = [exclusion_geometry(f, graph.crs) for f in features]
        self.tree = STRtree(self.geometries)
        self.crossings = []

    def clipped_edge(self, edge, region):
        line = self.graph.lines[edge.id]
        kept = line_intervals(line, region)
        losses = []
        reason = structure_reason(edge.tags)
        if reason:
            return [], [
                {
                    "edge_id": edge.id,
                    "way_id": edge.way_id,
                    "reason": reason,
                    "start_m": 0,
                    "end_m": edge.length,
                }
            ]
        excluded = []
        direction = "FORWARD" if edge.traversal == 1 else "BACKWARD"
        for index in self.tree.query(line):
            feature = self.features[index]
            props = feature["properties"]
            if road_key(props.get("road_name")) != road_key(edge.tags.get("name")):
                continue
            if not direction_applies(props.get("filter_directions", "both"), direction):
                continue
            for a, b in line_intervals(line, self.geometries[index]):
                excluded.append((a, b))
                losses.append(
                    {
                        "edge_id": edge.id,
                        "way_id": edge.way_id,
                        "reason": "STUDY_EXCLUSION",
                        "exclusion_id": props["exclusion_id"],
                        "start_m": a,
                        "end_m": b,
                    }
                )
        # A bridge without connecting nodes can cross over the mainline. Nearby links alone do nothing.
        from shapely.geometry import mapping

        for index in self.graph.tree.query(line):
            other = self.graph.edges[self.graph.edge_ids[index]]
            if other.id == edge.id or {edge.start, edge.end}.intersection({other.start, other.end}):
                continue
            if not (
                active_structure(other.tags.get("bridge"))
                or active_structure(other.tags.get("tunnel"))
            ):
                continue
            cut = line.intersection(self.graph.lines[other.id])
            if cut.is_empty:
                continue
            # The crossing itself is known, but width/junction scope need actual data, not invented radii.
            self.crossings.append(
                {
                    "edge_id": edge.id,
                    "structure_way_id": other.way_id,
                    "reason": "GRADE_SEPARATED_CROSSING_EXTENT_REVIEW",
                    "geometry": mapping(project(cut, "EPSG:4326", self.graph.crs)),
                    "tags": other.tags,
                    "source_layer": other.tags.get("layer"),
                    "selected_layer": edge.tags.get("layer"),
                }
            )
        return subtract_intervals(kept, excluded), losses

    def path_review_reasons(self, path):
        line = project(shape(path.geometry), self.graph.crs)
        own = {e.edge_id for e in path.edges}
        return sorted(
            {
                c["reason"]
                for c in self.crossings
                if c["edge_id"] in own
                and line.intersects(project(shape(c["geometry"]), self.graph.crs))
            }
        )
