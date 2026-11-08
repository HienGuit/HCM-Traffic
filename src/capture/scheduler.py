"""Timezone-aware five-slot schedule, unique claims and explicit pilot runs."""

from datetime import datetime, date, time, timezone
from zoneinfo import ZoneInfo
import time as timer
import uuid
import portalocker
from ..storage.files import write_json
from ..storage.state import State
from .budget import BudgetController
from .collector import capture_snapshot
from .transport import Transport
from .recovery import reconcile


class Clock:
    def now_utc(self):
        return datetime.now(timezone.utc)

    def monotonic(self):
        return timer.monotonic()

    def sleep(self, seconds):
        timer.sleep(max(0, seconds))

    def wait_until(self, instant):
        last=None
        while (remaining := (instant - self.now_utc()).total_seconds()) > 0:
            if hasattr(self,'progress') and (last is None or self.monotonic()-last>=60):
                self.progress('Waiting',scheduled_at=instant.isoformat(),remaining_s=round(remaining));last=self.monotonic()
            self.sleep(min(remaining, 1))


def scheduled_slots(day, config):
    return [
        datetime.combine(day, time.fromisoformat(slot), ZoneInfo(config.study.timezone)).astimezone(
            timezone.utc
        )
        for slot in config.study.slots
    ]


def run_schedule(day, plan, config, clock, state, budget, transport, *, on_record=None, progress=None):
    inventory = []
    for index,scheduled in enumerate(scheduled_slots(day, config),1):
        if progress:progress('Slot waiting',slot=f'{index}/{len(config.study.slots)}',scheduled_at=scheduled.isoformat())
        clock.wait_until(scheduled)
        late = (clock.now_utc() - scheduled).total_seconds() > config.policy.start_grace_s
        snapshot = uuid.uuid4().hex
        claimed = state.claim(
            config.study.study_id,
            plan.registry_version,
            scheduled.isoformat(),
            snapshot,
            "missed" if late else "collecting",
        )
        if not claimed:
            with state.connect() as db:
                row = db.execute(
                    "SELECT * FROM slots WHERE study=? AND registry=? AND scheduled=?",
                    (config.study.study_id, plan.registry_version, scheduled.isoformat()),
                ).fetchone()
            record={
                    "scheduled_at": scheduled.isoformat(),
                    "snapshot_id": row["snapshot"],
                    "status": row["status"],
                }
            folder=config.output(f'raw/{config.study.study_id}/{day.isoformat()}/{row["snapshot"]}')
            if (folder/'manifest.json').is_file():record['snapshot']=str(folder)
            inventory.append(record)
            if on_record:on_record(record)
            continue
        if late:
            record={"scheduled_at": scheduled.isoformat(), "snapshot_id": None, "status": "missed"}
            inventory.append(record)
            if progress:progress('MISSED',slot=index)
            if on_record:on_record(record)
            continue
        if progress:progress('Slot capture started',slot=f'{index}/{len(config.study.slots)}',scheduled_at=scheduled.isoformat(),actual_start=clock.now_utc().isoformat())
        try:
            result = capture_snapshot(plan,config,clock,budget,transport,scheduled_at=scheduled,snapshot_id=snapshot,progress=progress)
        except Exception as exc:
            folder=config.output(f'raw/{config.study.study_id}/{day.isoformat()}/{snapshot}')
            result={'status':'interrupted','snapshot_id':snapshot,'error':type(exc).__name__}
            if (folder/'manifest.json').is_file():result['snapshot']=str(folder)
            if progress:progress.failure('Capture failed; continuing schedule',exc)
        with state.connect() as db:
            db.execute(
                "UPDATE slots SET status=? WHERE study=? AND registry=? AND scheduled=?",
                (
                    result["status"],
                    config.study.study_id,
                    plan.registry_version,
                    scheduled.isoformat(),
                ),
            )
        record={"scheduled_at": scheduled.isoformat(), **result};inventory.append(record)
        if on_record:on_record(record)
    return inventory


def capture_command(plan, config, action, day=None):
    state = State(config.output("state/capture.sqlite"))
    clock = Clock()
    lock = config.output("state/capture.lock")
    with portalocker.Lock(lock, timeout=0):
        reconcile(state, config)
        budget = BudgetController(state, config.budget, clock)
        transport = Transport(config.sources, clock)
        try:
            if action == "run":
                results = run_schedule(
                    date.fromisoformat(day), plan, config, clock, state, budget, transport
                )
            else:
                results = []
                for index in range(config.budget.pilot_snapshots if action == "pilot" else 1):
                    if index:
                        clock.sleep(config.budget.pilot_spacing_s)
                    result = capture_snapshot(plan, config, clock, budget, transport)
                    results.append(result)
                    if result["status"] != "complete":
                        break
            inventory = config.output("raw/inventory_" + uuid.uuid4().hex + ".json")
            write_json(
                inventory,
                {
                    "collection_kind": "scheduled" if action == "run" else "pilot",
                    "snapshots": results,
                    "config_hash": config.hash,
                },
                immutable=True,
            )
            return {
                "inventory": str(inventory),
                "snapshots": results,
                "ledger_counts": state.counts(config.budget.scope),
            }
        finally:
            transport.close()
