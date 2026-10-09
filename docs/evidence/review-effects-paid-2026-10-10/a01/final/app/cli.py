import argparse

from .api import handle
from .storage import render_json, render_text


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    parser.add_argument("value")
    parser.add_argument("--correlation-id", default=None)
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="output format (default: text)",
    )
    args = parser.parse_args(argv)
    record = handle(args.name, args.value, correlation_id=args.correlation_id)
    render = render_json if args.format == "json" else render_text
    print(render(record))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
