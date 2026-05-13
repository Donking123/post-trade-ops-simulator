"""Tests for M2: Confirmation workflow.

Invariants verified:
- Layer-1 exact external_id match progresses pending -> affirmed
- Layer-1 match with field mismatch flags 'disputed' + records breaks
- Price mismatch within tolerance does NOT flag a break
- Layer-2 fuzzy tuple match works when external_id is absent
- Trades with no counterparty reply remain 'pending' and log a 'no_match' break
- ISDA confirm generated for IRS contains all 10 required fields
- ISDA confirm not applicable for FX spot / cash equity
- Confirm step progresses only IRS-affirmed -> confirmed; FX/equity stay affirmed
"""

from datetime import date

import pytest

from post_trade.trade import parse_fix_like_message
from post_trade.confirmation import (
    MatchBreak,
    confirm_irs_trades,
    generate_isda_confirm,
    match_against_counterparty,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fx_trade(trade_id="T001", external_id="EXT-001", price=1.3450, qty=1_000_000):
    """Build a sample FX spot trade for tests."""
    return parse_fix_like_message({
        "trade_id": trade_id,
        "external_id": external_id,
        "product_type": "fx_spot",
        "direction": "buy",
        "quantity": qty,
        "price": price,
        "trade_date": date(2026, 5, 13),
        "counterparty": "BANK-A",
        "portfolio": "PB-USD-1",
        "currency_pair": "USD/SGD",
    })


def _irs_trade(trade_id="T002", external_id="EXT-002", rate=0.042, notional=100_000_000):
    """Build a sample IRS trade for tests."""
    return parse_fix_like_message({
        "trade_id": trade_id,
        "external_id": external_id,
        "product_type": "irs",
        "direction": "sell",
        "quantity": notional,
        "price": rate,
        "trade_date": date(2026, 5, 13),
        "counterparty": "BANK-B",
        "portfolio": "PB-USD-1",
        "tenor": "5Y",
        "floating_index": "SOFR",
    })


def _matching_reply(trade):
    """Build a counterparty reply that exactly matches a trade."""
    return {
        "external_id": trade.external_id,
        "counterparty": trade.counterparty,
        "product_type": trade.product_type,
        "quantity": trade.quantity,
        "price": trade.price,
        "value_date": trade.value_date,
    }


# ---------------------------------------------------------------------------
# Layer-1 matching tests
# ---------------------------------------------------------------------------

def test_external_id_exact_match_affirms_trade():
    trade = _fx_trade()
    reply = _matching_reply(trade)

    trades, breaks = match_against_counterparty([trade], [reply])

    assert trades[0].confirmation_status == "affirmed"
    assert breaks == []


def test_external_id_match_with_qty_mismatch_disputes():
    """Same external_id but different quantity -> dispute + qty_mismatch break."""
    trade = _fx_trade(qty=1_000_000)
    reply = _matching_reply(trade)
    reply["quantity"] = 999_999  # off by one

    trades, breaks = match_against_counterparty([trade], [reply])

    assert trades[0].confirmation_status == "disputed"
    assert any(b.break_type == "qty_mismatch" for b in breaks)


def test_external_id_match_with_price_mismatch_disputes():
    """FX price drift larger than 1e-4 tolerance -> dispute."""
    trade = _fx_trade(price=1.3450)
    reply = _matching_reply(trade)
    reply["price"] = 1.3500  # drift > 1e-4

    trades, breaks = match_against_counterparty([trade], [reply])

    assert trades[0].confirmation_status == "disputed"
    assert any(b.break_type == "price_mismatch" for b in breaks)


def test_price_mismatch_within_tolerance_still_affirms():
    """Sub-1e-4 price drift (e.g., 1e-5) should NOT flag a break for FX."""
    trade = _fx_trade(price=1.3450)
    reply = _matching_reply(trade)
    reply["price"] = 1.34501  # drift = 1e-5, well within 1e-4 tolerance

    trades, breaks = match_against_counterparty([trade], [reply])

    assert trades[0].confirmation_status == "affirmed"
    assert breaks == []


# ---------------------------------------------------------------------------
# Layer-2 matching tests
# ---------------------------------------------------------------------------

def test_fuzzy_match_on_tuple_when_no_external_id():
    """Trade with no external_id should still match via Layer-2 tuple match."""
    trade = _fx_trade(external_id=None)
    reply = {
        # no external_id
        "counterparty": trade.counterparty,
        "product_type": trade.product_type,
        "quantity": trade.quantity,
        "price": trade.price,
        "value_date": trade.value_date,
    }

    trades, breaks = match_against_counterparty([trade], [reply])

    assert trades[0].confirmation_status == "affirmed"


# ---------------------------------------------------------------------------
# No-match handling
# ---------------------------------------------------------------------------

def test_no_match_recorded_as_break_and_status_stays_pending():
    """Trade with no counterparty reply at all -> no_match break, status pending.
    Reply must be truly unrelated (different counterparty) so Layer-2 fuzzy
    match doesn't accidentally hit on shared economic fields."""
    trade = _fx_trade(external_id="EXT-XXX")
    unrelated_reply = {
        "external_id": "EXT-OTHER",
        "counterparty": "BANK-Z",            # different counterparty
        "product_type": "fx_spot",
        "quantity": 1_000_000,
        "price": 1.3450,
        "value_date": trade.value_date,
    }

    trades, breaks = match_against_counterparty([trade], [unrelated_reply])

    assert trades[0].confirmation_status == "pending"
    assert any(b.break_type == "no_match" and b.trade_id == trade.trade_id for b in breaks)


# ---------------------------------------------------------------------------
# ISDA confirm generation tests
# ---------------------------------------------------------------------------

def test_isda_confirm_generated_for_irs_with_required_fields():
    """ISDA confirm dict contains all 10 expected economic fields."""
    trade = _irs_trade()
    confirm = generate_isda_confirm(trade)

    expected_fields = {
        "confirm_id", "trade_id", "our_party", "counterparty", "product",
        "notional", "fixed_rate", "floating_index", "tenor",
        "effective_date", "maturity_date",
    }
    assert expected_fields.issubset(confirm.keys())

    assert confirm["trade_id"] == trade.trade_id
    assert confirm["product"] == "IRS"
    assert confirm["notional"] == trade.quantity
    assert confirm["fixed_rate"] == trade.price
    assert confirm["floating_index"] == "SOFR"
    assert confirm["tenor"] == "5Y"


def test_isda_confirm_not_applicable_for_fx_spot():
    """FX spot doesn't use ISDA confirms (CLS handles FX matching)."""
    fx = _fx_trade()
    with pytest.raises(ValueError, match="ISDA confirm not applicable"):
        generate_isda_confirm(fx)


# ---------------------------------------------------------------------------
# Confirm step (progress affirmed IRS -> confirmed) tests
# ---------------------------------------------------------------------------

def test_confirm_step_progresses_only_irs_from_affirmed_to_confirmed():
    """Affirmed IRS -> confirmed (with ISDA confirm generated).
    Affirmed FX spot stays affirmed (no ISDA confirm for vanilla FX)."""
    irs = _irs_trade()
    irs.confirmation_status = "affirmed"
    fx = _fx_trade()
    fx.confirmation_status = "affirmed"

    confirms = confirm_irs_trades([irs, fx])

    assert irs.confirmation_status == "confirmed"
    assert fx.confirmation_status == "affirmed"          # unchanged
    assert len(confirms) == 1
    assert confirms[0]["trade_id"] == irs.trade_id


def test_confirm_step_skips_unaffirmed_irs():
    """IRS still 'pending' or 'disputed' should NOT be confirmed."""
    pending_irs = _irs_trade(trade_id="T_PENDING")
    disputed_irs = _irs_trade(trade_id="T_DISPUTED")
    disputed_irs.confirmation_status = "disputed"

    confirms = confirm_irs_trades([pending_irs, disputed_irs])

    assert pending_irs.confirmation_status == "pending"
    assert disputed_irs.confirmation_status == "disputed"
    assert confirms == []
