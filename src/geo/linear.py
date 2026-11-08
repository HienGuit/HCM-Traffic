from shapely.geometry import LineString
from shapely.ops import substring


def subline(line: LineString, start_m: float, end_m: float) -> LineString:
    """Directed metric subpath; reject invalid or empty offsets."""
    if not 0 <= start_m < end_m <= line.length + 1e-6:
        raise ValueError("Invalid metric path offsets")
    return substring(line, start_m, min(end_m, line.length))


def path_geometry(graph, entries):
    """Join ordered directed edges; never interpolate across a topology gap."""
    coords = []
    previous = None
    for entry in entries:
        edge = graph.edges[entry.edge_id]
        if previous is not None and previous != edge.start:
            raise ValueError("Disconnected directed path")
        line = subline(graph.lines[edge.id], entry.start_m, entry.end_m)
        points = list(line.coords)
        if coords and abs(coords[-1][0] - points[0][0]) + abs(coords[-1][1] - points[0][1]) > 1e-4:
            raise ValueError("Offsets create a path gap")
        coords.extend(points if not coords else points[1:])
        previous = edge.end
    return LineString(coords)
