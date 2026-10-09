# Event processing

Use `python -m app.cli NAME VALUE` to render an event.

## Correlation IDs

Events accept an optional correlation ID. When it is omitted (`None`) the
output is unchanged; an empty string is a valid, present ID.

### Python API

```python
from app.api import handle

handle("created", "42")
# {"name": "created", "value": "42"}

handle("created", "42", correlation_id="abc")
# {"name": "created", "value": "42", "correlation_id": "abc"}
```

`correlation_id` is optional in `handle(name, value, correlation_id=None)` and
in the `Event` model, so existing callers keep working unchanged.

### Command line

Text output is the default and appends a trailing ` correlation_id=ID` when an
ID is supplied:

```console
$ python -m app.cli created 42
created=42

$ python -m app.cli created 42 --correlation-id abc
created=42 correlation_id=abc
```

Use `--format json` for JSON output, where the ID appears as
`correlation_id`:

```console
$ python -m app.cli created 42 --format json
{"name": "created", "value": "42"}

$ python -m app.cli created 42 --format json --correlation-id abc
{"name": "created", "value": "42", "correlation_id": "abc"}
```

## Tests

```console
$ python -m pytest -q tests
```
