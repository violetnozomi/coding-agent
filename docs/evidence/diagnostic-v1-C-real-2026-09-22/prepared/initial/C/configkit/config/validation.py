from .errors import ConfigError


def validate(data):
    for key in ("name", "host"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ConfigError(f"invalid {key}")
    port = data.get("port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ConfigError("invalid port")
    if type(data.get("enabled", True)) is not bool:
        raise ConfigError("invalid enabled")
