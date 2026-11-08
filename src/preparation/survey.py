"""Bounded four-road candidate survey using the existing graph traversal generator."""

from pathlib import Path
from datetime import datetime, timezone
import math
import html
from shapely.geometry import box, mapping
from ..storage.files import read_json, write_json, write_csv, sha256, digest
from ..storage.paths import safe_output
from ..geo.crs import project
from ..geo.linear import path_geometry
from ..domain.reference import PathEdge
from ..reference.static import static_features
from ..provenance import source_fingerprint
from .candidates import generate_candidates
from .filters import CandidateFilter, road_key


def trim_path(path, graph, low, high):
    entries = []
    offset = 0.0
    for entry in path.edges:
        length = entry.end_m - entry.start_m
        a, b = max(0, low - offset), min(length, high - offset)
        if b > a + 1e-8:
            entries.append(
                PathEdge(edge_id=entry.edge_id, start_m=entry.start_m + a, end_m=entry.start_m + b)
            )
        offset += length
    line = path_geometry(graph, entries)
    return path.model_copy(
        update={
            "edges": entries,
            "length_m": line.length,
            "geometry": mapping(project(line, "EPSG:4326", graph.crs)),
            "geometry_sha256": None,
        }
    )


def physical_key(path, graph):
    pieces = []
    for p in path.edges:
        edge = graph.edges[p.edge_id]
        a, b = (
            (p.start_m, p.end_m)
            if edge.traversal == 1
            else (edge.length - p.end_m, edge.length - p.start_m)
        )
        pieces.append([edge.way_id, edge.first_index, edge.last_index, round(a, 3), round(b, 3)])
    return min(pieces, list(reversed(pieces)))


def road_axis(edges, graph):
    points = [p for e in edges for p in graph.lines[e.id].coords]
    x = sum(p[0] for p in points) / len(points)
    y = sum(p[1] for p in points) / len(points)
    xx = sum((p[0] - x) ** 2 for p in points)
    yy = sum((p[1] - y) ** 2 for p in points)
    xy = sum((p[0] - x) * (p[1] - y) for p in points)
    angle = 0.5 * math.atan2(2 * xy, xx - yy)
    return {
        "origin": [x, y],
        "unit": [math.cos(angle), math.sin(angle)],
        "method": "longitudinal_projection_heuristic_v1",
    }


def stations(line, axis):
    x, y = axis["origin"]
    u, v = axis["unit"]
    values = [(a - x) * u + (b - y) * v for a, b in line.coords]
    sign = 1 if values[-1] >= values[0] else -1
    monotone = all(sign * (b - a) >= -1e-6 for a, b in zip(values, values[1:]))
    return min(values), max(values), sign, monotone


def station_trim(path, graph, axis, low, high):
    line = path_geometry(graph, path.edges)
    coords = list(line.coords)
    x, y = axis["origin"]
    u, v = axis["unit"]
    positions = []
    offset = 0.0
    for a, b in zip(coords, coords[1:]):
        sa = (a[0] - x) * u + (a[1] - y) * v
        sb = (b[0] - x) * u + (b[1] - y) * v
        length = math.dist(a, b)
        if abs(sb - sa) > 1e-9:
            ts = sorted(((low - sa) / (sb - sa), (high - sa) / (sb - sa)))
            ta, tb = max(0, ts[0]), min(1, ts[1])
            if tb > ta:
                positions.extend([offset + ta * length, offset + tb * length])
        offset += length
    return trim_path(path, graph, min(positions), max(positions)) if positions else None


def assess(path, graph, config, filtering):
    features = static_features(path, graph, config.policy)
    reasons = filtering.path_review_reasons(path)
    if not path_geometry(graph, path.edges).is_simple:
        reasons.append("PATH_SELF_INTERSECTION")
    if (
        not config.policy.candidate_exception_min_m
        <= path.length_m
        <= config.policy.candidate_exception_max_m
    ):
        reasons.append("LENGTH_OUT_OF_POLICY")
    if not features["reference_complete"]:
        reasons.append("REFERENCE_INCOMPLETE")
    if features["lanes_mean"] is None:
        reasons.append("DIRECTIONAL_LANES_MISSING")
    if features["name_coverage"] < config.policy.name_coverage:
        reasons.append("NAME_COVERAGE_LOW")
    if any(graph.edges[e.edge_id].legal != "supported" for e in path.edges):
        reasons.append("OSM_DIRECTION_OR_TURN_RULE_UNRESOLVED")
    return features, sorted(set(reasons))


def continuous_runs(intervals, graph):
    fragments = {(edge, a, b) for edge, parts in intervals.items() for a, b in parts}

    def next_parts(item):
        edge, a, b = item
        e = graph.edges[edge]
        if b < e.length - 1e-6:
            return []
        return [
            (k, c, d)
            for k in graph.outgoing[e.end]
            for c, d in intervals.get(k, [])
            if c <= 1e-6 and graph.edges[k].end != e.start
        ]

    incoming = {k: 0 for k in fragments}
    for item in fragments:
        for nxt in next_parts(item):
            incoming[nxt] += 1
    visited = set()
    runs = []
    for first in sorted(fragments, key=lambda k: (incoming[k] == 1, k)):
        if first in visited:
            continue
        run = []
        current = first
        while current not in visited:
            visited.add(current)
            run.append(current)
            nxt = next_parts(current)
            if len(nxt) != 1 or incoming[nxt[0]] != 1:
                break
            current = nxt[0]
        entries = [PathEdge(edge_id=k, start_m=a, end_m=b) for k, a, b in run]
        line = path_geometry(graph, entries)
        runs.append(
            {
                "length_m": line.length,
                "edges": [e.model_dump(mode="json") for e in entries],
                "geometry": mapping(project(line, "EPSG:4326", graph.crs)),
            }
        )
    return runs


def survey_candidates(graph, config):
    if config.study.candidate_policy_file is None:
        raise ValueError("candidate_policy_file is required")
    policy = read_json(config.study.candidate_policy_file)
    repo = config.study.project_root
    targets = read_json(repo / policy["targets_file"])
    exclusions = read_json(repo / policy["exclusions_file"])
    watchlist = read_json(repo / policy["watchlist_file"])
    if len(targets["roads"]) != 4 or len({r["road_id"] for r in targets["roads"]}) != 4:
        raise ValueError("Exactly four unambiguous target roads required")
    filtering = CandidateFilter(exclusions["features"], graph)
    rows = []
    drafts = []
    selected = []
    physical = []
    static = {}
    road_reports = []
    removed = []
    axes = {}
    reverse_index = {
        (e.way_id, e.first_index, e.last_index, e.traversal): e for e in graph.edges.values()
    }
    for road in targets["roads"]:
        region = project(box(*road["scope_bbox"]), graph.crs)
        edges = [
            e
            for e in graph.edges.values()
            if road_key(e.tags.get("name")) == road_key(road["osm_name"])
            and graph.lines[e.id].intersects(region)
            and e.tags.get("highway") in config.policy.candidate_highways
        ]
        if not edges:
            road_reports.append(
                {
                    "road_id": road["road_id"],
                    "road_name": road["osm_name"],
                    "physical_selected": 0,
                    "missing_to_min": policy["min_physical_per_road"],
                    "reason": "NO_MATCHING_LOCAL_PBF_ROAD",
                }
            )
            continue
        axis = road_axis(edges, graph)
        axes[road["road_id"]] = axis
        intervals = {}
        starts = []
        for edge in sorted(edges, key=lambda e: e.id):
            kept, loss = filtering.clipped_edge(edge, region)
            removed.extend(dict(r, road_id=road["road_id"]) for r in loss)
            if kept:
                intervals[edge.id] = kept
            for low, high in kept:
                starts.append((edge.id, low))
                step = low + config.policy.candidate_min_m
                while step < high - 1e-6:
                    starts.append((edge.id, step))
                    step += config.policy.candidate_min_m
        available_starts = len(starts)
        starts = starts[: config.policy.max_starts]
        candidates, stats = generate_candidates(
            graph,
            config,
            starts=starts,
            edge_intervals=intervals,
            allowed_ids=set(intervals),
            region=region.buffer(1e-6),
        )
        eligible = []
        for path in candidates:
            features, reasons = assess(path, graph, config, filtering)
            lo, hi, sign, monotone = stations(path_geometry(graph, path.edges), axis)
            if not monotone:
                reasons.append("PHYSICAL_STATION_MAPPING_UNRESOLVED")
            row = {
                "candidate_id": path.segment_id,
                "road_id": road["road_id"],
                "road_name": road["osm_name"],
                "length_m": path.length_m,
                "status": "rejected" if reasons else "eligible_draft",
                "reasons": reasons,
                "station_low_m": lo,
                "station_high_m": hi,
                "direction_axis_sign": sign,
                "selected": False,
                "length_exception": not config.policy.candidate_min_m
                <= path.length_m
                <= config.policy.candidate_max_m,
            }
            rows.append(row)
            drafts.append(path)
            if not reasons:
                eligible.append((path, features, row))
        eligible.sort(
            key=lambda item: (
                -item[1]["lanes_known_coverage"],
                abs(item[0].length_m - policy["preferred_length_m"]),
                item[0].segment_id,
            )
        )
        packed = []
        direction_count = 0
        for path, features, row in eligible:
            lo, hi = row["station_low_m"], row["station_high_m"]
            if any(min(hi, b) - max(lo, a) > policy["geometry_tolerance_m"] for a, b in packed):
                row["status"] = "not_selected"
                row["reasons"].append("PHYSICAL_CORRIDOR_OVERLAP")
                continue
            if len(packed) >= policy["max_physical_per_road"]:
                continue
            physical_id = (
                "P_" + digest([road["road_id"], graph.version, physical_key(path, graph)])[:16]
            )
            direction = "A_TO_B" if row["direction_axis_sign"] > 0 else "B_TO_A"
            chosen = path.model_copy(
                update={
                    "segment_id": physical_id,
                    "direction_id": direction,
                    "road_name": road["osm_name"],
                    "legal_direction_status": "supported",
                }
            )
            paths = [chosen]
            pair_reason = "one_directed_carriageway_selected"
            opposite = []
            for entry in reversed(path.edges):
                e = graph.edges[entry.edge_id]
                other = reverse_index.get((e.way_id, e.first_index, e.last_index, -e.traversal))
                if other is None or other.legal != "supported":
                    opposite = []
                    break
                opposite.append(
                    PathEdge(
                        edge_id=other.id,
                        start_m=max(0.0, e.length - entry.end_m),
                        end_m=e.length - entry.start_m,
                    )
                )
            if opposite:
                line = path_geometry(graph, opposite)
                other = chosen.model_copy(
                    update={
                        "edges": opposite,
                        "length_m": line.length,
                        "direction_id": "B_TO_A" if direction == "A_TO_B" else "A_TO_B",
                        "geometry": mapping(project(line, "EPSG:4326", graph.crs)),
                        "geometry_sha256": None,
                    }
                )
                other = type(other).model_validate(other.model_dump(mode="json"))
                _, reasons = assess(other, graph, config, filtering)
                if not reasons:
                    paths.append(other)
                    pair_reason = "exact_opposite_original_way_offsets"
            else:
                matches = {}
                for candidate, _, r in eligible:
                    if r["direction_axis_sign"] == row["direction_axis_sign"]:
                        continue
                    if r["station_low_m"] > lo + 1e-6 or r["station_high_m"] < hi - 1e-6:
                        continue
                    if (
                        path_geometry(graph, candidate.edges).hausdorff_distance(
                            path_geometry(graph, path.edges)
                        )
                        > policy["physical_pair_tolerance_m"]
                    ):
                        continue
                    other = station_trim(candidate, graph, axis, lo, hi)
                    if other is None:
                        continue
                    _, reasons = assess(other, graph, config, filtering)
                    if reasons:
                        continue
                    identity = digest(physical_key(other, graph))
                    matches[identity] = other
                if len(matches) == 1:
                    other = next(iter(matches.values())).model_copy(
                        update={
                            "segment_id": physical_id,
                            "direction_id": "B_TO_A" if direction == "A_TO_B" else "A_TO_B",
                            "road_name": road["osm_name"],
                        }
                    )
                    other = type(other).model_validate(other.model_dump(mode="json"))
                    paths.append(other)
                    pair_reason = "parallel_carriageway_same_station_extent_unique"
                elif len(matches) > 1:
                    pair_reason = "parallel_direction_pair_unresolved_not_added"
            packed.append((lo, hi))
            row.update(
                selected=True, status="selected_pilot_draft", physical_segment_id=physical_id
            )
            for chosen in paths:
                selected.append(chosen)
                static[chosen.segment_id + "__" + chosen.direction_id] = static_features(
                    chosen, graph, config.policy
                )
            direction_count += len(paths)
            watches = [
                w["id"]
                for w in watchlist
                if road["osm_name"] in w["roads"] and w.get("geometry") is None
            ]
            physical.append(
                {
                    "type": "Feature",
                    "geometry": path.geometry,
                    "properties": {
                        "physical_segment_id": physical_id,
                        "road_id": road["road_id"],
                        "road_name": road["osm_name"],
                        "segment_direction_ids": [
                            p.segment_id + "__" + p.direction_id for p in paths
                        ],
                        "direction_pair_mapping": pair_reason,
                        "station_interval_m": [lo, hi],
                        "length_m": path.length_m,
                        "start_lonlat": path.geometry["coordinates"][0],
                        "end_lonlat": path.geometry["coordinates"][-1],
                        "watchlist_unresolved": watches,
                        "review_status": "draft",
                        "length_exception_reason": "CONNECTED_PATH_WITHIN_EXISTING_800_1600_POLICY"
                        if row["length_exception"]
                        else None,
                    },
                }
            )
        runs = continuous_runs(intervals, graph)
        road_reports.append(
            {
                "road_id": road["road_id"],
                "road_name": road["osm_name"],
                "matching_directed_edges": len(edges),
                "before_filter_directed_m": sum(
                    graph.lines[e.id].intersection(region).length for e in edges
                ),
                "remaining_directed_m": sum(
                    b - a for parts in intervals.values() for a, b in parts
                ),
                "continuous_runs": runs,
                "drafts_generated": len(candidates),
                "eligible_drafts": len(eligible),
                "physical_selected": len(packed),
                "segment_directions": direction_count,
                "missing_to_min": max(0, policy["min_physical_per_road"] - len(packed)),
                "search_stats": stats,
                "unsearched_starts": available_starts - len(starts),
                "packing": "deterministic conservative station interval packing; not a theoretical maximum",
            }
        )
    hashes = {
        str(repo / policy[k]): sha256(repo / policy[k])
        for k in ("targets_file", "exclusions_file", "watchlist_file", "survey_manifest_file")
    }
    hashes[str(config.study.candidate_policy_file)] = sha256(config.study.candidate_policy_file)
    version = (
        "candidates_"
        + digest(
            {
                "inputs": hashes,
                "graph": graph.version,
                "policy": config.policy.model_dump(mode="json"),
                "paths": [p.model_dump(mode="json") for p in selected],
            }
        )[:18]
    )
    check_root = (
        config.study.candidate_checks_root or config.study.project_files_root / "candidate_checks"
    )
    checks = safe_output(check_root / version, check_root, (repo, config.study.osm_pbf))
    checks.mkdir(parents=True, exist_ok=False)
    write_json(checks / "draft_paths.json", [p.model_dump(mode="json") for p in drafts])
    write_json(checks / "road_survey.json", road_reports)
    write_json(checks / "structural_review.json", filtering.crossings)
    write_json(checks / "excluded_intervals.json", removed)
    write_json(checks / "watchlist.json", watchlist)
    destination = config.candidate_output(version)
    destination.mkdir(parents=True, exist_ok=False)
    write_json(destination / "paths.json", [p.model_dump(mode="json") for p in selected])
    write_json(destination / "shortlist.json", [p.model_dump(mode="json") for p in selected])
    write_json(
        destination / "candidates.geojson", {"type": "FeatureCollection", "features": physical}
    )
    fields = sorted({k for r in rows for k in r})
    write_csv(destination / "candidate_assessment.csv", rows, fields)
    write_json(destination / "static.json", static)
    manifest = {
        "candidate_version": version,
        "target_roads": targets["roads"],
        "input_hashes": hashes,
        "pbf_sha256": sha256(config.study.osm_pbf),
        "graph_version": graph.version,
        "graph_manifest_sha256": sha256(Path(graph.artifact_root) / "manifest.json"),
        "policy_sha256": sha256(config.study.policy_file),
        "source_fingerprint": source_fingerprint(repo),
        "metric_crs": graph.crs,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "axes": axes,
        "physical_segments": len(physical),
        "segment_directions": len(selected),
        "partial": any(r["missing_to_min"] for r in road_reports),
        "watchlist_status": "unresolved items retained; no safety or legal approval claimed",
        "offline_survey": str(checks),
        "road_summary": [
            {k: v for k, v in r.items() if k != "continuous_runs"} for r in road_reports
        ],
        "artifacts": {},
    }
    render_review(destination / "review.html", physical, exclusions, graph, watchlist, static)
    for p in destination.iterdir():
        if p.is_file():
            manifest["artifacts"][p.name] = sha256(p)
    write_json(destination / "manifest.json", manifest, immutable=True)
    return {
        "candidates": str(destination),
        "candidate_version": version,
        "survey": str(checks),
        "physical_segments": len(physical),
        "segment_directions": len(selected),
        "roads": manifest["road_summary"],
        "partial": manifest["partial"],
    }


def render_review(path, physical, exclusions, graph, watchlist, static):
    geometries = [p["geometry"] for p in physical] + [f["geometry"] for f in exclusions["features"]]
    from shapely.geometry import shape
    from shapely.ops import unary_union

    bounds = unary_union([shape(g) for g in geometries]).bounds
    w, s, e, n = bounds

    def coords(points):
        return " ".join(
            f"{30 + (x - w) / (e - w) * 1000:.2f},{830 - (y - s) / (n - s) * 800:.2f}"
            for x, y in points
        )

    pieces = []
    context = set()
    junctions = {}
    for values in static.values():
        for node in values["node_audit"]:
            if node["junction_included"]:
                junctions[node["node_id"]] = node
                context.update(graph.outgoing.get(node["node_id"], []))
                context.update(graph.incoming.get(node["node_id"], []))
    for edge_id in sorted(context):
        pieces.append(
            f'<polyline points="{coords(graph.edges[edge_id].coords)}" fill="none" stroke="#cbd5e1" stroke-width="1"/>'
        )
    for node in junctions.values():
        px, py = coords([(node["longitude"], node["latitude"])]).split(",")
        pieces.append(
            f'<circle cx="{px}" cy="{py}" r="2" fill="#15803d"><title>Retained normal junction {node["node_id"]}</title></circle>'
        )
    for f in exclusions["features"]:
        geometry = shape(f["geometry"])
        parts = list(geometry.geoms) if hasattr(geometry, "geoms") else [geometry]
        for part in parts:
            points = (
                list(part.exterior.coords) if part.geom_type == "Polygon" else list(part.coords)
            )
            tag = "polygon" if part.geom_type == "Polygon" else "polyline"
            pieces.append(
                f'<{tag} points="{coords(points)}" fill="{"#fed7aa" if tag == "polygon" else "none"}" stroke="#d97706" stroke-width="2"><title>{html.escape(f["properties"]["exclusion_id"])}</title></{tag}>'
            )
    for f in physical:
        pieces.append(
            f'<polyline points="{coords(f["geometry"]["coordinates"])}" fill="none" stroke="#075985" stroke-width="4"><title>{html.escape(str(f["properties"]))}</title></polyline>'
        )
    path.write_text(
        '<html><meta charset="utf-8"><h1>Four-road candidate review</h1><p>Blue: selected physical paths. Orange: original survey exclusions (line buffers applied in computation). Draft only. Normal intersections and signals are retained. WATCH_D3 has no fabricated geometry; relevance remains unreviewed.</p><svg viewBox="0 0 1080 880">'
        + "".join(pieces)
        + "</svg><pre>"
        + html.escape(str(watchlist))
        + "</pre></html>",
        encoding="utf-8",
    )
