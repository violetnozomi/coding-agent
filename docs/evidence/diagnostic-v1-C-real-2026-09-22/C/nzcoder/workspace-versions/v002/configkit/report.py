def summary(config):
    return {"name": config.name, "address": f"{config.host}:{config.port}", "enabled": config.enabled}
