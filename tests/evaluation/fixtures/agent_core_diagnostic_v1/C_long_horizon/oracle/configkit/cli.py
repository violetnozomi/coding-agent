import argparse
import json
import sys

from .commands import COMMANDS
from .config.errors import ConfigError


def main(argv=None):
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    for name in sorted(COMMANDS):
        command = commands.add_parser(name)
        command.add_argument("path")
        if name == "migrate":
            command.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(COMMANDS[args.command](args)))
    except ConfigError as exc:
        print(json.dumps(exc.to_dict()), file=sys.stderr)
        return 2
    return 0
