import json

from .validation import validate


def to_dict(config):
    validate({"name": config.name, "host": config.host, "port": config.port, "enabled": config.enabled})
    return {"version": 2, "service": {"name": config.name,
            "endpoint": {"host": config.host, "port": config.port}}, "enabled": config.enabled}


def dumps(config):
    return json.dumps(to_dict(config), sort_keys=True) + "\n"
