import argparse

from .api import handle
from .storage import render_text


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    parser.add_argument("value")
    args = parser.parse_args(argv)
    print(render_text(handle(args.name, args.value)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
