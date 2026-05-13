"""M2: Confirmation workflow.

After M1 books a trade, the counterparty books their version at their
firm. These two records should match. M2 is the matching + confirmation
engine.

Two vendor systems modelled (conceptually, with synthetic data):
- CTM (Confirm Trade Manager, DTCC): matches our trades against the
  counterparty's trades on key economic fields. Match -> 'affirmed'.
- Markitwire / OSTTRA: generates long-form ISDA confirmation documents
  for OTC derivatives. Both sides sign -> 'confirmed'.

State progression (from M1 'pending'):
  pending  --(matched in CTM)--------------> affirmed
  affirmed --(ISDA confirm signed, OTC)----> confirmed
  any      --(mismatch on fields)----------> disputed

Two-layer matching strategy:
  Layer 1: exact match on `external_id` (the explicit shared reference;
           in real life, the UTI -- Unique Trade Identifier).
  Layer 2: fuzzy match on (counterparty, product_type, quantity, price,
           value_date) tuple, when `external_id` is missing or differs.

Tolerances for the price field:
  FX rates: |our - their| <= 1e-4 vol points (4 decimal places typical)
  IR rates: |our - their| <= 1e-6 (6 decimal places typical)
  Equity prices: exact (cents granularity, no float drift expected)
  Quantities: exact (integer match required)
  Value dates: exact

Future enhancements parked:
- Real Markitwire / OSTTRA API integration
- Full ISDA long-form confirm template (30+ fields: governing law,
  election notices, collateral, dispute resolution, etc.)
- Multi-step confirmation flow with amendments and breaks-of-position
- Per-asset-class tolerance configuration
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from uuid import uuid4

from post_trade.trade import Trade, ProductType


# Price-field mismatch tolerances per product type.
PRICE_TOLERANCES: dict[ProductType, float] = {
    "fx_spot": 1e-4,
    "fx_forward": 1e-4,
    "fx_option": 1e-6,     # premium quoted as fraction of notional (e.g., 0.0085)
    "irs": 1e-6,
    "cash_equity": 0.0,    # cents granularity, exact match expected
    "futures": 0.01,       # exchange tick sizes (e.g., ES = 0.25); 0.01 covers smaller ticks
}


# Products eligible for ISDA confirmation generation (OTC derivatives).
# Vanilla FX spot and cash equity don't need ISDA confirms — they use
# simpler matching workflows (CLS for FX, CCP novation for equity).
ISDA_CONFIRM_PRODUCTS: set[ProductType] = {"irs"}


@dataclass
class MatchBreak:
    """Recorded when a trade can't be matched, or is matched-with-mismatch."""

    trade_id: str
    break_type: str       # 'no_match' / 'qty_mismatch' / 'price_mismatch' / 'date_mismatch'
    our_value: Any = None
    their_value: Any = None
    flagged_at: datetime = field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# Matching (CTM-style)
# ---------------------------------------------------------------------------

def match_against_counterparty(
    trades: list[Trade],
    counterparty_replies: list[dict[str, Any]],
) -> tuple[list[Trade], list[MatchBreak]]:
    """Match our booked trades against synthetic counterparty replies.

    Layer 1: exact match on external_id (the explicit shared reference)
    Layer 2: fuzzy tuple match on key economic fields

    Updates each Trade's confirmation_status in place:
    - 'affirmed' on successful match
    - 'disputed' on Layer-1 match with field-level mismatch
    - 'pending' (unchanged) if no match found; recorded as 'no_match' break

    Returns (trades, breaks).
    """
    breaks: list[MatchBreak] = []

    # Index replies by external_id for fast Layer-1 lookup
    by_external_id = {
        r["external_id"]: r for r in counterparty_replies if r.get("external_id")
    }

    for trade in trades:
        # Layer 1: exact external_id match
        if trade.external_id and trade.external_id in by_external_id:
            reply = by_external_id[trade.external_id]
            mismatches = _detect_field_mismatches(trade, reply)
            if mismatches:
                trade.confirmation_status = "disputed"
                for break_type, our_val, their_val in mismatches:
                    breaks.append(MatchBreak(
                        trade_id=trade.trade_id,
                        break_type=break_type,
                        our_value=our_val,
                        their_value=their_val,
                    ))
            else:
                trade.confirmation_status = "affirmed"
            continue

        # Layer 2: fuzzy tuple match (no external_id, or external_id not in replies)
        layer2_match = next(
            (r for r in counterparty_replies if _fuzzy_match(trade, r)),
            None,
        )
        if layer2_match is not None:
            trade.confirmation_status = "affirmed"
            continue

        # No match in either layer — leave pending, log break for visibility
        breaks.append(MatchBreak(trade_id=trade.trade_id, break_type="no_match"))

    return trades, breaks


def _fuzzy_match(trade: Trade, reply: dict[str, Any]) -> bool:
    """Layer-2 tuple match: counterparty + product + qty + price + value_date."""
    if reply.get("counterparty") != trade.counterparty:
        return False
    if reply.get("product_type") != trade.product_type:
        return False
    if reply.get("quantity") != trade.quantity:
        return False
    if reply.get("value_date") != trade.value_date:
        return False
    tol = PRICE_TOLERANCES[trade.product_type]
    if abs(reply.get("price", 0.0) - trade.price) > tol:
        return False
    return True


def _detect_field_mismatches(
    trade: Trade,
    reply: dict[str, Any],
) -> list[tuple[str, Any, Any]]:
    """Return (break_type, our_value, their_value) for each mismatching field."""
    mismatches: list[tuple[str, Any, Any]] = []

    if reply.get("quantity") != trade.quantity:
        mismatches.append(("qty_mismatch", trade.quantity, reply.get("quantity")))

    tol = PRICE_TOLERANCES[trade.product_type]
    if abs(reply.get("price", 0.0) - trade.price) > tol:
        mismatches.append(("price_mismatch", trade.price, reply.get("price")))

    if reply.get("value_date") != trade.value_date:
        mismatches.append(("date_mismatch", trade.value_date, reply.get("value_date")))

    return mismatches


# ---------------------------------------------------------------------------
# ISDA confirmation generation (Markitwire-style)
# ---------------------------------------------------------------------------

def confirm_irs_trades(trades: list[Trade]) -> list[dict[str, Any]]:
    """Generate ISDA confirms for affirmed IRS trades; progress to 'confirmed'.

    Only IRS trades that are already 'affirmed' (from match step) get
    confirmed. Vanilla FX and cash equity remain affirmed — they don't
    use ISDA confirms.

    Returns the list of generated confirm dicts.
    """
    confirms: list[dict[str, Any]] = []
    for trade in trades:
        if (trade.product_type in ISDA_CONFIRM_PRODUCTS
                and trade.confirmation_status == "affirmed"):
            confirms.append(generate_isda_confirm(trade))
            trade.confirmation_status = "confirmed"
    return confirms


def generate_isda_confirm(trade: Trade) -> dict[str, Any]:
    """Generate a synthetic ISDA confirmation document for an OTC trade.

    Real ISDA long-form confirms have 30+ fields (governing law, election
    notices, collateral, dispute resolution, etc.). This MVP uses 10
    fields covering the basic economics — sufficient to demonstrate the
    workflow without simulating legal text.

    Raises ValueError if the trade's product_type isn't ISDA-eligible.
    """
    if trade.product_type not in ISDA_CONFIRM_PRODUCTS:
        raise ValueError(
            f"ISDA confirm not applicable for product_type={trade.product_type!r}"
        )

    effective_date = trade.value_date
    maturity_date = _add_tenor(effective_date, trade.tenor or "0D")

    return {
        "confirm_id": f"ISDA-{uuid4().hex[:8].upper()}",
        "trade_id": trade.trade_id,
        "our_party": trade.portfolio,
        "counterparty": trade.counterparty,
        "product": trade.product_type.upper(),
        "notional": trade.quantity,
        "fixed_rate": trade.price,
        "floating_index": trade.floating_index,
        "tenor": trade.tenor,
        "effective_date": effective_date.isoformat(),
        "maturity_date": maturity_date.isoformat(),
    }


def _add_tenor(start: date, tenor: str) -> date:
    """Add a tenor string (e.g., '5Y', '6M', '90D') to a date.

    Simplified — doesn't handle leap years or month-end edge cases.
    Future enhancement: use dateutil.relativedelta for full correctness.
    """
    if not tenor or tenor == "0D":
        return start
    num = int(tenor[:-1])
    unit = tenor[-1].upper()
    if unit == "Y":
        return start.replace(year=start.year + num)
    if unit == "M":
        year = start.year + (start.month + num - 1) // 12
        month = (start.month + num - 1) % 12 + 1
        return start.replace(year=year, month=month)
    if unit == "D":
        return start + timedelta(days=num)
    raise ValueError(f"Unknown tenor unit: {tenor!r}")
