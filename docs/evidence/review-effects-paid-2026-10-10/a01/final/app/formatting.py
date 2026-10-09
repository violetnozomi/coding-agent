import json


def as_json(record: dict) -> str:
    return json.dumps(record, sort_keys=True)


def as_text(record: dict) -> str:
    text = f"{record['name']}={record['value']}"
    correlation_id = record.get("correlation_id")
    if correlation_id is not None:
        text += f" correlation_id={correlation_id}"
    return text
