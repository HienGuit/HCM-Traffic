"""Edge-correspondence intersection and non-overlapping boundary sweep."""

from ..domain.processing import Interval
from ..domain.processing import Footprint, FeatureOccurrence
from ..domain.reference import RegistryPath
from ..reference.graph import Graph
from ..geo.support import length
import math

METHOD = "independent_interval_policy_v2"
RELATIVE_EPSILON = 1e-9


def _measurement(ids, features, field, *, relative=False):
    valid, invalid, missing = [], [], []
    for identity in ids:
        feature = features[identity]
        value = getattr(feature, field)
        if field in feature.invalid_fields or (value is not None and (
            isinstance(value, bool) or not math.isfinite(value)
            or value < 0 or (relative and value > 1)
        )):
            invalid.append(identity)
        elif value is None:
            missing.append(identity)
        else:
            valid.append(identity)
    values = {getattr(features[i], field) for i in valid}
    prefix = "RELATIVE_SPEED" if relative else "ABSOLUTE_SPEED"
    reasons = ([prefix + "_MISSING"] if missing else []) + ([prefix + "_INVALID"] if invalid else [])
    if len(values) > 1:
        return None, "conflict", valid, reasons + [prefix + "_CONFLICT"]
    # A missing copy supplies no measurement. Keep only the valid contributors;
    # never populate the missing copy from another source.
    if not values:
        return None, "invalid" if invalid else "missing", [], reasons
    return next(iter(values)), "consistent", valid, reasons


def resolve_relative(ids, features, policy=None):
    """Explicit user policy; unique values, no copy-count voting or cross-pairing."""
    value,status,valid,reasons=_measurement(ids,features,'relative_speed',relative=True)
    values=sorted({features[i].relative_speed for i in valid})
    spread=values[-1]-values[0] if values else None
    selected=list(ids);method='source_relative_consistent_v1'
    priority=False
    if status=='conflict':
        small=getattr(policy,'relative_conflict_small_spread',.05)
        if spread <= small+RELATIVE_EPSILON:
            value=(values[0]+values[-1])/2
            status='resolved';method='relative_midrange_v1'
        elif len(values)==2 and any(abs(v-1)<=RELATIVE_EPSILON for v in values):
            selected=[i for i in valid if abs(features[i].relative_speed-1)>RELATIVE_EPSILON]
            valid=selected;value=features[selected[0]].relative_speed
            status='resolved';method='prefer_nonunity_on_large_conflict_v1';priority=True
        else:
            method='relative_conflict_unresolved_v1'
        if status=='resolved':
            reasons=[r for r in reasons if r!='RELATIVE_SPEED_CONFLICT']
            reasons.append('RELATIVE_POLICY_RESOLVED')
    return dict(value=value,status=status,contributors=valid,reasons=reasons,
        selected=selected,method=method,values=values,spread=spread,priority=priority)


def partition_registry(
    footprints: list[Footprint],
    path: RegistryPath,
    features: dict[str, FeatureOccurrence],
    graph: Graph,
    associations: dict | None = None,
    receipts: dict | None = None,
    policy=None,
) -> tuple[list[Interval], list[dict]]:
    """Intersect same-version directed support; repeated ambiguous occurrences are excluded."""
    contributions = []
    observed = []
    registry_offset = 0.0
    registry_ids = [e.edge_id for e in path.edges]
    for registry_index, entry in enumerate(path.edges):
        for footprint in footprints:
            for piece in footprint.diagnostics.get("observed_support_before_consensus", []):
                if piece["edge_id"] == entry.edge_id:
                    low, high = max(entry.start_m,piece["edge_start_m"]), min(entry.end_m,piece["edge_end_m"])
                    if high > low:
                        observed.append({"start":registry_offset+low-entry.start_m,
                            "end":registry_offset+high-entry.start_m,
                            "occurrence":footprint.occurrence_id, "observed_only":True})
            if footprint.status in ("ambiguous", "uncertain", "failed"):
                continue
            for piece in footprint.pieces:
                if piece["edge_id"] != entry.edge_id:
                    continue
                if registry_ids.count(entry.edge_id) > 1:
                    decoded_ids = footprint.diagnostics.get("decoded_edges", [])
                    if decoded_ids != registry_ids or piece["path_edge_index"] != registry_index:
                        continue
                low = max(entry.start_m, piece["edge_start_m"])
                high = min(entry.end_m, piece["edge_end_m"])
                if high > low:
                    contributions.append(
                        {
                            "start": registry_offset + low - entry.start_m,
                            "end": registry_offset + high - entry.start_m,
                            "occurrence": footprint.occurrence_id,
                            "footprint_status": footprint.status,
                            "registry_edge_index": registry_index,
                            "path_edge_index": piece["path_edge_index"],
                        }
                    )
        registry_offset += entry.end_m - entry.start_m
    bounds = sorted({0.0, path.length_m} | {c[k] for c in contributions for k in ("start", "end")})
    intervals = []
    for start, end in zip(bounds, bounds[1:]):
        if end <= start:
            continue
        active = [c for c in contributions if c["start"] < end and c["end"] > start]
        ids = sorted({c["occurrence"] for c in active})
        if not ids:
            intervals.append(Interval(start_m=start, end_m=end, contributors=[], status="gap"))
            continue
        resolution=resolve_relative(ids,features,policy)
        relative,relative_status,relative_ids,relative_reasons=(resolution[k] for k in ('value','status','contributors','reasons'))
        absolute, absolute_status, absolute_ids, absolute_reasons = _measurement(
            resolution['selected'], features, "absolute_speed"
        )
        contexts = [(receipts or {}).get(i, {}) for i in ids]
        snapshots = {c.get("snapshot_id") for c in contexts}
        known_versions = {
            c.get("source_version") for c in contexts if c.get("source_version") is not None
        }
        context_conflict = len(snapshots) > 1 or len(known_versions) > 1
        directions = [
            (associations or {}).get(
                i, {"status": "unverified", "reasons": ["SOURCE_DIRECTION_METHOD_UNAPPROVED"]}
            )
            for i in ids
        ]
        direction_conflict = any(d["status"] == "conflict" for d in directions)
        closure_conflict = len({features[i].road_closure for i in ids}) > 1
        payload_conflict = relative_status == "conflict" or absolute_status == "conflict"
        # Time/version checks belong to each measurement's actual contributors.
        # All source contexts remain in audit, including discarded unity payloads.
        conflict = direction_conflict or closure_conflict
        overlap_class = "single"
        if len(ids) > 1:
            overlap_class = (
                "direction_conflict"
                if direction_conflict
                else "closure_conflict"
                if closure_conflict
                else "payload_conflict"
                if payload_conflict
                else "tile_duplicate"
                if len({features[i].resource_id for i in ids}) > 1
                else "same_snapshot_compatible_support"
            )
        # A shared OpenLR/speed alone never establishes duplicate support: this atom
        # already has explicit directed-edge, offset and footprint correspondence.
        # Unknown source direction stays unknown, rather than becoming a data conflict.
        source_status = (
            "conflict"
            if direction_conflict
            else "supported"
            if all(d["status"] == "supported" for d in directions)
            else "unverified"
        )
        reasons = []
        if conflict or payload_conflict:
            reasons.append("OVERLAP_CONFLICT")
        if context_conflict:
            reasons.append("RAW_SOURCE_CONTEXTS_DIFFER")
        if closure_conflict:
            reasons.append("CLOSURE_CONFLICT")
        if source_status != "supported":
            reasons.extend(r for d in directions for r in d.get("reasons", []))
        intervals.append(
            Interval(
                start_m=start,
                end_m=end,
                contributors=ids,
                status="conflict"
                if conflict
                else "accepted"
                if all(c["footprint_status"] == "matched" for c in active)
                else "provisional",
                current=absolute if not conflict else None,
                relative_speed=relative if not conflict else None,
                measurement_method=METHOD,
                relative_status=relative_status,
                absolute_status=absolute_status,
                relative_contributors=relative_ids,
                absolute_contributors=absolute_ids,
                relative_reasons=relative_reasons,
                absolute_reasons=absolute_reasons,
                relative_resolution_method=resolution['method'],
                relative_raw_values=resolution['values'],
                relative_raw_min=min(resolution['values']) if resolution['values'] else None,
                relative_raw_max=max(resolution['values']) if resolution['values'] else None,
                relative_spread=resolution['spread'],
                selected_contributors=resolution['selected'],
                congestion_priority_resolution=resolution['priority'],
                source_contexts={i:(receipts or {}).get(i,{}) for i in ids},
                closure=True
                if any(features[i].road_closure is True for i in ids)
                else False
                if all(features[i].road_closure is False for i in ids)
                else None,
                source_direction=source_status,
                overlap_class=overlap_class,
                footprint_status="matched"
                if all(c["footprint_status"] == "matched" for c in active)
                else "provisional",
                association_ids=[
                    d["association_id"] for d in directions if d.get("association_id")
                ],
                evidence_ids=sorted({e for d in directions for e in d.get("evidence_ids", [])}),
                reasons=sorted(set(reasons)),
            )
        )
    return intervals, contributions + observed


def compute_coverage(intervals: list[Interval], length_m: float, *, raw_support=()) -> dict:
    """Exact interval union, full registry denominator and explicit longest gap."""
    present = sum(i.end_m - i.start_m for i in intervals if i.contributors)
    if raw_support:
        present = length([(r["start"],r["end"]) for r in raw_support])
    geometry = sum(i.end_m - i.start_m for i in intervals if i.contributors and i.footprint_status == "matched")
    direction = sum(
        i.end_m - i.start_m
        for i in intervals
        if i.status not in ("gap", "conflict") and i.source_direction == "supported"
    )
    target = sum(i.end_m - i.start_m for i in intervals if i.target_valid)
    speed = sum(i.end_m - i.start_m for i in intervals if i.speed_valid)
    gaps = []
    for interval in intervals:
        if interval.status in ("gap", "conflict"):
            if gaps and abs(gaps[-1][1] - interval.start_m) < 1e-8:
                gaps[-1][1] = interval.end_m
            else:
                gaps.append([interval.start_m, interval.end_m])
    longest = max(gaps, key=lambda p: p[1] - p[0], default=[0, 0])
    return {
        "geometry_coverage": geometry / length_m,
        "geometry_present_coverage": present / length_m,
        "geometry_association_coverage": geometry / length_m,
        "source_direction_coverage": direction / length_m,
        "target_valid_coverage": target / length_m,
        "absolute_speed_coverage": speed / length_m,
        "longest_gap_m": longest[1] - longest[0],
        "longest_gap_interval": longest,
    }
