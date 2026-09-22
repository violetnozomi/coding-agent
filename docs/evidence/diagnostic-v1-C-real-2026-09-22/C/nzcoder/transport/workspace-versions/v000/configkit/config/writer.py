import json


def to_dict(config):
    """Serialize a config using only the v2 representation."""
    return {
        "version": 2,
        "service": {
            "name": config.name,
            "endpoint": {"host": config.host, "port": config.port},
        },
        "enabled": config.enabled,
    }


def dumps(config):
    return json.dumps(to_dict(config), sort_keys=True) + "\n"
