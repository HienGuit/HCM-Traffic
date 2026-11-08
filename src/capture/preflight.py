"""Offline preflight does not probe any provider endpoint."""

import os
import shutil
import uuid
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
from ..storage.state import State


def preflight(plan, config, allow_online=False, *, state=None, day=None, clock=None, snapshot_count=None):
    reasons = []
    state = state or State(config.output("state/capture.sqlite"))
    now=clock.now_utc() if clock else datetime.now(timezone.utc)
    remaining_slots=len(config.study.slots) if plan.collection_kind=='scheduled' else (snapshot_count or 1)
    forecast=[now]*remaining_slots
    if day is not None:
        from .scheduler import scheduled_slots
        with state.connect() as db:
            claimed={r[0] for r in db.execute('SELECT scheduled FROM slots WHERE study=? AND registry=?',(config.study.study_id,plan.registry_version))}
        forecast=[max(now,t) for t in scheduled_slots(day,config) if t.isoformat() not in claimed and (now-t).total_seconds()<=config.policy.start_grace_s]
        remaining_slots=len(forecast)
        if remaining_slots and config.budget.authorized_dates and day not in config.budget.authorized_dates:
            reasons.append('BUDGET_NOT_AUTHORIZED_FOR_DATE')
        if remaining_slots and config.study.dates and day not in config.study.dates:
            reasons.append('STUDY_DATE_NOT_CONFIGURED')
    if remaining_slots and not allow_online:
        reasons.append("ONLINE_NOT_EXPLICIT")
    if remaining_slots and any(r.provider == "traffic" for r in plan.resources) and not os.environ.get(
        "TOMTOM_API_KEY"
    ):
        reasons.append("KEY_MISSING")
    if not plan.resources:
        reasons.append("EMPTY_CAPTURE_PLAN")
    if plan.config_sha256 != config.hash:
        reasons.append("CONFIG_HASH_MISMATCH")
    for resource in plan.resources:
        expected = (
            config.sources.traffic_url.format(
                z=resource.tile[0], x=resource.tile[1], y=resource.tile[2]
            )
            if resource.provider == "traffic" and resource.tile
            else config.sources.weather_url
        )
        if resource.url != expected:
            reasons.append("ENDPOINT_NOT_IN_CONTRACT")
    if not config.study.provisional:
        from ..preparation.registry import validate_published_registry

        registry = validate_published_registry(config.study.registry_artifact, plan, config)
        reasons.extend(registry["reasons"])
        from ..preparation.gates import source_contract_readiness

        source = source_contract_readiness(config)
        reasons.extend(source["reasons"])
    if config.study.operational_selection_file is not None:
        from ..storage.files import read_json, sha256, digest

        selection = read_json(config.study.operational_selection_file)
        if (
            config.study.selected_paths_file is None
            or sha256(config.study.selected_paths_file) != selection["artifacts"]["paths.json"]
            or digest(read_json(config.study.selected_paths_file)) != plan.registry_sha256
            or selection["graph_version"] != plan.graph_version
        ):
            reasons.append("OPERATIONAL_SELECTION_HASH_MISMATCH")
    if (
        plan.collection_kind == "scheduled"
        and config.study.provisional
        and config.study.operational_selection_file is None
    ):
        reasons.append("SCHEDULED_PLAN_REQUIRES_OFFICIAL_REGISTRY")
    if plan.collection_kind == "pilot":
        if sum(r.provider == "traffic" for r in plan.resources) > config.budget.pilot_tiles:
            reasons.append("PILOT_TILE_SCOPE_EXCEEDED")
        if sum(r.provider == "weather" for r in plan.resources) > config.budget.pilot_weather:
            reasons.append("PILOT_WEATHER_SCOPE_EXCEEDED")
    rate = config.budget.rate_per_second
    worst = (
        max(
            (
                sum(r.provider == p for r in plan.resources) * config.budget.max_attempts / rate
                for p in ("traffic", "weather")
            ),
            default=0,
        )
        + config.budget.timeout_s
        + sum((1.,3.)[:max(0,config.budget.max_attempts-1)])
    )
    if worst > config.budget.deadline_s:
        reasons.append("DEADLINE_INFEASIBLE")
    config.output("raw/guard")
    for category in ('raw','interim'):
        root=config.output(category);root.mkdir(parents=True,exist_ok=True)
        probe=root/('.preflight_probe_'+uuid.uuid4().hex)
        try:probe.write_bytes(b'ok')
        except OSError:reasons.append(category.upper()+'_NOT_WRITABLE')
        finally:
            if probe.exists():probe.unlink()
    for name in ('selected_paths_file','capture_plan_file','graph_cache'):
        value=getattr(config.study,name)
        if value is not None and not value.exists():reasons.append(name.upper()+'_MISSING')
    counts = state.counts()
    accounting_zone=ZoneInfo(config.budget.accounting_timezone)
    today=now.astimezone(accounting_zone).date()
    used_today=state.daily_counts(now,config.budget.accounting_timezone)
    forecast_days={instant.astimezone(accounting_zone).date() for instant in forecast}
    provider_budgets={}
    for provider in ("traffic", "weather"):
        count = sum(r.provider == provider for r in plan.resources)
        cap=getattr(config.budget,provider+'_daily_attempt_cap')
        by_day={}
        for accounting_day in sorted(forecast_days):
            slots=sum(t.astimezone(accounting_zone).date()==accounting_day for t in forecast)
            used=state.daily_counts(now,config.budget.accounting_timezone,day=accounting_day)[provider]
            required=count*slots*config.budget.max_attempts
            by_day[str(accounting_day)]={'used':used,'remaining':max(0,cap-used),'logical_requests':count*slots,'required_attempts_worst_case':required}
            if required>max(0,cap-used):reasons.append(provider.upper()+'_DAILY_BUDGET_INSUFFICIENT')
        independent={name:getattr(config.budget,provider+'_'+name) for name in ('attempt_cap','remaining') if getattr(config.budget,provider+'_'+name) is not None}
        if independent and counts.get(provider,0)+count*remaining_slots*config.budget.max_attempts>min(independent.values()):
            reasons.append(provider.upper()+'_INDEPENDENT_ACCOUNT_LIMIT_INSUFFICIENT')
        stopped=state.active_stop(provider,min(forecast) if forecast else now,config.budget.accounting_timezone)
        if remaining_slots and count and stopped:reasons.append(provider.upper()+'_STOPPED_'+stopped)
        provider_budgets[provider]={'daily_cap':cap,'used_today':used_today[provider],'remaining_today':max(0,cap-used_today[provider]),'resources_per_snapshot':count,'logical_requests_remaining':count*remaining_slots,'worst_case_attempts_remaining':count*remaining_slots*config.budget.max_attempts,'forecast_by_accounting_day':by_day,'independent_cumulative_limits':independent}
    if shutil.disk_usage(config.study.data_root).free < 1024**3:
        reasons.append("DISK_LOW")
    return {
        "ready": not reasons,
        "reasons": sorted(set(reasons)),
        "network_calls": 0,
        "worst_case_s": worst,
        "existing_attempts": state.counts(config.budget.scope),
        "quota_remaining_known": config.budget.traffic_remaining is not None,
        "quota_mode": config.budget.quota_mode,
        "remaining_capture_slots":remaining_slots,
        "accounting_timezone":config.budget.accounting_timezone,
        "accounting_day_today":str(today),
        "daily_usage_today":used_today,
        "providers":provider_budgets,
        "max_attempts_per_resource":config.budget.max_attempts,
        "time_estimate_kind":"rate/timeout/backoff estimate; not a guarantee of response latency",
    }
