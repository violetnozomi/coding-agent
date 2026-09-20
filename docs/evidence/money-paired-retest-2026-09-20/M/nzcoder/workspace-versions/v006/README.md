# Invoice desk

Pricing is centralized in `billing.quote.quote_order`, which returns an
immutable `Quote` of integer **minor units** (cents for USD/EUR, whole yen for
JPY). `billing.legacy.total_amount` remains available for external users, but
no code inside this repository calls it.

## Usage

```python
from billing.quote import quote_order

quote = quote_order(
    [
        {"unit_price": "12.30", "quantity": 2, "sku": "ignored"},
        {"unit_price": "0.005", "quantity": 3},
    ],
    currency="USD",
    discount_bps=500,  # 5%
)

quote.currency        # "USD"  (major-unit formatting is the caller's job)
quote.subtotal_minor  # 2460 + 2 = 2462  (rounding happens per line)
quote.discount_minor  # 123
quote.total_minor     # 2339
```

`quote_order(lines, *, currency="USD", discount_bps=0)` accepts any iterable of
mappings with `unit_price` (a decimal string, e.g. `"12.30"`; `Decimal` is also
accepted) and `quantity` (a positive integer). Unrelated keys are ignored, the
caller's mappings are never mutated, and an empty order is valid.

## Rounding

Money is never kept in binary floating point. For each line the exact decimal
`unit_price * quantity` is computed and rounded **HALF_UP once** to the
currency's minor unit; the line must not be rounded per unit first. For example
`0.005 * 3 == 0.015` prices as `0.02`, and two lines of `"1.005" x 1` sum to
`2.02` rather than `2.00` because each line rounds before summing. The
total-order discount (`subtotal_minor * discount_bps / 10000`) is likewise
rounded HALF_UP, and `total_minor = subtotal_minor - discount_minor`.

## Validation

All of the following raise `ValueError`:

- an unsupported `currency` (only `USD`, `EUR` and `JPY` are supported)
- a `discount_bps` that is not an integer in `0..10000` (booleans are invalid)
- `unit_price` values that are not strings/`Decimal`, or that are invalid,
  non-finite or negative (floats are rejected; pass a decimal string instead)
- `quantity` values that are not positive integers (booleans are invalid)

Currency and discount are validated even for an empty order, so a caller can
rely on the checks without sending any lines.

## Consumers

All five in-repository consumers accept the same `lines` and the same
keyword-only `currency`/`discount_bps` defaults, and every value below comes
from the central `Quote` rather than duplicated pricing arithmetic:

| Consumer            | Returns                                                        |
| ------------------- | -------------------------------------------------------------- |
| `web.checkout`      | `{"currency": "USD", "total_minor": 1230}`                      |
| `cli.receipt`       | `"USD 12.30"` (`"JPY 1230"` — JPY has no decimals)              |
| `jobs.export`       | `"currency,total_minor\nUSD,1230\n"`                            |
| `mobile.summary`    | `"12.30"` (formatted amount, no currency code)                  |
| `reporting.monthly` | `1230` (integer minor units)                                    |

## Compatibility boundary

`billing.legacy.total_amount(lines)` is kept exactly as before for external
callers. It still uses floats and therefore still drifts (`0.1 * 3 != 0.3`);
new code should use `quote_order`. Nothing inside this repository imports it,
and migrating a caller is a matter of replacing the call with `quote_order`
and reading `quote.total_minor`.

## Tests

Run the suite with the standard library only, no network access:

```console
python -m pytest -q tests
```
