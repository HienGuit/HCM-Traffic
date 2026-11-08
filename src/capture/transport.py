"""Single-attempt HTTP transport. No implicit retries or redirects."""

from dataclasses import dataclass
import os
import httpx
from ..sources.orbis import request_spec as traffic_spec
from ..sources.weather import request_spec as weather_spec


@dataclass
class Response:
    status: int | None
    body: bytes
    headers: dict
    error: str | None = None


class Transport:
    def __init__(self, contracts, clock, transport=None):
        self.contracts, self.clock = contracts, clock
        self.client = httpx.Client(
            transport=transport or httpx.HTTPTransport(retries=0),
            follow_redirects=False,
            trust_env=False,
        )

    def send(self, resource, timeout, deadline):
        spec = (
            traffic_spec(resource, self.contracts)
            if resource.provider == "traffic"
            else weather_spec(resource)
        )
        if resource.provider == "traffic":
            spec["headers"]["TomTom-Api-Key"] = os.environ.get("TOMTOM_API_KEY", "")
        body = bytearray()
        status = None
        headers = {}
        try:
            with self.client.stream(
                "GET", **spec, timeout=min(timeout, max(0.001, deadline - self.clock.monotonic()))
            ) as response:
                status = response.status_code
                headers = {
                    k: v
                    for k, v in response.headers.items()
                    if k.lower()
                    in (
                        "date",
                        "etag",
                        "content-type",
                        "content-encoding",
                        "retry-after",
                        "x-ratelimit-remaining",
                        "ratelimit-remaining",
                    )
                }
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if self.clock.monotonic() > deadline:
                        return Response(status, bytes(body), headers, "DEADLINE_EXCEEDED")
            return Response(status, bytes(body), headers)
        except httpx.HTTPError as exc:
            return Response(status, bytes(body), headers, type(exc).__name__)

    def close(self):
        self.client.close()
