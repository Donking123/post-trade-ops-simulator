"""M6: Plotly dashboard — exception management view.

Three panels surface the operational outputs of M2, M4, and M5 for human
review by an MO analyst:

1. **Breaks queue** — merged view of MatchBreak (M2) + SettlementBreak (M4)
   + ReconBreak (M5). Sortable by urgency. First screen analyst looks at.

2. **Settlement calendar** — net cash flow per date for the next N days,
   per currency. Hover detail shows trade count behind each bar.

3. **Recon variance** — paired bars (ours vs PB's) for each reconciliation
   break, showing magnitude of disagreement.

Tech: Plotly graph_objects. Renders inline in Jupyter and exports to
standalone HTML for README screenshots.

Future enhancements parked:
- Click-through drill-down (click break → see source trade)
- Real-time refresh (WebSocket / polling)
- Multi-portfolio filtering UI
- Counterparty risk view
- Time-series of break counts over weeks/months
"""

from __future__ import annotations

from datetime import date
from typing import Iterable

import pandas as pd
import plotly.graph_objects as go

from post_trade.confirmation import MatchBreak
from post_trade.settlement import Settlement, SettlementBreak
from post_trade.reconciliation import ReconBreak


# ---------------------------------------------------------------------------
# Urgency ranking and colour coding
# ---------------------------------------------------------------------------

URGENCY_LEVELS: dict[str, int] = {
    # Critical (4) — unknown obligations, hardest to resolve
    "missing_our_side": 4,
    # High (3) — clear errors needing investigation
    "amount_mismatch": 3,
    "currency_mismatch": 3,
    "position_qty_mismatch": 3,
    "cash_balance_mismatch": 3,
    "qty_mismatch": 3,
    "price_mismatch": 3,
    # Medium (2) — likely investigatable but less urgent
    "missing_position": 2,
    "missing_cash": 2,
    "date_mismatch": 2,
    # Low (1) — often timing artefacts
    "missing_their_side": 1,
    "no_match": 1,
}


URGENCY_LABELS: dict[int, str] = {
    4: "CRITICAL",
    3: "HIGH",
    2: "MEDIUM",
    1: "LOW",
}


# Plain-English description of each break type (1 sentence, ≤55 chars per
# line; literal '<br>' for forced line breaks in Plotly Table cells).
# Shown in the dashboard table so readers without MO domain knowledge can
# understand what's wrong without consulting docs/PROCEDURES.md.
BREAK_DESCRIPTIONS: dict[str, str] = {
    # M2 confirmation
    "no_match": "No matching counterparty reply<br>(may be timing)",
    "qty_mismatch": "Quantity disagrees on Layer-1 match<br>(fat-finger or amendment)",
    "price_mismatch": "Price/rate disagrees beyond tolerance<br>(Layer-1 match)",
    # M4 settlement
    "missing_their_side": "Counterparty hasn't sent matching<br>instruction yet",
    "missing_our_side": "Counterparty expects settlement we<br>don't acknowledge — CRITICAL",
    "amount_mismatch": "Settlement amount disagrees beyond<br>max(1bp, $1) tolerance",
    "currency_mismatch": "Settlement currency code disagrees",
    "date_mismatch": "Settlement date disagrees<br>(often holiday-calendar diff)",
    # M5 reconciliation
    "position_qty_mismatch": "Share count differs<br>between our books and PB",
    "cash_balance_mismatch": "Cash balance differs<br>between our books and PB",
    "missing_position": "Equity position recorded<br>on only one side",
    "missing_cash": "Cash balance recorded<br>on only one side",
}


def _urgency_score(break_type: str) -> int:
    return URGENCY_LEVELS.get(break_type, 1)


def _urgency_label(break_type: str) -> str:
    return URGENCY_LABELS.get(_urgency_score(break_type), "LOW")


def _break_description(break_type: str) -> str:
    return BREAK_DESCRIPTIONS.get(break_type, "(no description)")


def _urgency_color(urgency: int) -> str:
    """Light pastel backgrounds for table rows."""
    if urgency >= 4:
        return "#ffcccc"  # red-ish — critical
    if urgency >= 3:
        return "#ffe5cc"  # orange-ish — high
    if urgency >= 2:
        return "#ffffcc"  # yellow-ish — medium
    return "#e5ffcc"      # green-ish — timing/low


# ---------------------------------------------------------------------------
# Panel 1: Breaks Queue
# ---------------------------------------------------------------------------

def build_breaks_queue(
    match_breaks: Iterable[MatchBreak] = (),
    settlement_breaks: Iterable[SettlementBreak] = (),
    recon_breaks: Iterable[ReconBreak] = (),
) -> go.Figure:
    """Merged breaks-queue table from M2 + M4 + M5, sorted by urgency desc."""
    rows: list[dict] = []

    for b in match_breaks:
        rows.append({
            "urgency_label": _urgency_label(b.break_type),
            "source": "M2 Confirmation",
            "break_type": b.break_type,
            "description": _break_description(b.break_type),
            "trade_id": b.trade_id,
            "key": "-",
            "our_value": _short_str(b.our_value),
            "their_value": _short_str(b.their_value),
            "urgency": _urgency_score(b.break_type),
        })

    for b in settlement_breaks:
        rows.append({
            "urgency_label": _urgency_label(b.break_type),
            "source": "M4 Settlement",
            "break_type": b.break_type,
            "description": _break_description(b.break_type),
            "trade_id": b.trade_id,
            "key": "-",
            "our_value": _short_str(b.our_value),
            "their_value": _short_str(b.their_value),
            "urgency": _urgency_score(b.break_type),
        })

    for b in recon_breaks:
        rows.append({
            "urgency_label": _urgency_label(b.break_type),
            "source": "M5 Reconciliation",
            "break_type": b.break_type,
            "description": _break_description(b.break_type),
            "trade_id": "-",
            "key": f"{b.portfolio}/{b.key}",
            "our_value": _short_str(b.our_value),
            "their_value": _short_str(b.their_value),
            "urgency": _urgency_score(b.break_type),
        })

    if not rows:
        return _empty_state_figure("Breaks Queue", "No breaks — clean day!", color="green")

    rows.sort(key=lambda r: (-r["urgency"], r["break_type"]))
    colors = [_urgency_color(r["urgency"]) for r in rows]

    df = pd.DataFrame(rows)

    fig = go.Figure(data=[go.Table(
        columnwidth=[70, 110, 140, 220, 90, 130, 180, 180],
        header=dict(
            values=["Urgency", "Source", "Break Type", "What it means",
                    "Trade ID", "Position / Cash", "Our value", "Their value"],
            fill_color="#444",
            font=dict(color="white", size=12),
            align="left",
            height=32,
        ),
        cells=dict(
            values=[
                df["urgency_label"],
                df["source"],
                df["break_type"],
                df["description"],
                df["trade_id"],
                df["key"],
                df["our_value"],
                df["their_value"],
            ],
            fill_color=[colors],
            font=dict(size=11),
            align="left",
            height=44,    # taller rows so 2-line descriptions display fully
        ),
    )])

    fig.update_layout(
        title=(f"Breaks Queue ({len(rows)} items) — "
               "investigate in urgency order; see docs/PROCEDURES.md for per-type playbooks"),
        height=max(240, 100 + 44 * len(rows)),
        margin=dict(t=60, b=20, l=10, r=10),
    )
    return fig


def _short_str(v) -> str:
    """Truncate long repr for table display."""
    if v is None:
        return "-"
    s = str(v)
    return s if len(s) <= 40 else s[:37] + "..."


# ---------------------------------------------------------------------------
# Panel 2: Settlement Calendar
# ---------------------------------------------------------------------------

def _settlement_source_label(s: Settlement) -> str:
    """Categorize a Settlement by source type for bar labels.

    Reads from the description and event_id fields produced by M4.
    """
    desc = s.description.lower()
    if s.event_id and "reset" in desc:
        return "IRS reset"
    if "dividend" in desc:
        return "Equity dividend"
    if "fx" in desc and "option" in desc:
        return "FX option"
    if "fx" in desc and "forward" in desc:
        return "FX forward"
    if "fx" in desc and "spot" in desc:
        return "FX spot"
    if "cash equity" in desc:
        return "Equity"
    if "futures" in desc:
        return "Futures"
    return "Other"


def build_settlement_calendar(
    settlements: Iterable[Settlement],
    horizon_days: int = 30,
    as_of: date | None = None,
) -> go.Figure:
    """Net cash flow per (date, currency) over the next N days.

    Renders one stacked sub-panel per currency so bars from different
    currencies don't compete on a shared y-axis (USD millions and SGD
    millions are different scales and signs; mixing them on one plot
    creates visual overlap when settlement dates cluster). Each panel
    is sized to its own scale; x-axes are shared so date alignment is
    visually consistent across panels.

    Each bar is annotated with the source type(s) that contributed
    (FX spot, IRS reset, etc.) so a viewer can identify what's driving
    each cash flow without inspecting underlying trades.
    """
    if as_of is None:
        as_of = date.today()

    rows: list[dict] = []
    for s in settlements:
        days_out = (s.settlement_date - as_of).days
        if 0 <= days_out <= horizon_days:
            rows.append({
                "settlement_date": s.settlement_date,
                "currency": s.currency,
                "amount": s.amount,
                "trade_id": s.trade_id,
                "source": _settlement_source_label(s),
            })

    if not rows:
        return _empty_state_figure(
            "Settlement Calendar",
            f"No settlements in next {horizon_days} days",
            color="gray",
        )

    df = pd.DataFrame(rows)
    agg = (df.groupby(["settlement_date", "currency"], as_index=False)
             .agg(net_amount=("amount", "sum"),
                  trade_count=("trade_id", "count"),
                  sources=("source", lambda s: ", ".join(sorted(set(s))))))

    currencies = sorted(agg["currency"].unique())
    n_ccys = len(currencies)

    # All unique settlement dates across currencies — used as the shared
    # categorical x-axis so every panel's bars line up vertically and the
    # bar width is determined by category spacing (not calendar spacing).
    # This avoids the "thin slivers on a 300-day date axis" problem.
    all_dates = sorted(set(agg["settlement_date"]))
    date_strs = [d.strftime("%b %d, %Y") for d in all_dates]

    palette = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b"]
    ccy_color = {ccy: palette[i % len(palette)] for i, ccy in enumerate(currencies)}

    from plotly.subplots import make_subplots
    fig = make_subplots(
        rows=n_ccys, cols=1,
        subplot_titles=[f"{ccy} cash flow" for ccy in currencies],
        shared_xaxes=True,
        vertical_spacing=0.10 if n_ccys > 1 else 0.0,
    )

    def _format_label(net_amount: float, sources: str) -> str:
        """Short bar label: '<signed amount>\\n<sources>'."""
        if abs(net_amount) >= 1_000_000:
            amt_str = f"{net_amount / 1_000_000:+.2f}M"
        elif abs(net_amount) >= 1_000:
            amt_str = f"{net_amount / 1_000:+.1f}K"
        else:
            amt_str = f"{net_amount:+.0f}"
        sources_break = sources.replace(", ", "<br>")
        return f"<b>{amt_str}</b><br>{sources_break}"

    for i, ccy in enumerate(currencies, start=1):
        sub = agg[agg["currency"] == ccy].set_index("settlement_date")
        # Build aligned vectors against the shared date categories so panels
        # show date columns in the same order (missing dates render as gaps).
        amounts = []
        labels = []
        trade_counts = []
        source_strs = []
        for d in all_dates:
            if d in sub.index:
                row = sub.loc[d]
                amounts.append(float(row["net_amount"]))
                labels.append(_format_label(row["net_amount"], row["sources"]))
                trade_counts.append(int(row["trade_count"]))
                source_strs.append(row["sources"])
            else:
                amounts.append(None)
                labels.append("")
                trade_counts.append(0)
                source_strs.append("")

        fig.add_trace(
            go.Bar(
                name=ccy,
                x=date_strs,
                y=amounts,
                marker_color=ccy_color[ccy],
                text=labels,
                textposition="auto",
                textfont=dict(size=12),
                insidetextfont=dict(size=12, color="white"),
                outsidetextfont=dict(size=12),
                cliponaxis=False,
                customdata=list(zip(trade_counts, source_strs)),
                hovertemplate=(
                    f"<b>{ccy}</b><br>"
                    "Date: %{x}<br>"
                    "Net: %{y:,.2f}<br>"
                    "Trades: %{customdata[0]}<br>"
                    "Source(s): %{customdata[1]}<extra></extra>"
                ),
                showlegend=False,
            ),
            row=i, col=1,
        )
        fig.update_yaxes(title_text=f"{ccy}", row=i, col=1, automargin=True)

        # Pad the y-axis so labels don't clip at the panel edges.
        non_null = [a for a in amounts if a is not None]
        y_min = min(non_null) if non_null else -1.0
        y_max = max(non_null) if non_null else 1.0
        if y_min >= 0:
            y_min = -0.1 * y_max
        if y_max <= 0:
            y_max = -0.2 * y_min
        span = y_max - y_min
        fig.update_yaxes(range=[y_min - 0.20 * span, y_max + 0.20 * span],
                         row=i, col=1)

    fig.update_xaxes(title_text="Settlement Date", row=n_ccys, col=1)

    fig.update_layout(
        title=f"Settlement Calendar — next {horizon_days} days",
        barmode="group",
        bargap=0.30,
        height=max(340, 300 * n_ccys),
        margin=dict(t=80, b=60, l=80, r=40),
    )
    return fig


# ---------------------------------------------------------------------------
# Panel 3: Reconciliation Variance
# ---------------------------------------------------------------------------

POSITION_BREAK_TYPES = frozenset({"position_qty_mismatch", "missing_position"})
CASH_BREAK_TYPES = frozenset({"cash_balance_mismatch", "missing_cash"})


def build_recon_variance(recon_breaks: Iterable[ReconBreak]) -> go.Figure:
    """Paired bars (our view vs PB view) per recon break.

    Splits position breaks (whole-unit share counts) and cash breaks (currency
    amounts) into separate subplots when both are present — they share no
    common y-axis scale (1,000 shares vs 1,400,000 SGD would make the smaller
    series invisible).
    """
    breaks_list = list(recon_breaks)
    if not breaks_list:
        return _empty_state_figure(
            "Reconciliation Variance",
            "No reconciliation breaks",
            color="green",
        )

    position_breaks = [b for b in breaks_list if b.break_type in POSITION_BREAK_TYPES]
    cash_breaks = [b for b in breaks_list if b.break_type in CASH_BREAK_TYPES]

    if position_breaks and cash_breaks:
        from plotly.subplots import make_subplots
        fig = make_subplots(
            rows=1, cols=2,
            subplot_titles=(
                f"Position Breaks ({len(position_breaks)})",
                f"Cash Breaks ({len(cash_breaks)})",
            ),
            horizontal_spacing=0.12,
        )
        _add_paired_bars(fig, position_breaks, row=1, col=1, show_legend=True)
        _add_paired_bars(fig, cash_breaks, row=1, col=2, show_legend=False)
        fig.update_yaxes(title_text="Shares", row=1, col=1, automargin=True)
        fig.update_yaxes(title_text="Cash amount", row=1, col=2, automargin=True)
        _pad_recon_yaxis(fig, position_breaks, row=1, col=1)
        _pad_recon_yaxis(fig, cash_breaks, row=1, col=2)
    elif position_breaks:
        fig = go.Figure()
        _add_paired_bars(fig, position_breaks, show_legend=True)
        fig.update_yaxes(title_text="Shares", automargin=True)
        _pad_recon_yaxis(fig, position_breaks)
    else:
        fig = go.Figure()
        _add_paired_bars(fig, cash_breaks, show_legend=True)
        fig.update_yaxes(title_text="Cash amount", automargin=True)
        _pad_recon_yaxis(fig, cash_breaks)

    fig.update_layout(
        title=f"Reconciliation Variance — Our vs PB ({len(breaks_list)} items)",
        barmode="group",
        bargap=0.30,
        bargroupgap=0.05,
        height=500,
        margin=dict(t=80, b=80, l=80, r=30),
    )
    return fig


def _pad_recon_yaxis(
    fig: go.Figure,
    breaks: list[ReconBreak],
    row: int | None = None,
    col: int | None = None,
) -> None:
    """Pad recon-variance y-axis so outside labels (Variance bar) don't clip."""
    our = [_coerce_float(b.our_value) for b in breaks]
    their = [_coerce_float(b.their_value) for b in breaks]
    variance = [o - t for o, t in zip(our, their)]
    all_values = our + their + variance
    if not all_values:
        return
    y_min, y_max = min(all_values), max(all_values)
    if y_min >= 0:
        y_min = -0.1 * y_max
    if y_max <= 0:
        y_max = -0.2 * y_min
    span = y_max - y_min if y_max != y_min else max(abs(y_max), 1.0)
    rng = [y_min - 0.20 * span, y_max + 0.20 * span]
    if row is not None and col is not None:
        fig.update_yaxes(range=rng, row=row, col=col)
    else:
        fig.update_yaxes(range=rng)


def _add_paired_bars(
    fig: go.Figure,
    breaks: list[ReconBreak],
    row: int | None = None,
    col: int | None = None,
    show_legend: bool = True,
) -> None:
    """Add our-view + pb-view + variance bars to a Figure (optionally in a subplot cell).

    Matches the settlement-calendar formatting style: bar text labels with
    M/K-formatted amounts, sized 12pt, positioned auto (inside/outside per
    bar). The third 'Variance' bar (Our − PB) makes small disagreements
    visible even when the absolute Our and PB bars look identical at chart
    scale. Coloured green when our value exceeds theirs, red when below.
    """
    labels = [f"{b.portfolio}/{b.key}<br>({b.break_type})" for b in breaks]
    our_values = [_coerce_float(b.our_value) for b in breaks]
    their_values = [_coerce_float(b.their_value) for b in breaks]
    variance = [o - t for o, t in zip(our_values, their_values)]
    variance_colors = ["#2ca02c" if v >= 0 else "#d62728" for v in variance]

    def _fmt(v: float) -> str:
        """Format a number with M/K suffix when large, raw with commas otherwise."""
        if abs(v) >= 1_000_000:
            return f"{v / 1_000_000:+.2f}M"
        if abs(v) >= 1_000:
            return f"{v / 1_000:+.1f}K"
        return f"{v:+,.0f}"

    our_text = [_fmt(v) for v in our_values]
    their_text = [_fmt(v) for v in their_values]
    variance_text = [f"<b>{_fmt(v)}</b>" for v in variance]

    our_trace = go.Bar(
        name="Our view", x=labels, y=our_values,
        marker_color="#1f77b4", width=0.25,
        text=our_text, textposition="auto",
        textfont=dict(size=11),
        insidetextfont=dict(size=11, color="white"),
        outsidetextfont=dict(size=11),
        cliponaxis=False,
        showlegend=show_legend, legendgroup="our",
    )
    pb_trace = go.Bar(
        name="PB view", x=labels, y=their_values,
        marker_color="#ff7f0e", width=0.25,
        text=their_text, textposition="auto",
        textfont=dict(size=11),
        insidetextfont=dict(size=11, color="white"),
        outsidetextfont=dict(size=11),
        cliponaxis=False,
        showlegend=show_legend, legendgroup="pb",
    )
    variance_trace = go.Bar(
        name="Variance (Our − PB)", x=labels, y=variance,
        marker_color=variance_colors, width=0.25,
        text=variance_text, textposition="outside",
        textfont=dict(size=12),
        outsidetextfont=dict(size=12),
        cliponaxis=False,
        showlegend=show_legend, legendgroup="variance",
    )

    if row is not None and col is not None:
        fig.add_trace(our_trace, row=row, col=col)
        fig.add_trace(pb_trace, row=row, col=col)
        fig.add_trace(variance_trace, row=row, col=col)
    else:
        fig.add_trace(our_trace)
        fig.add_trace(pb_trace)
        fig.add_trace(variance_trace)


def _coerce_float(v) -> float:
    """Convert to float for plotting; treat None as 0.0 (with marker indicating missing)."""
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Empty-state helper
# ---------------------------------------------------------------------------

def _empty_state_figure(title: str, message: str, color: str = "gray") -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(
        text=message,
        showarrow=False,
        xref="paper", yref="paper",
        x=0.5, y=0.5,
        font=dict(size=20, color=color),
    )
    fig.update_layout(
        title=title,
        height=300,
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
        margin=dict(t=60, b=20, l=20, r=20),
    )
    return fig


# ---------------------------------------------------------------------------
# Export helper
# ---------------------------------------------------------------------------

def export_to_html(fig: go.Figure, path: str) -> None:
    """Export figure to standalone HTML (Plotly CDN for the JS).

    Use this for README screenshots — open the HTML in a browser, screenshot,
    drop the PNG into docs/screenshots/. Manual step keeps the simulator
    dep-free of headless browsers (kaleido).
    """
    fig.write_html(path, include_plotlyjs="cdn")
