"""Portable capture identity; full resolved config hash remains provenance."""

from ..storage.files import digest

LEGACY_HASH = "resolved_config_v1"
CAPTURE_HASH = "capture_semantics_v2"


def capture_hash(plan, config):
    study = config.study.model_dump(mode="json")
    return digest(
        {
            "contract": CAPTURE_HASH,
            "study": {
                k: study[k]
                for k in (
                    "study_id",
                    "timezone",
                    "schedule_version",
                    "slots",
                    "dates",
                    "vehicle_scope",
                    "provisional",
                )
            },
            "collection_kind": plan.collection_kind,
            "policy": {
                k: getattr(config.policy, k)
                for k in (
                    "tile_zoom",
                    "tile_buffer_m",
                    "start_grace_s",
                )
            },
            "budget": config.budget.model_dump(mode="json"),
            "sources": {
                k: v
                for k, v in config.sources.model_dump(mode="json").items()
                if k not in ("documentation_urls", "ci_source_ready", "source_direction_verified")
            },
            "registry": plan.registry_sha256,
            "registry_version": plan.registry_version,
            "graph": plan.graph_version,
            "resources": [r.model_dump(mode="json") for r in plan.resources],
            "expected_attempts": plan.expected_attempts,
        }
    )


def plan_matches_config(plan, config):
    if plan.hash_contract == LEGACY_HASH:
        return plan.config_sha256 == config.hash
    if plan.hash_contract == CAPTURE_HASH:
        return plan.config_sha256 == capture_hash(plan, config)
    return False


def plan_document(plan):
    """Do not add a newly introduced default to historical plan documents."""
    value = plan.model_dump(mode="json")
    if "hash_contract" not in plan.model_fields_set:
        value.pop("hash_contract", None)
    return value
