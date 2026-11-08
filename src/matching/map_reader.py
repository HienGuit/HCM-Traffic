"""Adapter to the pinned decoder's MapReader/Node/Line protocol."""

from openlr import Coordinates, FRC, FOW
from openlr_dereferencer.maps.abstract import MapReader, Node, Line
from shapely.geometry import Point, LineString
from ..geo.crs import project


class GraphNode(Node):
    def __init__(self, node_id, reader):
        self._id, self.reader = node_id, reader

    @property
    def node_id(self):
        return self._id

    @property
    def coordinates(self):
        return Coordinates(*self.reader.graph.nodes[self._id][:2])

    def outgoing_lines(self):
        return (self.reader.get_line(e) for e in self.reader.graph.outgoing[self._id])

    def incoming_lines(self):
        return (self.reader.get_line(e) for e in self.reader.graph.incoming[self._id])

    def connected_lines(self):
        return (
            self.reader.get_line(e)
            for e in set(
                self.reader.graph.incoming[self._id] + self.reader.graph.outgoing[self._id]
            )
        )


class GraphLine(Line):
    def __init__(self, edge, reader):
        self.edge, self.reader = edge, reader

    @property
    def line_id(self):
        return self.edge.id

    @property
    def start_node(self):
        return self.reader.get_node(self.edge.start)

    @property
    def end_node(self):
        return self.reader.get_node(self.edge.end)

    @property
    def frc(self):
        return FRC(self.edge.frc)

    @property
    def fow(self):
        return FOW(self.edge.fow)

    @property
    def geometry(self):
        return LineString(self.edge.coords)

    @property
    def length(self):
        return self.edge.length

    def distance_to(self, coord):
        return self.reader.graph.lines[self.edge.id].distance(
            project(Point(*coord), self.reader.graph.crs)
        )


class OSMMapReader(MapReader):
    def __init__(self, graph):
        self.graph = graph
        self._lines = {}
        self._nodes = {}

    def get_line(self, line_id):
        if line_id not in self._lines:
            self._lines[line_id] = GraphLine(self.graph.edges[line_id], self)
        return self._lines[line_id]

    def get_node(self, node_id):
        if node_id not in self._nodes:
            self._nodes[node_id] = GraphNode(node_id, self)
        return self._nodes[node_id]

    def get_lines(self):
        return (self.get_line(k) for k in self.graph.edge_ids)

    def get_linecount(self):
        return len(self.graph.edges)

    def get_nodes(self):
        return (self.get_node(k) for k in self.graph.node_ids)

    def get_nodecount(self):
        return len(self.graph.node_ids)

    def find_nodes_close_to(self, coord, dist):
        point = project(Point(*coord), self.graph.crs)
        return (
            self.get_node(self.graph.node_ids[i])
            for i in self.graph.node_tree.query(point.buffer(dist))
            if point.distance(self.graph.node_points[i]) <= dist
        )

    def find_lines_close_to(self, coord, dist):
        point = project(Point(*coord), self.graph.crs)
        return (
            self.get_line(self.graph.edge_ids[i])
            for i in self.graph.tree.query(point.buffer(dist))
            if point.distance(self.graph.lines[self.graph.edge_ids[i]]) <= dist
        )
