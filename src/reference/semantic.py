"""Streaming graph equivalence, including original OSM provenance."""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from ..storage.files import canonical, read_json
from ..storage.files import digest
from ..storage.cache import validate_cache

SEMANTIC_METHOD = "original_osm_directed_semantics_v1"


def semantic_digest(folder: Path) -> str:
    h = hashlib.sha256()

    def add(kind, value):
        h.update(canonical([kind, value]) + b"\n")

    meta = read_json(folder / "graph.json")
    add("method", SEMANTIC_METHOD)
    add("metadata", {k: meta[k] for k in ("crs", "nodes", "edges", "restrictions", "missing_ways")})
    with closing(
        sqlite3.connect((folder / "normalized.sqlite").resolve().as_uri() + "?mode=ro", uri=True)
    ) as db:
        for r in db.execute("SELECT id,lon,lat,tags FROM nodes ORDER BY id"):
            add("original_node", [r[0], r[1], r[2], json.loads(r[3])])
        for r in db.execute("SELECT id,nodes,tags FROM ways ORDER BY id"):
            add("original_way", [r[0], json.loads(r[1]), json.loads(r[2])])
        for r in db.execute("SELECT id,members,tags FROM relations ORDER BY id"):
            add("original_relation", [r[0], json.loads(r[1]), json.loads(r[2])])
    with closing(
        sqlite3.connect((folder / "graph.sqlite").resolve().as_uri() + "?mode=ro", uri=True)
    ) as db:
        if (
            db.execute("SELECT count(*) FROM nodes").fetchone()[0] != meta["nodes"]
            or db.execute("SELECT count(*) FROM edges").fetchone()[0] != meta["edges"]
        ):
            raise ValueError("GRAPH_COUNTS_MISMATCH")
        for r in db.execute("SELECT id,payload FROM nodes ORDER BY id"):
            add("graph_node", [r[0], json.loads(r[1])])
        for r in db.execute("SELECT payload FROM edges ORDER BY substr(id,instr(id,':')+1)"):
            payload = json.loads(r[0])
            expected = f"{meta['version']}:{payload['way_id']}:{payload['first_index']}:{payload['last_index']}:{payload['traversal']}"
            if payload["id"] != expected:
                raise ValueError("GRAPH_EDGE_NAMESPACE_MISMATCH")
            payload.pop("id")
            add("directed_edge", payload)
    return h.hexdigest()


def checked_semantics(folder):
    manifest = read_json(folder / "manifest.json")
    if not validate_cache(folder, manifest["dependencies"]):
        raise ValueError("GRAPH_ARTIFACT_HASH_MISMATCH")
    meta = read_json(folder / "graph.json")
    if manifest["dependencies"].get("contract") == "reference_graph_v2":
        if meta["version"] != digest(manifest["dependencies"])[:20]:
            raise ValueError("GRAPH_DEPENDENCY_VERSION_MISMATCH")
    actual = semantic_digest(folder)
    if meta.get("semantic_sha256") is not None and meta["semantic_sha256"] != actual:
        raise ValueError("GRAPH_SEMANTIC_HASH_MISMATCH")
    return meta, actual
