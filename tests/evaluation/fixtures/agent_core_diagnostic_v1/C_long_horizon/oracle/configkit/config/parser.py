import json

from .defaults import DEFAULT_ENABLED
from .errors import ConfigError
from .model import Config
from .validation import validate


def loads(text):
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ConfigError("$") from exc
    if not isinstance(data, dict):
        raise ConfigError("$")
    version = data.get("version", 1)
    if type(version) is not int or version not in (1, 2):
        raise ConfigError("version", "unsupported")
    if version == 2:
        service = data.get("service")
        service = service if isinstance(service, dict) else {}
        endpoint = service.get("endpoint")
        endpoint = endpoint if isinstance(endpoint, dict) else {}
        flat = {"name": service.get("name"), "host": endpoint.get("host"),
                "port": endpoint.get("port"), "enabled": data.get("enabled", DEFAULT_ENABLED)}
    else:
        flat = data
    validate(flat, "service." if version == 2 else "")
    return Config(flat["name"], flat["host"], flat["port"], flat.get("enabled", DEFAULT_ENABLED))
