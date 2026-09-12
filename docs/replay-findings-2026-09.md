# Minute-bar replay findings (2026-09-12)

Read-only research over the bot's own signal ledgers and IBKR 1-minute bars.
No trading settings were changed. Regenerate with:

```bash
python -m bot.signal_dataset --out data/signals_dataset.jsonl \
  --discord logs/history.jsonl --discord <vps>/signals.jsonl \
  --heat <vps>/heat_ideas.jsonl --heat-decisions <vps>/heat_idea_decisions.jsonl \
  --manual <vps>/manual_day_trade_plans.json --positions <vps>/day_trade_positions.jsonl
python -m bot.market_data --dataset data/signals_dataset.jsonl   # IB Gateway (paper) must be up
python -m bot.replay                                               # touch entry (live-like)
python -m bot.replay --entry close                                 # 1-minute confirmation variant
```

## Data

| Source | Levels | Window | Entered by replay | Live lifecycles linked |
|---|---:|---|---:|---:|
| Discord PLAN | 127 | 2026-04-20 .. 09-11 | 93 (2 sessions) | 65 (24 closed) |
| Heat, level in Heat's text | 51 of 278 ideas | 07-16 .. 09-11 | 27 (10 sessions) | 33 (17 closed) |
| Heat, level read from the chart | 34 more | 07-17 .. 09-11 | 25 | |
| Manual | 3 | | 2 | 3 |

953 cached sessions of 1-minute bars. All 64 Heat ideas that carried a chart
attachment were reviewed by hand (`data/heat_levels_reviewed.jsonl`): 34
yielded a usable level (confidence >= 0.5), 16 attachments were option
position screenshots, 3 were charts of a different ticker, and the rest were
support/range commentary with no breakout level. The remaining 163
chart-less, level-less Heat ideas (commentary such as "watch the 21-day")
stay out of the replay.

## 1. Model versus reality (41 closed live trades)

| | value |
|---|---|
| Replay also entered | 39 / 41 |
| Same exit reason | 34 / 39 |
| Fill slippage, live minus model | mean +0.62 %, median +0.12 % |
| P&L per trade, live minus model | mean -0.27 %, median -0.04 % |

The replay reproduces the live policy well enough to trust its *relative*
comparisons. The absolute level is optimistic: the model has no spread and
fills at the level, and it enters 29 Discord plans the live bot refused,
21 of them because the ask was already above the +0.2 % entry cap at the
cross (`entry_gap_above_limit`, `entry_ask_above_cap`). Discord model P&L
is therefore an upper bound.

Slippage is the single largest cost in the system: a median 0.12 % and a
mean 0.62 % per fill against a strategy whose live average trade is +0.17 %.

## 2. Policy comparison (touch entry, $20 per trade, 122 entries)

| Policy | Net $ | Win % | Avg % | PF | Exits |
|---|---:|---:|---:|---:|---|
| **live** (-2 % stop, milestones, EOD tighten) | **+5.02** | 49 | +0.21 | 1.29 | 68 stop / 51 eod / 3 target |
| stop1 (-1 % flat) | +2.44 | 33 | +0.10 | 1.15 | |
| bracket (-2 % + signal target) | -1.74 | 41 | -0.07 | 0.93 | |
| stop2_flat (-2 %, no milestones) | -2.55 | 41 | -0.11 | 0.89 | |
| stop3 (-3 % flat) | -8.92 | 43 | -0.37 | 0.71 | |
| hold to 15:50 (no stop) | -10.97 | 50 | -0.45 | 0.72 | |

The deployed policy is the best of the six on this history. The stepped
milestones are what make it work: the same -2 % initial stop without them
loses $2.55. Wider stops and holding to the close are clearly worse; the
signals' average MFE is +1.9 % and MAE -1.1 %, so most of the move that
exists is gone by the close.

## 3. By source (live policy, touch entry)

| Source | Entered | Net $ | Win % | Avg % | PF |
|---|---:|---:|---:|---:|---:|
| Discord | 93 | +0.70 | 46 | +0.04 | 1.05 |
| Discord, pre-bot era (Apr-Jun) | 36 | +1.67 | | | |
| Discord, live era (Jun-Sep) | 57 | -0.95 | | | |
| Heat, level from text | 27 | +4.58 | 59 | +0.85 | 3.17 |
| Heat, level from chart review | 25 | +4.17 | 68 | +0.83 | 3.21 |
| Manual | 2 | -0.26 | | | |

Heat beats Discord under every policy tested, not only the live one, so the
difference is in the levels rather than the exit rules. The 34 chart-read
levels (mostly Heat's yellow prior-high lines, plus gap bottoms and
Fibonacci levels) perform the same as the levels Heat typed out, which is
the first evidence that the edge is in *his levels* rather than in the
subset he happens to put a number on. 52 Heat trades is still a small
sample and the replay assumes the leveraged ETF fills at the ETF's bar
close of the trigger minute.

Chart-level kinds (touch entry, live policy): yellow line n=15 avg +0.93 %,
gap bottom n=3 +0.79 %, fib retrace n=2 +0.96 %; the two box-top levels
were flat. Too few per kind to rank them.

## 4. One-minute confirmation (candidate rule, not deployed)

Requiring the first minute to *close* beyond the level, and paying that
close instead of the level:

| Policy | Entries | Net $ | Win % | PF |
|---|---:|---:|---:|---:|
| live rules, touch entry (baseline) | 147 | +9.19 | 52 | 1.48 |
| live rules, confirmed entry | 124 | +12.37 | 55 | 2.10 |
| stop2_flat, confirmed entry | 124 | +12.63 | 53 | 1.84 |
| hold, confirmed entry | 124 | +10.38 | 58 | 1.54 |

With the chart levels included the confirmation still helps overall
(+35 % net on 16 % fewer entries) but the effect is uneven: it doubles
text-level Heat (+4.58 to +8.28) and Discord (+0.70 to +2.02) while it
*hurts* the chart-read Heat levels (+4.17 to +1.96), which tend to be
prior-high lines that price runs through on the first touch. This is the one change
worth testing forward, on the paper account first, because it costs a
minute of delay (entry median 0.03 % above the level in this history) and
its benefit comes from skipping wick-only breakouts, which the live bot
currently buys within seconds.

## What this does not show

- No spread, no partial fills, no broker rejects: absolute numbers are
  optimistic, relative rankings are the usable output.
- Milestones confirm on 1-minute closes; the live bot confirms on 5-second
  polls and locks in slightly sooner.
- Chart levels were read by one reviewer from static screenshots; a second
  pass, or the chart analyzer's own reads, would show how stable they are.
- 147 entries over five months cannot separate a +0.2 % edge from zero;
  the value here is in the rankings and the slippage measurement.

## 5. Can the yellow lines be generated? (prior-high scanner, preliminary)

`bot/level_scanner.py` reverse-engineers the 21 reviewed yellow lines
(all on prior daily highs; 15/21 strict pivots; 4-65 sessions old; prior
close 1-8 % below) and emits, for every symbol-day, the nearest unbroken
pivot high above the prior close, plus a random-offset control level on the
same symbol-days. It reproduces 17 of the 21 lines. Run over the 137
tickers in the dataset for 2026-07-16..09-11 (Heat's window): 613 levels,
613 controls; 85 % of the required minute bars were cached when the IBKR
gateway started throttling, and the missing 15 % hit both groups equally.

| Policy | Heat (n=52) | Scanner (n=258) | Random control (n=255) |
|---|---:|---:|---:|
| live rules, avg % / trade | **+0.84 ± 0.34** | +0.17 ± 0.12 | +0.18 ± 0.15 |
| live rules, win % | 64 | 48 | 45 |
| hold to close, avg % | +0.84 ± 0.36 | +0.32 ± 0.16 | -0.02 ± 0.19 |
| flat -2 % stop, avg % | +0.79 ± 0.36 | +0.16 ± 0.14 | +0.03 ± 0.16 |

(± is one standard error of the mean.)

What this says:

1. **Prior highs carry some information.** Without stops the scanner's
   levels beat random levels (+0.32 % vs -0.02 %, 56 % vs 45 % winners),
   and with a flat stop too. Buying "somewhere above the close" is not the
   same as buying a prior high.
2. **The mechanical rule does not reproduce Heat.** Under the live rules
   the scanner is indistinguishable from random (+0.17 % vs +0.18 %) and a
   quarter of Heat's per-trade result; the trailing milestones lock in
   early on all three groups and erase the level's small edge. Restricting
   the scanner to Heat's own tickers helps (+0.30 %) but stays far below
   his +0.84 %. Distance band, pivot age and leverage do not separate.
3. **Most of Heat's edge is selection**: which symbol, which day, and which
   of several candidate lines. That is the part the scanner cannot see.
   With 52 Heat trades the gap (+0.84 vs +0.17, difference ~0.67 ± 0.36)
   is about two standard errors: suggestive, not conclusive.

Implications: a fully automatic "Heat without Heat" is not supported by
this evidence. What the scanner *is* good for is the opposite direction:
turning Heat's chart-only posts into executable levels automatically
(17/21 recall) so the live bot no longer waits for a typed number.

## 6. What Heat selects: feature table (Heat 52 vs scanner 258 vs random 255)

`bot/features.py` computes, for every replayed entry, features knowable at
signal time (market position, 20-day relative strength, distance to the
21-day SMA, ATR, prior-day volume ratio, breakout-minute volume ratio, entry
hour) and compares the distributions. Medians, live policy:

| Feature | Heat | Scanner | Random | effect (sd) |
|---|---:|---:|---:|---:|
| ret20 (%) | **-0.8** | +5.7 | +6.1 | -0.45 |
| rs20 vs SPY (%) | **0.0** | +3.6 | +3.8 | -0.48 |
| dist to SMA21 (%) | **+0.3** | +4.4 | +3.9 | -0.60 |
| above SMA21 | 56 % | 83 % | 78 % | -0.65 |
| above SMA50 | 47 % | 65 % | 61 % | -0.37 |
| ATR14 (%) | **3.4** | 5.1 | 5.7 | -0.56 |
| level distance (%) | **1.3** | 2.9 | 3.5 | -0.28 |
| SPY vs 8d / 21d | same | same | same | ~0 |
| prior-day volume ratio | 0.83 | 0.94 | 0.92 | -0.11 |
| breakout-minute volume ratio | 0.87 | 1.06 | 1.20 | -0.13 |

Heat is not buying momentum breakouts. His names have gone nowhere for 20
days, sit on the 21-day average, are calmer than the scanner's, and the
line is close (1.3 % away). The scanner, by construction, buys prior highs
in names that are already up 6 % in a month and 4 % above their 21-day:
that is chasing. Market-regime and volume features do **not** separate the
groups, so "add a market filter" or "add a volume filter" would not have
been the right guess.

Turning the difference into a filter is where it gets honest:

| Filter on scanner rows | n | avg % | win % | same filter on random |
|---|---:|---:|---:|---|
| near SMA21 (<=2 %) and ret20 <= 2 % | 60 | +0.37 ± 0.29 | 48 | +0.52 ± 0.29 (n=66) |
| + ATR <= 4.5 % | 20 | +1.17 ± 0.57 | 60 | +0.04 ± 0.62 (n=14) |
| breakout-minute volume >= 1.5x | 93 | +0.44 ± 0.23 | 48 | +0.26 ± 0.24 (n=102) |
| breakout volume >= 1.5x, quiet prior day | 61 | +0.49 ± 0.31 | 48 | |
| entry before 10:00 ET | 137 | +0.16 | 48 | +0.16 |

- The "pullback" filter helps the random control as much as the scanner:
  in this window it is a general mean-reversion effect, not a prior-high
  effect. The three-way filter that looks best (n=20) is too small to
  believe, and Heat's own trades that satisfy it did *worse* (+0.44 %,
  n=15) than the ones that do not (+1.00 %, n=37), so it does not capture
  what makes his picks good.
- Breakout-minute volume is the one feature with a prior-high-specific
  tilt (scanner +0.44 vs +0.03 below 1.5x, and Discord +0.92 vs +0.05),
  but the same cut lifts random levels too (+0.26 vs +0.13), and Heat's
  own entries are mostly low-volume (median 0.87x). Worth a forward test
  as an entry filter; not a substitute for selection.
- Heat's edge is concentrated in his early entries: before 10:00 ET
  +1.81 % (n=19) vs +0.28 % after; the scanner shows no such split. His
  early trades are levels posted the day before that fill at the open.

Net: the feature table explains *what* Heat picks (quiet names resting on
the 21-day with a nearby prior high) but no mechanical version of that
description reproduces his result on out-of-sample symbol-days. The next
honest test is forward, not backward: run the scanner with the breakout
-volume filter on paper next to the live Heat feed for a month.
