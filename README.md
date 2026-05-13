# Post-Trade Operations Simulator

Python toolkit modelling Middle Office / Trade Support workflows: trade booking, confirmation matching, lifecycle events, settlement projection, breaks resolution, and reconciliation. Includes a Plotly exception-management dashboard.

Built as an MVP demonstration of post-trade operations — designed for clarity over completeness.

After `python main.py` runs the end-to-end pipeline, three interactive Plotly dashboards land in [`docs/screenshots/`](docs/screenshots/) as standalone HTML files:

- `breaks_queue.html` — MO exception queue (M2 + M5 + M8 breaks, urgency-coded)
- `settlement_calendar.html` — net cash flow by date and currency, source-annotated
- `recon_variance.html` — position and cash recon: our view vs PB vs signed variance

Open any in a browser to hover, zoom, and filter.

## Scope

**6 modules (MVP):**
- `trade.py` (M1) — Trade booking via synthetic FIX-style parser
- `confirmation.py` (M2) — CTM-style matching + Markitwire-style ISDA confirm generation
- `lifecycle.py` (M3) — IR resets, equity dividends
- `settlement.py` (M5) — T+0/T+1/T+2 projection by product + breaks generator
- `reconciliation.py` (M8) — Prime-broker vs internal books
- `dashboard.py` (M10) — Plotly exception management view

**5 products** (data model is extensible via `product_type` handler dispatch):
- FX spot / forward — T+2 settlement, two-leg cash flow
- FX option — T+2 premium settlement, strike + expiry tracked
- Interest Rate Swap (IRS) — quarterly resets, payment legs, fixings
- Cash Equity — T+2 settlement, dividends as lifecycle events
- Futures — T+1 settlement (stub; daily MTM parked for future)

## Quick start

```bash
pip install -e .            # editable install of the post_trade package
python main.py              # run end-to-end pipeline + export dashboards
pytest tests/               # 73 invariant tests
```

After running `main.py`, open `docs/screenshots/breaks_queue.html` (and the other two `.html` files) in a browser to see the interactive Plotly dashboard panels.

## Operational coverage

Each operational requirement of a typical Middle Office / Trade Support role maps to a specific module, file, or test in this simulator:

| Operational requirement | Where it lives |
|---|---|
| Trade capture (T+0 affirmation/confirmation) | `post_trade/trade.py` (M1) + `post_trade/confirmation.py` (M2) |
| Multiple products and asset classes (Futures, Options, FX, IRS, Cash Equity, OTC) | 5 product types covered. `product_type` handler dispatch makes adding new products (bonds, equity swaps, credit derivatives) a one-function extension. |
| Knowledge of full product lifecycle (IR derivatives, FX, equities) | `post_trade/lifecycle.py` (M3) — IR resets with ACT/360 day-count, equity dividends |
| Established procedures and methods for accurate trade capture | `docs/PROCEDURES.md` — break-resolution playbook per break type |
| Accurate and timely trade affirmation across multiple asset classes on trade date | `post_trade/confirmation.py` (M2) — CTM-style two-layer matching + Markitwire-style ISDA confirms |
| Timely resolution of breaks | `post_trade/settlement.py` (M5) + `post_trade/reconciliation.py` (M8) — 9 break types across confirmation, settlement, and recon |
| Central point of contact for portfolio managers and trading on position, pricing, technology, risk, and security related issues | `post_trade/dashboard.py` (M10) — three Plotly panels surface every break + settlement + recon variance |
| Multi-module pipeline modelling cross-functional interaction (Technology, Risk, Operations, Trading) | Pipeline architecture: modules consume each other's typed outputs (Trade → Settlement → Position → Break) |
| Process discipline and control mindset | 73 pytest invariant tests act as code-level controls; each test names the real-world failure mode it prevents |
| Prime broker reconciliation and vendor system workflows | `compute_cash_balances` + `reconcile_*_positions` in M8 model PB recon flow |
| Familiarity with vendor confirmation systems (CTM, Markitwire) | CTM-style matching + Markitwire-style ISDA confirms modelled in M2 |
| Two-tier tolerance for break detection | `max(1bp, $1 floor)` — prevents false positives without missing real errors |

## Dashboard

Three Plotly panels surface the operational outputs of the simulator for human review. All three export to standalone HTML (open in a browser to hover, zoom, filter) via `python main.py`.

### Breaks Queue (`docs/screenshots/breaks_queue.html`)

Merged view of M2 confirmation breaks, M5 settlement breaks, and M8 reconciliation breaks. 8-column table — `Urgency` (CRITICAL/HIGH/MEDIUM/LOW), `Source`, `Break Type`, `What it means` (plain-English description), `Trade ID`, `Position / Cash`, `Our value`, `Their value`. Rows colour-coded by urgency, sorted critical → low.

### Settlement Calendar (`docs/screenshots/settlement_calendar.html`)

Net cash flow per (date, currency) over the projection horizon. One sub-panel per currency. Each bar is annotated with the source type(s) that produced it (FX spot, IRS reset, Equity, FX option, Futures, Equity dividend) so a viewer can identify what's driving each cash flow without inspecting trades.

### Reconciliation Variance (`docs/screenshots/recon_variance.html`)

Three bars per break: our view (blue), PB view (orange), and the signed variance (green if positive, red if negative). Variance bar carries a text label (e.g., `+1`, `-500`) so even small drifts are visible when absolute bars look identical. Mixed position + cash breaks render as two side-by-side subplots because shares and cash share no common y-axis scale.

## Project structure

```
post_trade/                       # core modules (M1, M2, M3, M5, M8, M10)
├── trade.py                      # M1: Trade booking
├── confirmation.py               # M2: CTM-style matching + ISDA confirms
├── lifecycle.py                  # M3: IR resets + equity dividends
├── settlement.py                 # M5: Cash settlement + breaks
├── reconciliation.py             # M8: Position + cash recon vs PB
└── dashboard.py                  # M10: Plotly dashboard
tests/                            # 73 pytest invariant tests
docs/
├── PROCEDURES.md                 # break-resolution playbook
└── screenshots/                  # Plotly dashboard HTML exports
data/                             # synthetic fixtures
main.py                           # end-to-end pipeline runner
pyproject.toml                    # editable package install config
```

## Design choices

- **Synthetic data only.** No real market data or vendor system integration.
- **Concept-demonstration realism.** Dict-based FIX, hand-rolled ISDA confirms, simplified break detection.
- **Extensibility framed.** Code structure supports adding new products via handler dispatch.

## Future enhancements (parked)

- Allocations & novations module (block trade → sub-accounts)
- Margin/collateral mechanics (real ISDA SIMM, VM/IM)
- Standalone intra-day NAV module (currently subsumed into M8 reconciliation)
- Reporting module (P&L attribution by Greek)
- Real FIX 4.4 parser (replace dict-based)
- Additional products: FX exotics, futures, bonds, OTC exotics, equity swaps
- Real market data integration (FRED, yfinance, Bloomberg API)
- Headless dashboard rendering (kaleido for PNG export)
- Click-through drill-down in dashboard (break → source trade)
- Multi-portfolio filter UI

## License

MIT — see `LICENSE` for the full text.
