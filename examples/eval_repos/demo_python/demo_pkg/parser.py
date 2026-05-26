"""Small parser module with intentional demo bugs."""

def parse_items(text: str) -> list[str]:
    return [part.strip() for part in text.split(",")]
