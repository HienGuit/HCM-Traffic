"""Automatic directed OpenLR correspondence; review metadata is kept separate."""

import math
from ..preparation.gates import association_identity

METHOD = "directed_openlr_occurrence_v2"


def automatic_direction(feature, footprint, path, location, graph):
    """Verify this occurrence's ordered directed correspondence and clipped offsets.

    Baseline interpretation: source speed belongs to its decoded OpenLR traversal.
    Vertex order is irrelevant. This is an executable method, not reviewed evidence.
    """
    result = {
        "status": "unverified",
        "reasons": ["ASSOCIATION_INVALID"],
        "evidence_ids": [],
        "association_id": association_identity(feature, footprint, path, location),
        "method_id": METHOD,
    }
    if (
        feature.openlr != location.code
        or location.status != "decoded"
        or location.graph_version != graph.version
        or path.graph_version != graph.version
        or footprint.status not in ("matched", "provisional")
        or not footprint.pieces
    ):
        return result
    if len(
        {e.edge_id for e in path.edges}
    ) != len(path.edges):
        result["reasons"] = ["REPEATED_EDGE_ASSOCIATION_UNRESOLVED"]
        return result
    try:
        from ..geo.linear import path_geometry
        from ..geo.crs import project
        from shapely.geometry import shape, Point

        line = path_geometry(graph, path.edges)
        declared = project(shape(path.geometry), graph.crs)
        if (
            abs(line.length - path.length_m) > 1e-4
            or declared.hausdorff_distance(line) > 1e-4
            or Point(declared.coords[0]).distance(Point(line.coords[0])) > 1e-4
            or Point(declared.coords[-1]).distance(Point(line.coords[-1])) > 1e-4
        ):
            result["reasons"] = ["REGISTRY_GEOMETRY_INVALID"]
            return result
        edges = [graph.edges[e] for e in location.edges]
        offsets = [0.0]
        for edge in edges:
            offsets.append(offsets[-1] + edge.length)
        if not (0 <= location.start_m < location.end_m <= offsets[-1] + 1e-6):
            result["reasons"] = ["DECODED_OFFSETS_INVALID"]
            return result
        indexes = {e.edge_id: i for i, e in enumerate(path.edges)}
        components = {}
        for piece in footprint.pieces:
            components.setdefault(piece.get("component", 0), []).append(piece["path_edge_index"])
        for selected in components.values():
            relevant = edges[min(selected):max(selected)+1]
            if len({e.id for e in relevant}) != len(relevant):
                result["reasons"] = ["REPEATED_EDGE_ASSOCIATION_UNRESOLVED"]
                return result
            if any(a.end != b.start for a,b in zip(relevant,relevant[1:])):
                result["reasons"] = ["DECODED_PATH_DISCONNECTED"]
                return result
            order = [indexes[e.id] for e in relevant if e.id in indexes]
            if order != sorted(order):
                result["reasons"] = ["NO_SAME_DIRECTION_CORRESPONDENCE"]
                return result
        overlap = False
        for piece in footprint.pieces:
            i = piece["path_edge_index"]
            if not (0 <= i < len(edges)) or edges[i].id != piece["edge_id"]:
                return result
            low, high = piece["edge_start_m"], piece["edge_end_m"]
            if not (
                math.isfinite(low)
                and math.isfinite(high)
                and 0 <= low < high <= edges[i].length + 1e-6
                and low + offsets[i] >= location.start_m - 1e-6
                and high + offsets[i] <= location.end_m + 1e-6
            ):
                return result
            if edges[i].id in indexes:
                entry = path.edges[indexes[edges[i].id]]
                overlap |= min(high, entry.end_m) > max(low, entry.start_m)
        if not overlap:
            return result
    except (KeyError, IndexError, TypeError, ValueError):
        return result
    result.update(status="supported", reasons=[])
    return result


def legal_direction(path, graph):
    segments = []
    offset = 0.0
    for entry in path.edges:
        edge = graph.edges[entry.edge_id]
        length = entry.end_m - entry.start_m
        segments.append(
            {
                "start_m": offset,
                "end_m": offset + length,
                "status": edge.legal,
                "reason": "OSM_RESTRICTION_OR_ACCESS_UNRESOLVED"
                if edge.legal != "supported"
                else None,
            }
        )
        offset += length
    return {
        "status": "supported"
        if all(s["status"] == "supported" for s in segments)
        else "unverified",
        "evidence_ids": [],
        "segments": segments,
        "method_id": "directed_osm_rules_v1",
    }


def relevant_support(location, path, graph):
    """Clip decoded offsets to each ordered registry occurrence, excluding outside tails."""
    from ..geo.support import union

    if location.status != "decoded":
        return None
    pieces = []
    decoded_offset = 0.0
    for edge_id in location.edges:
        edge = graph.edges[edge_id]
        registry_offset = 0.0
        for entry in path.edges:
            if edge_id == entry.edge_id:
                low = max(entry.start_m, location.start_m - decoded_offset, 0.0)
                high = min(entry.end_m, location.end_m - decoded_offset, edge.length)
                if high > low:
                    pieces.append(
                        (
                            registry_offset + low - entry.start_m,
                            registry_offset + high - entry.start_m,
                        )
                    )
            registry_offset += entry.end_m - entry.start_m
        decoded_offset += edge.length
    return union(pieces)


def profiles_agree(first, second, path, graph):
    if second is None:
        return True  # A second profile is optional, never a procedural approval.
    try:
        for location in (first,second):
            edges = [graph.edges[k] for k in location.edges]
            if location.graph_version != graph.version or location.status != "decoded" or not 0 <= location.start_m < location.end_m <= sum(e.length for e in edges)+1e-6:
                return False
            relevant=[];indices=[];offset=0.
            for index,edge in enumerate(edges):
                if min(offset+edge.length,location.end_m)>max(offset,location.start_m) and edge.id in {e.edge_id for e in path.edges}:
                    relevant.append(edge)
                    indices.append(index)
                offset+=edge.length
            if not relevant or len({e.id for e in relevant})!=len(relevant):return False
            # No interior detour/return leg between registry occurrences.
            traversal=edges[min(indices):max(indices)+1]
            if any(e.id not in {p.edge_id for p in path.edges} for e in traversal):return False
            if any(a.end!=b.start for a,b in zip(traversal,traversal[1:])):return False
            order=[next(i for i,p in enumerate(path.edges) if p.edge_id==e.id) for e in traversal]
            if order!=sorted(order):return False
    except (KeyError, ValueError):
        return False
    a, b = relevant_support(first, path, graph), relevant_support(second, path, graph)
    return (
        a is not None
        and b is not None
        and len(a) == len(b)
        and bool(a)
        and all(min(y,v)>max(x,u) for (x,y),(u,v) in zip(a,b))
    )


def profile_consensus(footprint, first, second, path, graph, *, required=False):
    """Clip actual pieces to shared profile support; a boolean never retains union."""
    from ..geo.support import intersection
    diagnostics = dict(footprint.diagnostics)
    if second is None and not required:
        return footprint
    if second is None or not profiles_agree(first, second, path, graph):
        diagnostics['profile_consensus']={'method_id':'profile_support_intersection_v1','status':'unresolved'}
        return footprint.model_copy(update={'status':'ambiguous','pieces':[],
            'reasons':sorted(set(footprint.reasons+['DECODER_PROFILE_DISAGREEMENT'])),
            'diagnostics':diagnostics})
    common=intersection(relevant_support(first,path,graph), relevant_support(second,path,graph))
    pieces=[];offset=0.
    for entry in path.edges:
        for piece in footprint.pieces:
            if piece['edge_id']!=entry.edge_id:continue
            a=offset+max(piece['edge_start_m'],entry.start_m)-entry.start_m
            b=offset+min(piece['edge_end_m'],entry.end_m)-entry.start_m
            for low,high in intersection([(a,b)],common):
                clipped=dict(piece)
                clipped['edge_start_m']=entry.start_m+low-offset
                clipped['edge_end_m']=entry.start_m+high-offset
                delta=clipped['edge_start_m']-piece['edge_start_m']
                if 'location_start_m' in piece:
                    clipped['location_start_m']=piece['location_start_m']+delta
                    clipped['location_end_m']=clipped['location_start_m']+high-low
                pieces.append(clipped)
        offset+=entry.end_m-entry.start_m
    before=sum(p['edge_end_m']-p['edge_start_m'] for p in footprint.pieces)
    after=sum(p['edge_end_m']-p['edge_start_m'] for p in pieces)
    diagnostics['profile_consensus']={'method_id':'profile_support_intersection_v1',
        'status':'shared' if pieces else 'unresolved','common_registry_support':common,
        'before_length_m':before,'retained_length_m':after,'removed_length_m':max(0.,before-after)}
    return footprint.model_copy(update={'pieces':pieces,'status':footprint.status if pieces else 'ambiguous','diagnostics':diagnostics})
