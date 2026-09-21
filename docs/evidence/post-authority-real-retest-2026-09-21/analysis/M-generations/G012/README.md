# Invoice desk

Pricing is centralized in `billing.quote.quote_order`, which returns an
immutable `billing.quote.Quote` value object. The float-based
`billing.legacy.total_amount` helper is kept for external callers only and is
no longer used anywhere inside this repository.

## Usage

```python
from billing.quote import quote_order

quote = quote_order(
    [{"unit_price": "1.25", "quantity": 2}],
    currency="USD",
    discount_bps=0,
)
assert quote.total_minor == 250
```

`quote_order(lines, *, currency="USD", discount_bps=0)` accepts any iterable of
mappings with:

- `unit_price` — the exact price of one unit as a decimal string (a `Decimal`
  or integer is also accepted). Unrelated keys are ignored and the caller's
  mappings are never mutated, so `lines` can be safely reused.
- `quantity` — a positive integer. `bool` is rejected.

`Quote` is a frozen dataclass with four integer minor-unit fields:

| field | meaning |
| --- | --- |
| `currency` | the validated ISO code (`USD`, `EUR` or `JPY`) |
| `subtotal_minor` | sum of the rounded line totals |
| `discount_minor` | order-level discount in minor units |
| `total_minor` | `subtotal_minor - discount_minor` |

All monetary values are integers in minor units (cents for `USD`/`EUR`, whole
yen for `JPY`), so no binary floating point value ever participates in a total.
`format_minor(amount_minor, currency)` renders those integers for display
(`"12.30"` for `USD`/`EUR`, `"1230"` for `JPY`).

Empty orders are valid and produce a zero quote.

## Currencies

| currency | minor-unit decimal places |
| --- | --- |
| `USD` | 2 |
| `EUR` | 2 |
| `JPY` | 0 |

Unsupported or non-string currency codes raise `ValueError`. Currency and
`discount_bps` are validated even for empty orders, so a misconfigured call
fails fast instead of silently returning zero.

## Rounding

- Each line is multiplied with exact decimal arithmetic (`unit_price *
  quantity`) and only then rounded HALF_UP to the currency's minor units. Unit
  prices are never rounded before multiplying, so `0.005 * 3` becomes `0.02`
  rather than `0.01`.
- The rounded line totals are summed to form `subtotal_minor`; lines are not
  summed first and rounded afterwards.
- The total-order discount is computed from `subtotal_minor` and `discount_bps`
  (`discount_bps / 10000`) and rounded HALF_UP to minor units.
- `total_minor = subtotal_minor - discount_minor`.

## Validation

`ValueError` is raised for:

- unsupported currencies (`GBP`, `usd`, non-string values),
- `unit_price` values that are not valid decimal notation, are non-finite
  (`NaN`/`Infinity`), or are negative,
- `unit_price` given as a `float` or `bool` (floats cannot represent decimal
  money exactly),
- `quantity` that is not a positive integer (`0`, negatives, `bool`, strings),
- `discount_bps` that is not an integer in `0..10000` (`bool` is rejected),
- lines that are not mappings or that omit `unit_price`/`quantity`.

## Legacy compatibility boundary

`billing.legacy.total_amount(lines)` remains callable and unchanged for
external callers that still consume the old float-based API:

```python
from billing.legacy import total_amount

total_amount([{"unit_price": "1.25", "quantity": 2}])  # 2.5
```

No module inside this repository calls it. New code should use `quote_order`,
which is the single source of pricing truth.

## Consumers

All five consumers accept the same `lines` plus keyword-only `currency` and
`discount_bps` defaults, and every output is derived from the central `Quote`
rather than duplicating pricing arithmetic:

| module | function | result |
| --- | --- | --- |
| `web` | `checkout(lines, *, currency="USD", discount_bps=0)` | `{"currency": "USD", "total_minor": 1230}` |
| `cli` | `receipt(...)` | `"USD 12.30"` (`"JPY 1230"` for yen, no decimals) |
| `jobs` | `export(...)` | `"currency,total_minor\nUSD,1230\n"` |
| `mobile` | `summary(...)` | `"12.30"` (formatted numeric amount, no currency) |
| `reporting` | `monthly(...)` | `1230` (integer minor units) |

## Verification

```
python -m pytest -q tests
```

`tests/test_quote.py` covers rounding, currency exponents, discounts and
validation; `tests/test_consumers.py` covers the consumer output formats, the
shared defaults and the legacy compatibility boundary.
