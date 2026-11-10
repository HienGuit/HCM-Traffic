"""Immutable per-attempt bodies, stored-byte hashes and finalized snapshots."""

from ..storage.files import atomic_bytes, write_json
from .contract import plan_document


def store_attempt(folder, resource, reservation, body, headers):
    relative = f"attempts/{resource.resource_id}/{reservation}.body"
    checksum = atomic_bytes(folder / relative, body, immutable=True)
    write_json(
        folder / f"attempts/{resource.resource_id}/{reservation}.metadata.json",
        {
            "headers": headers,
            "decompression": "httpx_decoded_content",
            "hash_scope": "stored_bytes",
            "bytes": len(body),
            "sha256": checksum,
        },
        immutable=True,
    )
    return relative, checksum


def finalize_snapshot(folder, manifest, plan):
    write_json(folder / "plan.json", plan_document(plan), immutable=True)
    write_json(folder / "manifest.json", manifest.model_dump(mode="json"), immutable=True)
