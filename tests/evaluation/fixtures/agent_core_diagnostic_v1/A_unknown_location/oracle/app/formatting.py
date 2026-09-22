import json


def as_json(record: dict) -> str:
    return json.dumps(record, sort_keys=True)


def as_text(record: dict) -> str:
    suffix = f" correlation_id={record['correlation_id']}" if "correlation_id" in record else ""
    return f"{record['name']}={record['value']}" + suffix
