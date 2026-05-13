# MO Procedures — Break Resolution Playbook

Per-break-type investigation procedures for an MO analyst using this simulator's output. Maps directly to the JD line *"Establish procedures and methods for accurate Trade Capture within the firm on a trade date basis, in conjunction with Risk and Technology."*

Each procedure follows the same structure: **what the break means**, **likely root cause**, **investigation steps**, **urgency level**, **resolution path**.

---

## 1. M2 — Confirmation breaks

### 1.1 `no_match`

**What it means.** Our trade has no matching counterparty reply in the CTM batch. Both layers of matching (`external_id` and fuzzy tuple) returned empty.

**Likely root cause.**
- Their booking is delayed (most common — minutes-to-hours behind)
- Their batch upload failed or hasn't run yet
- They've genuinely never received the trade (rare; suggests upstream MO communication gap)

**Investigation steps.**
1. Wait 1 cycle (typically 1 hour). Re-run match.
2. If still no_match, contact counterparty ops desk directly.
3. If they confirm receipt, request their CTM submission reference; investigate why CTM rejected.
4. If they have no record at all, walk back to M1 — was the trade actually booked our side?

**Urgency.** Low → Medium (escalates with time elapsed).

**Resolution path.** Re-affirm once both sides have records in CTM.

---

### 1.2 `qty_mismatch` (Layer-1 match with field mismatch)

**What it means.** We and the counterparty both have the trade under the same `external_id`, but the quantity field disagrees.

**Likely root cause.**
- Fat-finger error one side
- One side captured a partial fill vs the full notional
- One side applied an amendment we didn't propagate

**Investigation steps.**
1. Pull our trade audit log — what qty was first booked? Any amendments?
2. Request counterparty's audit equivalent.
3. Identify whose number is wrong; re-book the correct one.

**Urgency.** High (settlement amount depends on this).

**Resolution path.** Re-book the wrong side; status returns to `affirmed` on next match.

---

### 1.3 `price_mismatch` (Layer-1 match with price disagreement beyond tolerance)

**What it means.** Same external_id but the price differs by more than the per-product tolerance (FX 1e-4, IRS 1e-6, equity exact).

**Likely root cause.**
- Genuine price disagreement (one side captured the wrong rate)
- Manual override / amendment one side
- Different sources (e.g., one side uses ECN rate, other uses voice-broker rate)

**Investigation steps.**
1. Pull voice tape or chat transcript from time of execution.
2. Compare against both sides' system records.
3. Identify which side has the canonical execution record.

**Urgency.** High.

**Resolution path.** Re-book wrong side.

---

## 2. M4 — Settlement breaks

### 2.1 `missing_their_side`

**What it means.** We projected a cash settlement; counterparty's settlement instruction file has no matching entry.

**Likely root cause.**
- Their settlement instruction batch hasn't run yet (timing artefact)
- They processed the trade but didn't generate a settlement instruction (system bug)
- The trade was cancelled and they're aware, we're not

**Investigation steps.**
1. Wait 2 hours, re-run break check.
2. If persists, contact counterparty operations.
3. If they confirm cancellation, locate the cancellation in our system; rebook or void.

**Urgency.** Medium (often timing).

**Resolution path.** Their instruction arrives, or our trade is corrected.

---

### 2.2 `missing_our_side` ⚠ CRITICAL

**What it means.** Counterparty expects a settlement we don't project. We have no record of the underlying trade.

**Likely root cause.**
- We missed booking a trade entirely (manual flow gap)
- Trade was deleted from our system
- Counterparty is claiming a trade that didn't happen (rare; could be fraud or genuine error)

**Investigation steps.**
1. **Immediate**: pull our front-office trade blotter for this counterparty and trade date.
2. Check execution logs (voice tape, electronic platform, chat).
3. If we have no record, escalate to senior MO + trading desk supervisor.
4. Request counterparty's evidence (trade ticket, voice tape, electronic confirmation).
5. If counterparty has hard evidence of execution and we don't → unauthorized trade or system corruption. Senior escalation.

**Urgency.** **CRITICAL** — asymmetric downside; unknown obligation has unbounded risk.

**Resolution path.** Either (a) book the missing trade and re-run, or (b) push back on counterparty with evidence of non-existence.

---

### 2.3 `amount_mismatch`

**What it means.** Same trade, same date, different settlement amount beyond `max(1bp, $1)` tolerance.

**Likely root cause.**
- Fee miscalculation one side
- Different rounding conventions
- Different settlement-amount derivation (e.g., gross vs net of commission)

**Investigation steps.**
1. Pull counterparty's fee breakdown for this trade.
2. Compare against our internal fee schedule.
3. Identify the specific fee component that differs.

**Urgency.** Medium-High.

**Resolution path.** Adjust the wrong side's fee/commission.

---

### 2.4 `date_mismatch`

**What it means.** Settlement dates disagree.

**Likely root cause.**
- Holiday calendar difference (we use US holidays; they use SG; one of us has wrong calendar applied)
- Late amendment to value_date one side
- Different value-date convention (e.g., FX forward where one side uses "next valid business day", other uses "modified following")

**Investigation steps.**
1. Pull both sides' holiday calendars for the relevant market.
2. Verify the trade's documented value-date rule (in ISDA confirm if applicable).
3. Move one side to match.

**Urgency.** Medium.

**Resolution path.** Update wrong side's value_date.

---

### 2.5 `currency_mismatch`

**What it means.** Same trade, same date, different currency code in settlement instruction.

**Likely root cause.**
- Pure booking error one side
- Confusion in cross-currency products (which leg is "base" vs "quote")

**Urgency.** High (rare but obvious; usually requires immediate correction).

**Resolution path.** Identify which currency is canonical (from ISDA confirm or master agreement); update wrong side.

---

## 3. M5 — Reconciliation breaks

### 3.1 `position_qty_mismatch`

**What it means.** End-of-day equity position count differs between our books and the PB's overnight position file.

**Likely root cause (by drift size).**
| Drift | Most likely cause |
|---|---|
| 1–10 shares | Stock dividend, fractional share rounding, split-adjustment difference |
| Round lot (100+) | Missing buy/sell booking on one side |
| Large (1000+) | Misclassified portfolio (booked to wrong account), or major settlement break upstream |

**Investigation steps.**
1. Pull both blotters for the ticker.
2. Compare trade-by-trade.
3. If trade lists match, look at corporate actions calendar (recent splits, dividends, rights issues).
4. If corp actions match, check portfolio classification.
5. Escalate to PB if no internal cause found.

**Urgency.** Medium-High.

**Resolution path.** Book the missing trade or process the missed corp action.

---

### 3.2 `missing_position` (PB shows position we don't have)

**What it means.** PB's overnight file shows a position our books don't reflect.

**Likely root cause.**
- A trade we don't know about (worst case — possibly unauthorized)
- A corporate action that allocated shares we didn't process (e.g., rights issue, spin-off)
- An inbound transfer (from another portfolio or another PB) we didn't expect

**Investigation steps.**
1. Pull PB's trade history for this ticker + portfolio.
2. Check for inbound transfers or corp-action allocations.
3. If none, escalate to ops + trading desk to identify origin.

**Urgency.** **HIGH** (asymmetric — unknown asset is less scary than unknown liability, but still demands investigation).

**Resolution path.** Identify origin and book; or remove from PB's records if erroneous.

---

### 3.3 `missing_position` (we show position PB doesn't have)

**What it means.** Our books show a position PB doesn't acknowledge.

**Likely root cause.**
- Failed settlement (we think we bought but shares never arrived; possibly buy-in pending)
- Misallocation to wrong portfolio in PB's records
- Recently cancelled trade not reflected on PB side yet

**Investigation steps.**
1. Pull settlement history for the trade — did it actually settle?
2. If failed, identify failure reason (insufficient counterparty securities, instruction error).
3. Check PB's allocation across portfolios.

**Urgency.** Medium-High.

**Resolution path.** Either (a) settlement processed late and PB catches up, or (b) our books need adjusting to reflect the failed/cancelled trade.

---

### 3.4 `cash_balance_mismatch`

**What it means.** End-of-day cash balance per (portfolio, currency) differs beyond tolerance.

**Likely root cause.**
- Fee posting timing difference (PB applied fee today, we'll apply tomorrow)
- Interest accrual posting difference
- Missing settlement on either side
- Manual cash adjustment one side

**Investigation steps.**
1. Compute the gap precisely.
2. Compare against expected fees, interest, and settlement movements.
3. Identify which specific posting is missing or extra.

**Urgency.** Medium.

**Resolution path.** Post the missing fee/interest/settlement.

---

### 3.5 `missing_cash` (asymmetric)

Same urgency framework as `missing_position`:
- **PB shows cash we don't expect** — typically a fee credit or interest we didn't anticipate. Medium urgency.
- **We expect cash PB doesn't show** — typically a failed/late settlement. Medium-High urgency.

---

## 4. Escalation matrix

| Urgency | Escalation path | Timeframe |
|---|---|---|
| **CRITICAL** (`missing_our_side`) | Senior MO + trading desk supervisor + compliance | Same-day |
| **HIGH** (qty/price/currency mismatch on confirmed trades, missing PB-side position) | Counterparty ops team + senior MO | Same-day |
| **MEDIUM** (amount mismatch, date mismatch, cash balance drift, missing positions on our side) | Counterparty ops team | Next-day |
| **LOW** (no_match, missing_their_side) | None — wait one cycle, re-check | Hours |

---

## 5. Process improvement signals

Breaks aren't just operational fires — they're **signals about systemic issues**:

- **Recurring small breaks against one counterparty** → likely a configuration mismatch in fee handling or rounding convention. Worth escalating broker integration.
- **Month-end / quarter-end break spikes** → balance-sheet pressure stress-tests the settlement infrastructure. Coordinate with Treasury on funding.
- **Same break type from different products** → systemic issue in a shared infrastructure layer (e.g., the calendar service, the fee engine).
- **Asymmetric escalation pattern** (we always catch their errors, they never catch ours) → audit our process discipline; we might be the lax side.

Track break frequency by type, counterparty, asset class. Use the trend to identify which process improvements pay back.

---

## 6. Source code references

| Break type | Module | Function |
|---|---|---|
| `no_match`, `qty_mismatch`, `price_mismatch`, `date_mismatch` | `post_trade/confirmation.py` | `match_against_counterparty` |
| `amount_mismatch`, `currency_mismatch`, `missing_their_side`, `missing_our_side` | `post_trade/settlement.py` | `check_breaks` |
| `position_qty_mismatch`, `missing_position` | `post_trade/reconciliation.py` | `reconcile_equity_positions` |
| `cash_balance_mismatch`, `missing_cash` | `post_trade/reconciliation.py` | `reconcile_cash_balances` |
| Display + urgency colour-coding | `post_trade/dashboard.py` | `URGENCY_LEVELS`, `_urgency_color` |
