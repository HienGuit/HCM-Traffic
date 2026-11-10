"""Read historical artifacts through an explicit migration map, never rewrite raw."""

from pathlib import Path
from .files import read_json


def resolve_artifact(path, mapping=None):
    path = Path(path)
    if mapping is None or not Path(mapping).exists():
        return path
    moves = read_json(Path(mapping)).get("moves", [])
    for move in sorted(moves, key=lambda m: len(m["old"]), reverse=True):
        old = Path(move["old"])
        try:
            tail = path.relative_to(old)
        except ValueError:
            continue
        destination = Path(move["new"]) / tail
        if destination.exists():
            return destination
    return path
