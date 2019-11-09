from utils import generate_hedge_ratio, dot, is_stationary
import numpy as np
import sys
import logging
from statistics import mean, stdev
from itertools import combinations

from abc import ABCMeta, abstractmethod

from event import SignalEvent

logger = logging.getLogger("backtester")


class Strategy(object):
    """
    Strategy is an abstract base class providing an interface for
    all subsequent (inherited) strategy handling objects.

    The goal of a (derived) Strategy object is to generate Signal
    objects for a particular symbols based on the inputs of Bars
    (OLHCVI) generated by a DataHandler object.

    This is designed to work both with historic and live data as
    the Strategy object is agnostic to the data source,
    since it obtains the bar tuples from a queue object.
    """

    __metaclass__ = ABCMeta

    @abstractmethod
    def calculate_signals(self):
        """
        Provides the mechanisms to calculate the list of signals.
        """
        raise NotImplementedError("Should implement calculate_signals()")


class BuyAndHoldStrategy(Strategy):
    """
    This is an extremely simple strategy that goes LONG all of the
    symbols as soon as a bar is received. It will never exit a position

    It is primarily used as a testing mechanism for the Strategy class
    as well as a benchmark upon which to compare other strategies.
    """
    def __init__(self, bars, events):
        """
        Initialises the buy and hold strategy.

        Parameters:
        bars - The DataHandler object that provides bar information
        events - The Event Queue object.
        """
        self.bars = bars
        self.symbol_list = self.bars.symbol_list
        self.events = events

        # Once buy & hold signal is given, these are set to True
        self.bought = self._calculate_initial_bought()

    def _calculate_initial_bought(self):
        """
        Adds keys to the bought dictionary for all symbols
        and sets them to False.
        """
        bought = {}
        for s in self.symbol_list:
            bought[s] = False
        return bought

    def calculate_signals(self, event):
        """
        For "Buy and Hold" we generate a single signal per symbol
        and then no additional signals. This means we are
        constantly long the market from the date of strategy
        initialisation.

        Parameters
        event = A MarketEvent object.
        """
        if event.type == 'MARKET':
            for s in self.symbol_list:
                bar = self.bars.get_latest_bars(s, N=1)[0]
                if bar is not None and bar != {}:
                    if not self.bought[s]:
                        # (Symbol, Datetime, Type = LONG, SHORT or EXIT)
                        signal = SignalEvent(bar['Symbol'], bar['Date'],
                                             'LONG')
                        self.events.put(signal)
                        self.bought[s] = True


class BollingerBandJohansenStrategy(Strategy):
    """
    Uses a Johansen test to create a mean reverting portfolio,
    and uses bollinger bands strategy using resuling hedge ratios.
    """
    def __init__(self, bars, events, enter, exit, start_date):
        """
        Initialises the bollinger band johansen strategy.

        Parameters:
        bars - The DataHandler object that provides bar information
        events - The Event Queue object.
        """
        self.bars = bars
        self.events = events
        self.enter = enter
        self.exit = exit
        self.current_date = start_date

        # self.hedge_ratio = generate_hedge_ratio(
        #     self.bars.generate_train_set('Close'))
        self.hedge_ratio = self._find_stationary_portfolio()
        self.long = False
        self.short = False

    def _find_stationary_portfolio(self):
        """
        TODO: scrape all s&p tickers and
        create DataHandler object to generate
        hedge ratio for. If hedge ratio is stationary,
        return the DataHandler object.
        """
        tickers = self.bars.sort_oldest()
        for port in combinations(tickers, 12):
            self.bars.update_symbol_list(port, self.current_date)
            prices = self.bars.generate_train_set('Close')
            try:
                results = generate_hedge_ratio(prices)
            except np.linalg.LinAlgError as error:
                logger.info("Error: {error}".format(error=error))
            else:
                hedge_ratio = results.evec[:, 0]
                self.portfolio_prices = dot(prices, hedge_ratio)
                if results.lr1[0] >= results.cvt[0][-1] and\
                    results.lr2[0] >= results.cvm[0][-1] and\
                        is_stationary(self.portfolio_prices):
                    logger.info("Stationary portfolio found. Tickers: {port}"
                                .format(port=port))
                    self.portfolio_prices = self.portfolio_prices.tolist()
                    return hedge_ratio
            logger.info("{port} not stationary".format(port=port))
        logger.error("No stationary portfolios found!")
        sys.exit(0)

    def _current_portfolio_price(self, price_type):
        prices = []
        for i, s in enumerate(self.bars.symbol_list):
            bar = self.bars.get_latest_bars(s, N=1)[0]
            self.current_date = bar['Date']
            adj_ratio = bar['Split']
            adj_ratio *= (bar['Close'] + bar['Dividend'])\
                / bar['Close']
            self.hedge_ratio[i] *= adj_ratio
            prices.append(bar[price_type])
        return dot(prices, self.hedge_ratio)

    def _order_portfolio(self, direction):
        for i, s in enumerate(self.bars.symbol_list):
            bar = self.bars.get_latest_bars(s, N=1)[0]
            signal = SignalEvent(bar['Symbol'], bar['Date'], direction,
                                 self.hedge_ratio[i])
            logger.info("Signal Event created for {sym} on {date} to "
                        "{direction} with strength {strength}".format(
                            sym=bar['Symbol'], date=bar['Date'],
                            direction=direction, strength=self.hedge_ratio[i]))
            self.events.put(signal)

    def calculate_signals(self, event):
        """
        Calculates how many standard deviations away
        the current bar is from rolling average of the portfolio

        Parameters:
        :param event: Event object
        :type event: Event
        """
        if event.type == "MARKET":
            price = self._current_portfolio_price('Close')
            self.portfolio_prices.append(price)
            if is_stationary(self.portfolio_prices):
                rolling_avg = mean(self.portfolio_prices)
                rolling_std = stdev(self.portfolio_prices)
                zscore = (price - rolling_avg) / rolling_std
                logger.debug(zscore)

                if self.long and zscore >= self.exit:
                    self._order_portfolio(direction='EXIT')
                    self.long = False
                elif self.short and zscore <= -self.exit:
                    self._order_portfolio(direction='EXIT')
                    self.short = False
                elif not self.short and zscore >= self.enter:
                    self._order_portfolio(direction='SHORT')
                    self.short = True
                elif not self.long and zscore <= -self.enter:
                    self._order_portfolio(direction='LONG')
                    self.long = True
            else:
                self._order_portfolio(direction='EXIT')
                self.hedge_ratio = self._find_stationary_portfolio()
