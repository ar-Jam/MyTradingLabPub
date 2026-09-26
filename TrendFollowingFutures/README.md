# Trend-Following Futures (QuantConnect / Lean)

A diversified time-series momentum strategy on CME futures. It trades long or short based on the sign of each market's 12-month return, and sizes each market so they all carry the same risk.

## How to run it on QuantConnect

1. Go to quantconnect.com → **Algorithm Lab** → **Create New Algorithm** → **Python**.
2. Replace the contents of `main.py` with [`main.py`](main.py) from this folder.
3. Optional: add parameters under **Project → Parameters** (see the table below).
4. Click **Backtest**.

Futures data on QuantConnect starts around 2009. The full-size universe starts in 2010. The micro universe starts in 2020, because most micro contracts launched between 2019 and 2021.

## How the code maps to the plan

| Step | Where in the code |
|---|---|
| 1. Universe | `FULL_SIZE_UNIVERSE` / `MICRO_UNIVERSE`; `add_future(...)` with back-adjusted (Panama canal) continuous contracts, rolled on open interest |
| 2. Daily volatility | `daily_point_volatility`: std-dev of the daily price change over `vol_window` days, measured in price points |
| 3. Signal | `trend_forecast`: the average of `sign(price change)` over each lookback. The default is `252`, so the position is simply long or short |
| 4. Target contracts | `target_contracts`: `forecast × risk budget ÷ (daily vol × point value × √252)` |
| 5. Rebalance threshold | `needs_rebalance`: the position is changed only if it is off by more than `buffer`, or if it needs to open, close or flip |
| 6. Rolls | `_manage_market`: any holding in a contract that is no longer the mapped front contract is closed and reopened in the new contract, as real orders |
| 7. Costs | Interactive Brokers brokerage model (commissions and exchange fees), plus `slippage_ticks` of slippage on every fill, rolls included |

**Risk budget per market** = `equity × target_vol × idm ÷ number of markets with enough history`.
`idm` (instrument diversification multiplier) scales the per-market risk up. Without it, imperfect correlation between markets would leave the portfolio well below `target_vol`.

**Why point changes and not % returns?** Back-adjusted prices are shifted by a constant amount at each roll and can turn negative (crude oil in 2020). Price differences stay correct after that shift, but percentage returns do not. Dollar volatility per contract = point volatility × contract multiplier, and that is the number the sizing formula needs.

**Why history is reloaded at each roll:** at a roll, Lean re-bases the whole back-adjusted series. The strategy reloads its price window on every `SymbolChangedEvent` so the roll gap is not mistaken for a price move.

## Parameters

| Name | Default | Meaning |
|---|---|---|
| `use_micros` | `0` | `1` = micro universe (MES, MNQ, M2K, ZF, ZN, M6E, MJY, M6B, M6A, MGC, SIL, MCL) |
| `cash` | `5000000` full-size / `250000` micro | Starting capital |
| `target_vol` | `0.12` | Annual portfolio volatility target |
| `idm` | `1.5` | Diversification multiplier |
| `lookbacks` | `252` | Comma-separated lookbacks in trading days, e.g. `21,63,252` to blend 1, 3 and 12 months |
| `vol_window` | `60` | Days used for the volatility estimate |
| `buffer` | `0.15` | No-trade band as a fraction of position size |
| `rebalance` | `daily` | `daily` or `weekly`. Rolls are always checked daily |
| `max_contracts` | `0` | Per-market cap on contracts (0 = no cap) |
| `slippage_ticks` | `1` | Slippage per fill, in minimum ticks |

## Account size

The strategy trades whole contracts, so a market whose risk budget is smaller than one contract's risk rounds to **zero** and is never traded. Approximate yearly dollar volatility of one contract: ES ≈ $40k, NQ ≈ $85k, GC/SI ≈ $40k, CL ≈ $25k, ZF ≈ $4k, 6E ≈ $9k.

With 18 markets, 12% target vol and IDM 1.5, each market's budget is about 1% of equity per year. At $1M that is $10k, so ES, NQ, GC, SI and CL would all show zero positions. That is why the full-size default is $5M. At the end of each backtest, the log lists every market that never got a position. If that list is long, raise `cash`, drop the most volatile markets, or set `use_micros=1`.

## What to expect

- Most of the profit comes from a few strong trend years (2008, 2014, 2022). There can be 1–3 flat or losing years in choppy markets.
- The win rate is often below 50%. The edge comes from letting winners run.
- A realistic long-run Sharpe ratio is about 0.5–0.8. A much higher figure usually means overfitting or missing costs.
