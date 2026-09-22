from ..config.migrate import migrate_file


def execute(args):
    return migrate_file(args.path)
