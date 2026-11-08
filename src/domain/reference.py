"""Reference/path contracts; lengths and offsets are metres in metric CRS."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, AwareDatetime, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ReferenceManifest(Contract):
    reference_id: str
    source_sha256: str
    original_path: str
    source_timestamp: datetime | None = None
    spatial_extent: list[float] | None = None
    completeness: dict = Field(default_factory=dict)
    parser_version: str = "reference_v1"


@dataclass(frozen=True)
class DirectedEdge:
    id: str
    way_id: int
    start: int
    end: int
    first_index: int
    last_index: int
    traversal: int
    length: float
    coords: tuple
    tags: dict
    frc: int
    fow: int
    legal: str = "unverified"
    boundary: bool = False


class PathEdge(Contract):
    edge_id: str
    start_m: float = Field(ge=0)
    end_m: float = Field(gt=0)


class RegistryPath(Contract):
    segment_id: str
    direction_id: str = "FORWARD"
    registry_version: str = "draft_v1"
    graph_version: str
    road_name: str
    edges: list[PathEdge]
    length_m: float = Field(gt=0)
    geometry: dict
    approval_status: Literal["draft", "approved", "retired"] = "draft"
    approved_by: str | None = None
    approved_at: AwareDatetime | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    legal_direction_status: str = "unverified"
    source_direction_status: str = "unverified"
    geometry_sha256: str | None = None

    @model_validator(mode="after")
    def geometry_hash(self):
        import hashlib
        import json

        value = hashlib.sha256(
            json.dumps(
                self.geometry,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        if self.geometry_sha256 is not None and self.geometry_sha256 != value:
            raise ValueError("Registry geometry hash mismatch")
        self.geometry_sha256 = value
        return self


class ReviewEvidence(Contract):
    evidence_id: str
    evidence_type: str
    source_url_or_path: str
    source_sha256: str
    published_at: AwareDatetime | None = None
    effective_from: AwareDatetime
    effective_to: AwareDatetime | None = None
    daily_time_window: list[str]
    spatial_scope: list[str]
    direction_scope: list[str]
    vehicle_scope: str
    occurrence_ids: list[str] = Field(default_factory=list)
    checked_at: AwareDatetime
    reviewer: str
    limitations: str
    method_id: str = "scoped_review_v1"
    graph_version: str | None = None
    matching_policy_hash: str | None = None
    product: str | None = None
    association_ids: list[str] = Field(default_factory=list)
    method_evidence_ids: list[str] = Field(default_factory=list)
    status: Literal["experimental", "approved", "rejected"] = "experimental"
    artifact_path: str | None = None
