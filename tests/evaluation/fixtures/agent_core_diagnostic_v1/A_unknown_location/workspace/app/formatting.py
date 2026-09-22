import json


def as_json(record: dict) -> str:
    return json.dumps(record, sort_keys=True)


def as_text(record: dict) -> str:
    return f"{record['name']}={record['value']}"
