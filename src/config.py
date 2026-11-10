"""Strict YAML boundary, explicit roots and redacted resolved provenance."""

from pathlib import Path
from datetime import date
from typing import Literal
import yaml
from pydantic import Field, model_validator
from .domain.reference import Contract
from .storage.files import digest
from .storage.paths import distinct_roots, safe_output


class Policy(Contract):
    version: str
    relative_conflict_small_spread: float = Field(default=0.05, ge=0, le=1)
    relative_small_conflict_method: Literal["midrange"] = "midrange"
    relative_large_conflict_method: Literal["prefer_nonunity_pair"] = "prefer_nonunity_pair"
    minimum_coverage: float = Field(ge=0, le=1)
    lanes_coverage: float
    name_coverage: float
    road_type_dominance: float
    cluster_radius_m: float
    candidate_min_m: float
    candidate_max_m: float
    candidate_exception_min_m: float
    candidate_exception_max_m: float
    max_starts: int = Field(gt=0)
    beam_width: int = Field(gt=0)
    max_expansions: int = Field(gt=0)
    max_candidates: int = Field(gt=0)
    seed_grid_m: float
    max_shortlist: int
    max_overlap: float
    candidate_highways: list[str]
    run_sensitivity: bool = False
    matching_calibrated: bool
    projection_states: int
    projection_candidates: int
    diagnostic_search_m: float
    diagnostic_segment_step_m: float
    decoder_profiles: list[str]
    weather_age_s: int
    weather_future_s: int
    weather_grid_m: float
    traffic_spread_s: int
    start_grace_s: int
    tile_zoom: int = Field(ge=0, le=22)
    tile_buffer_m: float
    max_replay_codes: int | None
    projection_residual_limit_m: float | None = None
    projection_step_error_limit_m: float | None = None
    seed_strategy: str = "spatial_diverse_hash_v2"
    seed_salt: str = "hcm_graph_seed_v1"
    association_profiles: dict[str, dict] = {
        "baseline": {},
        "constrained": {
            "diagnostic_search_m": 30.0,
            "projection_candidates": 2,
            "projection_states": 4,
        },
    }


class Sources(Contract):
    version: str
    traffic_url: str
    api_version: str
    attributes: str
    traffic_product: str
    weather_url: str
    weather_current: str
    ci_source_ready: bool
    target_method: str
    source_direction_verified: bool
    documentation_urls: list[str] = []
    relative_speed_semantics: str = "fraction_of_freeflow"
    absolute_speed_unit: str = "km/h"
    source_direction_rule: str = "directed_openlr_occurrence_v2"
    closure_semantics: str = "presence_true_absent_false"

    @model_validator(mode="after")
    def baseline(self):
        if (
            self.api_version != "2"
            or "/maps/orbis/traffic/flow/vector/tile/" not in self.traffic_url
        ):
            raise ValueError("Only Orbis v2 source contract supported")
        from .domain.processing import TARGET_METHOD

        if self.target_method not in (TARGET_METHOD, "harmonic_paired_current_freeflow_v1"):
            raise ValueError("Unsupported target method; explicit migration required")
        if self.weather_current != "precipitation,weather_code" or "openlr" not in self.attributes:
            raise ValueError("Unexpected source variables")
        return self


class Budget(Contract):
    version: str
    traffic_daily_attempt_cap: int = Field(default=200, gt=0, strict=True)
    weather_daily_attempt_cap: int = Field(default=200, gt=0, strict=True)
    accounting_timezone: str = "Asia/Ho_Chi_Minh"
    authorized_dates: list[date] = []
    scope: str
    quota_mode: str
    traffic_remaining: int | None = Field(default=None, ge=0)
    weather_remaining: int | None = Field(default=None, ge=0)
    traffic_attempt_cap: int | None = Field(default=None, ge=0, description="Optional independent legacy cumulative ceiling, not the daily budget")
    weather_attempt_cap: int | None = Field(default=None, ge=0, description="Optional independent legacy cumulative ceiling, not the daily budget")
    rate_per_second: float = Field(gt=0)
    workers: int = Field(gt=0, le=4)
    traffic_workers: int = Field(gt=0, le=3)
    max_attempts: int = Field(default=3, gt=0, le=3, strict=True)
    timeout_s: float = Field(gt=0, le=10)
    deadline_s: float = Field(gt=0, le=120)
    pilot_snapshots: int = Field(gt=0, le=3)
    pilot_spacing_s: int = Field(ge=300)
    pilot_tiles: int = Field(gt=0)
    pilot_weather: int = Field(ge=0)

    @model_validator(mode='after')
    def valid_accounting_timezone(self):
        from zoneinfo import ZoneInfo
        ZoneInfo(self.accounting_timezone)
        return self


class Study(Contract):
    schema_version: str
    study_id: str
    project_root: Path
    data_root: Path
    qa_root: Path
    project_files_root: Path
    osm_pbf: Path
    policy_file: Path
    contracts_file: Path
    budget_file: Path
    bbox: tuple[float, float, float, float]
    graph_buffer_m: float = Field(gt=0)
    metric_crs: str
    timezone: str
    schedule_version: str
    slots: list[str]
    dates: list[date]
    provisional: bool
    vehicle_scope: str = "motor_vehicle"
    evidence_file: Path | None = None
    source_evidence_file: Path | None = None
    registry_artifact: Path | None = None
    candidate_policy_file: Path | None = None
    candidate_checks_root: Path | None = None
    pilot_output_root: Path | None = None
    pilot_run_root: Path | None = None
    cache_root: Path | None = None
    state_root: Path | None = None
    selected_paths_file: Path | None = None
    operational_selection_file: Path | None = None
    capture_plan_file: Path | None = None
    graph_cache: Path | None = None
    processing_root: Path | None = None
    archive_root: Path | None = None
    log_root: Path | None = None
    relocation_file: Path | None = None

    @model_validator(mode="after")
    def paths(self):
        roots = [self.project_root, self.data_root, self.qa_root, self.project_files_root]
        if not all(p.is_absolute() for p in roots):
            raise ValueError("All roots must be absolute")
        distinct_roots([self.data_root, self.qa_root, self.project_files_root])
        repo = self.project_root.resolve()
        for name, relative in (('data_root','data'),('qa_root','.local/qa'),('project_files_root','.local/project')):
            root = getattr(self,name).resolve()
            if root.is_relative_to(repo) and root != repo/relative:
                raise ValueError(f"Only repo/{relative} is permitted for local {name}")
        for name, relative in (
            ('cache_root','.local/cache'),('state_root','.local/state'),
            ('processing_root','data/processed'),('archive_root','.local/archive'),
            ('pilot_output_root','.local/pilots'),('pilot_run_root','.local/pilots'),
            ('candidate_checks_root','.local/project/candidate_checks'),('log_root','logs'),
        ):
            value=getattr(self,name)
            # Read legacy configurations while new defaults use .local/ operational roots.
            legacy = relative.replace('.local/','data/') if relative in ('.local/cache','.local/state','.local/archive','.local/pilots') else relative
            if value is not None and value.resolve().is_relative_to(repo) and not any(value.resolve().is_relative_to(repo/p) for p in (relative,legacy)):
                raise ValueError(f"Local {name} must stay within repo/{relative}")
        w, s, e, n = self.bbox
        if not (-180 <= w < e <= 180 and -85 < s < n < 85):
            raise ValueError("Invalid lon/lat bbox")
        from datetime import time
        from zoneinfo import ZoneInfo

        ZoneInfo(self.timezone)
        parsed = [time.fromisoformat(slot) for slot in self.slots]
        if len(set(parsed)) != len(parsed) or parsed != sorted(parsed):
            raise ValueError("Schedule must have unique ordered local times")
        return self


class ResolvedConfig:
    def __init__(self, study: Study, policy: Policy, sources: Sources, budget: Budget):
        self.study, self.policy, self.sources, self.budget = study, policy, sources, budget
        self.hash = digest(self.as_dict())

    def as_dict(self):
        return {
            k: getattr(self, k).model_dump(mode="json")
            for k in ("study", "policy", "sources", "budget")
        }

    def _protected(self, root):
        repo=self.study.project_root.resolve()
        if any(root.resolve().is_relative_to(repo/p) for p in ('data','logs','.local/project','.local/qa','.local/cache','.local/state','.local/archive','.local/pilots','.local/dev')):
            return tuple([self.study.osm_pbf]+[repo/p for p in ('.git','src','scripts','config','main.py','README.md','pyproject.toml','requirements.txt')])
        return (repo,self.study.osm_pbf)

    def output(self, relative: str) -> Path:
        """All runtime destinations pass through resolved write boundary."""
        parts = Path(relative).parts
        if (
            not parts
            or Path(relative).is_absolute()
            or ".." in parts
            or parts[0]
            not in {
                "settings",
                "reference",
                "cache",
                "registry",
                "static",
                "plans",
                "raw",
                "state",
                "processed",
                "interim",
                "logs",
                "dev",
            }
        ):
            raise ValueError("Unknown or escaping artifact category")
        root = self.study.data_root
        local = self.study.project_root / '.local' if root.resolve() == self.study.project_root.resolve()/'data' else root
        if parts[0] == "processed":
            root = self.study.processing_root or self.study.project_files_root / "processing" / self.study.study_id
            return safe_output(root.joinpath(*parts[1:]), root,
                self._protected(root))
        if parts[0] == "logs":
            root = self.study.log_root or self.study.project_root / "logs"
            return safe_output(root.joinpath(*parts[1:]),root,
                (self.study.osm_pbf,self.study.project_root/"src",self.study.project_root/"config"))
        if parts[0] == "settings":
            raise ValueError("Runtime settings belong to config/local, not data/settings")
        if parts[0] == "plans":
            relative = str(Path("reference/plans").joinpath(*parts[1:]))
        if parts[0] in ('registry','static'):
            relative = str(Path('reference').joinpath(*parts))
        if parts[:2] == ("cache", "graph"):
            # Graphs are versioned reference inputs; other caches remain disposable.
            relative = str(Path("reference/graph").joinpath(*parts[2:]))
            parts = Path(relative).parts
        if parts[0] == "cache":
            cache_root=self.study.cache_root or local/'cache'
            return safe_output(
                cache_root.joinpath(*parts[1:]),
                cache_root,
                self._protected(cache_root),
            )
        if parts[0] == "state":
            state_root=self.study.state_root or local/'state'
            return safe_output(
                state_root.joinpath(*parts[1:]),
                state_root,
                self._protected(state_root),
            )
        if parts[0] == 'dev':
            dev_root=local/'dev'
            return safe_output(dev_root.joinpath(*parts[1:]),dev_root,self._protected(dev_root))
        if self.study.pilot_run_root is not None and parts[0] not in ("cache", "reference"):
            root = self.study.pilot_run_root
        protected = self._protected(root)
        if root.resolve() == self.study.project_root.resolve() / "data":
            protected = (
                self.study.osm_pbf,
                self.study.project_root / "src",
                self.study.project_root / "config",
                self.study.project_root / "scripts",
            )
        return safe_output(
            root / relative,
            root,
            protected,
        )

    def candidate_output(self, version: str) -> Path:
        if Path(version).name != version or version in (".", ".."):
            raise ValueError("Candidate version must be a plain directory name")
        repo = self.study.project_root.resolve()
        root = repo / "data/reference/candidates"
        if root.resolve() != root:
            raise ValueError("Candidate destination aliases another location")
        return safe_output(
            root / version,
            root,
            (self.study.osm_pbf, repo / "src", repo / "config", repo / "scripts"),
        )


def load_config(path: Path) -> ResolvedConfig:
    """Resolve input paths relative to declaring YAML; PBF relative to data_root."""
    path = path.resolve()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for key in ('project_root','data_root','qa_root','project_files_root'):
        if data.get(key) is not None:
            value=Path(data[key]);data[key]=value if value.is_absolute() else (path.parent/value).resolve()
    for key in (
        "policy_file",
        "contracts_file",
        "budget_file",
        "evidence_file",
        "source_evidence_file",
        "registry_artifact",
        "candidate_policy_file",
        "candidate_checks_root",
        "pilot_output_root",
        "pilot_run_root",
        "cache_root",
        "state_root",
        "selected_paths_file",
        "operational_selection_file",
        "capture_plan_file",
        "graph_cache",
        "processing_root",
        "archive_root",
        "log_root",
        "relocation_file",
    ):
        if data.get(key) is None:
            continue
        value = Path(data[key])
        data[key] = value if value.is_absolute() else path.parent / value
    pbf = Path(data["osm_pbf"])
    data["osm_pbf"] = pbf if pbf.is_absolute() else Path(data["data_root"]) / pbf
    study = Study.model_validate(data)
    models = [
        cls.model_validate(yaml.safe_load(p.read_text(encoding="utf-8")))
        for cls, p in [
            (Policy, study.policy_file),
            (Sources, study.contracts_file),
            (Budget, study.budget_file),
        ]
    ]
    return ResolvedConfig(study, *models)
