from .errors import ConfigError


def validate(data, prefix=""):
    for key in ("name", "host"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            path = prefix + ("endpoint." if prefix and key == "host" else "") + key
            raise ConfigError(path)
    port = data.get("port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ConfigError(prefix + ("endpoint." if prefix else "") + "port")
    if type(data.get("enabled", True)) is not bool:
        raise ConfigError("enabled")
