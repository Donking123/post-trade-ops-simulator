"""Tests for M5: Settlement projection + breaks.

Invariants verified:
- FX buy generates 2 settlements with correct sign (+ base / - quote)
- FX sell flips signs
- Cash equity buy is cash outflow (- SGD)
- Cash equity sell is cash inflow (+ SGD)
- IRS trade alone produces no settlements (lifecycle events drive payments)
- IR reset lifecycle event generates 1 settlement on period_end
- Equity dividend lifecycle event generates 1 settlement with signed cash flow
- Matching their-instruction returns zero breaks
- Amount mismatch beyond 1bp tolerance flags a break
- Amount mismatch within 1bp tolerance does NOT flag
- Date mismatch flags a break
- Missing their_side / our_side breaks recorded
"""

from datetime import date

from post_trade.trade import parse_fix_like_message
from post_trade.lifecycle import LifecycleEvent, SOFRFixings, generate_lifecycle_events
from post_trade.settlement import (
    Settlement,
    SettlementBreak,
    check_breaks,
    project_settlements,
)


# ---------------------------------------------------------------------------
# Trade builders
# ---------------------------------------------------------------------------

def _fx_buy():
    return parse_fix_like_message({
        "trade_id": "T_FX_BUY", "external_id": "EXT_FX_BUY",
        "product_type": "fx_spot", "direction": "buy",
        "quantity": 1_000_000, "price": 1.3450,
        "trade_date": date(2026, 5, 13),
        "counterparty": "BANK-A", "portfolio": "PB-USD-1",
        "currency_pair": "USD/SGD",
    })


def _fx_sell():
    return parse_fix_like_message({
        "trade_id": "T_FX_SELL", "external_id": "EXT_FX_SELL",
        "product_type": "fx_spot", "direction": "sell",
        "quantity": 1_000_000, "price": 1.3450,
        "trade_date": date(2026, 5, 13),
        "counterparty": "BANK-A", "portfolio": "PB-USD-1",
        "currency_pair": "USD/SGD",
    })


def _equity_buy():
    return parse_fix_like_message({
        "trade_id": "T_EQ_BUY", "external_id": "EXT_EQ_BUY",
        "product_type": "cash_equity", "direction": "buy",
        "quantity": 1_000, "price": 28.50,
        "trade_date": date(2026, 5, 13),
        "counterparty": "BROKER-X", "portfolio": "EQ-SG-1",
        "underlying": "DBS",
    })


def _equity_sell():
    return parse_fix_like_message({
        "trade_id": "T_EQ_SELL", "external_id": "EXT_EQ_SELL",
        "product_type": "cash_equity", "direction": "sell",
        "quantity": 1_000, "price": 28.50,
        "trade_date": date(2026, 5, 13),
        "counterparty": "BROKER-X", "portfolio": "EQ-SG-1",
        "underlying": "DBS",
    })


def _irs():
    return parse_fix_like_message({
        "trade_id": "T_IRS", "external_id": "EXT_IRS",
        "product_type": "irs", "direction": "sell",
        "quantity": 100_000_000, "price": 0.042,
        "trade_date": date(2026, 5, 13),
        "counterparty": "BANK-B", "portfolio": "PB-USD-1",
        "tenor": "1Y", "floating_index": "SOFR",
    })


# ---------------------------------------------------------------------------
# Trade-level projection
# ---------------------------------------------------------------------------

def test_fx_buy_generates_two_settlements_with_correct_signs():
    fx = _fx_buy()
    settlements = project_settlements([fx])

    assert len(settlements) == 2
    by_ccy = {s.currency: s for s in settlements}
    # Buy USD/SGD: receive 1M USD, pay 1.345M SGD
    assert by_ccy["USD"].amount == 1_000_000
    assert by_ccy["SGD"].amount == -1_345_000.0


def test_fx_sell_flips_signs():
    fx = _fx_sell()
    settlements = project_settlements([fx])

    by_ccy = {s.currency: s for s in settlements}
    assert by_ccy["USD"].amount == -1_000_000
    assert by_ccy["SGD"].amount == +1_345_000.0


def test_cash_equity_buy_is_cash_outflow():
    eq = _equity_buy()
    settlements = project_settlements([eq])

    assert len(settlements) == 1
    assert settlements[0].currency == "SGD"
    assert settlements[0].amount == -28_500.0   # negative = pay


def test_cash_equity_sell_is_cash_inflow():
    eq = _equity_sell()
    settlements = project_settlements([eq])

    assert settlements[0].amount == +28_500.0   # positive = receive


def test_irs_trade_alone_has_no_settlements():
    """IRS principal is notional, not exchanged. Trade-level produces 0."""
    irs = _irs()
    settlements = project_settlements([irs])
    assert settlements == []


def test_ir_reset_lifecycle_event_generates_one_settlement():
    irs = _irs()
    # Provide fixings for the value_date so we get a reset
    fixings = SOFRFixings(fixings={
        date(2026, 5, 13): 0.05,
        date(2026, 8, 13): 0.05,
        date(2026, 11, 13): 0.05,
        date(2027, 2, 13): 0.05,
    })
    events = generate_lifecycle_events(irs, sofr_fixings=fixings)
    assert len(events) > 0

    settlements = project_settlements([irs], events)
    irs_settlements = [s for s in settlements if s.trade_id == "T_IRS"]
    assert len(irs_settlements) == len(events)
    # IRS 'sell' = pay floating -> negative amount
    assert all(s.amount < 0 for s in irs_settlements)


def test_equity_dividend_lifecycle_event_generates_settlement():
    eq = _equity_buy()
    div_event = LifecycleEvent(
        event_id="DIV-1", trade_id=eq.trade_id, event_type="equity_dividend",
        event_date=date(2026, 8, 15),
        payload={"ticker": "DBS", "dividend_per_share": 0.39,
                 "shares": 1_000, "cash_flow": 390.0, "direction": "buy"},
    )

    settlements = project_settlements([eq], [div_event])
    dividend_setts = [s for s in settlements if s.event_id == "DIV-1"]

    assert len(dividend_setts) == 1
    assert dividend_setts[0].amount == 390.0
    assert dividend_setts[0].currency == "SGD"


# ---------------------------------------------------------------------------
# Breaks
# ---------------------------------------------------------------------------

def test_matching_instructions_return_zero_breaks():
    eq = _equity_buy()
    settlements = project_settlements([eq])
    their = [{
        "trade_id": eq.trade_id,
        "settlement_date": settlements[0].settlement_date,
        "currency": settlements[0].currency,
        "amount": settlements[0].amount,
    }]

    breaks = check_breaks(settlements, their)
    assert breaks == []


def test_amount_mismatch_beyond_tolerance_flagged():
    eq = _equity_buy()   # our settlement: -28,500 SGD
    settlements = project_settlements([eq])
    their = [{
        "trade_id": eq.trade_id,
        "settlement_date": settlements[0].settlement_date,
        "currency": settlements[0].currency,
        "amount": -28_400.0,   # off by 100 (well above 1bp = $2.85, well above $1 floor)
    }]

    breaks = check_breaks(settlements, their)
    assert any(b.break_type == "amount_mismatch" for b in breaks)


def test_amount_within_tolerance_no_break():
    """1bp of $28,500 is $2.85 — drift of $1 stays under tolerance (max($2.85, $1) = $2.85)."""
    eq = _equity_buy()
    settlements = project_settlements([eq])
    their = [{
        "trade_id": eq.trade_id,
        "settlement_date": settlements[0].settlement_date,
        "currency": settlements[0].currency,
        "amount": settlements[0].amount - 1.0,   # off by $1
    }]

    breaks = check_breaks(settlements, their)
    assert breaks == []


def test_date_mismatch_flagged():
    eq = _equity_buy()
    settlements = project_settlements([eq])
    their = [{
        "trade_id": eq.trade_id,
        "settlement_date": date(2026, 5, 18),     # we have 5-15
        "currency": settlements[0].currency,
        "amount": settlements[0].amount,
    }]

    breaks = check_breaks(settlements, their)
    assert any(b.break_type == "date_mismatch" for b in breaks)


def test_missing_their_side_flagged():
    eq = _equity_buy()
    settlements = project_settlements([eq])

    breaks = check_breaks(settlements, [])    # no counterparty instructions
    assert any(b.break_type == "missing_their_side" for b in breaks)


def test_missing_our_side_flagged():
    eq = _equity_buy()
    settlements = project_settlements([eq])
    # Instruction references a trade we don't have a settlement for
    their = [{
        "trade_id": "T_UNKNOWN",
        "settlement_date": date(2026, 5, 15),
        "currency": "SGD",
        "amount": 1000.0,
    }, {
        "trade_id": eq.trade_id,
        "settlement_date": settlements[0].settlement_date,
        "currency": settlements[0].currency,
        "amount": settlements[0].amount,
    }]

    breaks = check_breaks(settlements, their)
    assert any(b.break_type == "missing_our_side" and b.trade_id == "T_UNKNOWN" for b in breaks)
