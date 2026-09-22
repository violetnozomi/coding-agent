from .config.parser import loads
from .config.writer import dumps
from .paths import config_path


def load(path):
    return loads(config_path(path).read_text())


def save(path, config):
    encoded = dumps(config)
    config_path(path).write_text(encoded)
