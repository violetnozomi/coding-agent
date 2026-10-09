from .events import Event


def process(event: Event) -> dict:
    return {"name": event.name, "value": event.value}
