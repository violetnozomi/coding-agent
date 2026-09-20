"""Frozen external checks; only published requirements, no model access."""
import copy
import dataclasses
import importlib
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import os
import pytest


@pytest.fixture
def api():
    try:
        return importlib.import_module('billing.quote')
    except ImportError as exc:
        pytest.fail(f'Required quote API missing: {exc}')


def test_rounding_currency_and_input_ownership(api):
    for currency, scale in [('USD', 100), ('EUR', 100), ('JPY', 1)]:
        for price, qty, bps in [('0.005', 3, 0), ('2.675', 7, 3333), ('1.5', 3, 10000), ('0', 1, 1)]:
            lines = [{'unit_price': price, 'quantity': qty, 'tag': ['untouched']}]
            before = copy.deepcopy(lines)
            q = api.quote_order(iter(lines), currency=currency, discount_bps=bps)
            subtotal = int((Decimal(price) * qty * scale).quantize(Decimal(1), rounding=ROUND_HALF_UP))
            discount = int((Decimal(subtotal) * bps / 10000).quantize(Decimal(1), rounding=ROUND_HALF_UP))
            assert (q.currency, q.subtotal_minor, q.discount_minor, q.total_minor) == (currency, subtotal, discount, subtotal-discount)
            assert type(q.total_minor) is int and lines == before
            assert dataclasses.is_dataclass(q)
            with pytest.raises(dataclasses.FrozenInstanceError):
                q.total_minor = 0


def test_line_rounding_not_aggregate_rounding(api):
    lines = [{'unit_price': '0.005', 'quantity': 1}] * 2
    q = api.quote_order(lines, discount_bps=2500)
    assert (q.subtotal_minor, q.discount_minor, q.total_minor) == (2, 1, 1)
    assert api.quote_order([]).total_minor == 0


@pytest.mark.parametrize('price', ['-0.01', 'NaN', 'Infinity', 'bad', 1.2, None])
def test_invalid_price(api, price):
    with pytest.raises(ValueError):
        api.quote_order([{'unit_price': price, 'quantity': 1}])


@pytest.mark.parametrize('qty', [0, -1, 1.5, True, '2', None])
def test_invalid_quantity(api, qty):
    with pytest.raises(ValueError):
        api.quote_order([{'unit_price': '1', 'quantity': qty}])


def test_validation_on_empty_order(api):
    for bps in [-1, 10001, True, 1.5, '10']:
        with pytest.raises(ValueError):
            api.quote_order([], discount_bps=bps)
    with pytest.raises(ValueError):
        api.quote_order([], currency='BTC')


def test_all_five_consumers(api, monkeypatch):
    import billing.legacy
    monkeypatch.setattr(billing.legacy, 'total_amount', lambda *_: pytest.fail('Legacy API still used internally'))
    web, cli, jobs, mobile, reporting = [importlib.import_module(n) for n in ['web', 'cli', 'jobs', 'mobile', 'reporting']]
    for currency, expected in [('EUR', 135), ('JPY', 1)]:
        lines = [{'unit_price': '0.5', 'quantity': 3}]
        args = {'currency': currency, 'discount_bps': 1000}
        amount = '1.35' if currency == 'EUR' else '1'
        # JPY rounds 1.5 to 2; discount 0.2 rounds 0, total is 2.
        if currency == 'JPY':
            expected, amount = 2, '2'
        assert web.checkout(lines, **args) == {'currency': currency, 'total_minor': expected}
        assert cli.receipt(lines, **args) == f'{currency} {amount}'
        assert jobs.export(lines, **args) == f'currency,total_minor\n{currency},{expected}\n'
        assert mobile.summary(lines, **args) == amount
        assert reporting.monthly(lines, **args) == expected


def test_consumers_use_central_quote(api, monkeypatch):
    calls = []
    def sentinel(*args, **kwargs):
        calls.append((args, kwargs))
        return api.Quote(currency='USD', subtotal_minor=987, discount_minor=0, total_minor=987)
    monkeypatch.setattr(api, 'quote_order', sentinel)
    modules = [importlib.reload(importlib.import_module(n)) for n in ['web', 'cli', 'jobs', 'mobile', 'reporting']]
    lines = [{'unit_price': '1', 'quantity': 1}]
    assert modules[0].checkout(lines)['total_minor'] == 987
    assert modules[1].receipt(lines) == 'USD 9.87'
    assert modules[2].export(lines) == 'currency,total_minor\nUSD,987\n'
    assert modules[3].summary(lines) == '9.87'
    assert modules[4].monthly(lines) == 987
    assert len(calls) == 5


def test_legacy_remains_callable():
    from billing.legacy import total_amount
    assert total_amount([{'unit_price': '1.25', 'quantity': 2}]) == 2.5


def test_documentation_updated():
    text = (Path(os.environ['TASK_WORKSPACE']) / 'README.md').read_text().lower()
    assert 'quote_order' in text and 'round' in text and 'legacy' in text
