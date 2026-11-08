"""Explicit inventory selection, hash validation and network denial."""

from contextlib import contextmanager
from pathlib import Path, PureWindowsPath
import socket
from ..storage.files import read_json, sha256
from ..domain.capture import CapturePlan, SnapshotManifest


@contextmanager
def network_off():
    original = socket.socket.connect
    original_ex = socket.socket.connect_ex

    def denied(*args, **kwargs):
        raise RuntimeError("NETWORK_DISABLED_FOR_OFFLINE_PROCESSING")

    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    try:
        yield
    finally:
        socket.socket.connect = original
        socket.socket.connect_ex = original_ex


def body_path(folder: Path, relative: str) -> Path:
    """Normalize Windows separators without allowing traversal or drive escapes."""
    if PureWindowsPath(relative).is_absolute() or PureWindowsPath(relative).drive:
        raise ValueError("Body path must be snapshot-relative")
    path = (folder / relative.replace("\\", "/")).resolve()
    if not path.is_relative_to(folder.resolve()):
        raise ValueError("Body traversal outside snapshot")
    return path


def load_inventory(path: Path):
    inventory = read_json(path)
    snapshots = []
    quarantined = []
    hashes = {str(path): sha256(path)}
    for record in inventory["snapshots"]:
        if not record.get("snapshot"):
            continue
        folder = Path(record["snapshot"]).resolve()
        hashes[str(folder / "manifest.json")] = sha256(folder / "manifest.json")
        hashes[str(folder / "plan.json")] = sha256(folder / "plan.json")
        manifest = SnapshotManifest.model_validate(read_json(folder / "manifest.json"))
        if manifest.status == "collecting":
            raise ValueError("Replay requires finalized snapshot")
        raw_plan = read_json(folder / "plan.json")
        plan = CapturePlan.model_validate(raw_plan)
        from ..storage.files import digest

        # Validate the exact finalized schema/document, not newly supplied model defaults.
        if digest(raw_plan) != manifest.plan_sha256:
            raise ValueError("Plan hash mismatch")
        selected = {}
        for attempt in manifest.attempts:
            if not attempt.body_path:
                continue
            body = body_path(folder, attempt.body_path)
            if not body.is_file() or sha256(body) != attempt.body_sha256:
                quarantined.append(
                    {
                        "snapshot": manifest.snapshot_id,
                        "resource": attempt.resource_id,
                        "reason": "RAW_HASH_MISMATCH",
                        "path": str(body),
                    }
                )
                continue
            hashes[str(body)] = attempt.body_sha256
            if attempt.outcome == "success":
                previous = selected.get(attempt.resource_id)
                if previous is None or attempt.attempt_number > previous[0].attempt_number:
                    selected[attempt.resource_id] = (attempt, body)
        snapshots.append((folder, manifest, plan, selected))
    return inventory, snapshots, quarantined, hashes
