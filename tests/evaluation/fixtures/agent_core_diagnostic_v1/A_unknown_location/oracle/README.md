# Event processing

Legacy: `python -m app.cli created 42` prints `created=42`.

Optional correlation ID:
`handle("created", "42", correlation_id="request-7")`
adds a correlation_id key. None omits the key; an empty string is retained.
`python -m app.cli created 42 --correlation-id request-7 --format json`
prints JSON containing that key; default text ends in ` correlation_id=request-7`.
Run `python -m pytest -q tests`.
