"""to_price_cents is the one place a dollar amount becomes the integer cents everything downstream
(the checkout rail, the publish tool) trusts. Every caller wraps the call in `except ValueError` —
so the contract that matters isn't just "rejects bad input," it's "always rejects it as ValueError."
"""

from __future__ import annotations

import pytest

from sellee.money import to_price_cents


def test_a_typical_price_rounds_half_up_to_cents() -> None:
    assert to_price_cents("19.995") == 2000
    assert to_price_cents(19.99) == 1999
    assert to_price_cents(20) == 2000


def test_non_positive_input_is_rejected() -> None:
    with pytest.raises(ValueError):
        to_price_cents(0)
    with pytest.raises(ValueError):
        to_price_cents(-5)


def test_non_numeric_input_is_rejected() -> None:
    with pytest.raises(ValueError):
        to_price_cents("not a number")
    with pytest.raises(ValueError):
        to_price_cents(None)


def test_infinity_and_nan_are_rejected_as_value_error_not_decimal_error() -> None:
    """Both slip past the `<= 0` positivity check (it's False/raises for neither), so without an
    explicit finiteness check they reach the quantize() call and blow up with
    decimal.InvalidOperation — a type none of this function's callers catch."""
    with pytest.raises(ValueError):
        to_price_cents(float("inf"))
    with pytest.raises(ValueError):
        to_price_cents(float("-inf"))
    with pytest.raises(ValueError):
        to_price_cents(float("nan"))
    with pytest.raises(ValueError):
        to_price_cents("Infinity")
    with pytest.raises(ValueError):
        to_price_cents("NaN")
