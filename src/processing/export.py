"""Deterministic offline artifacts and separate diagnostic/research publication."""

from collections import Counter
from ..storage.files import write_json, write_jsonl, write_csv, sha256, digest
from .dataset import FIELDS
from ..domain.processing import Observation


def semantic_rows(rows):
    return [{k: v for k, v in row.items() if k not in ("processing_run_id",)} for row in rows]


def technical_matching_ready(rows, policy):
    """Readiness of actual support, independent of registry approval metadata."""
    return any(
        all(
            row.get(key, 0) is not None and row.get(key, 0) >= policy.minimum_coverage
            for key in ("geometry_coverage", "accepted_direction_coverage", "target_valid_coverage")
        )
        for row in rows
    )


def export_run(folder, rows, audit, metadata, validation, config, *, update_pointer=True):
    scientific = False
    publication_gate = {"ready": False, "reasons": ["DIAGNOSTIC_OR_PROVISIONAL_SCOPE"]}
    if (
        validation["valid"]
        and not config.study.provisional
        and metadata.get("collection_kind") == "scheduled"
        and rows
        and any(r["ci_ready"] for r in rows)
    ):
        from ..preparation.gates import source_contract_readiness
        from ..preparation.registry import validate_published_registry
        from .inputs import load_inventory

        _, snapshots, quarantine, _ = load_inventory(
            __import__("pathlib").Path(metadata["inventory_path"])
        )
        source = source_contract_readiness(config)
        registry = (
            validate_published_registry(config.study.registry_artifact, snapshots[0][2], config)
            if snapshots
            else {"valid": False, "reasons": ["MISSING_CAPTURE"]}
        )
        scientific = bool(source["ready"] and registry["valid"] and not quarantine)
        publication_gate = {
            "ready": scientific,
            "reasons": source["reasons"]
            + registry["reasons"]
            + (["RAW_HASH_MISMATCH"] if quarantine else []),
        }
    from .features import export_feature_tables

    feature_tables = export_feature_tables(rows, audit, metadata, config)
    metadata["feature_tables"] = feature_tables
    write_csv(folder / "outputs/all_observations.csv", rows, FIELDS)
    for name in ("ci_ready", "model_ready", "needs_review"):
        write_csv(folder / f"outputs/{name}.csv", [r for r in rows if r[name]], FIELDS)
    for name, values in audit.items():
        write_jsonl(folder / f"audit/{name}.jsonl", values)
    summary = {
        "feature_tables": feature_tables,
        "calibration_status": "evaluated"
        if [*rows,*audit["pilot_observations"]] and all(r.get("calibration_status")=="evaluated" for r in [*rows,*audit["pilot_observations"]])
        else "not_evaluated",
        "canonical_rows": len(rows),
        "pilot_diagnostic_rows": len(audit["pilot_observations"]),
        "pilot_ci_ready": sum(r.get("ci") is not None for r in audit["pilot_observations"]),
        "registry_review_ready": metadata.get("registry_gate", {}).get("valid", False),
        "ci_ready": sum(r["ci_ready"] for r in rows),
        "model_ready": sum(r["model_ready"] for r in rows),
        "needs_review": sum(r["needs_review"] for r in rows),
        "reason_counts": dict(Counter(reason for row in rows for reason in row["quality_reasons"])),
        "pilot_reason_counts": dict(
            Counter(
                reason for row in audit["pilot_observations"] for reason in row["quality_reasons"]
            )
        ),
        "decoded_status_counts": dict(
            Counter(row["profile"] + ":" + row["status"] for row in audit["decoded_locations"])
        ),
        "footprint_status_counts": dict(
            Counter(row["status"] for row in audit["feature_footprints"])
        ),
        "validation": validation,
        "scientific_release": scientific,
        "publication_gate": publication_gate,
        "readiness": {
            "tile_capture_ready": metadata.get("valid_traffic_resources", 0) > 0,
            "matching_ready": technical_matching_ready(
                [*rows, *audit["pilot_observations"]], config.policy
            ),
            "ci_source_ready": metadata.get("source_contract_gate", {}).get("ready", False),
        },
    }
    write_json(folder / "reports/summary.json", summary)
    write_csv(
        folder / "reports/data_dictionary.csv",
        [
            {
                "field": field,
                "type": str(model.annotation),
                "required": model.is_required(),
                "units": "km/h"
                if "speed_kmh" in field
                else "mm"
                if field == "precipitation_mm"
                else "m"
                if field.endswith("length_m")
                else "seconds"
                if field.endswith("seconds")
                else "ratio"
                if "coverage" in field
                else "dimensionless"
                if field in ("ci", "ci_raw")
                else "category_or_metadata",
            }
            for field, model in Observation.model_fields.items()
        ],
    )
    write_json(folder / "reports/observation.schema.json", Observation.model_json_schema())
    write_json(folder / "resolved_config.json", config.as_dict())
    checksums = {
        str(p.relative_to(folder)).replace("\\", "/"): sha256(p)
        for p in folder.rglob("*")
        if p.is_file()
    }
    manifest = {
        **metadata,
        "validation": validation,
        "status": "passed" if validation["valid"] else "failed",
        "semantic_sha256": digest(
            {"rows": semantic_rows(rows), "pilot": audit["pilot_observations"]}
        ),
        "checksums": checksums,
        "scientific_release": scientific,
    }
    write_json(folder / "manifest.json", manifest, immutable=True)
    if validation["valid"] and update_pointer:
        # A diagnostic run may pass invariants while all scientific gates remain closed.
        write_json(
            config.output("processed/diagnostic_current.json"),
            {"path": str(folder), "fingerprint": metadata["fingerprint"]},
        )
        if scientific:
            write_json(
                config.output("processed/scientific_current.json"),
                {
                    "path": str(folder),
                    "fingerprint": metadata["fingerprint"],
                    "target_method": metadata["target_method"],
                    "schema_version": metadata["output_schema"],
                },
            )
    return {
        "run": str(folder),
        "status": manifest["status"],
        "summary": summary,
        "semantic_sha256": manifest["semantic_sha256"],
    }
