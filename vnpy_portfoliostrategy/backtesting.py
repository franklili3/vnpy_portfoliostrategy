from collections import defaultdict
from datetime import date, datetime, timedelta
from tkinter.messagebox import NO
from typing import Dict, List, Set, Tuple
from functools import lru_cache, partial
from copy import copy
import traceback

import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from pandas import DataFrame

from vnpy.trader.constant import Direction, Offset, Interval, Status
from vnpy.trader.database import get_database
from vnpy.trader.object import OrderData, TradeData, BarData
from vnpy.trader.utility import round_to, extract_vt_symbol
from vnpy.trader.optimize import (
    OptimizationSetting,
    check_optimization_setting,
    run_bf_optimization,
    run_ga_optimization
)

from .template import StrategyTemplate


INTERVAL_DELTA_MAP = {
    Interval.MINUTE: timedelta(minutes=1),
    Interval.HOUR: timedelta(hours=1),
    Interval.DAILY: timedelta(days=1),
}


class BacktestingEngine:
    """"""

    gateway_name = "BACKTESTING"

    def __init__(self):
        """"""
        self.vt_symbols: List[str] = []
        self.start: datetime = None
        self.end: datetime = None

        self.rates: Dict[str, float] = 0
        self.slippages: Dict[str, float] = 0
        self.sizes: Dict[str, float] = 1
        self.priceticks: Dict[str, float] = 0

        self.capital: float = 1_000_000
        self.risk_free: float = 0

        self.strategy_class: StrategyTemplate = None
        self.strategy: StrategyTemplate = None
        self.bars: Dict[str, BarData] = {}
        self.datetime: datetime = None

        self.interval: Interval = None
        self.days: int = 0
        self.history_data: Dict[Tuple, BarData] = {}
        self.dts: Set[datetime] = set()

        self.limit_order_count = 0
        self.limit_orders = {}
        self.active_limit_orders = {}

        self.trade_count = 0
        self.trades = {}

        self.logs = []

        self.daily_results = {}
        self.daily_df = None
        self.inverse_option = False
        self.btc_vt_symbol: str = ''

    def clear_data(self) -> None:
        """
        Clear all data of last backtesting.
        """
        self.strategy = None
        self.bars = {}
        self.datetime = None

        self.limit_order_count = 0
        self.limit_orders.clear()
        self.active_limit_orders.clear()

        self.trade_count = 0
        self.trades.clear()

        self.logs.clear()
        self.daily_results.clear()
        self.daily_df = None

    def set_parameters(
        self,
        vt_symbols: List[str],
        interval: Interval,
        start: datetime,
        rates: Dict[str, float],
        slippages: Dict[str, float],
        sizes: Dict[str, float],
        priceticks: Dict[str, float],
        capital: int = 0,
        end: datetime = None,
        risk_free: float = 0,
        inverse_option : bool = False,
        btc_vt_symbol: str = ''
    ) -> None:
        """"""
        self.vt_symbols = vt_symbols
        self.interval = interval

        self.rates = rates
        self.slippages = slippages
        self.sizes = sizes
        self.priceticks = priceticks

        self.start = start
        self.end = end
        self.capital = capital
        self.risk_free = risk_free
        self.inverse_option = inverse_option
        self.btc_vt_symbol = btc_vt_symbol

    def add_strategy(self, strategy_class: type, setting: dict) -> None:
        """"""
        self.strategy_class = strategy_class
        self.strategy = strategy_class(
            self, strategy_class.__name__, copy(self.vt_symbols), setting
        )

    def load_data(self, isdaily = False) -> None:
        """"""
        self.output("开始加载历史数据")

        if not self.end:
            self.end = datetime.now()

        if self.start >= self.end:
            self.output("起始日期必须小于结束日期")
            return

        # Clear previously loaded history data
        self.history_data.clear()
        self.dts.clear()

        # Load 1095 days of data each time and allow for progress update
        progress_delta = timedelta(days=1095)
        total_delta = self.end - self.start
        interval_delta = INTERVAL_DELTA_MAP[self.interval]

        for vt_symbol in self.vt_symbols:
            start = self.start
            end = self.start + progress_delta
            progress = 0

            data_count = 0
            while start < self.end:
                end = min(end, self.end)  # Make sure end time stays within set range

                data = load_bar_data(
                    vt_symbol,
                    self.interval,
                    start,
                    end
                )

                for bar in data:

                    
                    if isdaily == False:
                        self.dts.add(bar.datetime)
                        self.history_data[(bar.datetime, vt_symbol)] = bar
                    else:
                        self.dts.add(bar.datetime.replace(hour = 0, minute = 0, second = 0))
                        self.history_data[(bar.datetime.replace(hour = 0, minute = 0, second = 0), vt_symbol)] = bar
                    data_count += 1

                progress += progress_delta / total_delta
                progress = min(progress, 1)
                progress_bar = "#" * int(progress * 10)
                self.output(f"{vt_symbol}加载进度：{progress_bar} [{progress:.0%}]")

                start = end + interval_delta
                end += (progress_delta + interval_delta)

            self.output(f"{vt_symbol}历史数据加载完成，数据量：{data_count}")

        self.output("所有历史数据加载完成")

    def run_backtesting(self) -> None:
        """"""
        self.strategy.on_init()

        dts = list(self.dts)

        # Generate sorted datetime list
        dts.sort()



        # Use the first [days] of history data for initializing strategy
        day_count = 0
        ix = 0
        for ix, dt in enumerate(dts):
            if self.datetime and dt.day != self.datetime.day:
                day_count += 1
                if day_count >= self.days:
                    break

            try:
                self.new_bars(dt)
            except Exception:
                self.output("触发异常，回测终止")
                self.output(traceback.format_exc())
                return

        self.strategy.inited = True
        self.output("策略初始化完成")

        self.strategy.on_start()
        self.strategy.trading = True
        self.output("开始回放历史数据")

        # Use the rest of history data for running backtesting
        for dt in dts[ix:]:
            try:
                self.new_bars(dt)
            except Exception:
                self.output("触发异常，回测终止")
                self.output(traceback.format_exc())
                return

        self.output("历史数据回放结束")

    def calculate_result(self) -> None:
        """"""
        self.output("开始计算逐日盯市盈亏")

        if not self.trades:
            self.output("成交记录为空，无法计算")
            return

        # Add trade data into daily reuslt.
        for trade in self.trades.values():
            d = trade.datetime.date()
            daily_result = self.daily_results[d]
            daily_result.add_trade(trade)

        # Calculate daily result by iteration.
        pre_closes = {}
        start_poses = {}

        for daily_result in self.daily_results.values():
            daily_result.calculate_portfolio_pnl(
                pre_closes,
                start_poses,
                self.sizes,
                self.rates,
                self.slippages
            )

            pre_closes = daily_result.close_prices
            start_poses = daily_result.end_poses

        # Generate dataframe
        results = defaultdict(list)

        for daily_result in self.daily_results.values():
            fields = [
                "date", "trade_count", "turnover",
                "commission", "slippage", "trading_pnl",
                "holding_pnl", "total_pnl", "net_pnl"
            ]
            for key in fields:
                value = getattr(daily_result, key)
                results[key].append(value)

        self.daily_df = DataFrame.from_dict(results).set_index("date")

        self.output("逐日盯市盈亏计算完成")
        return self.daily_df

    def calculate_statistics(self, df: DataFrame = None, output=True) -> None:
        """"""
        self.output("开始计算策略统计指标")

        # Check DataFrame input exterior
        if df is None:
            df = self.daily_df

        # Check for init DataFrame
        if df is None:
            # Set all statistics to 0 if no trade.
            start_date = ""
            end_date = ""
            total_days = 0
            profit_days = 0
            loss_days = 0
            end_balance = 0
            max_drawdown = 0
            max_ddpercent = 0
            max_drawdown_duration = 0
            total_net_pnl = 0
            daily_net_pnl = 0
            total_commission = 0
            daily_commission = 0
            total_slippage = 0
            daily_slippage = 0
            total_turnover = 0
            daily_turnover = 0
            total_trade_count = 0
            daily_trade_count = 0
            total_return = 0
            annual_return = 0
            daily_return = 0
            return_std = 0
            sharpe_ratio = 0
            return_drawdown_ratio = 0
        else:
            # Calculate balance related time series data
            df["balance"] = df["net_pnl"].cumsum() + self.capital
            df["return"] = np.log(df["balance"] / df["balance"].shift(1)).fillna(0)
            df["highlevel"] = (
                df["balance"].rolling(
                    min_periods=1, window=len(df), center=False).max()
            )
            df["drawdown"] = df["balance"] - df["highlevel"]
            df["ddpercent"] = df["drawdown"] / df["highlevel"] * 100

            # Calculate statistics value
            start_date = df.index[0]
            end_date = df.index[-1]

            total_days = len(df)
            profit_days = len(df[df["net_pnl"] > 0])
            loss_days = len(df[df["net_pnl"] < 0])

            end_balance = df["balance"].iloc[-1]
            max_drawdown = df["drawdown"].min()
            max_ddpercent = df["ddpercent"].min()
            max_drawdown_end = df["drawdown"].idxmin()

            if isinstance(max_drawdown_end, date):
                max_drawdown_start = df["balance"][:max_drawdown_end].idxmax()
                max_drawdown_duration = (max_drawdown_end - max_drawdown_start).days
            else:
                max_drawdown_duration = 0

            total_net_pnl = df["net_pnl"].sum()
            daily_net_pnl = total_net_pnl / total_days

            total_commission = df["commission"].sum()
            daily_commission = total_commission / total_days

            total_slippage = df["slippage"].sum()
            daily_slippage = total_slippage / total_days

            total_turnover = df["turnover"].sum()
            daily_turnover = total_turnover / total_days

            total_trade_count = df["trade_count"].sum()
            daily_trade_count = total_trade_count / total_days

            total_return = (end_balance / self.capital - 1) * 100
            annual_return = total_return / total_days * 240
            daily_return = df["return"].mean() * 100
            return_std = df["return"].std() * 100

            if return_std:
                daily_risk_free = self.risk_free / np.sqrt(240)
                sharpe_ratio = (daily_return - daily_risk_free) / return_std * np.sqrt(240)
            else:
                sharpe_ratio = 0

            return_drawdown_ratio = -total_net_pnl / max_drawdown

        # Output
        if output:
            self.output("-" * 30)
            self.output(f"首个交易日：\t{start_date}")
            self.output(f"最后交易日：\t{end_date}")

            self.output(f"总交易日：\t{total_days}")
            self.output(f"盈利交易日：\t{profit_days}")
            self.output(f"亏损交易日：\t{loss_days}")

            self.output(f"起始资金：\t{self.capital:,.2f}")
            self.output(f"结束资金：\t{end_balance:,.2f}")

            self.output(f"总收益率：\t{total_return:,.2f}%")
            self.output(f"年化收益：\t{annual_return:,.2f}%")
            self.output(f"最大回撤: \t{max_drawdown:,.2f}")
            self.output(f"百分比最大回撤: {max_ddpercent:,.2f}%")
            self.output(f"最长回撤天数: \t{max_drawdown_duration}")

            self.output(f"总盈亏：\t{total_net_pnl:,.2f}")
            self.output(f"总手续费：\t{total_commission:,.2f}")
            self.output(f"总滑点：\t{total_slippage:,.2f}")
            self.output(f"总成交金额：\t{total_turnover:,.2f}")
            self.output(f"总成交笔数：\t{total_trade_count}")

            self.output(f"日均盈亏：\t{daily_net_pnl:,.2f}")
            self.output(f"日均手续费：\t{daily_commission:,.2f}")
            self.output(f"日均滑点：\t{daily_slippage:,.2f}")
            self.output(f"日均成交金额：\t{daily_turnover:,.2f}")
            self.output(f"日均成交笔数：\t{daily_trade_count}")

            self.output(f"日均收益率：\t{daily_return:,.2f}%")
            self.output(f"收益标准差：\t{return_std:,.2f}%")
            self.output(f"Sharpe Ratio：\t{sharpe_ratio:,.2f}")
            self.output(f"收益回撤比：\t{return_drawdown_ratio:,.2f}")

        statistics = {
            "start_date": start_date,
            "end_date": end_date,
            "total_days": total_days,
            "profit_days": profit_days,
            "loss_days": loss_days,
            "capital": self.capital,
            "end_balance": end_balance,
            "max_drawdown": max_drawdown,
            "max_ddpercent": max_ddpercent,
            "max_drawdown_duration": max_drawdown_duration,
            "total_net_pnl": total_net_pnl,
            "daily_net_pnl": daily_net_pnl,
            "total_commission": total_commission,
            "daily_commission": daily_commission,
            "total_slippage": total_slippage,
            "daily_slippage": daily_slippage,
            "total_turnover": total_turnover,
            "daily_turnover": daily_turnover,
            "total_trade_count": total_trade_count,
            "daily_trade_count": daily_trade_count,
            "total_return": total_return,
            "annual_return": annual_return,
            "daily_return": daily_return,
            "return_std": return_std,
            "sharpe_ratio": sharpe_ratio,
            "return_drawdown_ratio": return_drawdown_ratio,
        }

        # Filter potential error infinite value
        for key, value in statistics.items():
            if value in (np.inf, -np.inf):
                value = 0
            statistics[key] = np.nan_to_num(value)

        self.output("策略统计指标计算完成")
        return statistics

    def show_chart(self, df: DataFrame = None) -> None:
        """"""
        # Check DataFrame input exterior
        if df is None:
            df = self.daily_df

        # Check for init DataFrame
        if df is None:
            return

        fig = make_subplots(
            rows=4,
            cols=1,
            subplot_titles=["Balance", "Drawdown", "Daily Pnl", "Pnl Distribution"],
            vertical_spacing=0.06
        )

        balance_line = go.Scatter(
            x=df.index,
            y=df["balance"],
            mode="lines",
            name="Balance"
        )
        drawdown_scatter = go.Scatter(
            x=df.index,
            y=df["drawdown"],
            fillcolor="red",
            fill='tozeroy',
            mode="lines",
            name="Drawdown"
        )
        pnl_bar = go.Bar(y=df["net_pnl"], name="Daily Pnl")
        pnl_histogram = go.Histogram(x=df["net_pnl"], nbinsx=100, name="Days")

        fig.add_trace(balance_line, row=1, col=1)
        fig.add_trace(drawdown_scatter, row=2, col=1)
        fig.add_trace(pnl_bar, row=3, col=1)
        fig.add_trace(pnl_histogram, row=4, col=1)

        fig.update_layout(height=1000, width=1000)
        fig.show()

    def run_bf_optimization(self, optimization_setting: OptimizationSetting, output=True):
        """"""
        if not check_optimization_setting(optimization_setting):
            return

        evaluate_func: callable = wrap_evaluate(self, optimization_setting.target_name)
        results = run_bf_optimization(
            evaluate_func,
            optimization_setting,
            get_target_value,
            output=self.output,
        )

        if output:
            for result in results:
                msg: str = f"参数：{result[0]}, 目标：{result[1]}"
                self.output(msg)

        return results

    run_optimization = run_bf_optimization

    def run_ga_optimization(self, optimization_setting: OptimizationSetting, output=True):
        """"""
        if not check_optimization_setting(optimization_setting):
            return

        evaluate_func: callable = wrap_evaluate(self, optimization_setting.target_name)
        results = run_ga_optimization(
            evaluate_func,
            optimization_setting,
            get_target_value,
            output=self.output
        )

        if output:
            for result in results:
                msg: str = f"参数：{result[0]}, 目标：{result[1]}"
                self.output(msg)

        return results

    def update_daily_close(self, bars: Dict[str, BarData], dt: datetime) -> None:
        """"""
        d = dt.date()

        close_prices = {}
        for bar in bars.values():
            close_prices[bar.vt_symbol] = bar.close_price

        daily_result = self.daily_results.get(d, None)

        if daily_result:
            daily_result.update_close_prices(close_prices)
        else:
            self.daily_results[d] = PortfolioDailyResult(d, close_prices, inverse_option=self.inverse_option, btc_vt_symbol=self.btc_vt_symbol)

    def new_bars(self, dt) -> None:
        """"""
        self.datetime = dt

        bars: Dict[str, BarData] = {}
        for vt_symbol in self.vt_symbols:
            bar = self.history_data.get((dt, vt_symbol), None)

            # If bar data of vt_symbol at dt exists
            if bar:
                # Update bar data for crossing order
                self.bars[vt_symbol] = bar

                # Put bar into dict for strategy.on_bars update
                bars[vt_symbol] = bar
            # Otherwise, use previous close to backfill
            elif vt_symbol in self.bars:
                old_bar = self.bars[vt_symbol]

                bar = BarData(
                    symbol=old_bar.symbol,
                    exchange=old_bar.exchange,
                    datetime=dt,
                    open_price=old_bar.close_price,
                    high_price=old_bar.close_price,
                    low_price=old_bar.close_price,
                    close_price=old_bar.close_price,
                    gateway_name=old_bar.gateway_name
                )
                self.bars[vt_symbol] = bar

        self.cross_limit_order()
        self.strategy.on_bars(bars)

        if self.strategy.inited:
            self.update_daily_close(self.bars, dt)

    def cross_limit_order(self) -> None:
        """
        Cross limit order with last bar/tick data.
        """
        for order in list(self.active_limit_orders.values()):
            bar = self.bars[order.vt_symbol]

            long_cross_price = bar.low_price
            short_cross_price = bar.high_price
            long_best_price = bar.open_price
            short_best_price = bar.open_price

            # Push order update with status "not traded" (pending).
            if order.status == Status.SUBMITTING:
                order.status = Status.NOTTRADED
                self.strategy.update_order(order)

            # Check whether limit orders can be filled.
            long_cross = (
                order.direction == Direction.LONG
                and order.price >= long_cross_price
                and long_cross_price > 0
            )

            short_cross = (
                order.direction == Direction.SHORT
                and order.price <= short_cross_price
                and short_cross_price > 0
            )

            if not long_cross and not short_cross:
                continue

            # Push order update with status "all traded" (filled).
            order.traded = order.volume
            order.status = Status.ALLTRADED
            self.strategy.update_order(order)

            self.active_limit_orders.pop(order.vt_orderid)

            # Push trade update
            self.trade_count += 1

            if long_cross:
                trade_price = min(order.price, long_best_price)
            else:
                trade_price = max(order.price, short_best_price)

            trade = TradeData(
                symbol=order.symbol,
                exchange=order.exchange,
                orderid=order.orderid,
                tradeid=str(self.trade_count),
                direction=order.direction,
                offset=order.offset,
                price=trade_price,
                volume=order.volume,
                datetime=self.datetime,
                gateway_name=self.gateway_name,
            )

            self.strategy.update_trade(trade)
            self.trades[trade.vt_tradeid] = trade

    def load_bars(
        self,
        strategy: StrategyTemplate,
        days: int,
        interval: Interval
    ) -> None:
        """"""
        self.days = days

    def send_order(
        self,
        strategy: StrategyTemplate,
        vt_symbol: str,
        direction: Direction,
        offset: Offset,
        price: float,
        volume: float,
        lock: bool,
        net: bool
    ) -> List[str]:
        """"""
        price = round_to(price, self.priceticks[vt_symbol])
        symbol, exchange = extract_vt_symbol(vt_symbol)

        self.limit_order_count += 1

        order = OrderData(
            symbol=symbol,
            exchange=exchange,
            orderid=str(self.limit_order_count),
            direction=direction,
            offset=offset,
            price=price,
            volume=volume,
            status=Status.SUBMITTING,
            datetime=self.datetime,
            gateway_name=self.gateway_name,
        )

        self.active_limit_orders[order.vt_orderid] = order
        self.limit_orders[order.vt_orderid] = order

        return [order.vt_orderid]

    def cancel_order(self, strategy: StrategyTemplate, vt_orderid: str) -> None:
        """
        Cancel order by vt_orderid.
        """
        if vt_orderid not in self.active_limit_orders:
            return
        order = self.active_limit_orders.pop(vt_orderid)

        order.status = Status.CANCELLED
        self.strategy.update_order(order)

    def write_log(self, msg: str, strategy: StrategyTemplate = None) -> None:
        """
        Write log message.
        """
        msg = f"{self.datetime}\t{msg}"
        self.logs.append(msg)

    def send_email(self, msg: str, strategy: StrategyTemplate = None) -> None:
        """
        Send email to default receiver.
        """
        pass

    def sync_strategy_data(self, strategy: StrategyTemplate) -> None:
        """
        Sync strategy data into json file.
        """
        pass

    def get_pricetick(self, strategy: StrategyTemplate, vt_symbol) -> float:
        """
        Return contract pricetick data.
        """
        return self.priceticks[vt_symbol]

    def put_strategy_event(self, strategy: StrategyTemplate) -> None:
        """
        Put an event to update strategy status.
        """
        pass

    def output(self, msg) -> None:
        """
        Output message of backtesting engine.
        """
        print(f"{datetime.now()}\t{msg}")

    def get_all_trades(self) -> List[TradeData]:
        """
        Return all trade data of current backtesting result.
        """
        return list(self.trades.values())

    def get_all_orders(self) -> List[OrderData]:
        """
        Return all limit order data of current backtesting result.
        """
        return list(self.limit_orders.values())

    def get_all_daily_results(self) -> List["PortfolioDailyResult"]:
        """
        Return all daily result data.
        """
        return list(self.daily_results.values())


class ContractDailyResult:
    """"""

    def __init__(self, result_date: date, close_price: float, **kw):
        """"""
        self.date: date = result_date
        self.close_price: float = close_price
        self.kw: dict = kw
        self.pre_close: float = 0
        self.pre_btc_close: float = 0

        self.trades: List[TradeData] = []
        self.trade_count: int = 0

        self.start_pos: float = 0
        self.end_pos: float = 0

        self.turnover: float = 0
        self.commission: float = 0
        self.slippage: float = 0

        self.trading_pnl: float = 0
        self.holding_pnl: float = 0
        self.total_pnl: float = 0
        self.net_pnl: float = 0

    def add_trade(self, trade: TradeData) -> None:
        """"""
        self.trades.append(trade)

    def calculate_contract_pnl(
        self,
        pre_close: float,
        pre_btc_close: float,
        start_pos: float,
        size: int,
        rate: float,
        slippage: float,
        inverse_option: bool
    ) -> None:
        """"""
        # If no pre_close provided on the first day,
        # use value 1 to avoid zero division error
        if pre_close:
            self.pre_close = pre_close
            self.pre_btc_close = pre_btc_close
        else:
            self.pre_close = 1
            self.pre_btc_close = 1

        # Holding pnl is the pnl from holding position at day start
        self.start_pos = start_pos
        self.end_pos = start_pos

        if not inverse_option:     # For normal contract
            self.holding_pnl = self.start_pos * (self.close_price - self.pre_close) * size
        else:               # For crypto currency inverse contract
            btc_close_price = self.kw['btc_close_price']
            self.holding_pnl = self.start_pos * (self.close_price * btc_close_price - self.pre_close * pre_btc_close) * size

        # Trading pnl is the pnl from new trade during the day
        self.trade_count = len(self.trades)

        for trade in self.trades:
            if trade.direction == Direction.LONG:
                pos_change = trade.volume
            else:
                pos_change = -trade.volume

            self.end_pos += pos_change

            # For normal contract
            if not inverse_option:
                self.trading_pnl += pos_change * (self.close_price - trade.price) * size
                turnover = trade.volume * size * trade.price
                self.slippage += trade.volume * size * slippage
                self.turnover += turnover
                self.commission += turnover * rate

            # For crypto currency inverse contract
            else:
                self.trading_pnl += pos_change * size * (self.close_price * btc_close_price - trade.price * pre_btc_close)
                turnover = trade.volume * size * trade.price * btc_close_price
                self.slippage += trade.volume * size * slippage * btc_close_price
                self.turnover += turnover
                self.commission += turnover * rate 


        # Net pnl takes account of commission and slippage cost
        self.total_pnl = self.trading_pnl + self.holding_pnl
        self.net_pnl = self.total_pnl - self.commission - self.slippage

    def update_close_price(self, close_price: float) -> None:
        """"""
        self.close_price = close_price


class PortfolioDailyResult:
    """"""

    def __init__(self, result_date: date, close_prices: Dict[str, float], **kw):
        """"""
        self.date: date = result_date
        self.close_prices: Dict[str, float] = close_prices
        self.pre_closes: Dict[str, float] = {}
        self.start_poses: Dict[str, float] = {}
        self.end_poses: Dict[str, float] = {}
        self.contract_results: Dict[str, ContractDailyResult] = {}
        self.inverse_option = False
        self.btc_vt_symbol = None
        for k, v in kw.items():
            if k == 'inverse_option':
                self.inverse_option = v
            elif k == 'btc_vt_symbol':
                self.btc_vt_symbol = v
        #print('self.btc_vt_symbol: ', self.btc_vt_symbol)
        
        for vt_symbol, close_price in close_prices.items():
            if not self.inverse_option: # for normal contract
                self.contract_results[vt_symbol] = ContractDailyResult(result_date, close_price)
            else:  # for crypto inverse contract
                btc_close_price = close_prices[self.btc_vt_symbol]
                self.contract_results[vt_symbol] = ContractDailyResult(result_date, close_price, btc_close_price=btc_close_price)

        self.trade_count: int = 0
        self.turnover: float = 0
        self.commission: float = 0
        self.slippage: float = 0
        self.trading_pnl: float = 0
        self.holding_pnl: float = 0
        self.total_pnl: float = 0
        self.net_pnl: float = 0

    def add_trade(self, trade: TradeData) -> None:
        """"""
        contract_result = self.contract_results[trade.vt_symbol]
        contract_result.add_trade(trade)

    def calculate_portfolio_pnl(
        self,
        pre_closes: Dict[str, float],
        start_poses: Dict[str, float],
        sizes: Dict[str, float],
        rates: Dict[str, float],
        slippages: Dict[str, float],
    ) -> None:
        """"""
        self.pre_closes = pre_closes

        for vt_symbol, contract_result in self.contract_results.items():
            contract_result.calculate_contract_pnl(
                pre_closes.get(vt_symbol, 0),
                pre_closes.get(self.btc_vt_symbol, 0),
                start_poses.get(vt_symbol, 0),
                sizes[vt_symbol],
                rates[vt_symbol],
                slippages[vt_symbol],
                self.inverse_option
            )

            self.trade_count += contract_result.trade_count
            self.turnover += contract_result.turnover
            self.commission += contract_result.commission
            self.slippage += contract_result.slippage
            self.trading_pnl += contract_result.trading_pnl
            self.holding_pnl += contract_result.holding_pnl
            self.total_pnl += contract_result.total_pnl
            self.net_pnl += contract_result.net_pnl

            self.end_poses[vt_symbol] = contract_result.end_pos

    def update_close_prices(self, close_prices: Dict[str, float]) -> None:
        """"""
        self.close_prices = close_prices

        for vt_symbol, close_price in close_prices.items():
            contract_result = self.contract_results.get(vt_symbol, None)
            if contract_result:
                contract_result.update_close_price(close_price)


@lru_cache(maxsize=999)
def load_bar_data(
    vt_symbol: str,
    interval: Interval,
    start: datetime,
    end: datetime
):
    """"""
    symbol, exchange = extract_vt_symbol(vt_symbol)

    database = get_database()

    return database.load_bar_data(
        symbol, exchange, interval, start, end
    )


def evaluate(
    target_name: str,
    strategy_class: StrategyTemplate,
    vt_symbols: List[str],
    interval: Interval,
    start: datetime,
    rates: Dict[str, float],
    slippages: Dict[str, float],
    sizes: Dict[str, float],
    priceticks: Dict[str, float],
    capital: int,
    end: datetime,
    setting: dict
):
    """
    Function for running in multiprocessing.pool
    """
    engine = BacktestingEngine()

    engine.set_parameters(
        vt_symbols=vt_symbols,
        interval=interval,
        start=start,
        rates=rates,
        slippages=slippages,
        sizes=sizes,
        priceticks=priceticks,
        capital=capital,
        end=end,
    )

    engine.add_strategy(strategy_class, setting)
    engine.load_data()
    engine.run_backtesting()
    engine.calculate_result()
    statistics = engine.calculate_statistics(output=False)

    target_value = statistics[target_name]
    return (str(setting), target_value, statistics)


def wrap_evaluate(engine: BacktestingEngine, target_name: str) -> callable:
    """
    Wrap evaluate function with given setting from backtesting engine.
    """
    func: callable = partial(
        evaluate,
        target_name,
        engine.strategy_class,
        engine.vt_symbols,
        engine.interval,
        engine.start,
        engine.rates,
        engine.slippages,
        engine.sizes,
        engine.priceticks,
        engine.capital,
        engine.end
    )
    return func


def get_target_value(result: list) -> float:
    """
    Get target value for sorting optimization results.
    """
    return result[1]
