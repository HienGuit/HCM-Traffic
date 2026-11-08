"""Hard static gates precede ranking; traffic speeds never enter selection."""

from ..reference.static import static_features
from ..storage.files import write_json, write_csv, digest
from ..geo.linear import path_geometry
from shapely.geometry import Point
from ..provenance import source_fingerprint


def overlap(a, b):
    shared = 0.0
    for x in a.edges:
        for y in b.edges:
            if x.edge_id == y.edge_id:
                shared += max(0, min(x.end_m, y.end_m) - max(x.start_m, y.start_m))
    return shared / min(a.length_m, b.length_m)


def assess_candidates(candidates, graph, config):
    rows = []
    static = {}
    for path in candidates:
        features = static_features(path, graph, config.policy)
        static[path.segment_id] = features
        reasons = []
        if not path_geometry(graph, path.edges).is_simple:
            reasons.append("GEOMETRY_SELF_INTERSECTION")
        if (
            not config.policy.candidate_exception_min_m
            <= path.length_m
            <= config.policy.candidate_exception_max_m
        ):
            reasons.append("LENGTH_OUT_OF_POLICY")
        if features["lanes_mean"] is None:
            reasons.append("LANES_MISSING")
        if features["name_coverage"] < config.policy.name_coverage:
            reasons.append("NAME_COVERAGE_LOW")
        if not features["reference_complete"]:
            reasons.append("REFERENCE_INCOMPLETE")
        if path.legal_direction_status != "supported":
            reasons.append("LEGAL_UNKNOWN")
        if any(
            graph.edges[e.edge_id].tags.get("junction") in ("roundabout", "circular")
            or graph.edges[e.edge_id].tags.get("highway", "").endswith("_link")
            for e in path.edges
        ):
            reasons.append("COMPLEX_JUNCTION")
        if any(graph.edges[e.edge_id].tags.get("construction") for e in path.edges):
            reasons.append("CONSTRUCTION")
        # Parallel alternatives sharing the same corridor name require map/source review.
        own = {e.edge_id for e in path.edges}
        alternatives = set()
        for entry in path.edges:
            edge = graph.edges[entry.edge_id]
            line = graph.lines[edge.id]
            for index in graph.tree.query(line.buffer(config.policy.diagnostic_search_m)):
                other = graph.edges[graph.edge_ids[index]]
                if other.id in own or other.way_id == edge.way_id:
                    continue
                if other.tags.get("name") and other.tags.get("name") == edge.tags.get("name"):
                    other_line = graph.lines[other.id]
                    # Compare directed endpoints; opposite carriageway alone is not a same-direction ambiguity.
                    a, b = line.coords[0], line.coords[-1]
                    c, d = other_line.coords[0], other_line.coords[-1]
                    dot = (b[0] - a[0]) * (d[0] - c[0]) + (b[1] - a[1]) * (d[1] - c[1])
                    projected = [line.project(Point(p)) for p in (c, d)]
                    positive_overlap = abs(projected[1] - projected[0]) > 1e-6
                    if (
                        dot > 0
                        and positive_overlap
                        and not {edge.start, edge.end}.intersection({other.start, other.end})
                    ):
                        alternatives.add(other.id)
        if alternatives:
            reasons.append("PARALLEL_REVIEW_REQUIRED")
        rows.append(
            {
                "candidate_id": path.segment_id,
                "road_name": path.road_name,
                "direction": path.direction_id,
                "length_m": path.length_m,
                "static_pass": not reasons,
                "status": "rejected" if reasons else "provisional",
                "lane_coverage": features["lanes_known_coverage"],
                "reference_review_status": "unreviewed",
                "source_direction_status": "unverified",
                "pilot_pass": False,
                "rejection_reasons": reasons,
                "parallel_alternative_ids": sorted(alternatives),
                "shortlisted": False,
                "length_exception": not config.policy.candidate_min_m
                <= path.length_m
                <= config.policy.candidate_max_m,
            }
        )
    by_id = {p.segment_id: p for p in candidates}
    shortlist = []
    eligible = sorted(
        (r for r in rows if r["static_pass"]),
        key=lambda r: (-r["lane_coverage"], abs(r["length_m"] - 1250), r["candidate_id"]),
    )
    for row in eligible:
        path = by_id[row["candidate_id"]]
        if any(overlap(path, other) > config.policy.max_overlap for other in shortlist):
            row["rejection_reasons"].append("SHORTLIST_OVERLAP")
            continue
        if len(shortlist) < config.policy.max_shortlist:
            shortlist.append(path)
            row["shortlisted"] = True
    return rows, static, shortlist


def export_candidates(folder, candidates, rows, static, shortlist, stats, config):
    folder.mkdir(parents=True, exist_ok=False)
    write_json(folder / "paths.json", [p.model_dump(mode="json") for p in candidates])
    write_json(folder / "shortlist.json", [p.model_dump(mode="json") for p in shortlist])
    write_json(folder / "static.json", static)
    write_csv(folder / "candidate_assessment.csv", rows)
    lookup = {r["candidate_id"]: r for r in rows}
    features = [
        {"type": "Feature", "geometry": p.geometry, "properties": lookup[p.segment_id]}
        for p in candidates
    ]
    write_json(folder / "candidates.geojson", {"type": "FeatureCollection", "features": features})
    write_json(
        folder / "manifest.json",
        {
            "schema": 1,
            "stats": stats,
            "config_hash": config.hash,
            "candidate_hash": digest([p.model_dump(mode="json") for p in candidates]),
            "official_approved": 0,
            "review": "pending",
            "study_bbox": config.study.bbox,
            "source_fingerprint": source_fingerprint(config.study.project_root),
            "graph_version": candidates[0].graph_version if candidates else None,
        },
    )
    from .evidence import review_map

    review_map(folder / "review.html", shortlist, config)
