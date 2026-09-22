import argparse
import json
import sys

from .commands import COMMANDS
from .config.errors import ConfigError


def build_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("path")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        print(json.dumps(COMMANDS[args.command](args)))
    except ConfigError as exc:
        print(json.dumps(exc.to_dict()), file=sys.stderr)
        return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0
