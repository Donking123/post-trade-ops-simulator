"""M4: Settlement projection + breaks.

For each trade and lifecycle event, project the expected cash settlement(s),
then check against counterparty settlement instructions and flag any
mismatches as breaks for human resolution.

Cash flow model (MVP scope):
- FX spot/forward: 2 cash settlements on value_date — one per currency leg
- IRS trade itself: no direct settlement (principal is notional, not exchanged)
  Each lifecycle ir_reset event generates 1 cash settlement
- Cash equity buy: 1 cash outflow on value_date (price * shares)
- Cash equity sell: 1 cash inflow on value_date
- Equity dividend lifecycle event: 1 cash flow (signed by direction)

Break types:
- date_mismatch — settlement date doesn't agree
- amount_mismatch — |our - their| exceeds tolerance
- currency_mismatch — currency code disagrees
- missing_their_side — we projected a settlement they don't acknowledge
- missing_our_side — they expect a settlement we don't project

Amount comparison uses a two-tier tolerance:
- 1 basis point of the absolute amount (relative tolerance for large amounts)
- $1 floor (absolute tolerance for small amounts)
Whichever is larger applies — so for a $1M settlement, the tolerance is $100;
for a $10 settlement, it's still $1.

Future enhancements parked:
- Net settlement via CLS (FX) and CCP novation (equity) — currently models gross
- Holiday-adjusted settlement dates (currently inherits value_date from M1)
- Multi-currency netting on a portfolio basis
- Partial settlement and failed-trade retry logic
- Cross-currency IRS (currently single-currency floating leg)
- Manufactured payments on short equity positions
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from uuid import uuid4

from post_trade.trade import Trade
from post_trade.lifecycle import LifecycleEvent


# Amount tolerance: 1 basis point (0.01% of amount) or $1 floor, whichever larger.
AMOUNT_TOLERANCE_BPS = 1.0
ABSOLUTE_AMOUNT_FLOOR = 1.0


@dataclass
class Settlement:
    """One projected cash settlement.

    `amount` is signed:
    - Positive = cash inflow (we receive)
    - Negative = cash outflow (we pay)
    """
    settlement_id: str
    trade_id: str
    event_id: str | None        # None for trade-level FX/equity; set for lifecycle events
    settlement_date: date
    currency: str
    amount: float               # signed
    account: str                # our portfolio
    counterparty: str
    description: str


@dataclass
class SettlementBreak:
    """A discrepancy between our projected settlement and their instruction."""
    break_id: str
    trade_id: str
    break_type: str             # 'date_mismatch' / 'amount_mismatch' / 'currency_mismatch'
                                # / 'missing_their_side' / 'missing_our_side'
    settlement_id: str | None = None
    our_value: Any = None
    their_value: Any = None
    flagged_at: datetime = field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------

def project_settlements(
    trades: list[Trade],
    lifecycle_events: list[LifecycleEvent] | None = None,
) -> list[Settlement]:
    """Project cash settlements from trades + lifecycle events."""
    settlements: list[Settlement] = []

    for trade in trades:
        settlements.extend(_project_trade_settlements(trade))

    if lifecycle_events:
        trades_by_id = {t.trade_id: t for t in trades}
        for event in lifecycle_events:
            trade = trades_by_id.get(event.trade_id)
            if trade is None:
                continue
            sett = _project_event_settlement(event, trade)
            if sett is not None:
                settlements.append(sett)

    return settlements


def _project_trade_settlements(trade: Trade) -> list[Settlement]:
    """Trade-level cash settlements (exclusive of lifecycle events)."""
    if trade.product_type in ("fx_spot", "fx_forward"):
        return _fx_settlements(trade)
    if trade.product_type == "fx_option":
        return _fx_option_settlements(trade)
    if trade.product_type == "cash_equity":
        return _equity_settlements(trade)
    if trade.product_type == "futures":
        return _futures_settlements(trade)
    if trade.product_type == "irs":
        return []   # IRS principal is notional, not exchanged; resets handled separately
    return []


def _fx_option_settlements(trade: Trade) -> list[Settlement]:
    """FX option produces 1 premium settlement on value_date.

    Premium = quantity × price (where price is premium per unit of notional).
    Buy: cash outflow (we pay premium). Sell: cash inflow (we collect premium).
    Premium is denominated in the quote currency of the pair (MVP simplification).
    """
    if trade.currency_pair is None:
        raise ValueError("FX option missing currency_pair")
    _, quote_ccy = trade.currency_pair.split("/")
    premium = trade.quantity * trade.price
    sign = -1 if trade.direction == "buy" else 1

    return [Settlement(
        settlement_id=f"SETT-{uuid4().hex[:8].upper()}",
        trade_id=trade.trade_id,
        event_id=None,
        settlement_date=trade.value_date,
        currency=quote_ccy,
        amount=sign * premium,
        account=trade.portfolio,
        counterparty=trade.counterparty,
        description=(
            f"FX option premium {trade.currency_pair} "
            f"{trade.option_type} K={trade.strike} {trade.direction}"
        ),
    )]


def _futures_settlements(trade: Trade) -> list[Settlement]:
    """Futures produce 1 cash settlement on value_date (MVP simplification).

    Real futures use daily mark-to-market variation margin; we collapse that to
    a single notional cash flow at value_date for stub-level demonstration.
    Currency assumed USD for MVP (most major exchange-traded futures).
    """
    notional = trade.quantity * trade.price
    sign = -1 if trade.direction == "buy" else 1   # buy = pay, sell = receive (placeholder)
    return [Settlement(
        settlement_id=f"SETT-{uuid4().hex[:8].upper()}",
        trade_id=trade.trade_id,
        event_id=None,
        settlement_date=trade.value_date,
        currency="USD",
        amount=sign * notional,
        account=trade.portfolio,
        counterparty=trade.counterparty,
        description=f"Futures {trade.direction} {trade.underlying} settlement",
    )]


def _fx_settlements(trade: Trade) -> list[Settlement]:
    """FX produces 2 cash settlements on value_date — one per currency leg.

    For 'buy USD/SGD at 1.3450, qty 1M':
    - We receive 1M USD (+ inflow)
    - We pay 1.345M SGD (- outflow)
    Direction reverses for 'sell'.
    """
    if trade.currency_pair is None:
        raise ValueError("FX trade missing currency_pair")
    base_ccy, quote_ccy = trade.currency_pair.split("/")
    base_amount = trade.quantity
    quote_amount = trade.quantity * trade.price

    if trade.direction == "buy":
        base_signed, quote_signed = base_amount, -quote_amount
    else:
        base_signed, quote_signed = -base_amount, quote_amount

    return [
        Settlement(
            settlement_id=f"SETT-{uuid4().hex[:8].upper()}",
            trade_id=trade.trade_id,
            event_id=None,
            settlement_date=trade.value_date,
            currency=base_ccy,
            amount=base_signed,
            account=trade.portfolio,
            counterparty=trade.counterparty,
            description=f"FX {trade.product_type} {trade.currency_pair} {trade.direction} {base_ccy} leg",
        ),
        Settlement(
            settlement_id=f"SETT-{uuid4().hex[:8].upper()}",
            trade_id=trade.trade_id,
            event_id=None,
            settlement_date=trade.value_date,
            currency=quote_ccy,
            amount=quote_signed,
            account=trade.portfolio,
            counterparty=trade.counterparty,
            description=f"FX {trade.product_type} {trade.currency_pair} {trade.direction} {quote_ccy} leg",
        ),
    ]


def _equity_settlements(trade: Trade) -> list[Settlement]:
    """Cash equity produces 1 cash settlement on value_date.

    Buy: cash outflow (-).
    Sell: cash inflow (+).
    Currency assumed SGD for MVP (real ops uses the listing exchange's ccy).
    """
    notional = trade.quantity * trade.price
    sign = -1 if trade.direction == "buy" else 1

    return [Settlement(
        settlement_id=f"SETT-{uuid4().hex[:8].upper()}",
        trade_id=trade.trade_id,
        event_id=None,
        settlement_date=trade.value_date,
        currency="SGD",
        amount=sign * notional,
        account=trade.portfolio,
        counterparty=trade.counterparty,
        description=f"Cash equity {trade.direction} {trade.underlying or 'unknown'} settlement",
    )]


def _project_event_settlement(
    event: LifecycleEvent,
    trade: Trade,
) -> Settlement | None:
    """Generate one cash settlement from a lifecycle event."""
    if event.event_type == "ir_reset":
        # For MVP: 'sell' the swap = pay floating; 'buy' = receive floating
        sign = 1 if trade.direction == "buy" else -1
        return Settlement(
            settlement_id=f"SETT-{uuid4().hex[:8].upper()}",
            trade_id=trade.trade_id,
            event_id=event.event_id,
            settlement_date=date.fromisoformat(event.payload["period_end"]),
            currency="USD",     # SOFR is USD-denominated; assumption for MVP
            amount=sign * event.payload["payment_amount"],
            account=trade.portfolio,
            counterparty=trade.counterparty,
            description=(
                f"IRS reset payment "
                f"{event.payload['period_start']} -> {event.payload['period_end']}"
            ),
        )

    if event.event_type == "equity_dividend":
        return Settlement(
            settlement_id=f"SETT-{uuid4().hex[:8].upper()}",
            trade_id=trade.trade_id,
            event_id=event.event_id,
            settlement_date=event.event_date,
            currency="SGD",     # assumed for MVP
            amount=event.payload["cash_flow"],   # already signed by direction
            account=trade.portfolio,
            counterparty=trade.counterparty,
            description=f"Equity dividend {event.payload['ticker']} (ex-date {event.event_date})",
        )

    return None


# ---------------------------------------------------------------------------
# Breaks
# ---------------------------------------------------------------------------

def check_breaks(
    our_settlements: list[Settlement],
    their_instructions: list[dict[str, Any]],
) -> list[SettlementBreak]:
    """Compare our projected settlements against counterparty instructions.

    Each instruction dict should provide:
    - `trade_id` (required, used for matching)
    - `event_id` (optional, used for matching lifecycle events)
    - `settlement_date`, `currency`, `amount` (the fields we compare against)
    """
    breaks: list[SettlementBreak] = []

    # Index instructions by (trade_id, event_id) for fast lookup
    by_key: dict[tuple[str, str | None], dict[str, Any]] = {
        (i["trade_id"], i.get("event_id")): i for i in their_instructions
    }
    used_keys: set[tuple[str, str | None]] = set()

    for sett in our_settlements:
        key = (sett.trade_id, sett.event_id)
        their = by_key.get(key)

        if their is None:
            # Special-case FX: we generate 2 settlements per trade (one per ccy);
            # instructions might be per-trade only. Try the trade-level key.
            # For simplicity in MVP we still record missing_their_side per leg.
            breaks.append(SettlementBreak(
                break_id=f"BRK-{uuid4().hex[:8].upper()}",
                settlement_id=sett.settlement_id,
                trade_id=sett.trade_id,
                break_type="missing_their_side",
                our_value=f"{sett.amount:+,.2f} {sett.currency} on {sett.settlement_date}",
            ))
            continue

        used_keys.add(key)

        # Date check
        if their.get("settlement_date") != sett.settlement_date:
            breaks.append(SettlementBreak(
                break_id=f"BRK-{uuid4().hex[:8].upper()}",
                settlement_id=sett.settlement_id,
                trade_id=sett.trade_id,
                break_type="date_mismatch",
                our_value=sett.settlement_date,
                their_value=their.get("settlement_date"),
            ))

        # Currency check
        if their.get("currency") != sett.currency:
            breaks.append(SettlementBreak(
                break_id=f"BRK-{uuid4().hex[:8].upper()}",
                settlement_id=sett.settlement_id,
                trade_id=sett.trade_id,
                break_type="currency_mismatch",
                our_value=sett.currency,
                their_value=their.get("currency"),
            ))

        # Amount check (1bp relative tolerance with $1 floor)
        tolerance = max(
            abs(sett.amount) * AMOUNT_TOLERANCE_BPS / 10_000.0,
            ABSOLUTE_AMOUNT_FLOOR,
        )
        if abs(their.get("amount", 0.0) - sett.amount) > tolerance:
            breaks.append(SettlementBreak(
                break_id=f"BRK-{uuid4().hex[:8].upper()}",
                settlement_id=sett.settlement_id,
                trade_id=sett.trade_id,
                break_type="amount_mismatch",
                our_value=sett.amount,
                their_value=their.get("amount"),
            ))

    # Instructions present but no matching settlement on our side
    for i in their_instructions:
        key = (i["trade_id"], i.get("event_id"))
        if key not in used_keys:
            amount = i.get("amount", 0.0)
            currency = i.get("currency", "?")
            settle_date = i.get("settlement_date", "?")
            breaks.append(SettlementBreak(
                break_id=f"BRK-{uuid4().hex[:8].upper()}",
                trade_id=i["trade_id"],
                break_type="missing_our_side",
                their_value=f"{amount:+,.2f} {currency} on {settle_date}",
            ))

    return breaks
