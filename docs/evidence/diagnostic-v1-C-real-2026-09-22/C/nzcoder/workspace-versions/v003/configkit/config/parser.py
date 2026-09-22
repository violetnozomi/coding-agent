import json

from .errors import ConfigError
from .model import Config
from .validation import validate


def loads(text):
    try:
        data = json.loads(text)
    except ValueError:
        raise ConfigError("$", "invalid") from None
    values = validate(data)
    return Config(values["name"], values["host"], values["port"], values["enabled"])
