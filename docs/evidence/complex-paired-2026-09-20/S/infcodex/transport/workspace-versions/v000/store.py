import json
from pathlib import Path
def load(path):
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else {}
def save(path, data):
    Path(path).write_text(json.dumps(data))
