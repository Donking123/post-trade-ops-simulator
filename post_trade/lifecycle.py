"""M3: Lifecycle events.

Between trade date and final settlement, trades may generate lifecycle
events. M3 models two event types:

1. **IR resets** on IRS trades — every period (typically quarterly for
   SOFR), the floating rate is "fixed" against the day's published value
   of the floating index. A payment leg is then computed:

       payment = notional × rate × day_count_fraction

   The day-count fraction depends on the convention (ACT/360, 30/360,
   etc.). MVP uses ACT/360 (the standard for USD SOFR).

2. **Equity dividends** — on the ex-dividend date, holders of the stock
   receive a cash flow proportional to their position size:

       cash_flow = shares × dividend_per_share  (positive for long position)

   The shares stay in the position; only cash changes.

FX spot has no lifecycle events — it just settles T+2 (handled in M5).

External market data needed (synthetic for MVP):
- `SOFRFixings` — a date → rate table
- `DividendCalendar` — a ticker → list of (ex_date, dividend_per_share) table

Future enhancements parked:
- FX option exercises (path-dependent for barriers/Asians)
- Bond coupon accruals (separate from IRS resets — different schedule rules)
- ESTR / SORA / TONA fixings (currently only SOFR)
- 30/360 and ACT/365 day-count conventions
- Dividend reinvestment / DRIP modelling
- Holiday-adjusted reset dates (currently uses calendar quarterly steps)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any
from uuid import uuid4

from post_trade.trade import Trade


@dataclass
class LifecycleEvent:
    """One lifecycle event hanging off a trade.

    `payload` carries event-type-specific data:
      ir_reset: fixing_rate, period_start, period_end, day_count,
                day_count_fraction, payment_amount, floating_index
      equity_dividend: ticker, dividend_per_share, shares, cash_flow, direction
    """
    event_id: str
    trade_id: str
    event_type: str           # "ir_reset" / "equity_dividend"
    event_date: date
    payload: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Market data containers (synthetic for MVP)
# ---------------------------------------------------------------------------

@dataclass
class SOFRFixings:
    """Synthetic SOFR fixings: a date -> rate map.

    Real ops fetch this from Bloomberg / NY Fed / vendor feeds. MVP
    uses a hand-built dict.
    """
    fixings: dict[date, float]

    def get(self, d: date) -> float:
        """Look up the fixing for date `d`. Raises if missing."""
        if d not in self.fixings:
            raise ValueError(f"No SOFR fixing available for {d.isoformat()}")
        return self.fixings[d]


@dataclass
class DividendCalendar:
    """Synthetic dividends calendar: ticker -> list of (ex_date, amount).

    Real ops fetch this from Bloomberg DVD function / vendor feeds.
    """
    dividends: dict[str, list[tuple[date, float]]]

    def for_ticker(self, ticker: str) -> list[tuple[date, float]]:
        """Return list of (ex_date, dividend_per_share) for a ticker. Empty if none."""
        return self.dividends.get(ticker, [])


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_lifecycle_events(
    trade: Trade,
    sofr_fixings: SOFRFixings | None = None,
    dividend_calendar: DividendCalendar | None = None,
    horizon: date | None = None,
) -> list[LifecycleEvent]:
    """Generate lifecycle events for a single trade up to `horizon`.

    - IRS: quarterly resets from value_date to maturity (or horizon, earlier).
      Each reset is one event with the payment leg computed.
    - Cash equity: dividend events for any ex-dates between value_date and
      horizon, looked up via dividend_calendar.
    - FX (spot/forward): no events.

    `horizon` defaults to 1 year for equity, full maturity for IRS.
    Missing market data (fixings/dividends) returns empty list.
    """
    if trade.product_type == "irs":
        if sofr_fixings is None:
            return []
        return _generate_ir_resets(trade, sofr_fixings, horizon)
    if trade.product_type == "cash_equity":
        if dividend_calendar is None:
            return []
        return _generate_dividends(trade, dividend_calendar, horizon)
    return []  # fx_spot, fx_forward, anything else


# ---------------------------------------------------------------------------
# IR resets
# ---------------------------------------------------------------------------

def _generate_ir_resets(
    trade: Trade,
    fixings: SOFRFixings,
    horizon: date | None,
) -> list[LifecycleEvent]:
    """Generate quarterly IR resets from value_date to maturity."""
    maturity = _add_tenor(trade.value_date, trade.tenor or "1Y")
    end = min(maturity, horizon) if horizon else maturity

    events: list[LifecycleEvent] = []
    period_start = trade.value_date
    while True:
        period_end = _add_months(period_start, 3)  # quarterly
        if period_end > end:
            break

        try:
            fixing_rate = fixings.get(period_start)
        except ValueError:
            period_start = period_end
            continue  # no fixing data -> skip this period

        day_count = (period_end - period_start).days
        # ACT/360 day-count convention (standard for USD SOFR)
        day_count_fraction = day_count / 360.0
        payment = trade.quantity * fixing_rate * day_count_fraction

        events.append(LifecycleEvent(
            event_id=f"RESET-{uuid4().hex[:8].upper()}",
            trade_id=trade.trade_id,
            event_type="ir_reset",
            event_date=period_start,
            payload={
                "fixing_rate": fixing_rate,
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "day_count": day_count,
                "day_count_fraction": day_count_fraction,
                "payment_amount": payment,
                "floating_index": trade.floating_index,
            },
        ))
        period_start = period_end

    return events


# ---------------------------------------------------------------------------
# Equity dividends
# ---------------------------------------------------------------------------

def _generate_dividends(
    trade: Trade,
    calendar: DividendCalendar,
    horizon: date | None,
) -> list[LifecycleEvent]:
    """Generate dividend events for cash equity between value_date and horizon."""
    if trade.underlying is None:
        return []  # can't look up dividends without a ticker

    end = horizon if horizon else _add_months(trade.value_date, 12)  # 1Y default lookahead

    events: list[LifecycleEvent] = []
    for ex_date, div_per_share in calendar.for_ticker(trade.underlying):
        if not (trade.value_date <= ex_date <= end):
            continue
        # Long position receives positive cash; short position pays
        sign = 1 if trade.direction == "buy" else -1
        cash_flow = sign * trade.quantity * div_per_share

        events.append(LifecycleEvent(
            event_id=f"DIV-{uuid4().hex[:8].upper()}",
            trade_id=trade.trade_id,
            event_type="equity_dividend",
            event_date=ex_date,
            payload={
                "ticker": trade.underlying,
                "dividend_per_share": div_per_share,
                "shares": trade.quantity,
                "cash_flow": cash_flow,
                "direction": trade.direction,
            },
        ))

    return events


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _add_months(d: date, n: int) -> date:
    """Add n calendar months to d. Caps day to last valid day of target month."""
    year = d.year + (d.month + n - 1) // 12
    month = (d.month + n - 1) % 12 + 1
    last_day = _last_day_of_month(year, month)
    return d.replace(year=year, month=month, day=min(d.day, last_day))


def _last_day_of_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    next_month_first = date(year, month + 1, 1)
    return (next_month_first - timedelta(days=1)).day


def _add_tenor(start: date, tenor: str) -> date:
    """Add a tenor string (e.g., '5Y', '6M', '90D') to a date."""
    if not tenor or tenor == "0D":
        return start
    num = int(tenor[:-1])
    unit = tenor[-1].upper()
    if unit == "Y":
        return start.replace(year=start.year + num)
    if unit == "M":
        return _add_months(start, num)
    if unit == "D":
        return start + timedelta(days=num)
    raise ValueError(f"Unknown tenor unit: {tenor!r}")
