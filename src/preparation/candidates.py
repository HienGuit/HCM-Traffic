"""Deterministic spatially distributed seed generation and bounded traversal."""

from collections import defaultdict, deque
from shapely.geometry import box, mapping
from ..domain.reference import RegistryPath, PathEdge
from ..geo.crs import project
from ..geo.linear import path_geometry
from ..storage.files import digest
from ..reference.graph import Graph
from ..config import ResolvedConfig
import math


def automatic_starts(graph: Graph, config: ResolvedConfig) -> tuple[list[tuple[str, float]], dict]:
    """Round-robin spatial cells, preserving the number of starts left unsearched."""
    region = project(box(*config.study.bbox), graph.crs)
    cells = defaultdict(list)
    for edge in sorted(graph.edges.values(), key=lambda e: e.id):
        if edge.tags.get("highway") not in config.policy.candidate_highways:
            continue
        line = graph.lines[edge.id]
        if not region.covers(line):
            continue
        offsets = [0.0]
        distance = config.policy.candidate_min_m
        while distance < edge.length:
            offsets.append(distance)
            distance += config.policy.candidate_min_m
        for offset in offsets:
            point = line.interpolate(offset)
            cell = (
                int(point.x // config.policy.seed_grid_m),
                int(point.y // config.policy.seed_grid_m),
            )
            cells[cell].append((edge.id, offset))
    if config.policy.seed_strategy != "spatial_diverse_hash_v2":
        raise ValueError("Unsupported seed strategy; no implicit ID-order fallback")

    def rank(item):
        edge_id, offset = item
        edge = graph.edges[edge_id]
        return digest(
            [
                config.policy.seed_salt,
                edge.tags.get("highway"),
                edge.tags.get("name"),
                edge.traversal,
                [[round(x, 7), round(y, 7)] for x, y in edge.coords],
                round(offset, 3),
            ]
        )

    queues = []
    cell_order = sorted(cells, key=lambda c: digest([config.policy.seed_salt, c]))
    for cell in cell_order:
        buckets = defaultdict(list)
        for item in cells[cell]:
            edge = graph.edges[item[0]]
            coords = graph.lines[edge.id].coords
            angle = (
                int(
                    (
                        math.atan2(coords[-1][1] - coords[0][1], coords[-1][0] - coords[0][0])
                        + math.pi
                    )
                    / (math.pi / 4)
                )
                % 8
            )
            buckets[
                (
                    edge.tags.get("highway"),
                    edge.tags.get("name") or edge.tags.get("ref") or "unnamed",
                    edge.traversal,
                    angle,
                )
            ].append(item)
        groups = [
            deque(sorted(buckets[k], key=rank))
            for k in sorted(buckets, key=lambda k: digest([config.policy.seed_salt, k]))
        ]
        diverse = []
        while groups:
            remaining = []
            for group in groups:
                diverse.append(group.popleft())
                if group:
                    remaining.append(group)
            groups = remaining
        queues.append(deque(diverse))
    all_count = sum(map(len, queues))
    selected = []
    while queues and len(selected) < config.policy.max_starts:
        remaining = []
        for queue in queues:
            if queue and len(selected) < config.policy.max_starts:
                selected.append(queue.popleft())
            if queue:
                remaining.append(queue)
        queues = remaining
    return selected, {
        "strategy": config.policy.seed_strategy,
        "salt": config.policy.seed_salt,
        "cell_count": len(cells),
        "cell_visit_order": "salted spatial hash",
        "within_cell_order": "round-robin highway/corridor/traversal/bearing; geometry hash",
        "available_starts": all_count,
        "selected_starts": len(selected),
        "unsearched_starts": all_count - len(selected),
    }


def generate_candidates(
    graph: Graph,
    config: ResolvedConfig,
    *,
    starts=None,
    edge_intervals=None,
    allowed_ids=None,
    region=None,
) -> tuple[list[RegistryPath], dict]:
    """Bounded legal-direction graph traversal in metric length, no speed-based ranking."""
    policy = config.policy
    if starts is None:
        starts, stats = automatic_starts(graph, config)
    else:
        stats = {"selected_starts": len(starts), "strategy": "explicit_clipped_corridor_starts"}
    candidates = []
    seen = set()
    bounded = 0
    region = region if region is not None else project(box(*config.study.bbox), graph.crs)
    for start_id, start_offset in starts:
        if len(candidates) >= policy.max_candidates:
            bounded += 1
            continue
        first = graph.edges[start_id]
        first_end = first.length
        if edge_intervals is not None:
            first_end = next(
                b for a, b in edge_intervals[start_id] if a - 1e-6 <= start_offset < b - 1e-6
            )
        beam = [
            (
                [PathEdge(edge_id=start_id, start_m=start_offset, end_m=first_end)],
                first_end - start_offset,
            )
        ]
        expanded = 0
        while beam and expanded < policy.max_expansions and len(candidates) < policy.max_candidates:
            next_beam = []
            for entries, length in beam:
                if expanded >= policy.max_expansions:
                    break
                expanded += 1
                last = graph.edges[entries[-1].edge_id]
                if length >= policy.candidate_min_m:
                    trimmed = list(entries)
                    if length > policy.candidate_max_m:
                        excess = length - policy.candidate_max_m
                        if trimmed[-1].end_m - excess <= trimmed[-1].start_m:
                            continue
                        trimmed[-1] = trimmed[-1].model_copy(
                            update={"end_m": trimmed[-1].end_m - excess}
                        )
                    key = digest([e.model_dump() for e in trimmed])
                    if key not in seen:
                        seen.add(key)
                        try:
                            geometry = path_geometry(graph, trimmed)
                        except ValueError:
                            continue
                        if region.covers(geometry):
                            candidates.append(
                                RegistryPath(
                                    segment_id="C_" + key[:14],
                                    graph_version=graph.version,
                                    road_name=first.tags.get("name")
                                    or first.tags.get("ref")
                                    or "unknown",
                                    edges=trimmed,
                                    length_m=geometry.length,
                                    geometry=mapping(project(geometry, "EPSG:4326", graph.crs)),
                                    legal_direction_status="supported"
                                    if all(
                                        graph.edges[e.edge_id].legal == "supported" for e in trimmed
                                    )
                                    else "unverified",
                                )
                            )
                    continue
                visited = {graph.edges[e.edge_id].start for e in entries}
                options = []
                corridor = {first.tags.get(k) for k in ("name", "ref") if first.tags.get(k)}
                for edge_id in graph.outgoing[last.end]:
                    if entries[-1].end_m < last.length - 1e-6:
                        break  # An exclusion/clip ends this connected component; never cross the gap.
                    if allowed_ids is not None and edge_id not in allowed_ids:
                        continue
                    edge = graph.edges[edge_id]
                    if (
                        edge.end in visited
                        or edge.tags.get("highway") not in policy.candidate_highways
                    ):
                        continue
                    # Named corridor continuity is necessary but does not establish carriageway identity.
                    if corridor and not corridor.intersection(
                        {edge.tags.get("name"), edge.tags.get("ref")}
                    ):
                        continue
                    end = edge.length
                    if edge_intervals is not None:
                        starts_at_node = [b for a, b in edge_intervals[edge_id] if a <= 1e-6]
                        if not starts_at_node:
                            continue
                        end = starts_at_node[0]
                    elif not region.covers(graph.lines[edge_id]):
                        continue
                    options.append((edge, end))
                for edge, end in sorted(options, key=lambda e: (e[0].frc, e[0].id))[
                    : policy.beam_width
                ]:
                    next_beam.append(
                        (
                            entries + [PathEdge(edge_id=edge.id, start_m=0, end_m=end)],
                            length + end,
                        )
                    )
                if (
                    not options
                    and policy.candidate_exception_min_m <= length < policy.candidate_min_m
                ):
                    key = digest([e.model_dump() for e in entries])
                    if key not in seen:
                        seen.add(key)
                        geometry = path_geometry(graph, entries)
                        candidates.append(
                            RegistryPath(
                                segment_id="C_" + key[:14],
                                graph_version=graph.version,
                                road_name=first.tags.get("name", "unknown"),
                                edges=entries,
                                length_m=length,
                                geometry=mapping(project(geometry, "EPSG:4326", graph.crs)),
                            )
                        )
            beam = sorted(next_beam, key=lambda item: ([e.edge_id for e in item[0]]))[
                : policy.beam_width
            ]
        bounded += bool(beam)
    return candidates, {**stats, "bounded_searches": bounded, "generated": len(candidates)}
