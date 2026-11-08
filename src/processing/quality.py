"""Common topology/closure checks and independent contributor metadata gates."""
from datetime import datetime
import math
from .target import valid_relative


def _context(identities, receipts, limit):
    times=[receipts[i] for i in identities if i in receipts]
    reasons=[]
    spread=(max(datetime.fromisoformat(t["received_at"]) for t in times)
        -min(datetime.fromisoformat(t["requested_at"]) for t in times)).total_seconds() if times and len(times)==len(identities) else None
    if identities and (spread is None or spread>limit): reasons.append("TRAFFIC_TIME_MISALIGNED")
    versions={t.get("source_version") for t in times if t.get("source_version") is not None}
    snapshots={t.get("snapshot_id") for t in times if t.get("snapshot_id") is not None}
    if len(versions)>1: reasons.append("SOURCE_VERSION_CONFLICT")
    if len(snapshots)>1: reasons.append("SOURCE_CONTEXT_CONFLICT")
    status="conflict" if len(versions)>1 or len(snapshots)>1 else "known" if times and all(t.get("source_version") is not None for t in times) else "unknown"
    return {"reasons":reasons,"spread_s":spread,"version_status":status}


def assess_quality(intervals,path,receipts,policy,coverage=None,*,legal=None):
    legal=legal or {"status":"unverified","evidence_ids":[],"reasons":["LEGAL_UNKNOWN"]}
    common=[]
    for i in intervals:
        if not i.contributors: continue
        segments=[s for s in legal.get("segments",[]) if s["start_m"]<i.end_m and s["end_m"]>i.start_m]
        i.legal_direction=("supported" if all(s["status"]=="supported" for s in segments) else "unverified") if segments else legal["status"]
        i.reasons.extend(s["reason"] for s in segments if s.get("reason"))
        i.evidence_ids=sorted(set(i.evidence_ids+legal.get("evidence_ids",[])))
        if i.legal_direction!="supported": i.reasons.append("LEGAL_UNKNOWN")
        if i.source_direction!="supported": i.reasons.append("DIRECTION_UNVERIFIED")
        rel_ids=i.contributors if i.relative_status=="legacy" else i.relative_contributors
        abs_ids=i.contributors if i.absolute_status=="legacy" else i.absolute_contributors
        rc=_context(rel_ids,receipts,policy.traffic_spread_s)
        ac=_context(abs_ids,receipts,policy.traffic_spread_s)
        i.relative_time_aligned=bool(rel_ids) and "TRAFFIC_TIME_MISALIGNED" not in rc["reasons"]
        i.absolute_time_aligned=bool(abs_ids) and "TRAFFIC_TIME_MISALIGNED" not in ac["reasons"]
        i.relative_context_status=rc["version_status"];i.absolute_context_status=ac["version_status"]
        i.relative_reasons=sorted(set(i.relative_reasons+rc["reasons"]))
        i.absolute_reasons=sorted(set(i.absolute_reasons+ac["reasons"]))
        i.time_aligned=i.relative_time_aligned or i.absolute_time_aligned
        physical=(i.status=="accepted" and i.footprint_status=="matched" and i.source_direction=="supported" and i.legal_direction=="supported" and i.closure is False)
        i.target_valid=physical and i.relative_status in ("legacy","consistent","resolved") and valid_relative(i.relative_speed) and not rc["reasons"]
        i.speed_valid=physical and i.absolute_status in ("legacy","consistent") and i.current is not None and math.isfinite(i.current) and i.current>=0 and not ac["reasons"]
        i.reasons=sorted(set(i.reasons))
    ci_ids={identity for i in intervals if i.target_valid for identity in (i.contributors if i.relative_status=="legacy" else i.relative_contributors)}
    speed_ids={identity for i in intervals if i.speed_valid for identity in (i.contributors if i.absolute_status=="legacy" else i.absolute_contributors)}
    ci=_context(ci_ids,receipts,policy.traffic_spread_s);speed=_context(speed_ids,receipts,policy.traffic_spread_s)
    if any(i.closure is True and i.contributors for i in intervals): common.append("CLOSED")
    if not any(i.target_valid or i.speed_valid for i in intervals):
        if any(i.contributors for i in intervals) and not any(i.source_direction=="supported" for i in intervals): common.append("DIRECTION_UNVERIFIED")
        if legal["status"]!="supported": common.append("LEGAL_UNKNOWN")
    return {"reasons":sorted(set(common)),"ci_reasons":ci["reasons"],"speed_reasons":speed["reasons"],"traffic_spread_s":ci["spread_s"],"ci_spread_s":ci["spread_s"],"speed_spread_s":speed["spread_s"],"source_version_status":ci["version_status"],"speed_source_version_status":speed["version_status"]}
