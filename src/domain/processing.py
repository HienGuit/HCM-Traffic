"""Source occurrences and directed support are distinct from decoded locations."""

from pydantic import Field, AwareDatetime
from .reference import Contract

TARGET_METHOD = "length_weighted_relative_speed_v1"
OUTPUT_SCHEMA = "2.0-relative-ci"


class FeatureOccurrence(Contract):
    occurrence_id: str
    resource_id: str
    layer: str
    feature_index: int
    source_feature_id: str | int | None = None
    components: list[list[tuple[float, float]]]
    tags: dict
    openlr: str | None
    absolute_speed: float | None = None
    relative_speed: float | None = None
    road_closure: bool | None = None
    quantization_m: float = 0.0
    reasons: list[str] = Field(default_factory=list)
    invalid_fields: dict[str, str] = Field(default_factory=dict)


class DecodedLocation(Contract):
    code: str
    graph_version: str
    profile: str
    status: str
    edges: list[str] = Field(default_factory=list)
    source_positive_offset: float = 0
    source_negative_offset: float = 0
    start_m: float = 0
    end_m: float = 0
    reasons: list[str] = Field(default_factory=list)
    dependencies: dict = Field(default_factory=dict)


class Footprint(Contract):
    occurrence_id: str
    status: str
    pieces: list[dict] = Field(default_factory=list)
    diagnostics: dict = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)


class Interval(Contract):
    start_m: float
    end_m: float
    contributors: list[str]
    status: str
    current: float | None = None
    freeflow: float | None = Field(default=None, deprecated=True)
    relative_speed: float | None = None
    closure: bool | None = None
    source_direction: str = "unverified"
    legal_direction: str = "unverified"
    time_aligned: bool = False
    target_valid: bool = False
    speed_valid: bool = False
    footprint_status: str = "provisional"
    overlap_class: str = "single"
    association_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    measurement_method: str = "legacy_joint_v1"
    relative_status: str = "legacy"
    absolute_status: str = "legacy"
    relative_contributors: list[str] = Field(default_factory=list)
    absolute_contributors: list[str] = Field(default_factory=list)
    relative_reasons: list[str] = Field(default_factory=list)
    absolute_reasons: list[str] = Field(default_factory=list)
    relative_resolution_method: str = "source_relative_consistent_v1"
    relative_raw_min: float | None = None
    relative_raw_max: float | None = None
    relative_spread: float | None = None
    relative_raw_values: list[float] = Field(default_factory=list)
    selected_contributors: list[str] = Field(default_factory=list)
    congestion_priority_resolution: bool = False
    source_contexts: dict = Field(default_factory=dict)
    relative_time_aligned: bool = False
    absolute_time_aligned: bool = False
    relative_context_status: str = "unknown"
    absolute_context_status: str = "unknown"


class Observation(Contract):
    """One scheduled segment-direction key. Null measurements never mean zero."""

    schema_version: str = OUTPUT_SCHEMA
    calibration_status: str = "not_evaluated"
    review_status: str = "draft"
    technical_status: str = "incomplete"
    direction_method: str = "directed_openlr_occurrence_v2"
    observation_id: str
    registry_version: str
    segment_id: str
    direction_id: str
    scheduled_at: AwareDatetime
    local_date: str
    time_slot: str
    day_of_week: int = Field(ge=1, le=7)
    day_type: str
    road_name: str
    segment_length_m: float = Field(gt=0)
    current_speed_kmh: float | None = None
    freeflow_speed_kmh: float | None = Field(default=None, deprecated=True)
    ci_raw: float | None = None
    ci: float | None = None
    paired_speed_coverage: float | None = Field(default=None, deprecated=True)
    target_valid_coverage: float | None = None
    absolute_speed_coverage: float | None = None
    accepted_direction_coverage: float | None = None
    geometry_coverage: float | None = None
    geometry_present_coverage: float | None = None
    geometry_association_coverage: float | None = None
    ci_policy_resolution_used: bool = False
    ci_reasons: list[str] = Field(default_factory=list)
    speed_reasons: list[str] = Field(default_factory=list)
    processing_policy_sha256: str | None = None
    footprint_method: str | None = None
    interval_method: str | None = None
    lanes_known_coverage: float | None = None
    speed_method: str
    target_method: str
    speed_source_product: str
    road_closure: bool | None = None
    closure_status: str | None = None
    precipitation_mm: float | None = None
    weather_code: int | None = None
    precipitation_interval_seconds: float | None = None
    weather_valid_at: AwareDatetime | None = None
    road_type: str | None = None
    oneway: bool | None = None
    lanes_mean: float | None = None
    lanes_min: int | None = None
    lanes_max: int | None = None
    junction_count: int | None = None
    signal_count: int | None = None
    ci_status: str
    model_status: str
    quality_reasons: list[str]
    ci_ready: bool
    model_ready: bool
    needs_review: bool
    source_snapshot_id: str | None = None
    static_feature_version: str | None = None
    processing_run_id: str
