"""Orbis v2 request contract. Credentials are injected only by transport."""


def request_spec(resource, contracts):
    if (
        resource.provider != "traffic"
        or "/maps/orbis/traffic/flow/vector/tile/" not in resource.url
    ):
        raise ValueError("Invalid Orbis resource")
    return {
        "url": resource.url,
        "params": resource.params,
        "headers": {"TomTom-Api-Version": contracts.api_version},
    }
