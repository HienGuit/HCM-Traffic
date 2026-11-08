"""Content-addressed caches validate artifact hashes, never existence alone."""

from pathlib import Path
from .files import read_json, sha256, write_json


def validate_cache(folder: Path, dependencies: dict) -> bool:
    try:
        manifest = read_json(folder / "manifest.json")
        return (
            manifest["schema"] == 1
            and manifest["dependencies"] == dependencies
            and manifest["complete"] is True
            and bool(manifest["files"])
            and all(
                sha256(folder / name) == checksum for name, checksum in manifest["files"].items()
            )
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def seal_cache(folder: Path, dependencies: dict, files: list[str]) -> None:
    write_json(
        folder / "manifest.json",
        {
            "schema": 1,
            "complete": True,
            "dependencies": dependencies,
            "files": {f: sha256(folder / f) for f in files},
        },
    )
