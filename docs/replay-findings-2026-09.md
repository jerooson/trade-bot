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
- Heat's chart-only ideas (82 % of them) are missing. Building levels for
  those (chart analyzer output plus manual review) is the next dataset job.
- 122 entries over five months cannot separate a +0.2 % edge from zero;
  the value here is in the rankings and the slippage measurement.
