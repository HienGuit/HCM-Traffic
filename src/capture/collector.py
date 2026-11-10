"""Bounded concurrent collection using one shared monotonic deadline."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
import threading
import uuid
import math
from ..domain.capture import SnapshotManifest, RequestAttempt
from ..storage.files import digest, write_json, sha256
from ..provenance import source_fingerprint
from .raw import store_attempt, finalize_snapshot
from .contract import plan_document

RETRYABLE_STATUS = {408,429,500,502,503,504}
RETRYABLE_ERRORS = {'ConnectError','ConnectTimeout','ReadError','ReadTimeout',
    'WriteError','WriteTimeout','PoolTimeout','RemoteProtocolError','NetworkError','TimeoutException'}

def _daily_progress(budget,provider):
    used=budget.usage(provider);cap=getattr(budget.policy,provider+'_daily_attempt_cap')
    return {'daily_usage':f'{used}/{cap}'}


def retry_after(value, now, default=1.0):
    try:
        numeric=float(value)
        return max(0,numeric) if math.isfinite(numeric) else default
    except (ValueError, TypeError):
        try:
            return max(0, (parsedate_to_datetime(value) - now).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return default


def capture_snapshot(
    plan, config, clock, budget, transport, *, scheduled_at=None, snapshot_id=None, progress=None
):
    snapshot_id = (
        snapshot_id
        or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    )
    folder = config.output(
        "raw/"
        + config.study.study_id
        + "/"
        + clock.now_utc().date().isoformat()
        + "/"
        + snapshot_id
    )
    folder.mkdir(parents=True, exist_ok=False)
    started = clock.now_utc()
    deadline = clock.monotonic() + config.budget.deadline_s
    manifest = SnapshotManifest(
        snapshot_id=snapshot_id,
        collection_kind="scheduled" if scheduled_at else plan.collection_kind,
        registry_version=plan.registry_version,
        schedule_version=config.study.schedule_version,
        scheduled_at=scheduled_at,
        started_at=started,
        status="collecting",
        plan_sha256=digest(plan_document(plan)),
        capture_config_sha256=config.hash,
        source_fingerprint=source_fingerprint(config.study.project_root),
        dependency_lock_sha256=sha256(config.study.project_root / "requirements.txt"),
    )
    write_json(folder / "collecting.json", manifest.model_dump(mode="json"), immutable=True)
    locks = {p: threading.Event() for p in ("traffic", "weather")}

    def worker(resource):
        attempts = []
        with budget.semaphores[resource.provider]:
            for number in range(1, config.budget.max_attempts + 1):
                if locks[resource.provider].is_set() or clock.monotonic() >= deadline:
                    break
                try:
                    reservation = budget.reserve(resource, snapshot_id, number, deadline)
                except RuntimeError as exc:
                    if progress:progress('Attempt not dispatched',provider=resource.provider,reason=str(exc),**_daily_progress(budget,resource.provider))
                    break
                requested = clock.now_utc()
                if progress:progress('HTTP attempt started',provider=resource.provider,attempt=f'{number}/{config.budget.max_attempts}',**_daily_progress(budget,resource.provider))
                response = transport.send(resource, min(config.budget.timeout_s,max(0.,deadline-clock.monotonic())), deadline)
                received = clock.now_utc()
                # Persist received body, including errors/empty HTTP responses, before completion state.
                body_path = checksum = None
                if response.status is not None or response.body:
                    body_path, checksum = store_attempt(
                        folder, resource, reservation, response.body, response.headers
                    )
                success = (
                    response.error is None
                    and response.status is not None
                    and 200 <= response.status < 300
                )
                outcome = "success" if success else response.error or f"HTTP_{response.status}"
                if progress:
                    progress('HTTP attempt',provider=resource.provider,attempt=f'{number}/{config.budget.max_attempts}',outcome=outcome)
                record = RequestAttempt(
                    reservation_id=reservation,
                    resource_id=resource.resource_id,
                    attempt_number=number,
                    requested_at=requested,
                    received_at=received,
                    http_status=response.status,
                    outcome=outcome,
                    body_path=body_path,
                    body_sha256=checksum,
                    body_bytes=len(response.body),
                    error_code=None if success else outcome,
                )
                write_json(
                    folder / f"events/{reservation}.json",
                    record.model_dump(mode="json"),
                    immutable=True,
                )
                budget.state.finish(reservation, outcome)
                attempts.append(record)
                body_lower = response.body[:8192].lower()
                exhausted = response.status in (402, 429) and any(
                    token in body_lower for token in (b"quota", b"exhaust", b"daily", b"credit")
                )
                exhausted |= (
                    response.headers.get("x-ratelimit-remaining") == "0" and response.status == 429
                )
                if response.status in (401, 403) or exhausted:
                    reason = "QUOTA_EXHAUSTED" if exhausted else "AUTH_FAILURE"
                    budget.state.stop(config.budget.scope, resource.provider, reason,stopped_at=clock.now_utc())
                    locks[resource.provider].set()
                    break
                retry = (
                    response.status not in (400,401,403)
                    and (response.error in RETRYABLE_ERRORS or response.status in RETRYABLE_STATUS)
                )
                provider_delay=None
                if response.status==429 and response.headers.get('retry-after'):
                    provider_delay=retry_after(response.headers['retry-after'],clock.now_utc(),default=1.0 if number==1 else 3.0)
                    try:expiry=clock.now_utc()+timedelta(seconds=provider_delay)
                    except OverflowError:expiry=None  # Preserve an unrepresentable provider wait, never shorten it.
                    budget.state.stop(config.budget.scope,resource.provider,'RATE_LIMIT_RETRY_AFTER',stopped_at=clock.now_utc(),expires_at=expiry)
                if success or not retry or number == config.budget.max_attempts:
                    break
                delay = provider_delay if provider_delay is not None else retry_after(response.headers.get("retry-after"), clock.now_utc(),default=1.0 if number==1 else 3.0)
                if progress:progress('Retry wait',provider=resource.provider,next_attempt=f'{number+1}/{config.budget.max_attempts}',reason=outcome,wait_s=delay,**_daily_progress(budget,resource.provider))
                if clock.monotonic() + delay >= deadline:
                    locks[resource.provider].set()
                    break
                clock.sleep(delay)
        return attempts

    try:
        with ThreadPoolExecutor(
            max_workers=config.budget.workers, thread_name_prefix="traffic-capture"
        ) as pool:
            futures = [pool.submit(worker, r) for r in plan.resources]
            for future in as_completed(futures):
                manifest.attempts.extend(future.result())
                if progress:
                    for provider in ('traffic','weather'):
                        done=len({a.resource_id for a in manifest.attempts if a.outcome=='success' and any(r.resource_id==a.resource_id and r.provider==provider for r in plan.resources)})
                        total=sum(r.provider==provider for r in plan.resources)
                        progress('Resource progress',provider=provider,success=f'{done}/{total}')
        manifest.attempts.sort(key=lambda a: a.reservation_id)
        successes = {
            a.resource_id for a in manifest.attempts if a.outcome == "success" and a.body_sha256
        }
        attempted = {a.resource_id for a in manifest.attempts}
        manifest.unattempted_resources = [
            r.resource_id for r in plan.resources if r.resource_id not in attempted
        ]
        manifest.status = "complete" if len(successes) == len(plan.resources) else "partial"
    except BaseException:
        manifest.status = "interrupted"
        raise
    finally:
        manifest.finished_at = clock.now_utc()
        finalize_snapshot(folder, manifest, plan)
        if progress:progress('Raw finalized',status=manifest.status,attempts=len(manifest.attempts),elapsed_s=round((clock.now_utc()-started).total_seconds(),2))
    return {
        "snapshot": str(folder),
        "status": manifest.status,
        "attempts": len(manifest.attempts),
        "started_at": started.isoformat(),
        "ledger_counts": budget.state.counts(config.budget.scope),
        "daily_counts":budget.state.daily_counts(clock.now_utc(),config.budget.accounting_timezone),
    }
