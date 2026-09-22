from .defaults import DEFAULT_ENABLED
from .errors import ConfigError

V1 = 1
V2 = 2


def _mapping(value, path):
    if not isinstance(value, dict):
        raise ConfigError(path, "invalid")
    return value


def _value(mapping, key, path):
    if key not in mapping:
        raise ConfigError(path, "invalid")
    return mapping[key]


def _check_str(value, path):
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(path, "invalid")


def _check_port(value, path):
    if type(value) is not int or not 1 <= value <= 65535:
        raise ConfigError(path, "invalid")


def _check_enabled(data):
    enabled = data.get("enabled", DEFAULT_ENABLED)
    if type(enabled) is not bool:
        raise ConfigError("enabled", "invalid")
    return enabled


def version_of(data):
    """Return the configuration version, raising on an unsupported value."""
    version = data.get("version", V1)
    if isinstance(version, bool) or type(version) is not int or version not in (V1, V2):
        raise ConfigError("version", "unsupported")
    return version


def validate(data):
    """Validate a v1 or v2 mapping and return the decoded field values.

    Checks run in the required order (version, name, host, port, enabled) and
    only the first problem is reported, using the leaf path of the input shape.
    """
    if not isinstance(data, dict):
        raise ConfigError("$", "invalid")

    version = version_of(data)

    if version == V2:
        service = _mapping(data.get("service"), "service.name")
        name = _value(service, "name", "service.name")
        _check_str(name, "service.name")

        endpoint = _mapping(service.get("endpoint"), "service.endpoint.host")
        host = _value(endpoint, "host", "service.endpoint.host")
        _check_str(host, "service.endpoint.host")

        port = _value(endpoint, "port", "service.endpoint.port")
        _check_port(port, "service.endpoint.port")
    else:
        name = _value(data, "name", "name")
        _check_str(name, "name")

        host = _value(data, "host", "host")
        _check_str(host, "host")

        port = _value(data, "port", "port")
        _check_port(port, "port")

    return {"name": name, "host": host, "port": port, "enabled": _check_enabled(data)}
