"""M5: Reconciliation engine.

After all trades + lifecycle events + settlements have processed, MO
reconciles the firm's internal position view against the prime broker's
overnight position file. Discrepancies flag as breaks for human resolution.

Position model (MVP scope — two views):
- **Equity positions**: net shares per (portfolio, ticker). Buys add, sells subtract.
- **Cash balances**: net amount per (portfolio, currency). Sum of all settlement amounts.

(IRS notional reconciliation is parked for future enhancement — real ops
tracks notional + accrued interest + collateral; we skip it for MVP.)

Break types:
- `position_qty_mismatch` — equity shares differ between our view and PB's
- `cash_balance_mismatch` — cash balance differs (with two-tier tolerance)
- `missing_position` — equity position on one side only
- `missing_cash` — cash balance on one side only

Cash balance tolerance: max(1bp of amount, $1 floor) — same as M4 break detection.

Future enhancements parked:
- IRS notional reconciliation with accrued interest
- Bond position recon
- Cross-currency netting
- Failed-trade tracking that adjusts unsettled positions
- Multi-custodian reconciliation (real funds split across several PBs)
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4

from post_trade.trade import Trade
from post_trade.settlement import Settlement


# Cash balance comparison tolerance (mirrors M4 amount tolerance)
CASH_TOLERANCE_BPS = 1.0
CASH_ABSOLUTE_FLOOR = 1.0


@dataclass
class EquityPosition:
    """Net equity position for one (portfolio, ticker) pair."""
    portfolio: str
    ticker: str
    shares: float            # net: long positive, short negative


@dataclass
class CashBalance:
    """Net cash balance for one (portfolio, currency) pair."""
    portfolio: str
    currency: str
    amount: float            # net: positive inflows, negative outflows accumulated


@dataclass
class ReconBreak:
    """A discrepancy in position or cash reconciliation."""
    break_id: str
    break_type: str          # position_qty_mismatch / cash_balance_mismatch
                             # / missing_position / missing_cash
    portfolio: str
    key: str                 # ticker (equity) or currency (cash)
    our_value: Any = None
    their_value: Any = None
    flagged_at: datetime = field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# Position derivation (our view)
# ---------------------------------------------------------------------------

def compute_equity_positions(trades: list[Trade]) -> list[EquityPosition]:
    """Sum net shares per (portfolio, ticker) across cash equity trades.

    Non-equity trades and equity trades missing `underlying` are excluded.
    Buys add shares; sells subtract.
    """
    pos_by_key: dict[tuple[str, str], float] = defaultdict(float)

    for trade in trades:
        if trade.product_type != "cash_equity":
            continue
        if trade.underlying is None:
            continue
        sign = 1 if trade.direction == "buy" else -1
        pos_by_key[(trade.portfolio, trade.underlying)] += sign * trade.quantity

    return [
        EquityPosition(portfolio=portfolio, ticker=ticker, shares=shares)
        for (portfolio, ticker), shares in pos_by_key.items()
    ]


def compute_cash_balances(settlements: list[Settlement]) -> list[CashBalance]:
    """Sum net cash per (account, currency) across settlements."""
    bal_by_key: dict[tuple[str, str], float] = defaultdict(float)

    for sett in settlements:
        bal_by_key[(sett.account, sett.currency)] += sett.amount

    return [
        CashBalance(portfolio=account, currency=currency, amount=amount)
        for (account, currency), amount in bal_by_key.items()
    ]


# ---------------------------------------------------------------------------
# Reconciliation (compare to PB)
# ---------------------------------------------------------------------------

def reconcile_equity_positions(
    our: list[EquityPosition],
    pb: list[EquityPosition],
) -> list[ReconBreak]:
    """Compare equity positions; flag any (portfolio, ticker) where shares disagree."""
    breaks: list[ReconBreak] = []
    our_by_key = {(p.portfolio, p.ticker): p for p in our}
    pb_by_key = {(p.portfolio, p.ticker): p for p in pb}

    for key in set(our_by_key.keys()) | set(pb_by_key.keys()):
        portfolio, ticker = key
        our_pos = our_by_key.get(key)
        pb_pos = pb_by_key.get(key)

        if our_pos is None:
            breaks.append(ReconBreak(
                break_id=f"BRK-{uuid4().hex[:8].upper()}",
                break_type="missing_position",
                portfolio=portfolio,
                key=ticker,
                our_value=None,
                their_value=pb_pos.shares,
            ))
            continue

        if pb_pos is None:
            breaks.append(ReconBreak(
                break_id=f"BRK-{uuid4().hex[:8].upper()}",
                break_type="missing_position",
                portfolio=portfolio,
                key=ticker,
                our_value=our_pos.shares,
                their_value=None,
            ))
            continue

        # Exact match required for shares (whole-unit quantities)
        if our_pos.shares != pb_pos.shares:
            breaks.append(ReconBreak(
                break_id=f"BRK-{uuid4().hex[:8].upper()}",
                break_type="position_qty_mismatch",
                portfolio=portfolio,
                key=ticker,
                our_value=our_pos.shares,
                their_value=pb_pos.shares,
            ))

    return breaks


def reconcile_cash_balances(
    our: list[CashBalance],
    pb: list[CashBalance],
) -> list[ReconBreak]:
    """Compare cash balances; flag any (portfolio, currency) beyond tolerance."""
    breaks: list[ReconBreak] = []
    our_by_key = {(b.portfolio, b.currency): b for b in our}
    pb_by_key = {(b.portfolio, b.currency): b for b in pb}

    for key in set(our_by_key.keys()) | set(pb_by_key.keys()):
        portfolio, currency = key
        our_bal = our_by_key.get(key)
        pb_bal = pb_by_key.get(key)

        if our_bal is None:
            breaks.append(ReconBreak(
                break_id=f"BRK-{uuid4().hex[:8].upper()}",
                break_type="missing_cash",
                portfolio=portfolio,
                key=currency,
                our_value=None,
                their_value=pb_bal.amount,
            ))
            continue

        if pb_bal is None:
            breaks.append(ReconBreak(
                break_id=f"BRK-{uuid4().hex[:8].upper()}",
                break_type="missing_cash",
                portfolio=portfolio,
                key=currency,
                our_value=our_bal.amount,
                their_value=None,
            ))
            continue

        tolerance = max(
            abs(our_bal.amount) * CASH_TOLERANCE_BPS / 10_000.0,
            CASH_ABSOLUTE_FLOOR,
        )
        if abs(our_bal.amount - pb_bal.amount) > tolerance:
            breaks.append(ReconBreak(
                break_id=f"BRK-{uuid4().hex[:8].upper()}",
                break_type="cash_balance_mismatch",
                portfolio=portfolio,
                key=currency,
                our_value=our_bal.amount,
                their_value=pb_bal.amount,
            ))

    return breaks
