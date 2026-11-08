"""Application composition root. Scientific algorithms live in stage modules."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import traceback
import uuid


def parser():
    p = argparse.ArgumentParser(
        prog="traffic-etl", description="Independent OSM/OpenLR traffic ETL"
    )
    p.add_argument("--config", type=Path)
    subs = p.add_subparsers(dest="command", required=True)
    for name in ('run','transform'):
        a=subs.add_parser(name)
        a.add_argument('--date')
        a.add_argument('--config',type=Path,default=argparse.SUPPRESS)
        if name=='run':a.add_argument('--allow-online',action='store_true')
    subs.add_parser("doctor")
    ref = subs.add_parser("reference")
    r = ref.add_subparsers(dest="action", required=True)
    r.add_parser("inspect")
    r.add_parser("extract")
    build = r.add_parser("build")
    build.add_argument("--extract", required=True, type=Path)
    cand = subs.add_parser("candidates")
    c = cand.add_subparsers(dest="action", required=True)
    for name in ("build", "review-export", "survey"):
        a = c.add_parser(name)
        a.add_argument("--graph", required=True, type=Path)
        if name == "review-export":
            a.add_argument("--paths", required=True, type=Path)
    pilot = subs.add_parser("pilot")
    ps = pilot.add_subparsers(dest="action", required=True)
    for action in ("plan", "run"):
        a = ps.add_parser(action)
        a.add_argument("--graph", required=True, type=Path)
        a.add_argument("--candidates", required=True, type=Path)
        a.add_argument("--pilot-id", required=True)
        a.add_argument("--resume", action="store_true")
        a.add_argument("--allow-online", action="store_true")
    reg = subs.add_parser("registry")
    rr = reg.add_subparsers(dest="action", required=True)
    for name in ("validate", "publish"):
        a = rr.add_parser(name)
        a.add_argument("--graph", required=True, type=Path)
        a.add_argument("--paths", required=True, type=Path)
        a.add_argument("--evidence", type=Path)
    plan = subs.add_parser("plan")
    plan.add_argument("--graph", required=True, type=Path)
    plan.add_argument("--paths", required=True, type=Path)
    capture = subs.add_parser("capture")
    cs = capture.add_subparsers(dest="action", required=True)
    for name in ("once", "run", "pilot", "dry-run"):
        a = cs.add_parser(name)
        a.add_argument("--plan", required=True, type=Path)
        a.add_argument("--allow-online", action="store_true")
        if name in ("run", "dry-run"):
            a.add_argument("--date", required=True)
    replay = subs.add_parser("replay")
    replay.add_argument("--graph", required=True, type=Path)
    replay.add_argument("--paths", required=True, type=Path)
    replay.add_argument("--inventory", required=True, type=Path)
    replay.add_argument("--network", required=True, choices=["off"])
    replay.add_argument("--force", action="store_true")
    diagnose = subs.add_parser("diagnose")
    diagnose.add_argument("--projection-cases", action="store_true")
    for field in ("graph", "paths", "inventory", "run"):
        diagnose.add_argument("--" + field, required=True, type=Path)
    for name in ("validate", "report"):
        a = subs.add_parser(name)
        a.add_argument("--run", required=True, type=Path)
    return p


def execute(args, config):
    from .storage.files import write_json, read_json
    if args.command in ('run','transform'):
        from zoneinfo import ZoneInfo
        from .workflow import run_day,transform_day
        day=args.date or datetime.now(ZoneInfo(config.study.timezone)).date().isoformat()
        if args.command=='run':return run_day(config,day,allow_online=args.allow_online)
        from .processing.inputs import network_off
        with network_off():return transform_day(config,day)

    if args.command == "doctor":
        import importlib.metadata
        from .preparation.gates import source_contract_readiness

        source_gate = source_contract_readiness(config)

        for root in (config.study.data_root, config.study.qa_root, config.study.project_files_root):
            root.mkdir(parents=True, exist_ok=True)
            probe = root / (".probe-" + uuid.uuid4().hex)
            probe.write_bytes(b"ok")
            probe.unlink()
        return {
            "status": "ok",
            "network": "off",
            "config_hash": config.hash,
            "pbf_exists": config.study.osm_pbf.is_file(),
            "versions": {
                n: importlib.metadata.version(n)
                for n in (
                    "hcm-traffic-etl",
                    "osmium",
                    "shapely",
                    "pyproj",
                    "openlr",
                    "openlr-dereferencer",
                )
            },
            "readiness": {
                "tile_capture_ready": False,
                "matching_ready": False,
                "ci_source_ready": source_gate["ready"],
            },
            "source_contract_gate": source_gate,
        }
    if args.command == "reference":
        if args.action == "inspect":
            from .reference.inspect import inspect_reference

            result = inspect_reference(config.study.osm_pbf).model_dump(mode="json")
            write_json(config.output("reference/source.json"), result)
            return result
        if args.action == "extract":
            from .reference.extract import extract_reference

            return {"extract": str(extract_reference(config))}
        from .reference.graph import build_reference

        return {"graph": str(build_reference(args.extract, config))}
    if args.command in ("candidates", "registry", "plan", "replay", "diagnose", "pilot"):
        from .reference.graph import load_graph

        graph = load_graph(args.graph)
    if args.command == "pilot":
        from .capture.pilot import run_pilot

        return run_pilot(
            args.candidates,
            args.pilot_id,
            graph,
            config,
            args.allow_online,
            args.action == "run",
            args.resume,
        )
    if args.command == "candidates":
        if args.action == "survey":
            from .preparation.survey import survey_candidates

            return survey_candidates(graph, config)
        from .preparation.assessment import assess_candidates, export_candidates

        if args.action == "build":
            from .preparation.candidates import generate_candidates

            paths, stats = generate_candidates(graph, config)
        else:
            from .preparation.registry import read_paths

            paths = read_paths(args.paths)
            stats = {"review_export": True}
        rows, static, shortlist = assess_candidates(paths, graph, config)
        folder = config.output(
            "plans/candidates_"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "_"
            + uuid.uuid4().hex[:6]
        )
        export_candidates(folder, paths, rows, static, shortlist, stats, config)
        return {
            "path": str(folder),
            "generated": len(paths),
            "static_pass": sum(r["static_pass"] for r in rows),
            "shortlisted": len(shortlist),
            "approved": 0,
            "stats": stats,
        }
    if args.command in ("registry", "plan", "replay", "diagnose"):
        from .preparation.registry import read_paths

        paths = read_paths(args.paths)
    if args.command == "registry":
        from .preparation.registry import validate_registry, publish_registry

        from .preparation.gates import read_evidence

        evidence = read_evidence(args.evidence)
        function = validate_registry if args.action == "validate" else publish_registry
        return function(paths, graph, evidence, config)
    if args.command == "plan":
        from .preparation.plan import build_capture_plan
        from .storage.files import digest

        plan = build_capture_plan(paths, graph, config)
        destination = config.output("plans/" + digest(plan.model_dump(mode="json"))[:20] + ".json")
        write_json(destination, plan.model_dump(mode="json"))
        return {
            "plan": str(destination),
            "resources": len(plan.resources),
            "max_attempts": plan.expected_attempts,
        }
    if args.command == "capture":
        from .domain.capture import CapturePlan
        from .capture.preflight import preflight
        from .capture.scheduler import capture_command

        plan = CapturePlan.model_validate(read_json(args.plan))
        from datetime import date
        ready = preflight(plan, config, args.allow_online,day=date.fromisoformat(args.date) if getattr(args,'date',None) else None,snapshot_count=config.budget.pilot_snapshots if args.action=='pilot' else None)
        if args.action == "dry-run":
            return {
                **ready,
                "collection_kind": plan.collection_kind,
                "date": args.date,
                "traffic_requests_per_slot": sum(r.provider == "traffic" for r in plan.resources),
                "weather_requests_per_slot": sum(r.provider == "weather" for r in plan.resources),
                "max_attempts_per_slot": plan.expected_attempts,
                "slots": config.study.slots,
            }
        if not ready["ready"]:
            return ready
        return capture_command(plan, config, args.action, getattr(args, "date", None))
    if args.command == "replay":
        from .processing.pipeline import process_inventory

        return process_inventory(args.inventory, paths, graph, config, force=args.force)
    if args.command == "diagnose":
        from .processing.diagnose import diagnose_run

        destination = config.output("processed/diagnosis_" + uuid.uuid4().hex[:12])
        return diagnose_run(
            args.run, paths, graph, config, destination, args.inventory, args.projection_cases
        )
    if args.command == "validate":
        from .processing.validate import validate_run

        return validate_run(args.run)
    if args.command == "report":
        return read_json(args.run / "reports/summary.json")
    raise ValueError("Unsupported command")


def main(argv=None):
    args = parser().parse_args(argv)
    if args.config is None:
        import os

        default = Path(
            os.environ.get(
                "TRAFFIC_ETL_CONFIG",
                Path(__file__).resolve().parents[1] / "config/local/study.yaml",
            )
        )
        if not default.is_file():
            parser().error("Supply --config or local config/local/study.yaml")
        args.config = default
    from .config import load_config

    config = None
    try:
        config = load_config(args.config)
        result = execute(args, config)
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 130 if result.get('status')=='interrupted' else 3 if result.get("valid") is False or result.get("ready") is False or result.get('status')=='failed' else 0
    except Exception as exc:
        # Offline errors retain full traceback; transports reduce secret-bearing errors to codes.
        if config:
            from .storage.files import atomic_bytes

            path = config.output("logs/error_" + uuid.uuid4().hex + ".log")
            atomic_bytes(path, traceback.format_exc().encode("utf-8"))
            print(json.dumps({"error": type(exc).__name__, "message": str(exc), "log": str(path)}))
        else:
            print(json.dumps({"error": type(exc).__name__, "message": str(exc)}))
        return 2
