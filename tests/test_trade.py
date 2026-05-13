"""Tests for M1: Trade booking.

Invariants verified:
- Each product computes the correct value_date from trade_date + convention
- Product-specific required fields are enforced (e.g., IRS needs floating_index)
- Weekend skipping works in T+N calculation
- Unknown product types raise ValueError
- Confirmation status defaults to "pending" until M2 affirms
"""

from datetime import date

import pytest

from post_trade.trade import Trade, parse_fix_like_message


def test_fx_spot_books_with_t_plus_2_value_date():
    """FX spot settles T+2 by global convention."""
    msg = {
        "trade_id": "T001",
        "external_id": "EXT-001",
        "product_type": "fx_spot",
        "direction": "buy",
        "quantity": 1_000_000,
        "price": 1.3450,
        "trade_date": date(2026, 5, 13),  # Wednesday
        "counterparty": "BANK-A",
        "portfolio": "PB-USD-1",
        "currency_pair": "USD/SGD",
    }
    trade = parse_fix_like_message(msg)
    assert trade.value_date == date(2026, 5, 15)  # Friday
    assert trade.product_type == "fx_spot"
    assert trade.currency_pair == "USD/SGD"


def test_irs_requires_floating_index():
    """IRS trades must specify the floating index (SOFR, ESTR, SORA, etc.)."""
    msg = {
        "trade_id": "T002",
        "external_id": None,
        "product_type": "irs",
        "direction": "sell",
        "quantity": 100_000_000,
        "price": 0.042,        # 4.2% fixed rate
        "trade_date": date(2026, 5, 13),
        "counterparty": "BANK-B",
        "portfolio": "PB-USD-1",
        "tenor": "5Y",
        # floating_index intentionally missing
    }
    with pytest.raises(ValueError, match="floating_index"):
        parse_fix_like_message(msg)


def test_irs_books_at_t_plus_0():
    """IRS starts same day; fixings and payments happen later per schedule."""
    msg = {
        "trade_id": "T002",
        "external_id": "EXT-002",
        "product_type": "irs",
        "direction": "sell",
        "quantity": 100_000_000,
        "price": 0.042,
        "trade_date": date(2026, 5, 13),
        "counterparty": "BANK-B",
        "portfolio": "PB-USD-1",
        "tenor": "5Y",
        "floating_index": "SOFR",
    }
    trade = parse_fix_like_message(msg)
    assert trade.value_date == date(2026, 5, 13)
    assert trade.floating_index == "SOFR"


def test_cash_equity_books_with_t_plus_2_value_date():
    """Singapore equities settle T+2 (per SGX). US T+1 since May 2024
    but SG/HK/EU still T+2."""
    msg = {
        "trade_id": "T003",
        "external_id": "EXT-003",
        "product_type": "cash_equity",
        "direction": "buy",
        "quantity": 10_000,
        "price": 28.50,
        "trade_date": date(2026, 5, 13),  # Wednesday
        "counterparty": "BROKER-X",
        "portfolio": "EQ-SG-1",
    }
    trade = parse_fix_like_message(msg)
    assert trade.value_date == date(2026, 5, 15)


def test_value_date_skips_weekend():
    """T+2 from Thursday should land on Monday, not Saturday."""
    msg = {
        "trade_id": "T004",
        "external_id": "EXT-004",
        "product_type": "fx_spot",
        "direction": "buy",
        "quantity": 1_000_000,
        "price": 1.345,
        "trade_date": date(2026, 5, 14),  # Thursday
        "counterparty": "BANK-A",
        "portfolio": "PB-USD-1",
        "currency_pair": "USD/SGD",
    }
    trade = parse_fix_like_message(msg)
    # Thu -> Fri -> (skip Sat/Sun) -> Mon
    assert trade.value_date == date(2026, 5, 18)


def test_fx_requires_currency_pair():
    msg = {
        "trade_id": "T005",
        "external_id": "EXT-005",
        "product_type": "fx_spot",
        "direction": "buy",
        "quantity": 1_000_000,
        "price": 1.345,
        "trade_date": date(2026, 5, 13),
        "counterparty": "BANK-A",
        "portfolio": "PB-USD-1",
        # currency_pair missing
    }
    with pytest.raises(ValueError, match="currency_pair"):
        parse_fix_like_message(msg)


def test_unknown_product_type_raises():
    msg = {"product_type": "unicorn_swap"}
    with pytest.raises(ValueError, match="Unknown product_type"):
        parse_fix_like_message(msg)


def test_confirmation_status_defaults_to_pending():
    """Freshly booked trade has not yet been affirmed (M2 handles that)."""
    msg = {
        "trade_id": "T006",
        "external_id": "EXT-006",
        "product_type": "fx_spot",
        "direction": "buy",
        "quantity": 1_000_000,
        "price": 1.345,
        "trade_date": date(2026, 5, 13),
        "counterparty": "BANK-A",
        "portfolio": "PB-USD-1",
        "currency_pair": "USD/SGD",
    }
    trade = parse_fix_like_message(msg)
    assert trade.confirmation_status == "pending"


def test_explicit_value_date_overrides_convention():
    """FX forward typically has a contract-specific value date,
    not T+2. Allow caller to supply explicit value_date."""
    msg = {
        "trade_id": "T007",
        "external_id": "EXT-007",
        "product_type": "fx_forward",
        "direction": "buy",
        "quantity": 1_000_000,
        "price": 1.345,
        "trade_date": date(2026, 5, 13),
        "value_date": date(2026, 11, 13),  # 6M forward
        "counterparty": "BANK-A",
        "portfolio": "PB-USD-1",
        "currency_pair": "USD/SGD",
    }
    trade = parse_fix_like_message(msg)
    assert trade.value_date == date(2026, 11, 13)
