"""Tests for extended product coverage (futures + fx_option stubs).

These products are MVP stubs — full settlement mechanics (daily MTM for futures,
exercise for options) are parked for future enhancement. The tests verify
booking + basic settlement dispatch, not full product semantics.
"""

from datetime import date

import pytest

from post_trade.trade import parse_fix_like_message
from post_trade.settlement import project_settlements


# ---------------------------------------------------------------------------
# Futures booking
# ---------------------------------------------------------------------------

def test_futures_books_with_t_plus_1_value_date():
    """Exchange-cleared futures settle T+1 (MVP simplification)."""
    msg = {
        "trade_id": "F001", "external_id": "EXT-F001",
        "product_type": "futures", "direction": "buy",
        "quantity": 10, "price": 5_000.0,         # 10 ES contracts at 5,000
        "trade_date": date(2026, 5, 13),          # Wednesday
        "counterparty": "EXCHANGE-CME", "portfolio": "FUT-USD-1",
        "underlying": "ES",
    }
    trade = parse_fix_like_message(msg)
    assert trade.product_type == "futures"
    assert trade.value_date == date(2026, 5, 14)  # T+1 = Thursday
    assert trade.underlying == "ES"


def test_futures_requires_underlying():
    msg = {
        "trade_id": "F002", "external_id": "EXT-F002",
        "product_type": "futures", "direction": "buy",
        "quantity": 10, "price": 5_000.0,
        "trade_date": date(2026, 5, 13),
        "counterparty": "EXCHANGE-CME", "portfolio": "FUT-USD-1",
        # underlying missing
    }
    with pytest.raises(ValueError, match="underlying"):
        parse_fix_like_message(msg)


def test_futures_settlement_produces_one_cash_flow():
    """Futures stub: 1 cash settlement = qty × price on value_date."""
    msg = {
        "trade_id": "F003", "external_id": "EXT-F003",
        "product_type": "futures", "direction": "buy",
        "quantity": 10, "price": 5_000.0,
        "trade_date": date(2026, 5, 13),
        "counterparty": "EXCHANGE-CME", "portfolio": "FUT-USD-1",
        "underlying": "ES",
    }
    trade = parse_fix_like_message(msg)
    settlements = project_settlements([trade])
    assert len(settlements) == 1
    assert settlements[0].currency == "USD"
    assert settlements[0].amount == -50_000.0   # buy = outflow, 10 × 5000


# ---------------------------------------------------------------------------
# FX option booking
# ---------------------------------------------------------------------------

def test_fx_option_books_with_t_plus_2_value_date():
    """FX option premium settles on FX spot convention (T+2)."""
    msg = {
        "trade_id": "O001", "external_id": "EXT-O001",
        "product_type": "fx_option", "direction": "buy",
        "quantity": 1_000_000, "price": 0.0085,    # 85bp premium on 1M notional
        "trade_date": date(2026, 5, 13),           # Wednesday
        "counterparty": "BANK-A", "portfolio": "PB-USD-1",
        "currency_pair": "USD/SGD",
        "strike": 1.3500,
        "expiry_date": date(2026, 8, 13),          # 3M expiry
        "option_type": "call",
    }
    trade = parse_fix_like_message(msg)
    assert trade.product_type == "fx_option"
    assert trade.value_date == date(2026, 5, 15)   # T+2 = Friday
    assert trade.option_type == "call"
    assert trade.strike == 1.3500


def test_fx_option_requires_strike():
    msg = {
        "trade_id": "O002", "external_id": "EXT-O002",
        "product_type": "fx_option", "direction": "buy",
        "quantity": 1_000_000, "price": 0.0085,
        "trade_date": date(2026, 5, 13),
        "counterparty": "BANK-A", "portfolio": "PB-USD-1",
        "currency_pair": "USD/SGD",
        "expiry_date": date(2026, 8, 13),
        "option_type": "call",
        # strike missing
    }
    with pytest.raises(ValueError, match="strike"):
        parse_fix_like_message(msg)


def test_fx_option_requires_option_type():
    msg = {
        "trade_id": "O003", "external_id": "EXT-O003",
        "product_type": "fx_option", "direction": "buy",
        "quantity": 1_000_000, "price": 0.0085,
        "trade_date": date(2026, 5, 13),
        "counterparty": "BANK-A", "portfolio": "PB-USD-1",
        "currency_pair": "USD/SGD",
        "strike": 1.3500,
        "expiry_date": date(2026, 8, 13),
        # option_type missing
    }
    with pytest.raises(ValueError, match="option_type"):
        parse_fix_like_message(msg)


def test_fx_option_settlement_is_premium_outflow_on_buy():
    """Buy option: pay premium (negative cash flow in quote ccy)."""
    msg = {
        "trade_id": "O004", "external_id": "EXT-O004",
        "product_type": "fx_option", "direction": "buy",
        "quantity": 1_000_000, "price": 0.0085,
        "trade_date": date(2026, 5, 13),
        "counterparty": "BANK-A", "portfolio": "PB-USD-1",
        "currency_pair": "USD/SGD",
        "strike": 1.3500,
        "expiry_date": date(2026, 8, 13),
        "option_type": "call",
    }
    trade = parse_fix_like_message(msg)
    settlements = project_settlements([trade])
    assert len(settlements) == 1
    assert settlements[0].currency == "SGD"      # quote ccy
    assert settlements[0].amount == -8_500.0     # buy = outflow, 1M × 0.0085


def test_fx_option_sell_flips_premium_sign():
    """Sell (write) option: receive premium (positive cash flow)."""
    msg = {
        "trade_id": "O005", "external_id": "EXT-O005",
        "product_type": "fx_option", "direction": "sell",
        "quantity": 1_000_000, "price": 0.0085,
        "trade_date": date(2026, 5, 13),
        "counterparty": "BANK-A", "portfolio": "PB-USD-1",
        "currency_pair": "USD/SGD",
        "strike": 1.3500,
        "expiry_date": date(2026, 8, 13),
        "option_type": "put",
    }
    trade = parse_fix_like_message(msg)
    settlements = project_settlements([trade])
    assert settlements[0].amount == +8_500.0     # sell = inflow
