"""Bounded projection sequences preserve edge occurrences and component gaps."""

import math
from shapely.geometry import Point, LineString
from shapely.strtree import STRtree
from ..geo.crs import project
from ..domain.processing import Footprint, FeatureOccurrence, DecodedLocation
from ..reference.graph import Graph
from ..config import Policy

METHOD = "directed_component_consensus_v2"


def _same_physical_traversal(states, location, graph, quantization_m=0.0):
    """Prove offset-only alternatives on one continuous, non-repeated traversal.

    Boundary assignments are normalized on the component's continuous chainage.
    Residuals and source quantization bound projection uncertainty; no endpoint snap
    radius is introduced. Return legs outside this component are irrelevant.
    """
    indexes = [p[0] for state in states for p in state[1]]
    if not indexes:
        return False
    first, last = min(indexes), max(indexes)
    edges = [graph.edges[k] for k in location.edges[first:last+1]]
    if len({e.id for e in edges}) != len(edges) or len(
        {(e.way_id, e.first_index, e.last_index) for e in edges}
    ) != len(edges) or any(a.end != b.start for a, b in zip(edges, edges[1:])):
        return False
    directions = []
    reference = states[0][1]
    for _, positions, state_residuals in states:
        deltas = [b[2] - a[2] for a, b in zip(positions, positions[1:])]
        signs = {1 if d > 0 else -1 for d in deltas if abs(d) > 1e-6}
        if len(signs) != 1:
            return False
        directions.append(next(iter(signs)))
        for n, (a, b) in enumerate(zip(reference, positions)):
            if a[0] == b[0]:
                continue
            if abs(a[0] - b[0]) != 1:
                return False
            # Both projections must be explained by this sample's residual and
            # MVT quantization, rather than a fixed distance or exact endpoint.
            residual_a = states[0][2][n] if states[0][2] else 0.0
            residual_b = state_residuals[n] if state_residuals else 0.0
            if abs(a[2]-b[2]) > residual_a + residual_b + 2*quantization_m + 1e-6:
                return False
    return len(set(directions)) == 1


def associate_footprint(
    feature: FeatureOccurrence,
    location: DecodedLocation,
    graph: Graph,
    policy: Policy,
    calibration: dict | None = None,
) -> Footprint:
    """Associate geometry provisionally; geometric orientation never verifies source direction."""
    if location.status != "decoded":
        return Footprint(
            occurrence_id=feature.occurrence_id, status="failed", reasons=location.reasons
        )
    lines = [graph.lines[k] for k in location.edges]
    tree = STRtree(lines)
    cumulative = [0.0]
    for line in lines:
        cumulative.append(cumulative[-1] + line.length)
    pieces = []
    diagnostics = []
    observed_pieces = []
    unresolved = False
    calibrated = bool(calibration and calibration.get("ready"))
    for component_index, component in enumerate(feature.components):
        geometry = project(LineString(component), graph.crs)
        distances = {0.0, geometry.length}
        distance = 0.0
        for a, b in zip(geometry.coords, list(geometry.coords)[1:]):
            distance += Point(a).distance(Point(b))
            distances.add(distance)
        step = policy.diagnostic_segment_step_m
        distances.update(i * step for i in range(1, int(geometry.length / step) + 1))
        samples = [geometry.interpolate(d) for d in sorted(distances)]
        states = [(0.0, [], [])]
        for point in samples:
            options = []
            for index in tree.query(point.buffer(policy.diagnostic_search_m)):
                line = lines[index]
                residual = line.distance(point)
                if residual <= policy.diagnostic_search_m:
                    local = line.project(point)
                    options.append((residual, int(index), local, cumulative[index] + local))
            options = sorted(options)[: policy.projection_candidates]
            if not options:
                states = []
                break
            new = []
            for cost, positions, residuals in states:
                for residual, index, local, chainage in options:
                    if positions:
                        delta = chainage - positions[-1][2]
                        if len(positions) > 1:
                            previous = positions[-1][2] - positions[-2][2]
                            if delta * previous < -1e-6:
                                continue
                        jump = abs(delta)
                        source_step = point.distance(samples[len(positions) - 1])
                        # Continuity guard is diagnostic and cannot certify calibrated support.
                        if jump > source_step + 2 * policy.diagnostic_search_m:
                            continue
                        penalty = abs(jump - source_step)
                    else:
                        penalty = 0
                    new.append(
                        (
                            cost + residual * residual + penalty * penalty,
                            positions + [(index, local, chainage)],
                            residuals + [residual],
                        )
                    )
            states = sorted(new, key=lambda s: (s[0], s[1]))[: policy.projection_states]
            if not states:
                break
        if not states:
            unresolved = True
            diagnostics.append({"component": component_index, "status": "unassociated"})
            continue
        best = states[0]
        positions = best[1]
        low = min(p[2] for p in positions)
        high = max(p[2] for p in positions)
        # Alternative occurrence associations within source quantization remain ambiguous.
        quantization_m = max(feature.quantization_m, 1e-6)
        feasible_states = [best] + [
            s
            for s in states[1:]
            if s[0] <= best[0] + len(samples) * quantization_m**2
        ]
        projection_alternatives = feasible_states[1:]
        # Different vertex projections are not different physical footprints when
        # they induce the identical interval on this fixed edge-occurrence path.
        # Numerical tolerance is one micrometre, not a tuned matching threshold.
        clipped_best = (max(low, location.start_m), min(high, location.end_m))
        alternatives = [
            state
            for state in projection_alternatives
            if abs(max(min(p[2] for p in state[1]), location.start_m) - clipped_best[0]) > 1e-6
            or abs(min(max(p[2] for p in state[1]), location.end_m) - clipped_best[1]) > 1e-6
        ]
        before = clipped_best
        for index, line in enumerate(lines):
            start, end = max(before[0], cumulative[index]), min(before[1], cumulative[index + 1])
            if end > start:
                observed_pieces.append({"component": component_index,
                    "edge_id": location.edges[index], "path_edge_index": index,
                    "edge_start_m": start-cumulative[index],
                    "edge_end_m": end-cumulative[index]})
        consensus = not projection_alternatives or _same_physical_traversal(
            feasible_states, location, graph, quantization_m
        )
        if not consensus:
            unresolved = True
            diagnostics.append({"component": component_index, "status": "ambiguous",
                "reason": "PHYSICAL_TRAVERSAL_EQUIVALENCE_UNPROVEN",
                "alternatives": len(alternatives), "retained_length_m": 0.0,
                "best_support_bounds_m": list(before)})
            continue
        # Intersection on this component's decoded chainage, never union or gap fill.
        low = max(max(min(p[2] for p in s[1]), location.start_m)
                  for s in feasible_states)
        high = min(min(max(p[2] for p in s[1]), location.end_m)
                   for s in feasible_states)
        if high <= low:
            unresolved = True
            diagnostics.append({"component": component_index, "status": "unresolved",
                "reason": "EMPTY_SUPPORT_INTERSECTION", "retained_length_m": 0.0})
            continue
        source_direction = (
            "reversed_vertices" if positions[-1][2] < positions[0][2] else "forward_vertices"
        )
        reconstructed = []
        from shapely.ops import substring

        component_pieces = []
        for index, line in enumerate(lines):
            start = max(low, cumulative[index])
            end = min(high, cumulative[index + 1])
            if end > start:
                segment = substring(line, start - cumulative[index], end - cumulative[index])
                reconstructed.extend(segment.coords)
                component_pieces.append(
                    {
                        "component": component_index,
                        "path_edge_index": index,
                        "edge_id": location.edges[index],
                        "edge_start_m": start - cumulative[index],
                        "edge_end_m": end - cumulative[index],
                        "location_start_m": start,
                        "location_end_m": end,
                        "geometry_orientation": source_direction,
                    }
                )
        between_error = (
            geometry.hausdorff_distance(LineString(reconstructed))
            if len(reconstructed) > 1
            else math.inf
        )
        component_uncertain = between_error > policy.diagnostic_search_m
        step_errors = [
            abs(abs(b[2] - a[2]) - samples[i + 1].distance(samples[i]))
            for i, (a, b) in enumerate(zip(positions, positions[1:]))
        ]
        max_step_error = max(step_errors, default=0)
        if calibrated and (
            max(best[2]) > calibration["limits"]["residual_m"]
            or between_error > calibration["limits"]["residual_m"]
            or max_step_error > calibration["limits"]["step_error_m"]
        ):
            component_uncertain = True
        if component_uncertain:
            unresolved = True
        else:
            pieces.extend(component_pieces)
        diagnostics.append(
            {
                "component": component_index,
                "status": "uncertain" if component_uncertain else "consensus" if alternatives else "matched",
                "support_before_m": list(before),
                "support_after_m": [low, high] if not component_uncertain else [],
                "retained_length_m": high - low if not component_uncertain else 0.0,
                "unresolved_boundary_length_m": max(0.0, before[1] - before[0] - (high - low)),
                "cost_sum_squared_m": best[0],
                "max_residual_m": max(best[2]),
                "between_vertices_hausdorff_m": between_error,
                "alternatives": len(alternatives),
                "equivalent_projection_alternatives": len(projection_alternatives)
                - len(alternatives),
                "orientation": source_direction,
                "max_step_error_m": max_step_error,
                "best_support_bounds_m": [low, high],
                "alternative_support_bounds_m": [
                    [
                        max(min(p[2] for p in state[1]), location.start_m),
                        min(max(p[2] for p in state[1]), location.end_m),
                    ]
                    for state in alternatives
                ],
            }
        )
    reasons = []
    if unresolved:
        reasons.append("PARTIAL_COMPONENT_SUPPORT_UNRESOLVED")
    if not pieces:
        reasons.append("GEOMETRY_UNCERTAIN")
    return Footprint(
        occurrence_id=feature.occurrence_id,
        status="matched" if pieces else "ambiguous" if unresolved else "uncertain",
        pieces=pieces,
        diagnostics={
            "method_id": METHOD,
            "components": diagnostics,
            "observed_support_before_consensus": observed_pieces,
            "cost": "sum(residual_m^2 + chainage_step_error_m^2)",
            "source_direction": "unverified",
            "calibration_status": "evaluated" if calibrated else "not_evaluated",
            "technical_limit_source": "policy.diagnostic_search_m and continuity <= source_step + 2*search_m",
            "technical_search_limit_m": policy.diagnostic_search_m,
            "decoded_edges": location.edges,
            "calibration_evidence_ids": calibration.get("evidence_ids", []) if calibration else [],
        },
        reasons=reasons,
    )
