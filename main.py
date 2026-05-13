"""Post-Trade Operations Simulator — end-to-end pipeline.

Runs M1 (booking) -> M2 (confirmation) -> M3 (lifecycle) ->
M5 (settlement + breaks) -> M8 (reconciliation) -> M10 (dashboard)
on synthetic data.

Usage:
    python main.py

Output:
    - Console: end-of-day reconciliation report
    - Files:   docs/screenshots/{breaks_queue, settlement_calendar, recon_variance}.html
               (open in browser to view interactive dashboards)
"""

from __future__ import annotations

import os
from datetime import date

from post_trade.trade import parse_fix_like_message
from post_trade.confirmation import match_against_counterparty
from post_trade.lifecycle import SOFRFixings, generate_lifecycle_events
from post_trade.settlement import project_settlements, check_breaks
from post_trade.reconciliation import (
    EquityPosition,
    CashBalance,
    compute_cash_balances,
    compute_equity_positions,
    reconcile_cash_balances,
    reconcile_equity_positions,
)
from post_trade.dashboard import (
    build_breaks_queue,
    build_recon_variance,
    build_settlement_calendar,
    export_to_html,
)


def main() -> None:
    trade_date = date(2026, 5, 13)

    # ---------------------------------------------------------------------
    # M1 — Trade booking
    # ---------------------------------------------------------------------
    fx = parse_fix_like_message({
        "trade_id": "T_FX", "external_id": "EXT-FX",
        "product_type": "fx_spot", "direction": "buy",
        "quantity": 1_000_000, "price": 1.3450,
        "trade_date": trade_date, "counterparty": "BANK-A",
        "portfolio": "PB-USD-1", "currency_pair": "USD/SGD",
    })
    irs = parse_fix_like_message({
        "trade_id": "T_IRS", "external_id": "EXT-IRS",
        "product_type": "irs", "direction": "sell",
        "quantity": 100_000_000, "price": 0.042,
        "trade_date": trade_date, "counterparty": "BANK-B",
        "portfolio": "PB-USD-1", "tenor": "1Y", "floating_index": "SOFR",
    })
    eq = parse_fix_like_message({
        "trade_id": "T_EQ", "external_id": "EXT-EQ",
        "product_type": "cash_equity", "direction": "buy",
        "quantity": 1_000, "price": 28.50,
        "trade_date": trade_date, "counterparty": "BROKER-X",
        "portfolio": "EQ-SG-1", "underlying": "DBS",
    })
    fx_opt = parse_fix_like_message({
        "trade_id": "T_FXOPT", "external_id": "EXT-FXOPT",
        "product_type": "fx_option", "direction": "buy",
        "quantity": 1_000_000, "price": 0.0085,
        "trade_date": trade_date, "counterparty": "BANK-A",
        "portfolio": "PB-USD-1", "currency_pair": "USD/SGD",
        "strike": 1.3500,
        "expiry_date": date(2026, 8, 13),
        "option_type": "call",
    })
    fut = parse_fix_like_message({
        "trade_id": "T_FUT", "external_id": "EXT-FUT",
        "product_type": "futures", "direction": "buy",
        "quantity": 10, "price": 5_000.0,
        "trade_date": trade_date, "counterparty": "EXCHANGE-CME",
        "portfolio": "FUT-USD-1", "underlying": "ES",
    })
    trades = [fx, irs, eq, fx_opt, fut]

    # ---------------------------------------------------------------------
    # M2 — Confirmation (one deliberate price mismatch on IRS)
    # ---------------------------------------------------------------------
    cp_replies = [
        {"external_id": "EXT-FX", "counterparty": "BANK-A", "product_type": "fx_spot",
         "quantity": 1_000_000, "price": 1.3450, "value_date": fx.value_date},
        {"external_id": "EXT-IRS", "counterparty": "BANK-B", "product_type": "irs",
         "quantity": 100_000_000, "price": 0.0421,  # 0.01% off -> break
         "value_date": irs.value_date},
        {"external_id": "EXT-EQ", "counterparty": "BROKER-X", "product_type": "cash_equity",
         "quantity": 1_000, "price": 28.50, "value_date": eq.value_date},
        {"external_id": "EXT-FXOPT", "counterparty": "BANK-A", "product_type": "fx_option",
         "quantity": 1_000_000, "price": 0.0085, "value_date": fx_opt.value_date},
        {"external_id": "EXT-FUT", "counterparty": "EXCHANGE-CME", "product_type": "futures",
         "quantity": 10, "price": 5_000.0, "value_date": fut.value_date},
    ]
    trades, match_breaks = match_against_counterparty(trades, cp_replies)

    # ---------------------------------------------------------------------
    # M3 — Lifecycle events (IRS quarterly resets)
    # ---------------------------------------------------------------------
    sofr_fixings = SOFRFixings(fixings={
        date(2026, 5, 13): 0.0510,
        date(2026, 8, 13): 0.0500,
        date(2026, 11, 13): 0.0480,
        date(2027, 2, 13): 0.0460,
    })
    events = generate_lifecycle_events(irs, sofr_fixings=sofr_fixings)

    # ---------------------------------------------------------------------
    # M5 — Settlement projection + breaks (phantom instruction injected)
    # ---------------------------------------------------------------------
    settlements = project_settlements(trades, events)
    their_instructions = [{
        "trade_id": "T_PHANTOM", "event_id": None,
        "settlement_date": date(2026, 5, 15),
        "currency": "SGD", "amount": 99_999.0,
    }]
    settlement_breaks = check_breaks(settlements, their_instructions)

    # ---------------------------------------------------------------------
    # M8 — Reconciliation (PB shows 999 DBS instead of our 1000)
    # ---------------------------------------------------------------------
    our_positions = compute_equity_positions(trades)
    our_cash = compute_cash_balances(settlements)
    # PB shows fewer DBS shares + a phantom UOB position + cash drift in one currency
    pb_positions = [
        EquityPosition(portfolio="EQ-SG-1", ticker="DBS", shares=999),        # off by 1
        EquityPosition(portfolio="EQ-SG-1", ticker="UOB", shares=500),        # phantom on PB side
    ]
    pb_cash = []
    for b in our_cash:
        # Inject a small SGD drift to demonstrate cash_balance_mismatch detection
        amount = b.amount - 250.0 if b.currency == "SGD" else b.amount
        pb_cash.append(CashBalance(portfolio=b.portfolio, currency=b.currency, amount=amount))
    recon_breaks = reconcile_equity_positions(our_positions, pb_positions)
    recon_breaks.extend(reconcile_cash_balances(our_cash, pb_cash))

    # ---------------------------------------------------------------------
    # Report
    # ---------------------------------------------------------------------
    print("=" * 60)
    print("Post-Trade Ops Simulator — End-of-Day Report")
    print("=" * 60)
    print(f"Trade date:            {trade_date}")
    print(f"Trades booked:         {len(trades)}")
    print(f"Lifecycle events:      {len(events)}")
    print(f"Settlements projected: {len(settlements)}")
    print(f"Equity positions:      {len(our_positions)}")
    print(f"Cash balances:         {len(our_cash)}")
    print("-" * 60)
    print(f"M2 confirmation breaks:    {len(match_breaks)}")
    print(f"M5 settlement breaks:      {len(settlement_breaks)}")
    print(f"M8 reconciliation breaks:  {len(recon_breaks)}")
    print("=" * 60)

    # ---------------------------------------------------------------------
    # M10 — Dashboard export
    # ---------------------------------------------------------------------
    os.makedirs("docs/screenshots", exist_ok=True)
    export_to_html(
        build_breaks_queue(match_breaks, settlement_breaks, recon_breaks),
        "docs/screenshots/breaks_queue.html",
    )
    export_to_html(
        build_settlement_calendar(settlements, horizon_days=300, as_of=trade_date),
        "docs/screenshots/settlement_calendar.html",
    )
    export_to_html(
        build_recon_variance(recon_breaks),
        "docs/screenshots/recon_variance.html",
    )
    print("\nDashboards exported to docs/screenshots/*.html (open in browser).")


if __name__ == "__main__":
    main()
