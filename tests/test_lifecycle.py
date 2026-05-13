"""Tests for M3: Lifecycle events.

Invariants verified:
- IRS generates quarterly resets from value_date to maturity
- IRS reset payment uses ACT/360 day-count: notional * rate * days/360
- IRS with no fixings table returns empty list (graceful degrade)
- IRS resets stop at horizon if horizon < maturity
- Cash equity generates dividend events for ex-dates in window
- Long (buy) position receives positive cash; short (sell) pays
- Equity with no underlying field returns empty list
- Equity with no dividends in calendar returns empty list
- FX spot generates no lifecycle events
- Dividend ex-dates before value_date or after horizon are skipped
"""

from datetime import date

from post_trade.trade import parse_fix_like_message
from post_trade.lifecycle import (
    DividendCalendar,
    LifecycleEvent,
    SOFRFixings,
    generate_lifecycle_events,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _irs(tenor="1Y"):
    """5Y IRS booked 2026-05-13 at 4.2% fixed against SOFR."""
    return parse_fix_like_message({
        "trade_id": "T_IRS", "external_id": "EXT-IRS",
        "product_type": "irs", "direction": "sell",
        "quantity": 100_000_000, "price": 0.042,
        "trade_date": date(2026, 5, 13),
        "counterparty": "BANK-B", "portfolio": "PB-USD-1",
        "tenor": tenor, "floating_index": "SOFR",
    })


def _equity(direction="buy", underlying="DBS"):
    """1k DBS shares booked 2026-05-13 at SGD 28.50."""
    return parse_fix_like_message({
        "trade_id": "T_EQ", "external_id": "EXT-EQ",
        "product_type": "cash_equity", "direction": direction,
        "quantity": 1_000, "price": 28.50,
        "trade_date": date(2026, 5, 13),
        "counterparty": "BROKER-X", "portfolio": "EQ-SG-1",
        "underlying": underlying,
    })


def _fx_spot():
    return parse_fix_like_message({
        "trade_id": "T_FX", "external_id": "EXT-FX",
        "product_type": "fx_spot", "direction": "buy",
        "quantity": 1_000_000, "price": 1.3450,
        "trade_date": date(2026, 5, 13),
        "counterparty": "BANK-A", "portfolio": "PB-USD-1",
        "currency_pair": "USD/SGD",
    })


def _flat_sofr_fixings(rate: float, start: date, periods: int = 8):
    """Build a SOFRFixings table with `rate` at quarterly intervals from start."""
    fixings: dict[date, float] = {}
    d = start
    for _ in range(periods):
        fixings[d] = rate
        # Step forward 3 months
        month = (d.month + 3 - 1) % 12 + 1
        year = d.year + (d.month + 3 - 1) // 12
        d = d.replace(year=year, month=month)
    return SOFRFixings(fixings=fixings)


# ---------------------------------------------------------------------------
# IRS reset tests
# ---------------------------------------------------------------------------

def test_irs_generates_quarterly_resets_over_tenor():
    """1Y IRS should generate 4 quarterly reset events."""
    irs = _irs(tenor="1Y")
    fixings = _flat_sofr_fixings(0.05, start=irs.value_date, periods=4)
    events = generate_lifecycle_events(irs, sofr_fixings=fixings)
    assert len(events) == 4
    assert all(e.event_type == "ir_reset" for e in events)


def test_irs_reset_payment_uses_act_360_day_count():
    """Payment = notional * rate * days/360 (ACT/360)."""
    irs = _irs(tenor="1Y")
    fixings = _flat_sofr_fixings(0.05, start=irs.value_date, periods=4)
    events = generate_lifecycle_events(irs, sofr_fixings=fixings)

    first = events[0]
    # First period: 2026-05-13 to 2026-08-13 = 92 days
    assert first.payload["day_count"] == 92
    # Payment = 100M * 0.05 * 92/360 = 1,277,777.78
    expected = 100_000_000 * 0.05 * (92 / 360.0)
    assert abs(first.payload["payment_amount"] - expected) < 1e-6


def test_irs_with_no_fixings_returns_empty():
    """Graceful degrade when no fixings supplied."""
    irs = _irs()
    events = generate_lifecycle_events(irs)  # no sofr_fixings
    assert events == []


def test_irs_resets_capped_by_horizon():
    """Horizon shorter than tenor caps the number of resets."""
    irs = _irs(tenor="2Y")  # would be 8 quarterly resets full-tenor
    fixings = _flat_sofr_fixings(0.05, start=irs.value_date, periods=8)
    horizon = date(2026, 11, 13)  # 6 months out -> only 2 resets fit
    events = generate_lifecycle_events(irs, sofr_fixings=fixings, horizon=horizon)
    assert len(events) == 2


# ---------------------------------------------------------------------------
# Equity dividend tests
# ---------------------------------------------------------------------------

def test_equity_dividend_in_window_generates_event():
    eq = _equity(direction="buy", underlying="DBS")
    cal = DividendCalendar(dividends={
        "DBS": [(date(2026, 8, 15), 0.39)],  # 39 cents per share
    })
    events = generate_lifecycle_events(eq, dividend_calendar=cal)
    assert len(events) == 1
    assert events[0].event_type == "equity_dividend"
    assert events[0].event_date == date(2026, 8, 15)


def test_equity_dividend_buy_position_receives_positive_cash():
    """Long position: cash_flow = +shares * div_per_share."""
    eq = _equity(direction="buy", underlying="DBS")
    cal = DividendCalendar(dividends={"DBS": [(date(2026, 8, 15), 0.39)]})
    events = generate_lifecycle_events(eq, dividend_calendar=cal)
    expected = 1_000 * 0.39  # 390.00
    assert abs(events[0].payload["cash_flow"] - expected) < 1e-6


def test_equity_dividend_sell_position_pays_negative_cash():
    """Short position: cash_flow = -shares * div_per_share."""
    eq = _equity(direction="sell", underlying="DBS")
    cal = DividendCalendar(dividends={"DBS": [(date(2026, 8, 15), 0.39)]})
    events = generate_lifecycle_events(eq, dividend_calendar=cal)
    expected = -1_000 * 0.39
    assert abs(events[0].payload["cash_flow"] - expected) < 1e-6


def test_equity_with_no_underlying_returns_empty():
    """Without a ticker, can't look up dividends."""
    eq = _equity(underlying=None)
    cal = DividendCalendar(dividends={"DBS": [(date(2026, 8, 15), 0.39)]})
    events = generate_lifecycle_events(eq, dividend_calendar=cal)
    assert events == []


def test_equity_dividends_outside_window_are_skipped():
    """Ex-dates before value_date or after horizon shouldn't generate events."""
    eq = _equity(underlying="DBS")
    cal = DividendCalendar(dividends={
        "DBS": [
            (date(2025, 8, 15), 0.39),  # before value_date
            (date(2026, 8, 15), 0.39),  # in window
            (date(2028, 8, 15), 0.39),  # after default 1Y horizon
        ],
    })
    events = generate_lifecycle_events(eq, dividend_calendar=cal)
    assert len(events) == 1
    assert events[0].event_date == date(2026, 8, 15)


# ---------------------------------------------------------------------------
# FX (no events) tests
# ---------------------------------------------------------------------------

def test_fx_spot_generates_no_lifecycle_events():
    """FX spot has no lifecycle events between trade date and settlement."""
    fx = _fx_spot()
    events = generate_lifecycle_events(fx)
    assert events == []
