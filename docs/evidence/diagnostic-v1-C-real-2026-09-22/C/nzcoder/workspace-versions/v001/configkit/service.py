from .report import summary
from .store import load, save


def inspect_config(path):
    return summary(load(path))


def export_config(source, destination):
    config = load(source)
    save(destination, config)
    return summary(config)
