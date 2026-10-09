from dataclasses import dataclass


@dataclass
class Event:
    name: str
    value: str
    correlation_id: str | None = None
