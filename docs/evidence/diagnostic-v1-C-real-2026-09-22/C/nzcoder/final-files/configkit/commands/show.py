from ..service import inspect_config


def execute(args):
    return inspect_config(args.path)
