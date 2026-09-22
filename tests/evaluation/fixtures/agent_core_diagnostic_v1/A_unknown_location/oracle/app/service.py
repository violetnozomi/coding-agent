from .events import Event


def process(event: Event) -> dict:
    record = {"name": event.name, "value": event.value}
    if event.correlation_id is not None:
        record["correlation_id"] = event.correlation_id
    return record
