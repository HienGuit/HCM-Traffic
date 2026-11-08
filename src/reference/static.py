"""Static features from trimmed directed OSM paths; counts are independent."""

from collections import defaultdict
import json
import sqlite3
from pathlib import Path
from shapely.geometry import LineString
from .classification import directional_lanes, directions, NON_MOTOR
from ..geo.linear import path_geometry
from ..geo.crs import project

JUNCTION_METHOD = "osm_topological_junction_nodes_v2_original_adjacency"
SIGNAL_METHOD = "osm_directional_signal_locations_v1"


def original_motor_adjacency(graph):
    """Undirected immediate original-ref neighbors, never contracted edge endpoints.

    Scan all normalized ways so branches outside the selected path are retained.
    The highway/area/access and complete-node filters mirror graph construction.
    This method-versioned cache is local to the already validated graph instance.
    """
    if getattr(graph, "_original_motor_topology_method", None) == JUNCTION_METHOD:
        return graph._original_motor_adjacency
    database = (
        Path(graph.artifact_root) / "normalized.sqlite" if hasattr(graph, "artifact_root") else None
    )
    adjacency = defaultdict(set)
    available = database is not None and database.is_file()
    if available:
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
            for refs_json, tags_json in db.execute("SELECT nodes,tags FROM ways"):
                tags = json.loads(tags_json)
                if (
                    not tags.get("highway")
                    or tags["highway"] in NON_MOTOR
                    or tags.get("area") == "yes"
                    or tags.get("access") == "no"
                    or tags.get("motor_vehicle") == "no"
                ):
                    continue
                refs = json.loads(refs_json)
                if any(node not in graph.nodes for node in refs):
                    continue
                for first, second in zip(refs, refs[1:]):
                    if first != second:
                        adjacency[first].add(second)
                        adjacency[second].add(first)
    graph._original_motor_adjacency = dict(adjacency)
    graph._original_motor_topology_method = JUNCTION_METHOD
    graph._original_motor_topology_available = available
    return graph._original_motor_adjacency


def selected_nodes(path, graph):
    """Recover original refs, including internal way nodes, with directed chainage."""
    adjacency = original_motor_adjacency(graph)
    refs_cache = getattr(graph, "original_way_refs", {})
    needed = {graph.edges[e.edge_id].way_id for e in path.edges} - refs_cache.keys()
    database = (
        Path(graph.artifact_root) / "normalized.sqlite" if hasattr(graph, "artifact_root") else None
    )
    if database and database.is_file() and needed:
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
            for way, nodes in db.execute(
                "SELECT id,nodes FROM ways WHERE id IN (" + ",".join("?" for _ in needed) + ")",
                sorted(needed),
            ):
                refs_cache[way] = json.loads(nodes)
        graph.original_way_refs = refs_cache
    rows, missing = [], []
    prefix = 0.0
    for entry in path.edges:
        edge = graph.edges[entry.edge_id]
        refs = refs_cache.get(edge.way_id)
        if refs is not None:
            ids = refs[edge.first_index : edge.last_index + 1]
            if edge.traversal == -1:
                ids = list(reversed(ids))
        elif not hasattr(graph, "artifact_root") and len(edge.coords) == 2:
            ids = [edge.start, edge.end]  # Explicit two-node synthetic graph.
        else:
            missing.append(edge.id)
            ids = []
        if len(ids) != len(edge.coords) or any(n not in graph.nodes for n in ids):
            missing.append(edge.id)
            prefix += entry.end_m - entry.start_m
            continue
        metric = project(LineString(edge.coords), graph.crs)
        points = list(metric.coords)
        offset = 0.0
        for index, node in enumerate(ids):
            if index:
                offset += (
                    (points[index][0] - points[index - 1][0]) ** 2
                    + (points[index][1] - points[index - 1][1]) ** 2
                ) ** 0.5
            if entry.start_m - 1e-6 <= offset <= entry.end_m + 1e-6:
                neighbors = adjacency.get(node, set())
                rows.append(
                    {
                        "node_id": node,
                        "way_id": edge.way_id,
                        "edge_id": edge.id,
                        "traversal": edge.traversal,
                        "edge_offset_m": offset,
                        "chainage_m": prefix + offset - entry.start_m,
                        "longitude": graph.nodes[node][0],
                        "latitude": graph.nodes[node][1],
                        "tags": graph.nodes[node][2],
                        "topology_neighbors": sorted(neighbors),
                        "topology_degree": len(neighbors),
                        "original_ref_index": edge.first_index + index
                        if edge.traversal == 1
                        else edge.last_index - index,
                    }
                )
        prefix += entry.end_m - entry.start_m
    return rows, sorted(set(missing))


def signal_decision(tags, traversal):
    mapped = (
        tags.get("highway") == "traffic_signals"
        or tags.get("crossing") == "traffic_signals"
        or tags.get("crossing:signals") == "yes"
    )
    if not mapped:
        return {"mapped_signal": False, "included": False, "reason": "not_mapped_signal"}
    values = {tags[k] for k in ("traffic_signals:direction", "direction") if tags.get(k)}
    if len(values) > 1:
        return {"mapped_signal": True, "included": None, "reason": "SIGNAL_DIRECTION_TAG_CONFLICT"}
    value = next(iter(values), None)
    if value is None:
        return {"mapped_signal": True, "included": True, "reason": "untagged_on_motor_way"}
    if value not in ("forward", "backward", "both"):
        return {"mapped_signal": True, "included": None, "reason": "SIGNAL_DIRECTION_UNSUPPORTED"}
    applies = value == "both" or value == ("forward" if traversal == 1 else "backward")
    return {
        "mapped_signal": True,
        "included": applies,
        "reason": "tagged_" + value if applies else "opposite_way_direction",
    }


def static_features(path, graph, policy) -> dict:
    lengths, lanes, names = defaultdict(float), [], 0.0
    lane_audit = []
    complete = not bool(getattr(graph, "missing_ways", 0))
    for entry in path.edges:
        edge = graph.edges[entry.edge_id]
        length = entry.end_m - entry.start_m
        lengths[edge.tags.get("highway", "unknown")] += length
        count = directional_lanes(edge.tags, edge.traversal)
        lane_audit.append(
            {
                "edge_id": edge.id,
                "length_m": length,
                "lanes": count,
                "tags": {k: v for k, v in edge.tags.items() if k.startswith("lanes")},
                "status": "osm_unverified",
            }
        )
        if count is not None:
            lanes.append((length, count))
        if edge.tags.get("name") or edge.tags.get("ref"):
            names += length
        complete &= not edge.boundary
    nodes, missing = selected_nodes(path, graph)
    complete &= not missing
    junction_complete = complete and graph._original_motor_topology_available
    junctions = {r["node_id"] for r in nodes if len(r["topology_neighbors"]) >= 3}
    signals, uncertain, decisions = set(), set(), defaultdict(set)
    for row in nodes:
        result = signal_decision(row["tags"], row["traversal"])
        row.update(result)
        row["direction_basis"] = result["reason"]
        row["junction_included"] = row["node_id"] in junctions
        row["junction_reason"] = "degree_ge_3" if row["junction_included"] else "degree_lt_3"
        if result["mapped_signal"]:
            decisions[row["node_id"]].add(result["included"])
            if result["included"] is True:
                signals.add(row["node_id"])
            elif result["included"] is None:
                uncertain.add(row["node_id"])
    for node, results in decisions.items():
        if True in results and False in results:
            uncertain.add(node)
            for row in nodes:
                if row["node_id"] == node:
                    row.update(included=None, reason="SIGNAL_WAY_ASSOCIATION_AMBIGUOUS")
    total = path.length_m
    known = sum(length for length, _ in lanes)
    mean = sum(length * count for length, count in lanes) / known if known else None
    dominant = max(lengths, key=lengths.get)
    signal_reasons = sorted({r["reason"] for r in nodes if r["node_id"] in uncertain})
    if not complete:
        signal_reasons.append("SIGNAL_PATH_SOURCE_INCOMPLETE")
    return {
        "segment_id": path.segment_id,
        "direction_id": path.direction_id,
        "length_m": path_geometry(graph, path.edges).length,
        "road_type": dominant
        if lengths[dominant] / total >= policy.road_type_dominance
        else "mixed",
        "road_type_distribution": dict(lengths),
        "lanes_mean": mean if known / total >= policy.lanes_coverage else None,
        "lanes_known_only_mean": mean,
        "lanes_min": min((n for _, n in lanes), default=None),
        "lanes_max": max((n for _, n in lanes), default=None),
        "lanes_known_coverage": known / total,
        "name_coverage": names / total,
        "lane_profile": lane_audit,
        "junction_count": len(junctions) if junction_complete else None,
        "junction_known_count": len(junctions),
        "junction_status": "complete" if junction_complete else "JUNCTION_TOPOLOGY_INCOMPLETE",
        "junction_method": JUNCTION_METHOD,
        "junction_ids": sorted(junctions),
        "signal_count": len(signals) if complete and not uncertain else None,
        "signal_known_count": len(signals - uncertain),
        "signal_ids": sorted(signals - uncertain),
        "signal_status": "incomplete"
        if not complete or uncertain
        else "mapped_signal"
        if signals
        else "no_mapped_signal",
        "signal_reasons": signal_reasons,
        "signal_method": SIGNAL_METHOD,
        "signal_unknown_ids": sorted(uncertain),
        "node_audit": nodes,
        "missing_ref_edges": missing,
        "unresolved_cluster_pairs": [],
        "oneway": all(len(directions(graph.edges[e.edge_id].tags)[0]) == 1 for e in path.edges),
        "reference_complete": complete,
        "lane_status": "osm_unverified",
    }
