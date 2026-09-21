# Invoice desk

Pricing is centralized in `billing.quote.quote_order`, which returns an immutable
`billing.quote.Quote` expressed in integer **minor units** (cents for USD/EUR, yen
for JPY). `billing.legacy.total_amount` is kept only for external callers; no
in-repository consumer uses it.

## Usage

```python
from billing.quote import quote_order

quote = quote_order(
    [{"unit_price": "1.25", "quantity": 2, "sku": "A1"}],
    currency="USD",
    discount_bps=1000,  # 10%
)
quote.currency        # "USD"
quote.subtotal_minor  # 250
quote.discount_minor  # 25
quote.total_minor     # 225
quote.total_display   # "2.25"
quote.formatted_total # "USD 2.25"
```

Each line is any mapping with a decimal `unit_price` string and a positive
integer `quantity`; unrelated keys are ignored and caller input is never mutated.
Empty orders are valid.

## Rounding

All arithmetic uses `decimal.Decimal` in exact decimal space:

- each line computes `unit_price * quantity` first and only then rounds HALF_UP
  to the currency's minor units (unit prices are never rounded before
  multiplying);
- rounded line amounts are summed into `subtotal_minor`;
- the order-level discount `subtotal_minor * discount_bps / 10000` is rounded
  HALF_UP to whole minor units;
- `total_minor = subtotal_minor - discount_minor`.

Supported currencies and their exponents: USD (2), EUR (2), JPY (0).

## Validation

`quote_order` raises `ValueError` for unsupported currencies, non-string/non-
decimal prices, non-finite or negative prices, non-positive or non-integer
quantities, and `discount_bps` outside the inclusive integer range `0..10000`.
`bool` is rejected for both `quantity` and `discount_bps`, and float input is
rejected for prices. Currency and discount are validated even for empty orders.

## Consumers

All five in-repository consumers accept the same lines plus keyword-only
`currency="USD"` and `discount_bps=0`, and derive their output from the central
`Quote`:

| Consumer | Returns |
| --- | --- |
| `web.checkout` | `{"currency": "USD", "total_minor": 250}` |
| `cli.receipt` | `"USD 2.50"` (JPY: `"JPY 250"` without decimals) |
| `jobs.export` | `"currency,total_minor\nUSD,250\n"` |
| `mobile.summary` | `"2.50"` (numeric amount, no currency) |
| `reporting.monthly` | `250` (integer minor units) |

## Legacy compatibility

`billing.legacy.total_amount(lines)` remains callable for external users and
still returns a float sum of raw unit prices times quantities. It is deprecated
in favour of `quote_order`, is not used anywhere inside this repository, and
does not offer currency handling, validation or discount support.

## Verification

```sh
python -m pytest -q tests
```
