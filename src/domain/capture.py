"""Capture identities separate logical resources, attempts and scheduled slots."""

from pydantic import AwareDatetime, Field
from .reference import Contract
from typing import Protocol
from datetime import datetime


class CaptureClock(Protocol):
    """Injectable UTC wall clock plus monotonic deadline clock, seconds throughout."""

    def now_utc(self) -> datetime: ...
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...
    def wait_until(self, instant: datetime) -> None: ...


class ResourceRequest(Contract):
    resource_id: str
    provider: str
    url: str
    params: dict
    segments: list[str]
    segment_directions: list[tuple[str, str]] = Field(default_factory=list)
    tile: tuple[int, int, int] | None = None
    point: tuple[float, float] | None = None


class CapturePlan(Contract):
    version: str = "capture_plan_v1"
    registry_version: str
    registry_sha256: str
    graph_version: str
    config_sha256: str
    collection_kind: str = "pilot"
    resources: list[ResourceRequest]
    expected_attempts: int
    hash_contract: str = "resolved_config_v1"


class SlotKey(Contract):
    study_id: str
    registry_version: str
    scheduled_at: AwareDatetime


class RequestAttempt(Contract):
    reservation_id: int
    resource_id: str
    attempt_number: int
    requested_at: AwareDatetime
    received_at: AwareDatetime | None = None
    http_status: int | None = None
    outcome: str
    body_path: str | None = None
    body_sha256: str | None = None
    body_bytes: int = 0
    source_valid_at: AwareDatetime | None = None
    source_version: str | None = None
    error_code: str | None = None


class SnapshotManifest(Contract):
    schema_version: str = "1"
    snapshot_id: str
    collection_kind: str
    registry_version: str
    schedule_version: str
    scheduled_at: AwareDatetime | None = None
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    status: str
    plan_sha256: str
    capture_config_sha256: str | None = None
    source_fingerprint: str | None = None
    dependency_lock_sha256: str | None = None
    code_commit: str | None = None
    attempts: list[RequestAttempt] = Field(default_factory=list)
    unattempted_resources: list[str] = Field(default_factory=list)
