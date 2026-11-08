"""Read-only support attribution on an existing run; no matching or network calls.

Raw proximity is an explicitly labelled diagnostic upper bound, not road assignment.
Each atom loses support at its first failing stage; secondary reasons are retained.
"""

import json
import html
from pathlib import Path
from shapely.geometry import LineString, box, mapping
from shapely.ops import unary_union, substring
from ..geo.crs import project
from ..geo.linear import path_geometry
from ..geo.support import union, length, intersection, contains, atomic
from ..geo.tiles import tile_lonlat
from ..storage.files import write_json, write_csv, atomic_bytes
from .inputs import load_inventory
from .normalize import normalize_snapshot


def jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def project_intersection(line, geometry):
    cut = line.intersection(geometry)
    parts = list(cut.geoms) if hasattr(cut, "geoms") else [cut]
    return union(
        [
            (
                min(
                    line.project(__import__("shapely").geometry.Point(p.coords[0])),
                    line.project(__import__("shapely").geometry.Point(p.coords[-1])),
                ),
                max(
                    line.project(__import__("shapely").geometry.Point(p.coords[0])),
                    line.project(__import__("shapely").geometry.Point(p.coords[-1])),
                ),
            )
            for p in parts
            if p.geom_type == "LineString" and p.length > 1e-8
        ]
    )


def path_support(path, edge_pieces):
    output = []
    offset = 0
    for entry in path.edges:
        for edge_id, start, end in edge_pieces:
            if edge_id == entry.edge_id:
                a, b = max(start, entry.start_m), min(end, entry.end_m)
                if b > a:
                    output.append((offset + a - entry.start_m, offset + b - entry.start_m))
        offset += entry.end_m - entry.start_m
    return union(output)


def location_support(location, path, graph):
    pieces = []
    offset = 0
    for edge_id in location.get("edges", []):
        edge = graph.edges[edge_id]
        start = max(0, location["start_m"] - offset)
        end = min(edge.length, location["end_m"] - offset)
        if end > start:
            pieces.append((edge_id, start, end))
        offset += edge.length
    return path_support(path, pieces)


STAGES = [
    "capture_tiles",
    "raw_geometry_search_upper_bound",
    "openlr_available",
    "openlr_evaluated",
    "openlr_decoded",
    "decoded_directed_path",
    "footprint_projection",
    "overlap_resolved_geometry",
    "source_direction_accepted",
    "target_valid",
]
REASONS = [
    "MISSING_CAPTURE",
    "NO_GEOMETRY_WITHIN_DIAGNOSTIC_SEARCH",
    "OPENLR_MISSING",
    "NOT_EVALUATED",
    "DECODE_FAILED",
    "NO_DIRECTED_PATH_CORRESPONDENCE",
    "FOOTPRINT_REJECTED_OR_UNCERTAIN",
    "OVERLAP_UNRESOLVED",
    "SOURCE_DIRECTION_EVIDENCE_MISSING",
    "TARGET_GATE_FAILED",
]


def diagnose_run(
    run: Path, paths, graph, config, destination: Path, inventory: Path, projection_cases=False
) -> dict:
    destination.mkdir(parents=True, exist_ok=False)
    _, snapshots, quarantine, _ = load_inventory(inventory)
    locations = {
        d["code"]: d
        for d in jsonl(run / "audit/decoded_locations.jsonl")
        if d["profile"] == "baseline"
    }
    footprints = {
        (d.get("segment_id"), d["occurrence_id"]): d
        for d in jsonl(run / "audit/feature_footprints.jsonl")
    }
    intervals = jsonl(run / "audit/traffic_intervals.jsonl")
    metrics = []
    losses = []
    detailed = []
    map_features = []
    occurrence_rows = []
    case_rows = []
    for _, snapshot, plan, selected in snapshots:
        features, _, receipts, errors = normalize_snapshot(snapshot, plan, selected)
        resources = {r.resource_id: r for r in plan.resources}
        for path in paths:
            line = path_geometry(graph, path.edges)
            stages = {s: [] for s in STAGES}
            relevant = {
                r.resource_id
                for r in plan.resources
                if path.segment_id in r.segments and r.provider == "traffic"
            }
            boxes = []
            for resource_id in relevant:
                if resource_id not in selected:
                    continue
                resource = resources[resource_id]
                z, x, y = resource.tile
                w, n = tile_lonlat(x, y, z)
                e, s = tile_lonlat(x + 1, y + 1, z)
                boxes.append(project(box(w, s, e, n), graph.crs))
            stages["capture_tiles"] = (
                project_intersection(line, unary_union(boxes)) if boxes else []
            )
            map_features.append(
                {
                    "type": "Feature",
                    "geometry": path.geometry,
                    "properties": {
                        "layer": "registry",
                        "snapshot": snapshot.snapshot_id,
                        "segment_id": path.segment_id,
                    },
                }
            )
            for feature in features:
                if feature.resource_id not in relevant:
                    continue
                raw_geometries = [project(LineString(c), graph.crs) for c in feature.components]
                raw_support = union(
                    [
                        pair
                        for geometry in raw_geometries
                        for pair in project_intersection(
                            line, geometry.buffer(config.policy.diagnostic_search_m)
                        )
                    ]
                )
                if not raw_support:
                    continue
                location = locations.get(feature.openlr)
                footprint = footprints.get(
                    (path.segment_id, feature.occurrence_id),
                    footprints.get((None, feature.occurrence_id)),
                )
                if (
                    projection_cases
                    and footprint
                    and footprint["status"] == "ambiguous"
                    and location
                    and location["status"] == "decoded"
                ):
                    from ..matching.footprint import associate_footprint
                    from ..domain.processing import DecodedLocation

                    reproduced = associate_footprint(
                        feature, DecodedLocation.model_validate(location), graph, config.policy
                    )
                    case_rows.append(
                        {
                            "snapshot_id": snapshot.snapshot_id,
                            "segment_id": path.segment_id,
                            "occurrence": feature.model_dump(mode="json"),
                            "location": location,
                            "reproduced_footprint": reproduced.model_dump(mode="json"),
                        }
                    )
                stages["raw_geometry_search_upper_bound"].extend(raw_support)
                if feature.openlr:
                    stages["openlr_available"].extend(raw_support)
                if location:
                    stages["openlr_evaluated"].extend(raw_support)
                if location and location["status"] == "decoded":
                    stages["openlr_decoded"].extend(raw_support)
                    support = location_support(location, path, graph)
                    stages["decoded_directed_path"].extend(support)
                    for edge_id in location["edges"]:
                        map_features.append(
                            {
                                "type": "Feature",
                                "geometry": mapping(LineString(graph.edges[edge_id].coords)),
                                "properties": {
                                    "layer": "decoded",
                                    "snapshot": snapshot.snapshot_id,
                                    "occurrence_id": feature.occurrence_id,
                                    "openlr": feature.openlr,
                                },
                            }
                        )
                if footprint and footprint["status"] in ("matched", "provisional"):
                    support = path_support(
                        path,
                        [
                            (p["edge_id"], p["edge_start_m"], p["edge_end_m"])
                            for p in footprint["pieces"]
                        ],
                    )
                    stages["footprint_projection"].extend(support)
                    for a, b in support:
                        map_features.append(
                            {
                                "type": "Feature",
                                "geometry": mapping(
                                    project(substring(line, a, b), "EPSG:4326", graph.crs)
                                ),
                                "properties": {
                                    "layer": "footprint",
                                    "snapshot": snapshot.snapshot_id,
                                    "occurrence_id": feature.occurrence_id,
                                },
                            }
                        )
                occurrence_rows.append(
                    {
                        "snapshot_id": snapshot.snapshot_id,
                        "segment_id": path.segment_id,
                        "occurrence_id": feature.occurrence_id,
                        "resource_id": feature.resource_id,
                        "tile": resources[feature.resource_id].tile,
                        "openlr": feature.openlr,
                        "relative_speed": feature.relative_speed,
                        "absolute_speed": feature.absolute_speed,
                        "decode_status": location["status"] if location else "not_evaluated",
                        "footprint_status": footprint["status"] if footprint else "missing",
                        "raw_search_support_m": length(raw_support),
                    }
                )
                for geometry in feature.components:
                    map_features.append(
                        {
                            "type": "Feature",
                            "geometry": {"type": "LineString", "coordinates": geometry},
                            "properties": {
                                "layer": "raw",
                                "snapshot": snapshot.snapshot_id,
                                "occurrence_id": feature.occurrence_id,
                                "resource": feature.resource_id,
                                "openlr": feature.openlr,
                            },
                        }
                    )
            selected_intervals = [
                i
                for i in intervals
                if i["segment_id"] == path.segment_id and i["snapshot_id"] == snapshot.snapshot_id
            ]
            for item in selected_intervals:
                a, b = item["start_m"], item["end_m"]
                if item["status"] not in ("gap", "conflict"):
                    stages["overlap_resolved_geometry"].append((a, b))
                if item["status"] == "accepted" and item["source_direction"] == "supported":
                    stages["source_direction_accepted"].append((a, b))
                if item.get("target_valid", False):
                    stages["target_valid"].append((a, b))
                ids = item["contributors"]
                lookup = {f.occurrence_id: f for f in features}
                detailed.append(
                    {
                        "snapshot_id": snapshot.snapshot_id,
                        "segment_id": path.segment_id,
                        "start_m": a,
                        "end_m": b,
                        "length_m": b - a,
                        "status": item["status"],
                        "occurrence_ids": ids,
                        "tile_ids": [resources[lookup[k].resource_id].tile for k in ids],
                        "openlr_ids": [lookup[k].openlr for k in ids],
                        "absolute_speeds": [lookup[k].absolute_speed for k in ids],
                        "relative_speeds": [lookup[k].relative_speed for k in ids],
                        "direction_status": item["source_direction"],
                        "overlap_class": item.get("overlap_class"),
                        "reasons": item.get("reasons", []),
                    }
                )
                map_features.append(
                    {
                        "type": "Feature",
                        "geometry": mapping(project(substring(line, a, b), "EPSG:4326", graph.crs)),
                        "properties": {
                            "layer": "accepted" if item.get("target_valid") else "rejected",
                            "snapshot": snapshot.snapshot_id,
                            "start_m": a,
                            "end_m": b,
                            "status": item["status"],
                            "reasons": item.get("reasons", []),
                        },
                    }
                )
            for name in STAGES:
                stages[name] = union(stages[name])
            cumulative = [(0, path.length_m)]
            for name in STAGES:
                previous = length(cumulative)
                cumulative = intersection(cumulative, stages[name])
                retained = length(cumulative)
                metrics.append(
                    {
                        "snapshot_id": snapshot.snapshot_id,
                        "segment_id": path.segment_id,
                        "stage": name,
                        "whole_path_m": path.length_m,
                        "native_support_m": length(stages[name]),
                        "cumulative_support_m": retained,
                        "lost_at_stage_m": previous - retained,
                        "diagnostic_only": name == "raw_geometry_search_upper_bound",
                    }
                )
            for a, b in atomic(list(stages.values()), path.length_m):
                failed = [n for n in STAGES if not contains(stages[n], a, b)]
                reason = REASONS[STAGES.index(failed[0])] if failed else "TARGET_ACCEPTED"
                losses.append(
                    {
                        "snapshot_id": snapshot.snapshot_id,
                        "segment_id": path.segment_id,
                        "start_m": a,
                        "end_m": b,
                        "length_m": b - a,
                        "primary_reason": reason,
                        "secondary_reasons": [REASONS[STAGES.index(n)] for n in failed[1:]],
                    }
                )
    write_csv(destination / "support_stages.csv", metrics)
    write_csv(destination / "loss_partition.csv", losses)
    write_csv(destination / "intervals.csv", detailed)
    write_csv(destination / "occurrences.csv", occurrence_rows)
    from ..storage.files import write_jsonl

    write_jsonl(destination / "projection_cases.jsonl", case_rows)
    write_json(
        destination / "layers.geojson", {"type": "FeatureCollection", "features": map_features}
    )
    write_json(
        destination / "manifest.json",
        {
            "source_run": str(run),
            "denominator": "whole registry directed metric path",
            "raw_search_radius_m": config.policy.diagnostic_search_m,
            "raw_search_is_upper_bound_not_association": True,
            "primary_loss_partition": "first failing stage, no double count",
            "quarantine": quarantine,
            "stages": STAGES,
        },
    )
    render_map(destination / "map.html", map_features, paths[0].geometry)
    return {
        "path": str(destination),
        "stage_rows": len(metrics),
        "intervals": len(detailed),
        "occurrences": len(occurrence_rows),
    }


def render_map(path, features, registry_geometry):
    bounds = LineString(registry_geometry["coordinates"]).bounds
    w, s, e, n = bounds
    dx = max(e - w, 0.003)
    dy = max(n - s, 0.003)
    w -= dx * 0.2
    e += dx * 0.2
    s -= dy * 0.2
    n += dy * 0.2
    colors = {
        "registry": "black",
        "raw": "#999",
        "decoded": "#2274aa",
        "footprint": "#df9400",
        "accepted": "#15803d",
        "rejected": "#dc2626",
    }
    groups = []
    for feature in features:
        props = feature["properties"]
        points = feature["geometry"].get("coordinates", [])
        if feature["geometry"]["type"] != "LineString":
            continue
        coordinates = " ".join(
            f"{(x - w) / (e - w) * 1200:.2f},{(n - y) / (n - s) * 850:.2f}" for x, y in points
        )
        groups.append(
            f'<polyline data-layer="{props["layer"]}" data-snapshot="{props["snapshot"]}" points="{coordinates}" fill="none" stroke="{colors[props["layer"]]}" stroke-width="{5 if props["layer"] in ("registry", "accepted", "rejected") else 2}"><title>{html.escape(json.dumps(props, ensure_ascii=False))}</title></polyline>'
        )
    snapshots = sorted({f["properties"]["snapshot"] for f in features})
    controls = "".join(
        f'<label><input type="checkbox" value="{k}" checked onchange="draw()">{k}</label> '
        for k in colors
    )
    body = (
        '<html><meta charset="utf-8"><title>Support attribution</title><h1>Registry / raw / decoded / footprint / accepted–rejected</h1><p>Raw proximity is diagnostic only. Hover lines for provenance.</p><select id="snapshot" onchange="draw()">'
        + "".join(f"<option>{s}</option>" for s in snapshots)
        + "</select><div>"
        + controls
        + '</div><svg viewBox="0 0 1200 850">'
        + "".join(groups)
        + '</svg><script>function draw(){const s=document.getElementById("snapshot").value;const layers=[...document.querySelectorAll("input:checked")].map(x=>x.value);document.querySelectorAll("polyline").forEach(x=>x.style.display=x.dataset.snapshot===s&&layers.includes(x.dataset.layer)?"":"none")}draw()</script></html>'
    )
    atomic_bytes(path, body.encode("utf-8"))
