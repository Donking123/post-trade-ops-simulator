"""Tests for M10: Plotly dashboard.

Smoke tests verifying:
- Each panel function returns a Plotly Figure
- Empty inputs produce empty-state figures (no errors)
- Breaks queue includes rows from all 3 break sources
- Breaks queue sorts by urgency (critical first)
- Settlement calendar filters by horizon
- Recon variance handles None values without errors
"""

from datetime import date

import plotly.graph_objects as go

from post_trade.confirmation import MatchBreak
from post_trade.settlement import Settlement, SettlementBreak
from post_trade.reconciliation import ReconBreak
from post_trade.dashboard import (
    build_breaks_queue,
    build_recon_variance,
    build_settlement_calendar,
)


# ---------------------------------------------------------------------------
# Breaks queue
# ---------------------------------------------------------------------------

def test_breaks_queue_empty_returns_clean_day_figure():
    fig = build_breaks_queue()
    assert isinstance(fig, go.Figure)
    # The "clean day" annotation should be in the figure
    annotations_text = [a.text for a in fig.layout.annotations]
    assert any("clean day" in t.lower() for t in annotations_text)


def test_breaks_queue_merges_all_three_sources():
    match = MatchBreak(trade_id="T1", break_type="no_match")
    sett = SettlementBreak(break_id="B1", trade_id="T2",
                           break_type="amount_mismatch", our_value=100, their_value=200)
    recon = ReconBreak(break_id="B2", break_type="position_qty_mismatch",
                       portfolio="EQ-SG-1", key="DBS",
                       our_value=1000, their_value=999)

    fig = build_breaks_queue([match], [sett], [recon])
    # Should be a Table figure
    assert isinstance(fig, go.Figure)
    assert len(fig.data) == 1
    # Each break shows up as one row — cells values has 6 cols, each col has 3 entries
    table = fig.data[0]
    cell_values = table.cells.values
    assert len(cell_values[0]) == 3   # 3 rows total


def test_breaks_queue_sorts_critical_breaks_first():
    """missing_our_side (urgency 4) should appear before missing_their_side (urgency 1)."""
    low_urgency = MatchBreak(trade_id="T1", break_type="no_match")        # urgency 1
    critical = SettlementBreak(break_id="B1", trade_id="T2",
                               break_type="missing_our_side")              # urgency 4

    fig = build_breaks_queue([low_urgency], [critical])
    table = fig.data[0]
    cells = table.cells.values
    # Columns: [Urgency, Source, Break Type, Description, Trade ID, Key, Our, Their]
    # Break Type is column index 2; first row should be missing_our_side
    break_type_col = cells[2]
    assert break_type_col[0] == "missing_our_side"
    # First row's Urgency column should read "CRITICAL"
    urgency_col = cells[0]
    assert urgency_col[0] == "CRITICAL"


# ---------------------------------------------------------------------------
# Settlement calendar
# ---------------------------------------------------------------------------

def test_settlement_calendar_empty_returns_empty_state():
    fig = build_settlement_calendar([], as_of=date(2026, 5, 13))
    assert isinstance(fig, go.Figure)
    annotations_text = [a.text for a in fig.layout.annotations]
    assert any("no settlements" in t.lower() for t in annotations_text)


def test_settlement_calendar_filters_by_horizon():
    """Settlements outside the horizon window are excluded."""
    in_window = Settlement(
        settlement_id="S1", trade_id="T1", event_id=None,
        settlement_date=date(2026, 5, 15),    # 2 days out
        currency="SGD", amount=-1000.0,
        account="EQ-SG-1", counterparty="X", description="in window",
    )
    out_of_window = Settlement(
        settlement_id="S2", trade_id="T2", event_id=None,
        settlement_date=date(2027, 5, 13),    # 365 days out
        currency="SGD", amount=-2000.0,
        account="EQ-SG-1", counterparty="X", description="out of window",
    )

    fig = build_settlement_calendar(
        [in_window, out_of_window],
        horizon_days=30,
        as_of=date(2026, 5, 13),
    )
    # Should have 1 trace (only SGD), with only the in-window settlement
    assert len(fig.data) == 1
    trace = fig.data[0]
    assert len(trace.x) == 1   # only 1 date


def test_settlement_calendar_groups_by_currency():
    settlements = [
        Settlement(settlement_id="S1", trade_id="T1", event_id=None,
                   settlement_date=date(2026, 5, 15), currency="USD",
                   amount=1000.0, account="A", counterparty="X", description=""),
        Settlement(settlement_id="S2", trade_id="T2", event_id=None,
                   settlement_date=date(2026, 5, 15), currency="SGD",
                   amount=-1500.0, account="A", counterparty="X", description=""),
    ]
    fig = build_settlement_calendar(settlements, as_of=date(2026, 5, 13))
    # 2 currencies = 2 traces
    assert len(fig.data) == 2
    trace_names = [t.name for t in fig.data]
    assert "USD" in trace_names
    assert "SGD" in trace_names


# ---------------------------------------------------------------------------
# Recon variance
# ---------------------------------------------------------------------------

def test_recon_variance_empty_returns_empty_state():
    fig = build_recon_variance([])
    assert isinstance(fig, go.Figure)
    annotations_text = [a.text for a in fig.layout.annotations]
    assert any("no reconciliation" in t.lower() for t in annotations_text)


def test_recon_variance_pairs_our_and_their_single_type():
    """Position-only breaks render as a single subplot with 3 traces
    (Our view, PB view, Variance)."""
    breaks = [
        ReconBreak(break_id="B1", break_type="position_qty_mismatch",
                   portfolio="EQ-SG-1", key="DBS", our_value=1000, their_value=999),
        ReconBreak(break_id="B2", break_type="missing_position",
                   portfolio="EQ-SG-1", key="UOB", our_value=None, their_value=500),
    ]
    fig = build_recon_variance(breaks)
    assert len(fig.data) == 3
    trace_names = [t.name for t in fig.data]
    assert "Our view" in trace_names
    assert "PB view" in trace_names
    assert "Variance (Our − PB)" in trace_names
    for trace in fig.data:
        assert len(trace.y) == 2


def test_recon_variance_splits_position_and_cash_into_subplots():
    """Mixed position + cash breaks render as 2 subplots (6 traces total — 3 per panel)."""
    breaks = [
        ReconBreak(break_id="B1", break_type="position_qty_mismatch",
                   portfolio="EQ-SG-1", key="DBS", our_value=1000, their_value=999),
        ReconBreak(break_id="B2", break_type="cash_balance_mismatch",
                   portfolio="EQ-SG-1", key="SGD", our_value=-100.0, their_value=-150.0),
    ]
    fig = build_recon_variance(breaks)
    assert len(fig.data) == 6   # 3 traces × 2 subplots
    # First three traces target xaxis "x" (position subplot); last three target "x2" (cash subplot)
    assert fig.data[0].xaxis == "x"
    assert fig.data[3].xaxis == "x2"


def test_recon_variance_variance_bar_is_signed_difference():
    """Variance bar = Our − Their. Positive when our > their; negative otherwise."""
    breaks = [
        ReconBreak(break_id="B1", break_type="position_qty_mismatch",
                   portfolio="EQ-SG-1", key="DBS",
                   our_value=1000, their_value=999),    # our > their -> +1
        ReconBreak(break_id="B2", break_type="missing_position",
                   portfolio="EQ-SG-1", key="UOB",
                   our_value=None, their_value=500),    # our (0) < their (500) -> -500
    ]
    fig = build_recon_variance(breaks)
    variance_trace = next(t for t in fig.data if t.name == "Variance (Our − PB)")
    assert variance_trace.y == (1.0, -500.0)


def test_recon_variance_handles_none_values():
    """missing_position breaks have None on one side — should coerce to 0 for plotting."""
    breaks = [
        ReconBreak(break_id="B1", break_type="missing_position",
                   portfolio="EQ-SG-1", key="SGX",
                   our_value=None, their_value=500),
    ]
    fig = build_recon_variance(breaks)
    # Should not raise; "Our view" trace should have 0 for the missing value
    our_trace = next(t for t in fig.data if t.name == "Our view")
    assert our_trace.y == (0.0,)
