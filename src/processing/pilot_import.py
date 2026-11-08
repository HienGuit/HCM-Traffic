"""Read-only normalized inventory adapter; historical inputs are never auto-discovered."""

from pathlib import Path
from .inputs import load_inventory


def import_pilot(inventory: Path):
    result = load_inventory(inventory)
    if result[0].get("collection_kind") != "pilot":
        raise ValueError("Explicit pilot inventory required")
    return result
