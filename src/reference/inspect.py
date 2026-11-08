from pathlib import Path
import osmium
from ..domain.reference import ReferenceManifest
from ..storage.files import sha256


def inspect_reference(pbf_path: Path, config=None) -> ReferenceManifest:
    """Hash source bytes and read source metadata; mtime is never source time."""
    checksum = sha256(pbf_path)
    with osmium.io.Reader(str(pbf_path), osmium.osm.NOTHING) as reader:
        header = reader.header()
        extent = None
        if header.box().valid():
            bounds = header.box()
            extent = [
                bounds.bottom_left.lon,
                bounds.bottom_left.lat,
                bounds.top_right.lon,
                bounds.top_right.lat,
            ]
        timestamp = header.get("osmosis_replication_timestamp") or None
    return ReferenceManifest(
        reference_id=checksum[:20],
        source_sha256=checksum,
        original_path=str(pbf_path),
        source_timestamp=timestamp,
        spatial_extent=extent,
    )
