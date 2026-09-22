import json


def to_dict(config):
    return {"name": config.name, "host": config.host, "port": config.port, "enabled": config.enabled}


def dumps(config):
    return json.dumps(to_dict(config), sort_keys=True) + "\n"
