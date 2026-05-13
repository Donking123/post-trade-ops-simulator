"""M1: Trade booking.

A `Trade` represents a single executed deal between two counterparties.
The MO desk receives trade tickets (typically via FIX messages from
execution systems) and books them into the firm's internal records on
trade date.

Key concepts modelled here:
- FIX (Financial Information eXchange): a messaging protocol used to
  communicate trade execution details between venues, brokers, and the
  firm's MO. Real FIX 4.4 uses numeric `tag=value` pairs separated by
  SOH (\\x01). This module uses a *synthetic dict-shaped* equivalent
  using descriptive keys, for clarity in the simulator.
- product_type handler dispatch: each product (FX spot/forward, IRS,
  cash equity) has its own required-field validation and value-date
  computation. Dispatch table makes adding a new product local.
- Value date: the settlement date. Set at booking time using a product
  convention table.

Future enhancements (parked for MVP):
- Real FIX 4.4 parser (quickfix-python or similar)
- Holiday calendars for T+N skip rules (currently weekends-only)
- Cross-product trades (e.g., FX/IR cross-currency swaps)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable, Literal

ProductType = Literal[
    "fx_spot", "fx_forward", "fx_option",
    "irs",
    "cash_equity",
    "futures",
]
Direction = Literal["buy", "sell"]
ConfirmationStatus = Literal["pending", "affirmed", "confirmed", "disputed"]
OptionType = Literal["call", "put"]


# Settlement conventions: trade_date -> value_date offset in business days.
# Defaults to Singapore conventions (T+2 for equity per SGX; FX globally T+2).
# Swap to {"cash_equity": 1} for US T+1 (May 2024 onward).
SETTLEMENT_OFFSETS: dict[ProductType, int] = {
    "fx_spot": 2,
    "fx_forward": 0,    # per-contract value date; computed separately
    "fx_option": 2,     # premium settles on FX spot convention (T+2)
    "irs": 0,           # swap "starts" T+0; fixings + payments per schedule
    "cash_equity": 2,
    "futures": 1,       # exchange-cleared, T+1 cash settlement (MVP simplification)
}


@dataclass
class Trade:
    """One executed deal, post-execution, pre-settlement.

    The MO desk owns this object's lifecycle:
    - Affirm + confirm with the counterparty (M2 updates confirmation_status)
    - Track lifecycle events that hang off this trade (M3)
    - Project settlement and flag breaks (M5)
    - Reconcile against the prime broker view (M8)
    """

    trade_id: str
    external_id: str | None        # vendor / counterparty reference (used for matching in M2)
    product_type: ProductType
    direction: Direction
    quantity: float                # FX: notional in base ccy; IRS: notional; Equity: shares
    price: float                   # FX: rate; IRS: fixed rate; Equity: price/share
    trade_date: date
    value_date: date
    counterparty: str
    portfolio: str

    # Product-specific (None when the product doesn't need them)
    currency_pair: str | None = None     # FX (spot/forward/option) only, e.g. "USD/SGD"
    tenor: str | None = None             # IRS only, e.g. "5Y"
    floating_index: str | None = None    # IRS only, e.g. "SOFR", "ESTR", "SORA"
    underlying: str | None = None        # Cash equity (M3 dividend lookup) or futures, e.g. "AAPL", "ES"
    strike: float | None = None          # FX option only, in quote-ccy-per-base
    expiry_date: date | None = None      # FX option only, the exercise date
    option_type: OptionType | None = None  # FX option only, "call" or "put"

    confirmation_status: ConfirmationStatus = "pending"


# ---------------------------------------------------------------------------
# Synthetic FIX-like parser
# ---------------------------------------------------------------------------

def parse_fix_like_message(msg: dict[str, Any]) -> Trade:
    """Parse a synthetic FIX-style dict into a Trade.

    Required keys (all products): trade_id, product_type, direction,
        quantity, price, trade_date, counterparty, portfolio.
    Product-specific keys are checked by each product handler.
    """
    product_type: ProductType = msg["product_type"]
    handler = PRODUCT_HANDLERS.get(product_type)
    if handler is None:
        raise ValueError(f"Unknown product_type: {product_type!r}")
    return handler(msg)


# ---------------------------------------------------------------------------
# Settlement helpers
# ---------------------------------------------------------------------------

def _add_business_days(d: date, n: int) -> date:
    """Add n business days to d (weekends-only skip; no holiday calendar)."""
    out = d
    added = 0
    while added < n:
        out = out + timedelta(days=1)
        if out.weekday() < 5:  # Mon-Fri
            added += 1
    return out


def _compute_value_date(
    trade_date: date,
    product: ProductType,
    explicit_value_date: date | None,
) -> date:
    """Compute settlement date from trade date + product convention."""
    if explicit_value_date is not None:
        return explicit_value_date
    return _add_business_days(trade_date, SETTLEMENT_OFFSETS[product])


# ---------------------------------------------------------------------------
# Product-specific handlers (dispatch table)
# ---------------------------------------------------------------------------

def _handle_fx(msg: dict[str, Any]) -> Trade:
    if msg["product_type"] not in ("fx_spot", "fx_forward"):
        raise ValueError("FX handler called with non-FX product_type")
    if "currency_pair" not in msg:
        raise ValueError("FX trade requires currency_pair")
    return Trade(
        trade_id=msg["trade_id"],
        external_id=msg.get("external_id"),
        product_type=msg["product_type"],
        direction=msg["direction"],
        quantity=float(msg["quantity"]),
        price=float(msg["price"]),
        trade_date=msg["trade_date"],
        value_date=_compute_value_date(
            msg["trade_date"], msg["product_type"], msg.get("value_date")
        ),
        counterparty=msg["counterparty"],
        portfolio=msg["portfolio"],
        currency_pair=msg["currency_pair"],
    )


def _handle_irs(msg: dict[str, Any]) -> Trade:
    for required in ("tenor", "floating_index"):
        if required not in msg:
            raise ValueError(f"IRS trade requires {required}")
    return Trade(
        trade_id=msg["trade_id"],
        external_id=msg.get("external_id"),
        product_type="irs",
        direction=msg["direction"],
        quantity=float(msg["quantity"]),
        price=float(msg["price"]),
        trade_date=msg["trade_date"],
        value_date=_compute_value_date(msg["trade_date"], "irs", msg.get("value_date")),
        counterparty=msg["counterparty"],
        portfolio=msg["portfolio"],
        tenor=msg["tenor"],
        floating_index=msg["floating_index"],
    )


def _handle_cash_equity(msg: dict[str, Any]) -> Trade:
    return Trade(
        trade_id=msg["trade_id"],
        external_id=msg.get("external_id"),
        product_type="cash_equity",
        direction=msg["direction"],
        quantity=float(msg["quantity"]),
        price=float(msg["price"]),
        trade_date=msg["trade_date"],
        value_date=_compute_value_date(
            msg["trade_date"], "cash_equity", msg.get("value_date")
        ),
        counterparty=msg["counterparty"],
        portfolio=msg["portfolio"],
        underlying=msg.get("underlying"),
    )


def _handle_fx_option(msg: dict[str, Any]) -> Trade:
    for required in ("currency_pair", "strike", "expiry_date", "option_type"):
        if required not in msg:
            raise ValueError(f"FX option trade requires {required}")
    return Trade(
        trade_id=msg["trade_id"],
        external_id=msg.get("external_id"),
        product_type="fx_option",
        direction=msg["direction"],
        quantity=float(msg["quantity"]),
        price=float(msg["price"]),    # premium per unit of notional
        trade_date=msg["trade_date"],
        value_date=_compute_value_date(
            msg["trade_date"], "fx_option", msg.get("value_date")
        ),
        counterparty=msg["counterparty"],
        portfolio=msg["portfolio"],
        currency_pair=msg["currency_pair"],
        strike=float(msg["strike"]),
        expiry_date=msg["expiry_date"],
        option_type=msg["option_type"],
    )


def _handle_futures(msg: dict[str, Any]) -> Trade:
    if "underlying" not in msg:
        raise ValueError("Futures trade requires underlying")
    return Trade(
        trade_id=msg["trade_id"],
        external_id=msg.get("external_id"),
        product_type="futures",
        direction=msg["direction"],
        quantity=float(msg["quantity"]),    # number of contracts
        price=float(msg["price"]),          # futures price
        trade_date=msg["trade_date"],
        value_date=_compute_value_date(
            msg["trade_date"], "futures", msg.get("value_date")
        ),
        counterparty=msg["counterparty"],
        portfolio=msg["portfolio"],
        underlying=msg["underlying"],
    )


PRODUCT_HANDLERS: dict[ProductType, Callable[[dict[str, Any]], Trade]] = {
    "fx_spot": _handle_fx,
    "fx_forward": _handle_fx,
    "fx_option": _handle_fx_option,
    "irs": _handle_irs,
    "cash_equity": _handle_cash_equity,
    "futures": _handle_futures,
}
