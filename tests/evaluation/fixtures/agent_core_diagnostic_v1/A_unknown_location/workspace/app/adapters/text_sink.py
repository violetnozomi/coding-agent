from app.storage import render_text


def write(record: dict) -> str:
    return render_text(record)
