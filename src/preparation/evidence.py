"""Review records must have real identities and scope; maps never assert approval."""

import html
import json
from ..domain.reference import ReviewEvidence
from ..storage.files import atomic_bytes


def validate_evidence(records):
    evidence = [ReviewEvidence.model_validate(r) for r in records]
    for item in evidence:
        if not item.reviewer.strip() or not item.spatial_scope or not item.direction_scope:
            raise ValueError("Review identity and scope required")
        if item.effective_to and item.effective_to < item.effective_from:
            raise ValueError("Invalid evidence validity")
    return evidence


def review_map(destination, paths, config):
    """Self-contained SVG/HTML extent and candidate map, without a basemap service."""
    from shapely.geometry import box, mapping
    from ..geo.crs import project

    bbox = config.study.bbox
    w, s, e, n = bbox
    buffer = config.study.graph_buffer_m
    buffered = project(
        project(box(*bbox), config.study.metric_crs).buffer(buffer),
        "EPSG:4326",
        config.study.metric_crs,
    )
    west, south, east, north = buffered.bounds

    def point(x, y):
        return f"{(x - west) / (east - west) * 1100:.2f},{(north - y) / (north - south) * 850:.2f}"

    outlines = [
        f'<polygon points="{" ".join(point(x, y) for x, y in buffered.exterior.coords)}" fill="#eef3f8" stroke="#888"/>',
        f'<polygon points="{" ".join(point(x, y) for x, y in box(*bbox).exterior.coords)}" fill="none" stroke="#222" stroke-dasharray="5 5"/>',
    ]
    for path in paths:
        coords = path.geometry["coordinates"]
        outlines.append(
            f'<polyline points="{" ".join(point(x, y) for x, y in coords)}" fill="none" stroke="#c14424" stroke-width="3"><title>{html.escape(path.road_name + " | " + path.segment_id + " | unreviewed")}</title></polyline>'
        )
    metadata = json.dumps(
        {"bbox": bbox, "buffer_geometry": mapping(buffered), "review": "pending"},
        ensure_ascii=False,
    )
    body = (
        '<html lang="vi"><meta charset="utf-8"><title>Candidate review</title><h1>Phạm vi nghiên cứu và shortlist chờ duyệt</h1><p>Bbox nghiên cứu; không phải địa giới hành chính. Graph buffer 3 km. Không có candidate approved.</p><svg viewBox="0 0 1100 850">'
        + "".join(outlines)
        + "</svg><pre>"
        + html.escape(metadata)
        + "</pre></html>"
    )
    atomic_bytes(destination, body.encode("utf-8"))
