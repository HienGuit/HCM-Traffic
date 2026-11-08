"""Candidate-pinned pilot planning and bounded capture using existing collector/ETL."""

from pathlib import Path
from datetime import datetime
import sqlite3
import yaml
import portalocker
from ..config import ResolvedConfig
from ..storage.files import read_json, write_json, sha256, digest, atomic_bytes
from ..storage.paths import safe_output
from ..storage.state import State
from ..preparation.registry import read_paths
from ..preparation.plan import build_capture_plan
from .preflight import preflight


def prepare_pilot(candidate_folder, pilot_id, graph, config, allow_online=False):
    if Path(pilot_id).name != pilot_id or pilot_id in (".", ".."):
        raise ValueError("Plain pilot_id required")
    candidate_folder = candidate_folder.resolve()
    manifest = read_json(candidate_folder / "manifest.json")
    for name, checksum in manifest["artifacts"].items():
        item = (candidate_folder / name).resolve()
        if not item.is_relative_to(candidate_folder) or sha256(item) != checksum:
            raise ValueError("Candidate artifact hash mismatch")
    if manifest["graph_version"] != graph.version:
        raise ValueError("Candidate graph mismatch")
    root = config.study.pilot_output_root or config.study.project_files_root / "pilots"
    folder = safe_output(root / pilot_id, root, (config.study.project_root, config.study.osm_pbf))
    folder.mkdir(parents=True, exist_ok=False)
    study = config.study.model_copy(update={"pilot_run_root": folder, "provisional": True})
    pilot = ResolvedConfig(study, config.policy, config.sources, config.budget)
    paths = [
        p.model_copy(update={"registry_version": "pilot_" + manifest["candidate_version"]})
        for p in read_paths(candidate_folder / "shortlist.json")
    ]
    registry = pilot.output("registry/paths.json")
    write_json(registry, [p.model_dump(mode="json") for p in paths], immutable=True)
    plan = build_capture_plan(paths, graph, pilot)
    plan_path = pilot.output("plans/capture_plan.json")
    write_json(plan_path, plan.model_dump(mode="json"), immutable=True)
    state = State(config.output("state/capture.sqlite"))
    ready = preflight(plan, pilot, allow_online, state=state)
    traffic = sum(r.provider == "traffic" for r in plan.resources)
    weather = sum(r.provider == "weather" for r in plan.resources)
    report = {
        "pilot_id": pilot_id,
        "pilot_root": str(folder),
        "candidate_version": manifest["candidate_version"],
        "candidate_folder": str(candidate_folder),
        "candidate_manifest_sha256": sha256(candidate_folder / "manifest.json"),
        "physical_segments": manifest["physical_segments"],
        "segment_directions": len(paths),
        "traffic_tiles": traffic,
        "weather_requests": weather,
        "max_attempts_per_snapshot": plan.expected_attempts,
        "max_campaign_attempts": plan.expected_attempts * config.budget.pilot_snapshots,
        "snapshots_requested": config.budget.pilot_snapshots,
        "minimum_start_spacing_s": config.budget.pilot_spacing_s,
        "minimum_campaign_duration_s": (config.budget.pilot_snapshots - 1)
        * config.budget.pilot_spacing_s,
        "snapshot_deadline_s": config.budget.deadline_s,
        "preflight": ready,
        "shared_cumulative_ledger": str(state.path),
        "registry": str(registry),
        "plan": str(plan_path),
        "partial_candidates": manifest["partial"],
        "online_attempted": False,
    }
    write_json(folder / "pilot_manifest.json", report, immutable=True)
    write_json(folder / "preflight.json", ready)
    settings = pilot.output("settings/study.yaml")
    atomic_bytes(
        settings,
        yaml.safe_dump(study.model_dump(mode="json"), allow_unicode=True, sort_keys=False).encode(
            "utf-8"
        ),
    )
    return pilot, paths, plan, state, report


def backup_cumulative_ledger(state, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with state.connect() as source, sqlite3.connect(destination) as target:
        source.backup(target)


def recover_pilot_snapshots(pilot, plan):
    """Read only finalized raw in this pinned pilot; never repeat a completed capture."""
    from ..domain.capture import SnapshotManifest

    snapshots = []
    for path in pilot.output("raw").glob("*/*/*/manifest.json"):
        manifest = SnapshotManifest.model_validate(read_json(path))
        if (
            manifest.plan_sha256 != digest(plan.model_dump(mode="json"))
            or manifest.capture_config_sha256 != pilot.hash
        ):
            raise ValueError("Resume raw does not match pinned plan/config")
        if manifest.collection_kind != "pilot" or manifest.scheduled_at is not None:
            raise ValueError("Resume requires actual pilot raw")
        snapshots.append(
            {
                "snapshot": str(path.parent),
                "status": manifest.status,
                "attempts": len(manifest.attempts),
                "started_at": manifest.started_at.isoformat(),
            }
        )
    snapshots.sort(key=lambda r: (r["started_at"], r["snapshot"]))
    if len(snapshots) > pilot.budget.pilot_snapshots:
        raise ValueError("Existing snapshots exceed this campaign")
    return snapshots


def run_pilot(
    candidate_folder, pilot_id, graph, config, allow_online=False, execute=False, resume=False
):
    recovered = []
    if resume:
        from ..domain.capture import CapturePlan

        root = config.study.pilot_output_root or config.study.project_files_root / "pilots"
        folder = safe_output(
            root / pilot_id, root, (config.study.project_root, config.study.osm_pbf)
        )
        report = read_json(folder / "pilot_manifest.json")
        if report["candidate_folder"] != str(candidate_folder.resolve()) or report[
            "candidate_manifest_sha256"
        ] != sha256(candidate_folder / "manifest.json"):
            raise ValueError("Resume candidate binding changed")
        study = config.study.model_copy(update={"pilot_run_root": folder, "provisional": True})
        pilot = ResolvedConfig(study, config.policy, config.sources, config.budget)
        paths = read_paths(Path(report["registry"]))
        plan = CapturePlan.model_validate(read_json(Path(report["plan"])))
        if (
            plan.config_sha256 != pilot.hash
            or plan.graph_version != graph.version
            or plan.registry_sha256 != digest([p.model_dump(mode="json") for p in paths])
        ):
            raise ValueError("Resume plan/config/graph mismatch")
        state = State(config.output("state/capture.sqlite"))
        recovered = recover_pilot_snapshots(pilot, plan)
    else:
        pilot, paths, plan, state, report = prepare_pilot(
            candidate_folder, pilot_id, graph, config, allow_online
        )
    if not execute or (not resume and not report["preflight"]["ready"]):
        return {"ready": report["preflight"]["ready"], **report}
    if not allow_online:
        raise ValueError("Explicit online flag required to continue campaign")
    from .scheduler import Clock
    from .collector import capture_snapshot
    from .budget import BudgetController
    from .transport import Transport
    from ..processing.pipeline import process_inventory

    clock = Clock()
    results = []
    inventories = []
    last_start = None

    # Reservations always use the pre-existing cumulative ledger, never a fresh quota counter.
    # A consistent backup in pilot/state is retained for this pilot's provenance and recovery.
    def backup_ledger():
        backup_cumulative_ledger(state, pilot.output("state/capture.sqlite"))

    with portalocker.Lock(config.output("state/capture.lock"), timeout=0):
        budget = BudgetController(state, pilot.budget, clock)
        transport = Transport(pilot.sources, clock)
        try:
            for index in range(pilot.budget.pilot_snapshots):
                if index < len(recovered):
                    snapshot = recovered[index]
                    last_start = (
                        clock.monotonic()
                        - (
                            clock.now_utc() - datetime.fromisoformat(snapshot["started_at"])
                        ).total_seconds()
                    )
                else:
                    if last_start is not None:
                        while clock.monotonic() < last_start + pilot.budget.pilot_spacing_s:
                            clock.sleep(
                                min(
                                    1, last_start + pilot.budget.pilot_spacing_s - clock.monotonic()
                                )
                            )
                    ready = preflight(plan, pilot, True, state=state)
                    if not ready["ready"]:
                        write_json(pilot.output("logs/stop_preflight.json"), ready)
                        break
                    last_start = clock.monotonic()
                    snapshot = capture_snapshot(plan, pilot, clock, budget, transport)
                backup_ledger()
                inventory = pilot.output("raw/inventory_" + str(index + 1) + ".json")
                inventory_value = {
                    "collection_kind": "pilot",
                    "snapshots": [snapshot],
                    "config_hash": pilot.hash,
                }
                if not inventory.exists():
                    write_json(inventory, inventory_value, immutable=True)
                elif read_json(inventory)["snapshots"][0]["snapshot"] != snapshot["snapshot"]:
                    raise ValueError("Existing inventory does not match recovered snapshot")
                inventories.append(str(inventory))
                transformed = process_inventory(inventory, paths, graph, pilot, force=True)
                results.append({"snapshot": snapshot, "transform": transformed})
                write_json(pilot.output("processed/pilot_progress.json"), results)
                if snapshot["status"] != "complete" or transformed["status"] != "passed":
                    break
                errors = Path(transformed["run"]) / "audit/errors.jsonl"
                if errors.stat().st_size:
                    break
        finally:
            transport.close()
            backup_ledger()
    report.update(
        online_attempted=True,
        results=results,
        inventories=inventories,
        ledger_counts=state.counts(pilot.budget.scope),
        snapshots_completed=len(results),
        ready=bool(results),
    )
    write_json(pilot.output("processed/pilot_result.json"), report)
    if results:
        write_json(
            pilot.output("raw/inventory_campaign.json"),
            {
                "collection_kind": "pilot",
                "snapshots": [r["snapshot"] for r in results],
                "config_hash": pilot.hash,
            },
            immutable=True,
        )
        quality = []
        previous = {}
        from ..storage.files import write_csv

        for result in results:
            folder = Path(result["transform"]["run"])
            observations = [
                __import__("json").loads(s)
                for s in (folder / "audit/pilot_observations.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            for row in observations:
                key = (row["segment_id"], row["direction_id"])
                intervals = [
                    __import__("json").loads(s)
                    for s in (folder / "audit/traffic_intervals.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ]
                features = {
                    f["occurrence_id"]: f
                    for f in [
                        __import__("json").loads(line)
                        for line in (folder / "audit/feature_occurrences.jsonl")
                        .read_text(encoding="utf-8")
                        .splitlines()
                    ]
                }
                signature = digest(
                    [
                        {
                            "start_m": i["start_m"],
                            "end_m": i["end_m"],
                            "status": i["status"],
                            "source_support": sorted(
                                digest(
                                    {
                                        k: features[c].get(k)
                                        for k in (
                                            "openlr",
                                            "resource_id",
                                            "components",
                                            "source_feature_id",
                                        )
                                    }
                                )
                                for c in i["contributors"]
                            ),
                        }
                        for i in intervals
                        if (i["segment_id"], i["direction_id"]) == key
                    ]
                )
                quality.append(
                    {
                        **row,
                        "assignment_signature": signature,
                        "same_assignment_as_previous": None
                        if key not in previous
                        else previous[key] == signature,
                    }
                )
                previous[key] = signature
        fields = sorted({k for row in quality for k in row})
        write_csv(pilot.output("processed/quality.csv"), quality, fields)
    return report
