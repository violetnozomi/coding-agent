"""Terminal interaction adapter for the structured question tool."""
from __future__ import annotations


def _parse_answer(raw: str, question: dict) -> list[str] | None:
    value = str(raw or "").strip()
    if not value:
        return None
    options = question["options"]
    multiple = bool(question.get("multiple"))
    parts = [part.strip() for part in value.split(",")] if multiple else [value]
    if all(part.isdigit() for part in parts):
        indexes = [int(part) for part in parts]
        if any(index < 1 or index > len(options) for index in indexes):
            return []
        answers: list[str] = []
        for index in indexes:
            label = options[index - 1]["label"]
            if label not in answers:
                answers.append(label)
        return answers
    return [value]


def build_terminal_question_asker(renderer):
    """Return a blocking terminal asker, or None when no console is available."""
    console = getattr(renderer, "console", None)
    if console is None or not hasattr(console, "input") or not hasattr(console, "print"):
        return None

    def ask(questions: list[dict]) -> list[list[str]] | None:
        renderer.pause()
        try:
            answers: list[list[str]] = []
            for item in questions:
                from nz_coder.interface.run_renderer import render_question_request

                render_question_request(console, item)
                mode = "numbers separated by commas" if item.get("multiple") else "a number"
                prompt = f"  Select {mode}, or type a custom answer (blank dismisses): "
                for _attempt in range(5):
                    try:
                        parsed = _parse_answer(console.input(prompt, markup=False), item)
                    except (EOFError, KeyboardInterrupt, OSError):
                        return None
                    if parsed is None:
                        return None
                    if parsed:
                        answers.append(parsed)
                        break
                    console.print(
                        "  Invalid selection. Choose a listed number or type an answer.",
                        markup=False,
                        highlight=False,
                    )
                else:
                    return None
            return answers
        finally:
            renderer.resume()

    return ask
