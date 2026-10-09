from .events import Event
from .service import process


def handle(name: str, value: str) -> dict:
    return process(Event(name, value))
