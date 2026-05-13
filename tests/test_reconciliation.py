"""Tests for M5: Reconciliation.

Invariants verified:
- Equity positions sum buys minus sells per (portfolio, ticker)
- Position computation groups correctly across portfolios + tickers
- Non-equity trades excluded from equity position computation
- Equity trades without `underlying` field excluded
- Cash balances aggregate settlement amounts per (portfolio, currency)
- Equity recon: exact match returns no breaks
- Equity recon: shares mismatch flags position_qty_mismatch
- Equity recon: position only on one side flags missing_position
- Cash recon: within tolerance returns no breaks
- Cash recon: beyond tolerance flags cash_balance_mismatch
- Cash recon: balance only on one side flags missing_cash
"""

from datetime import date

from post_trade.trade import parse_fix_like_message
from post_trade.settlement import Settlement, project_settlements
from post_trade.reconciliation import (
    CashBalance,
    EquityPosition,
    ReconBreak,
    compute_cash_balances,
    compute_equity_positions,
    reconcile_cash_balances,
    reconcile_equity_positions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _equity(trade_id, direction, qty, underlying="DBS", portfolio="EQ-SG-1"):
    return parse_fix_like_message({
        "trade_id": trade_id, "external_id": f"EXT-{trade_id}",
        "product_type": "cash_equity", "direction": direction,
        "quantity": qty, "price": 28.50,
        "trade_date": date(2026, 5, 13),
        "counterparty": "BROKER-X", "portfolio": portfolio,
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


# ---------------------------------------------------------------------------
# Equity position computation
# ---------------------------------------------------------------------------

def test_equity_position_sums_buys_minus_sells_for_same_key():
    trades = [
        _equity("T1", "buy", 1_000),
        _equity("T2", "buy", 500),
        _equity("T3", "sell", 300),
    ]
    positions = compute_equity_positions(trades)
    assert len(positions) == 1
    assert positions[0].shares == 1_200  # 1000 + 500 - 300


def test_equity_position_groups_by_portfolio_and_ticker():
    trades = [
        _equity("T1", "buy", 1_000, underlying="DBS", portfolio="EQ-SG-1"),
        _equity("T2", "buy", 500, underlying="DBS", portfolio="EQ-SG-2"),
        _equity("T3", "buy", 200, underlying="UOB", portfolio="EQ-SG-1"),
    ]
    positions = compute_equity_positions(trades)
    by_key = {(p.portfolio, p.ticker): p.shares for p in positions}
    assert by_key[("EQ-SG-1", "DBS")] == 1_000
    assert by_key[("EQ-SG-2", "DBS")] == 500
    assert by_key[("EQ-SG-1", "UOB")] == 200


def test_equity_position_excludes_non_equity_trades():
    trades = [_equity("T1", "buy", 1_000), _fx_spot()]
    positions = compute_equity_positions(trades)
    assert len(positions) == 1
    assert positions[0].ticker == "DBS"


def test_equity_position_excludes_trades_without_underlying():
    """Equity trade missing the `underlying` field can't be reconciled."""
    no_ticker = parse_fix_like_message({
        "trade_id": "T_NO", "external_id": "EXT-NO",
        "product_type": "cash_equity", "direction": "buy",
        "quantity": 1_000, "price": 28.50,
        "trade_date": date(2026, 5, 13),
        "counterparty": "BROKER-X", "portfolio": "EQ-SG-1",
        # underlying omitted
    })
    positions = compute_equity_positions([no_ticker])
    assert positions == []


# ---------------------------------------------------------------------------
# Cash balance computation
# ---------------------------------------------------------------------------

def test_cash_balance_aggregates_settlements_per_currency():
    fx = _fx_spot()
    eq = _equity("T1", "buy", 1_000)
    settlements = project_settlements([fx, eq])

    balances = compute_cash_balances(settlements)
    by_key = {(b.portfolio, b.currency): b.amount for b in balances}

    # FX buy: +1M USD inflow, -1.345M SGD outflow (PB-USD-1)
    assert by_key[("PB-USD-1", "USD")] == 1_000_000
    # FX SGD leg + Equity SGD leg are in different portfolios, so they stay separate
    assert by_key[("PB-USD-1", "SGD")] == -1_345_000
    # Equity buy: -28.5K SGD outflow (EQ-SG-1)
    assert by_key[("EQ-SG-1", "SGD")] == -28_500


# ---------------------------------------------------------------------------
# Equity reconciliation
# ---------------------------------------------------------------------------

def test_equity_recon_exact_match_returns_no_breaks():
    our = [EquityPosition(portfolio="EQ-SG-1", ticker="DBS", shares=1_000)]
    pb = [EquityPosition(portfolio="EQ-SG-1", ticker="DBS", shares=1_000)]
    assert reconcile_equity_positions(our, pb) == []


def test_equity_recon_qty_mismatch_flagged():
    our = [EquityPosition(portfolio="EQ-SG-1", ticker="DBS", shares=1_000)]
    pb = [EquityPosition(portfolio="EQ-SG-1", ticker="DBS", shares=999)]   # off by 1

    breaks = reconcile_equity_positions(our, pb)
    assert len(breaks) == 1
    assert breaks[0].break_type == "position_qty_mismatch"
    assert breaks[0].our_value == 1_000
    assert breaks[0].their_value == 999


def test_equity_recon_missing_on_our_side_flagged():
    our: list[EquityPosition] = []
    pb = [EquityPosition(portfolio="EQ-SG-1", ticker="DBS", shares=1_000)]

    breaks = reconcile_equity_positions(our, pb)
    assert len(breaks) == 1
    assert breaks[0].break_type == "missing_position"
    assert breaks[0].our_value is None
    assert breaks[0].their_value == 1_000


def test_equity_recon_missing_on_pb_side_flagged():
    our = [EquityPosition(portfolio="EQ-SG-1", ticker="DBS", shares=1_000)]
    pb: list[EquityPosition] = []

    breaks = reconcile_equity_positions(our, pb)
    assert breaks[0].break_type == "missing_position"
    assert breaks[0].our_value == 1_000
    assert breaks[0].their_value is None


# ---------------------------------------------------------------------------
# Cash reconciliation
# ---------------------------------------------------------------------------

def test_cash_recon_within_tolerance_no_break():
    """$1 drift on a $28,500 balance is within max(1bp, $1) = $2.85 tolerance."""
    our = [CashBalance(portfolio="EQ-SG-1", currency="SGD", amount=-28_500.0)]
    pb = [CashBalance(portfolio="EQ-SG-1", currency="SGD", amount=-28_499.0)]
    assert reconcile_cash_balances(our, pb) == []


def test_cash_recon_beyond_tolerance_flagged():
    """$100 drift on $28,500 is well beyond tolerance."""
    our = [CashBalance(portfolio="EQ-SG-1", currency="SGD", amount=-28_500.0)]
    pb = [CashBalance(portfolio="EQ-SG-1", currency="SGD", amount=-28_400.0)]

    breaks = reconcile_cash_balances(our, pb)
    assert len(breaks) == 1
    assert breaks[0].break_type == "cash_balance_mismatch"


def test_cash_recon_missing_balance_flagged():
    our = [CashBalance(portfolio="EQ-SG-1", currency="SGD", amount=-28_500.0)]
    pb: list[CashBalance] = []

    breaks = reconcile_cash_balances(our, pb)
    assert len(breaks) == 1
    assert breaks[0].break_type == "missing_cash"
