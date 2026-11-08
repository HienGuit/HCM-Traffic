"""Single-snapshot functional baseline; automatic associations, no scientific approval."""

import argparse
import html
from pathlib import Path

from shapely.geometry import LineString
from shapely.ops import substring

from .config import load_config, ResolvedConfig
from .geo.crs import project
from .geo.linear import path_geometry
from .matching.decode import decode_location
from .matching.map_reader import OSMMapReader
from .matching.footprint import associate_footprint
from .matching.intervals import partition_registry, compute_coverage
from .matching.direction import automatic_direction, legal_direction, METHOD
from .preparation.plan import build_capture_plan
from .preparation.registry import read_paths
from .processing.inputs import load_inventory, network_off
from .processing.normalize import normalize_snapshot
from .processing.quality import assess_quality
from .processing.target import compute_target
from .processing.covariates import align_weather
from .reference.graph import load_graph
from .reference.static import static_features
from .storage.files import write_json, write_jsonl, write_csv, digest


def process_snapshot(inventory, path, graph, config):
    """Process only one candidate corridor; no sensitivity or full-tile code replay."""
    _, snapshots, quarantine, hashes = load_inventory(inventory)
    if len(snapshots) != 1:
        raise ValueError("Baseline requires exactly one snapshot")
    _, manifest, plan, selected = snapshots[0]
    if manifest.collection_kind != "pilot" or manifest.scheduled_at is not None:
        raise ValueError("Baseline requires a genuine unscheduled pilot")
    features, weather, receipts, errors = normalize_snapshot(manifest, plan, selected)
    corridor = path_geometry(graph, path.edges).buffer(config.policy.diagnostic_search_m)
    relevant = [
        f
        for f in features
        if f.openlr
        and any(project(LineString(c), graph.crs).intersects(corridor) for c in f.components)
    ]
    reader = OSMMapReader(graph)
    locations = {}
    hits = 0
    for code in sorted({f.openlr for f in relevant}):
        location, hit = decode_location(
            code, graph, "baseline", config.output("cache/locations"), reader
        )
        locations[code] = location
        hits += hit
    footprints, decisions, audit = [], {}, []
    for feature in relevant:
        location = locations[feature.openlr]
        footprint = associate_footprint(feature, location, graph, config.policy)
        decision = automatic_direction(feature, footprint, path, location, graph)
        # Baseline accepts algorithmically unambiguous support, without claiming calibration.
        if decision["status"] == "supported":
            footprint = footprint.model_copy(update={"status": "matched"})
        footprints.append(footprint)
        decisions[feature.occurrence_id] = decision
        audit.append(
            {
                "occurrence_id": feature.occurrence_id,
                "openlr": feature.openlr,
                "direction": decision,
                "footprint": footprint.model_dump(mode="json"),
            }
        )
    intervals, _ = partition_registry(
        footprints, path, {f.occurrence_id: f for f in features}, graph, decisions, receipts
    )
    quality = assess_quality(
        intervals,
        path,
        receipts,
        config.policy,
        legal=legal_direction(path, graph),
    )
    target = compute_target(
        intervals,
        contract_ready=True,
        whole_path_length=path.length_m,
        minimum_coverage=config.policy.minimum_coverage,
        quality_reasons=quality["reasons"],
    )
    weather_row = align_weather(weather, path.segment_id, manifest.started_at, config.policy)
    blockers = list(target["reasons"]) + weather_row["reasons"]
    if errors or quarantine or manifest.status != "complete":
        blockers.append("RAW_OR_CAPTURE_INCOMPLETE")
    if blockers:
        target["ci"] = None
    row = {
        "segment_id": path.segment_id,
        "direction_id": path.direction_id,
        "snapshot_id": manifest.snapshot_id,
        "collection_kind": "pilot",
        "scheduled_at": None,
        "observed_at": manifest.started_at.isoformat(),
        "capture_finished_at": manifest.finished_at.isoformat(),
        "segment_length_m": path.length_m,
        "method": METHOD,
        "target_method": target["target_method"],
        "ci": target["ci"],
        **compute_coverage(intervals, path.length_m),
        "precipitation_mm": weather_row.get("precipitation_mm"),
        "weather_code": weather_row.get("weather_code"),
        "weather_valid_at": weather_row.get("valid_at"),
        "precipitation_window_start": weather_row.get("window_start"),
        "precipitation_window_end": weather_row.get("window_end"),
        "precipitation_interval_seconds": weather_row.get("interval_seconds"),
        "candidate_approved": False,
        "scientific_release": False,
        "calibration_validated": False,
        "legal_method": "existing_directed_osm_tags",
        "quality_reasons": sorted(set(blockers)),
        **static_features(path, graph, config.policy),
    }
    interval_rows = [
        {
            **i.model_dump(mode="json"),
            "length_m": i.end_m - i.start_m,
            "direction_id": path.direction_id,
            "method": METHOD,
        }
        for i in intervals
    ]
    return {
        "observation": row,
        "intervals": interval_rows,
        "associations": audit,
        "decoded": [v.model_dump(mode="json") for v in locations.values()],
        "input_hashes": hashes,
        "weather": weather_row,
        "errors": errors,
        "quarantine": quarantine,
        "decoder_cache_hits": hits,
        "unique_corridor_codes": len(locations),
        "http_attempts": len(manifest.attempts),
    }


def export_result(result, folder, path, graph):
    write_csv(folder / "observation.csv", [result["observation"]])
    write_csv(folder / "intervals.csv", result["intervals"])
    write_jsonl(folder / "associations.jsonl", result["associations"])
    write_jsonl(folder / "decoded.jsonl", result["decoded"])
    write_json(folder / "result.json", result)
    line = path_geometry(graph, path.edges)
    x0, y0, x1, y1 = line.bounds
    scale = min(700 / max(x1 - x0, 1), 700 / max(y1 - y0, 1))

    def points(coords):
        return " ".join(
            f"{50 + (x - x0) * scale:.2f},{750 - (y - y0) * scale:.2f}" for x, y in coords
        )

    shapes = [
        f'<polyline points="{points(line.coords)}" stroke="#bbb" stroke-width="9" fill="none"/>'
    ]
    for i in result["intervals"]:
        segment = substring(line, i["start_m"], i["end_m"])
        color = "#16804a" if i["target_valid"] else "#d33"
        if segment.geom_type == "LineString":
            shapes.append(
                f'<polyline points="{points(segment.coords)}" stroke="{color}" stroke-width="5" fill="none"><title>{html.escape(str(i))}</title></polyline>'
            )
    for distance in (line.length * 0.2, line.length * 0.5, line.length * 0.8):
        a, b = line.interpolate(distance), line.interpolate(min(line.length, distance + 20))
        shapes.append(
            f'<polyline points="{points([a.coords[0], b.coords[0]])}" stroke="#123" stroke-width="3" marker-end="url(#arrow)"/>'
        )
    body = (
        f'<html><meta charset="utf-8"><h1>Pilot baseline: {html.escape(path.direction_id)}</h1><p>Green: target support; red: gap/rejected. Arrows follow ordered directed candidate edges. Not reviewed scientific evidence.</p><svg viewBox="0 0 800 800" style="max-height:85vh"><defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L0,6 L8,3 z" fill="#123"/></marker></defs>'
        + "".join(shapes)
        + "</svg></html>"
    )
    (folder / "map.html").write_text(body, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("config", "graph", "paths", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--allow-online", action="store_true")
    args = parser.parse_args()
    config = load_config(args.config)
    output = args.output.resolve()
    from .storage.paths import safe_output

    safe_output(
        output, config.study.project_files_root, (config.study.project_root, config.study.osm_pbf)
    )
    output.mkdir(parents=True, exist_ok=False)
    if not config.study.provisional:
        raise ValueError("Baseline must remain provisional")
    paths = read_paths(args.paths)
    if len(paths) != 1:
        raise ValueError("Baseline requires one directed path")
    graph = load_graph(args.graph)
    inventory = args.inventory
    if inventory is None:
        from .capture.preflight import preflight
        from .capture.scheduler import capture_command

        config = ResolvedConfig(
            config.study,
            config.policy,
            config.sources,
            config.budget.model_copy(update={"pilot_snapshots": 1}),
        )
        plan = build_capture_plan(paths, graph, config)
        ready = preflight(plan, config, args.allow_online)
        write_json(output / "preflight.json", ready)
        if not ready["ready"]:
            raise ValueError("Preflight: " + str(ready["reasons"]))
        capture = capture_command(plan, config, "once")
        write_json(output / "capture.json", capture)
        inventory = Path(capture["inventory"])
    with network_off():
        result = process_snapshot(inventory, paths[0], graph, config)
        # Exactly one offline repeat on this snapshot, never the prior 97-code inventory.
        repeated = process_snapshot(inventory, paths[0], graph, config)

    def semantics(r):
        return digest({k: v for k, v in r.items() if k != "decoder_cache_hits"})

    equal = semantics(result) == semantics(repeated)
    write_json(
        output / "replay_check.json",
        {
            "equal": equal,
            "inventory": str(inventory),
            "semantic_sha256": semantics(result),
            "offline_repeat_count": 1,
            "first_cache_hits": result["decoder_cache_hits"],
            "repeat_cache_hits": repeated["decoder_cache_hits"],
        },
    )
    if not equal:
        raise ValueError("Offline semantic replay differs")
    export_result(result, output, paths[0], graph)
    print(
        {
            "output": str(output),
            "ci": result["observation"]["ci"],
            "coverage": result["observation"]["target_valid_coverage"],
            "http_attempts": result["http_attempts"],
        }
    )


if __name__ == "__main__":
    main()
