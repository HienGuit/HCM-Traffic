"""One main parse of regional extract into normalized, auditable SQLite tables."""

import json
import sqlite3
from pathlib import Path
import osmium


def import_extract(pbf_path: Path, database: Path) -> dict:
    db = sqlite3.connect(database)
    counts = {"nodes": 0, "ways": 0, "relations": 0}
    try:
        db.executescript("""CREATE TABLE nodes(id INTEGER PRIMARY KEY,lon REAL,lat REAL,tags TEXT);
        CREATE TABLE ways(id INTEGER PRIMARY KEY,nodes TEXT,tags TEXT);
        CREATE TABLE relations(id INTEGER PRIMARY KEY,members TEXT,tags TEXT);""")

        class Import(osmium.SimpleHandler):
            def node(self, obj):
                db.execute(
                    "INSERT INTO nodes VALUES(?,?,?,?)",
                    (
                        obj.id,
                        obj.location.lon,
                        obj.location.lat,
                        json.dumps(dict(obj.tags), ensure_ascii=False),
                    ),
                )
                counts["nodes"] += 1

            def way(self, obj):
                db.execute(
                    "INSERT INTO ways VALUES(?,?,?)",
                    (
                        obj.id,
                        json.dumps([n.ref for n in obj.nodes]),
                        json.dumps(dict(obj.tags), ensure_ascii=False),
                    ),
                )
                counts["ways"] += 1

            def relation(self, obj):
                db.execute(
                    "INSERT INTO relations VALUES(?,?,?)",
                    (
                        obj.id,
                        json.dumps([(m.type, m.ref, m.role) for m in obj.members]),
                        json.dumps(dict(obj.tags), ensure_ascii=False),
                    ),
                )
                counts["relations"] += 1

        Import().apply_file(str(pbf_path))
        db.commit()
    finally:
        db.close()
    return counts
