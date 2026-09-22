from ..store import load, save
from .writer import to_dict


def migrate_file(path, dry_run=False):
    config = load(path)
    if not dry_run:
        save(path, config)
    return to_dict(config)
