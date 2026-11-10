"""Junction-to-junction graph with immutable original-way correspondence."""

import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
import portalocker
from shapely.geometry import LineString, Point, box
from shapely.strtree import STRtree
from ..domain.reference import DirectedEdge
from ..geo.crs import project
from ..storage.files import digest, read_json, write_json, sha256
from ..storage.cache import validate_cache, seal_cache
from .classification import directions, frc_fow, NON_MOTOR, MAPPER_VERSION
from .importer import import_extract


class Graph:
    def __init__(
        self,
        version: str,
        edges: list[DirectedEdge],
        nodes: dict,
        crs: str,
        restrictions: list | None = None,
    ):
        self.version, self.crs, self.nodes = version, crs, nodes
        self.restrictions = restrictions or []
        self.edges = {e.id: e for e in edges}
        self.outgoing, self.incoming = defaultdict(list), defaultdict(list)
        self.lines = {}
        for edge in edges:
            self.outgoing[edge.start].append(edge.id)
            self.incoming[edge.end].append(edge.id)
            self.lines[edge.id] = project(LineString(edge.coords), crs)
        self.edge_ids = list(self.edges)
        self.tree = STRtree([self.lines[k] for k in self.edge_ids])
        self.node_ids = sorted(set(self.outgoing) | set(self.incoming))
        self.node_points = [project(Point(*self.nodes[k][0:2]), crs) for k in self.node_ids]
        self.node_tree = STRtree(self.node_points)


def graph_from_normalized(database: Path, version: str, crs: str, buffered_region=None) -> Graph:
    with closing(sqlite3.connect(database)) as db:
        nodes = {r[0]: (r[1], r[2], json.loads(r[3])) for r in db.execute("SELECT * FROM nodes")}
        ways = [
            (r[0], json.loads(r[1]), json.loads(r[2])) for r in db.execute("SELECT * FROM ways")
        ]
        relations = [
            (r[0], json.loads(r[1]), json.loads(r[2]))
            for r in db.execute("SELECT * FROM relations")
        ]
    motor_ways = [
        (wid, refs, tags)
        for wid, refs, tags in ways
        if tags.get("highway") and tags["highway"] not in NON_MOTOR and tags.get("area") != "yes"
    ]
    memberships, neighbors = defaultdict(set), defaultdict(set)
    for wid, refs, tags in motor_ways:
        for a, b in zip(refs, refs[1:]):
            neighbors[a].add(b)
            neighbors[b].add(a)
        for n in refs:
            memberships[n].add(wid)
    restricted_ways = {m[1] for _, members, _ in relations for m in members if m[0] == "w"}
    edges = []
    missing = 0
    for wid, refs, tags in motor_ways:
        if any(n not in nodes for n in refs):
            missing += 1
            continue
        cuts = [
            i
            for i, n in enumerate(refs)
            if i in (0, len(refs) - 1)
            or len(memberships[n]) > 1
            or len(neighbors[n]) != 2
            or nodes[n][2].get("highway") == "traffic_signals"
        ]
        traversals, legal = directions(tags)
        if wid in restricted_ways:
            legal = "unverified"
        if tags.get("access") == "no" or tags.get("motor_vehicle") == "no":
            continue
        frc, fow = frc_fow(tags)
        for first, last in zip(cuts, cuts[1:]):
            coords = tuple((nodes[n][0], nodes[n][1]) for n in refs[first : last + 1])
            if len(coords) < 2:
                continue
            metric = project(LineString(coords), crs)
            if metric.length < 0.01:
                continue
            boundary = buffered_region is not None and metric.distance(buffered_region.boundary) < 1
            for traversal in traversals:
                reverse = traversal == -1
                identity = f"{version}:{wid}:{first}:{last}:{traversal}"
                edges.append(
                    DirectedEdge(
                        identity,
                        wid,
                        refs[last] if reverse else refs[first],
                        refs[first] if reverse else refs[last],
                        first,
                        last,
                        traversal,
                        metric.length,
                        tuple(reversed(coords)) if reverse else coords,
                        tags,
                        frc,
                        fow,
                        legal,
                        boundary,
                    )
                )
    graph = Graph(version, edges, nodes, crs, relations)
    graph.missing_ways = missing
    return graph


def save_graph(graph: Graph, folder: Path) -> None:
    db = sqlite3.connect(folder / "graph.sqlite")
    try:
        db.executescript(
            "CREATE TABLE edges(id TEXT PRIMARY KEY,payload TEXT);"
            "CREATE TABLE nodes(id INTEGER PRIMARY KEY,payload TEXT);"
        )
        db.executemany(
            "INSERT INTO edges VALUES(?,?)",
            ((e.id, json.dumps(asdict(e), ensure_ascii=False)) for e in graph.edges.values()),
        )
        db.executemany(
            "INSERT INTO nodes VALUES(?,?)",
            ((k, json.dumps(v, ensure_ascii=False)) for k, v in graph.nodes.items()),
        )
        db.commit()
    finally:
        db.close()
    write_json(
        folder / "graph.json",
        {
            "version": graph.version,
            "crs": graph.crs,
            "edges": len(graph.edges),
            "nodes": len(graph.nodes),
            "restrictions": graph.restrictions,
            "missing_ways": graph.missing_ways,
        },
    )


def load_graph(folder: Path) -> Graph:
    meta = read_json(folder / "graph.json")
    manifest = read_json(folder / "manifest.json")
    if not validate_cache(folder, manifest["dependencies"]):
        raise ValueError("Graph cache hash/schema invalid")
    with closing(sqlite3.connect(folder / "graph.sqlite")) as db:
        edges = [
            DirectedEdge(**json.loads(r[0]))
            for r in db.execute("SELECT payload FROM edges ORDER BY id")
        ]
        nodes = {r[0]: json.loads(r[1]) for r in db.execute("SELECT * FROM nodes")}
    graph = Graph(meta["version"], edges, nodes, meta["crs"], meta["restrictions"])
    graph.artifact_root = folder.resolve()
    graph.missing_ways = meta.get("missing_ways", 0)
    return graph


def build_reference(pbf_path: Path, config) -> Path:
    deps = {
        "extract_sha256": sha256(pbf_path),
        "crs": config.study.metric_crs,
        "graph": "directed_junction_v1",
        "mapper": MAPPER_VERSION,
        "policy": config.policy.version,
    }
    folder = config.output("cache/graph/" + digest(deps)[:20])
    folder.parent.mkdir(parents=True, exist_ok=True)
    with portalocker.Lock(str(folder) + ".lock", timeout=30):
        if validate_cache(folder, deps):
            return folder
        if folder.exists():
            quarantine = folder.with_name(
                folder.name + ".invalid." + __import__("uuid").uuid4().hex[:8]
            )
            folder.rename(quarantine)
        folder.mkdir()
        import_extract(pbf_path, folder / "normalized.sqlite")
        region = project(box(*config.study.bbox), config.study.metric_crs).buffer(
            config.study.graph_buffer_m
        )
        graph = graph_from_normalized(
            folder / "normalized.sqlite", digest(deps)[:20], config.study.metric_crs, region
        )
        save_graph(graph, folder)
        seal_cache(folder, deps, ["graph.sqlite", "graph.json", "normalized.sqlite"])
    return folder
