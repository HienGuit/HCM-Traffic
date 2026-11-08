"""Deterministic encoding and durable per-file writes."""

import csv
import hashlib
import json
import os
import tempfile
from pathlib import Path


def canonical(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def atomic_bytes(path: Path, body: bytes, *, immutable: bool = False) -> str:
    """Write stored bytes atomically; immutable artifacts cannot be overwritten."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if immutable and path.exists():
        raise FileExistsError(path)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".writing-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        if immutable:
            # Hardlink creates the final name exclusively, including across processes.
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return hashlib.sha256(body).hexdigest()


def write_json(path: Path, value, *, immutable: bool = False) -> str:
    return atomic_bytes(path, canonical(value), immutable=immutable)


def write_jsonl(path: Path, rows) -> str:
    return atomic_bytes(path, b"".join(canonical(r) + b"\n" for r in rows))


def write_csv(path: Path, rows: list[dict], fields: list[str] | None = None) -> None:
    """CSV UTF-8 BOM; null blank, bool lowercase, structured fields JSON arrays."""
    import io

    out = io.StringIO(newline="")
    keys = fields or list(rows[0] if rows else {})
    writer = csv.DictWriter(out, fieldnames=keys, extrasaction="raise")
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                k: ""
                if v is None
                else str(v).lower()
                if isinstance(v, bool)
                else canonical(v).decode()
                if isinstance(v, (dict, list))
                else v
                for k, v in row.items()
            }
        )
    atomic_bytes(path, out.getvalue().encode("utf-8-sig"))
