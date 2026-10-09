from .events import Event
from .service import process


def handle(name: str, value: str, correlation_id: str | None = None) -> dict:
    return process(Event(name, value, correlation_id))
