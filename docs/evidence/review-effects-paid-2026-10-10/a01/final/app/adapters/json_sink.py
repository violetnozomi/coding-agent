from app.storage import render_json


def write(record: dict) -> str:
    return render_json(record)
