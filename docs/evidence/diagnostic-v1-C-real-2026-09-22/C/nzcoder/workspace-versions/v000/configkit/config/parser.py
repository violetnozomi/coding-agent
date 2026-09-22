import json

from .defaults import DEFAULT_ENABLED
from .model import Config
from .validation import validate


def loads(text):
    data = json.loads(text)
    validate(data)
    return Config(data["name"], data["host"], data["port"], data.get("enabled", DEFAULT_ENABLED))
