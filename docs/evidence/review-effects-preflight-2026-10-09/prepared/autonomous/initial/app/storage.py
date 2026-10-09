from .formatting import as_json, as_text


def render_json(record: dict) -> str:
    return as_json(record)


def render_text(record: dict) -> str:
    return as_text(record)
