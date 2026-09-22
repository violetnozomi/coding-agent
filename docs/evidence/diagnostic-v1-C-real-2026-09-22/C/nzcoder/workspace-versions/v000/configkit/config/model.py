from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    name: str
    host: str
    port: int
    enabled: bool = True
