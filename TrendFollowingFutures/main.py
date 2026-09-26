# region imports
from AlgorithmImports import *

from collections import deque
import math

import numpy as np
import pandas as pd
# endregion

"""
Diversified time-series momentum (trend-following) on CME futures.

Build order (mirrors the sections below):
    1. Universe            -> FULL_SIZE_UNIVERSE / MICRO_UNIVERSE, continuous contracts
    2. Daily volatility    -> std-dev of daily point changes on the back-adjusted series
    3. Signal              -> sign of the N-day price change (optionally blended lookbacks)
    4. Target contracts    -> equal risk budget per market / (dollar vol per contract)
    5. Rebalance threshold -> only trade when the position is off by more than `buffer`
    6. Roll handling       -> move any position in an expired/unmapped contract to the new front
    7. Cost model          -> Interactive Brokers futures commissions + N ticks slippage per fill

Paste this file into a QuantConnect Python project as main.py. Every tunable is exposed
through get_parameter so it can be changed in the project's Parameters panel or optimized.
"""

# ---------------------------------------------------------------------------------------
# 1. UNIVERSE
# ---------------------------------------------------------------------------------------
# Tickers are resolved by Lean's symbol-properties database (market is inferred).

FULL_SIZE_UNIVERSE = [
    # Equity indices
    "ES", "NQ", "RTY",
    # Bonds
    "ZN", "ZB", "ZF",
    # Currencies
    "6E", "6J", "6B", "6A",
    # Metals
    "GC", "SI", "HG",
    # Energy
    "CL", "NG",
    # Ags
    "ZC", "ZS", "ZW",
]

# Micro contracts exist for fewer markets (no micro ags or micro Treasury price futures),
# and most micros only started trading in 2019-2021, so backtests must start later.
MICRO_UNIVERSE = [
    # Equity indices
    "MES", "MNQ", "M2K",
    # Bonds (full-size 5-yr and 10-yr notes are the calmest contracts available)
    "ZF", "ZN",
    # Currencies
    "M6E", "MJY", "M6B", "M6A",
    # Metals
    "MGC", "SIL",
    # Energy
    "MCL",
]

TRADING_DAYS = 252


# ---------------------------------------------------------------------------------------
# Pure strategy math (no Lean dependencies, easy to unit test)
# ---------------------------------------------------------------------------------------

def daily_point_volatility(closes, window):
    """2. Std-dev of daily price changes (in price points) over the last `window` days.

    Point changes, not percentage returns, are used because back-adjusted continuous
    prices are shifted additively and can even go negative (e.g. crude in 2020), which
    makes percentage returns on the adjusted series meaningless.
    """
    if len(closes) < window + 1:
        return None
    recent = np.asarray(list(closes)[-(window + 1):], dtype=float)
    vol = float(np.std(np.diff(recent), ddof=1))
    return vol if vol > 0 and math.isfinite(vol) else None


def trend_forecast(closes, lookbacks):
    """3. Average of sign(price change) over each lookback. Returns a value in [-1, 1].

    With a single 252-day lookback this is simply +1 (long) or -1 (short).
    """
    if len(closes) < max(lookbacks) + 1:
        return None
    last = closes[-1]
    signs = [float(np.sign(last - closes[-1 - lb])) for lb in lookbacks]
    return sum(signs) / len(signs)


def target_contracts(forecast, annual_risk_budget, daily_point_vol, point_value, max_contracts=0):
    """4. Contracts = forecast * risk budget / (annualized dollar volatility of one contract)."""
    annual_dollar_vol_per_contract = daily_point_vol * point_value * math.sqrt(TRADING_DAYS)
    if annual_dollar_vol_per_contract <= 0:
        return 0
    contracts = int(round(forecast * annual_risk_budget / annual_dollar_vol_per_contract))
    if max_contracts > 0:
        contracts = max(-max_contracts, min(max_contracts, contracts))
    return contracts


def needs_rebalance(current, target, buffer):
    """5. Trade only when the position changes meaningfully.

    Always trade when opening, closing or flipping direction. Otherwise trade only if the
    difference exceeds `buffer` (e.g. 0.15 = 15%) of the larger of current/target size.
    """
    if target == current:
        return False
    if current == 0 or target == 0 or np.sign(current) != np.sign(target):
        return True
    return abs(target - current) > buffer * max(abs(current), abs(target))


# ---------------------------------------------------------------------------------------
# 7. COST MODEL: fixed number of ticks of slippage per fill
# ---------------------------------------------------------------------------------------

class TickSlippageModel:
    def __init__(self, ticks):
        self._ticks = ticks

    def get_slippage_approximation(self, asset, order):
        return self._ticks * asset.symbol_properties.minimum_price_variation


class TrendSecurityInitializer(BrokerageModelSecurityInitializer):
    """Keeps the brokerage's fee/fill/margin models and adds tick-based slippage."""

    def __init__(self, brokerage_model, security_seeder, slippage_ticks):
        super().__init__(brokerage_model, security_seeder)
        self._slippage_ticks = slippage_ticks

    def initialize(self, security):
        super().initialize(security)
        security.set_slippage_model(TickSlippageModel(self._slippage_ticks))


class Market:
    """Per-market state: continuous future, price window and rebalance bookkeeping."""

    def __init__(self, future, history_length):
        self.future = future
        self.symbol = future.symbol
        self.root = future.symbol.id.symbol
        self.closes = deque(maxlen=history_length)
        self.last_bar_time = None
        self.needs_history = True
        self.last_rebalance_key = None
        self.ever_positioned = False


# ---------------------------------------------------------------------------------------
# Algorithm
# ---------------------------------------------------------------------------------------

class TrendFollowingFutures(QCAlgorithm):

    def initialize(self):
        # ---- Parameters -------------------------------------------------------------
        self._use_micros = int(self.get_parameter("use_micros", 0)) == 1
        self._target_vol = float(self.get_parameter("target_vol", 0.12))
        # Instrument diversification multiplier: scales up per-market risk because the
        # markets are imperfectly correlated. ~1.5 is typical for 12-18 diversified markets.
        self._idm = float(self.get_parameter("idm", 1.5))
        self._lookbacks = sorted(
            int(x) for x in str(self.get_parameter("lookbacks", "252")).split(",") if x.strip()
        )
        self._vol_window = int(self.get_parameter("vol_window", 60))
        self._buffer = float(self.get_parameter("buffer", 0.15))
        self._rebalance = str(self.get_parameter("rebalance", "daily")).lower()
        self._max_contracts = int(self.get_parameter("max_contracts", 0))
        slippage_ticks = float(self.get_parameter("slippage_ticks", 1))

        if self._use_micros:
            tickers = MICRO_UNIVERSE
            self.set_start_date(2020, 1, 1)
            self.set_cash(int(self.get_parameter("cash", 250_000)))
        else:
            tickers = FULL_SIZE_UNIVERSE
            self.set_start_date(2010, 1, 1)
            self.set_cash(int(self.get_parameter("cash", 5_000_000)))
        self.set_end_date(2025, 12, 31)

        # ---- 7. Costs: IB futures commissions + exchange fees, plus slippage -----------
        self.set_brokerage_model(BrokerageName.INTERACTIVE_BROKERS_BROKERAGE, AccountType.MARGIN)
        self.set_security_initializer(TrendSecurityInitializer(
            self.brokerage_model, FuncSecuritySeeder(self.get_last_known_prices), slippage_ticks))

        # ---- 1. Universe: back-adjusted continuous contracts, rolled on open interest ----
        history_length = max(max(self._lookbacks), self._vol_window) + 5
        self._markets = {}
        for ticker in tickers:
            future = self.add_future(
                ticker,
                Resolution.DAILY,
                data_normalization_mode=DataNormalizationMode.BACKWARDS_PANAMA_CANAL,
                data_mapping_mode=DataMappingMode.OPEN_INTEREST,
                contract_depth_offset=0,
            )
            self._markets[future.symbol] = Market(future, history_length)
        self._history_length = history_length
        self._last_plot_date = None

        chart = Chart("Positions")
        chart.add_series(Series("Long markets", SeriesType.LINE, 0))
        chart.add_series(Series("Short markets", SeriesType.LINE, 0))
        self.add_chart(chart)

    # -----------------------------------------------------------------------------------

    def on_data(self, slice):
        # A roll re-bases the whole back-adjusted series, so the stored window must be
        # reloaded to stay consistent (otherwise the gap looks like a price move).
        for symbol, _ in slice.symbol_changed_events.items():
            market = self._markets.get(symbol)
            if market is not None:
                market.needs_history = True

        for symbol, market in self._markets.items():
            has_bar = symbol in slice.bars
            if not has_bar and symbol not in slice.symbol_changed_events:
                continue
            if market.needs_history:
                self._load_history(market)
            if has_bar:
                self._append_bar(market, slice.bars[symbol])
            self._manage_market(market)

        self._plot_positions()

    # -----------------------------------------------------------------------------------

    def _load_history(self, market):
        try:
            df = self.history(market.symbol, self._history_length, Resolution.DAILY)
        except Exception as e:
            self.debug(f"History failed for {market.root}: {e}")
            return
        if df is None or df.empty or "close" not in df.columns:
            return
        closes = df["close"]
        if isinstance(closes.index, pd.MultiIndex):
            level = "time" if "time" in closes.index.names else closes.index.nlevels - 1
            closes = closes.groupby(level=level).last()
        closes = closes.sort_index().dropna()
        if closes.empty:
            return
        market.closes = deque((float(x) for x in closes.values), maxlen=self._history_length)
        market.last_bar_time = pd.Timestamp(closes.index[-1])
        market.needs_history = False

    def _append_bar(self, market, bar):
        end_time = pd.Timestamp(bar.end_time)
        if market.last_bar_time is not None and end_time <= market.last_bar_time:
            return
        market.closes.append(float(bar.close))
        market.last_bar_time = end_time

    def _contract_holdings(self, market):
        return {
            h.symbol: int(h.quantity)
            for h in self.portfolio.values()
            if h.invested and h.symbol.security_type == SecurityType.FUTURE
            and h.symbol.id.symbol == market.root
        }

    def _has_open_orders(self, market):
        return any(o.symbol.id.symbol == market.root
                   and o.symbol.security_type == SecurityType.FUTURE
                   for o in self.transactions.get_open_orders())

    def _rebalance_key(self):
        if self._rebalance == "weekly":
            year, week, _ = self.time.isocalendar()
            return (year, week)
        return self.time.date()

    def _manage_market(self, market):
        if self.is_warming_up or self._has_open_orders(market):
            return

        mapped = market.future.mapped
        if mapped is None or not self.securities.contains_key(mapped) \
                or not self.securities[mapped].has_data:
            return

        holdings = self._contract_holdings(market)
        current = sum(holdings.values())
        in_mapped = holdings.get(mapped, 0)

        # ---- 6. Rolls: close positions in any contract that is no longer the front ----
        for contract, qty in holdings.items():
            if contract != mapped:
                self.liquidate(contract, tag=f"Roll {contract.value} -> {mapped.value}")

        # ---- 5. Rebalance (daily or weekly), with a no-trade buffer --------------------
        target = current
        key = self._rebalance_key()
        if key != market.last_rebalance_key:
            new_target = self._compute_target(market)
            if new_target is not None:
                market.last_rebalance_key = key
                market.ever_positioned |= new_target != 0
                if needs_rebalance(current, new_target, self._buffer):
                    target = new_target

        # One order in the front contract covers both the roll and the rebalance.
        order_qty = target - in_mapped
        if order_qty != 0:
            tag = "Rebalance" if target != current else "Roll"
            self.market_order(mapped, order_qty, tag=f"{tag} {market.root} -> {target}")

    def _compute_target(self, market):
        forecast = trend_forecast(market.closes, self._lookbacks)
        vol = daily_point_volatility(market.closes, self._vol_window)
        if forecast is None or vol is None:
            return None

        # Split the portfolio vol target equally across markets that have a signal.
        active = sum(1 for m in self._markets.values()
                     if len(m.closes) >= max(max(self._lookbacks), self._vol_window) + 1)
        if active == 0:
            return None
        equity = self.portfolio.total_portfolio_value
        annual_risk_budget = equity * self._target_vol * self._idm / active

        security = self.securities[market.symbol]
        point_value = security.symbol_properties.contract_multiplier \
            * security.quote_currency.conversion_rate
        return target_contracts(forecast, annual_risk_budget, vol, point_value, self._max_contracts)

    def on_end_of_algorithm(self):
        # Markets whose risk budget was always smaller than one contract never traded.
        # Raise `cash`, shrink the universe, or switch to micros if this list is long.
        skipped = [m.root for m in self._markets.values() if not m.ever_positioned]
        if skipped:
            self.log(f"Never held a position (budget < 1 contract or no data): {', '.join(skipped)}")

    def _plot_positions(self):
        today = self.time.date()
        if self.is_warming_up or today == self._last_plot_date:
            return
        self._last_plot_date = today
        longs = shorts = 0
        for market in self._markets.values():
            qty = sum(self._contract_holdings(market).values())
            longs += qty > 0
            shorts += qty < 0
        self.plot("Positions", "Long markets", longs)
        self.plot("Positions", "Short markets", shorts)
        gross = sum(abs(h.holdings_value) for h in self.portfolio.values() if h.invested)
        self.plot("Leverage", "Gross notional / equity",
                  gross / max(self.portfolio.total_portfolio_value, 1))
