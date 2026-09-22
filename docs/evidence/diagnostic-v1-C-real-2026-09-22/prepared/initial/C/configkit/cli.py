import argparse
import json
import sys

from .commands import COMMANDS


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("path")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(COMMANDS[args.command](args)))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0
