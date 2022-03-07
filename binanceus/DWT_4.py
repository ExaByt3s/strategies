import numpy as np
import scipy.fft
from scipy.fft import rfft, irfft
import talib.abstract as ta
import freqtrade.vendor.qtpylib.indicators as qtpylib
import arrow

from freqtrade.strategy import (IStrategy, merge_informative_pair, stoploss_from_open,
                                IntParameter, DecimalParameter, CategoricalParameter)

from typing import Dict, List, Optional, Tuple, Union
from pandas import DataFrame, Series
from functools import reduce
from datetime import datetime, timedelta
from freqtrade.persistence import Trade

# Get rid of pandas warnings during backtesting
import pandas as pd

pd.options.mode.chained_assignment = None  # default='warn'

# Strategy specific imports, files must reside in same folder as strategy
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent))

import logging
import warnings

log = logging.getLogger(__name__)
# log.setLevel(logging.DEBUG)
warnings.simplefilter(action='ignore', category=pd.errors.PerformanceWarning)

import custom_indicators as cta

import pywt


"""
####################################################################################
DWT - use a Discreet Wavelet Transform to estimate future price movements

####################################################################################
"""


class DWT_4(IStrategy):
    # Do *not* hyperopt for the roi and stoploss spaces

    # ROI table:
    minimal_roi = {
        "0": 10
    }

    # Stoploss:
    stoploss = -0.10

    # Trailing stop:
    trailing_stop = False
    trailing_stop_positive = None
    trailing_stop_positive_offset = 0.0
    trailing_only_offset_is_reached = False

    timeframe = '5m'
    inf_timeframe = '15m'

    use_custom_stoploss = True

    # Recommended
    use_sell_signal = True
    sell_profit_only = False
    ignore_roi_if_buy_signal = True

    # Required
    startup_candle_count: int = 128
    process_only_new_candles = True

    custom_trade_info = {}

    ###################################

    # Strategy Specific Variable Storage

    ## Hyperopt Variables

    buy_dwt_diff = DecimalParameter(0.000, 0.050, decimals=3, default=0.01, space='buy', load=True, optimize=True)
    # buy_dwt_window = IntParameter(8, 164, default=64, space='buy', load=True, optimize=True)
    # buy_dwt_lookahead = IntParameter(0, 64, default=0, space='buy', load=True, optimize=True)

    dwt_window = 128
    dwt_lookahead = 0

    sell_dwt_diff = DecimalParameter(-0.050, 0.000, decimals=3, default=-0.01, space='sell', load=True, optimize=True)

    ###################################

    """
    Informative Pair Definitions
    """

    def informative_pairs(self):
        pairs = self.dp.current_whitelist()
        informative_pairs = [(pair, self.inf_timeframe) for pair in pairs]
        return informative_pairs
    
    ###################################

    """
    Indicator Definitions
    """

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:


        # Base pair informative timeframe indicators
        curr_pair = metadata['pair']
        informative = self.dp.get_pair_dataframe(pair=curr_pair, timeframe=self.inf_timeframe)

        # DWT

        # dataframe['dwt_model'] = dataframe['close'].rolling(window=self.buy_dwt_window.value).apply(self.model)
        # informative['dwt_predict'] = informative['close'].rolling(window=self.buy_dwt_window.value).apply(self.predict)
        informative['dwt_predict'] = informative['close'].rolling(window=self.dwt_window).apply(self.predict)


        # merge into normal timeframe
        dataframe = merge_informative_pair(dataframe, informative, self.timeframe, self.inf_timeframe, ffill=True)

        # calculate predictive indicators in shorter timeframe (not informative)

        # dataframe['dwt_predict'] = ta.LINEARREG(dataframe[f"dwt_predict_{self.inf_timeframe}"], timeperiod=12)
        dataframe['dwt_predict'] = dataframe[f"dwt_predict_{self.inf_timeframe}"]
        # dataframe['dwt_model'] = dataframe[f"dwt_model_{self.inf_timeframe}"]
        # dataframe['dwt_predict_diff'] = (dataframe['dwt_predict'] - dataframe['dwt_model']) / dataframe['dwt_model']
        dataframe['dwt_predict_diff'] = (dataframe['dwt_predict'] - dataframe['close']) / dataframe['close']

        return dataframe

    ###################################


    def madev(self, d, axis=None):
        """ Mean absolute deviation of a signal """
        return np.mean(np.absolute(d - np.mean(d, axis)), axis)

    def dwtModel(self, data):

        # the choice of wavelet makes a big difference
        # for an overview, check out: https://www.kaggle.com/theoviel/denoising-with-direct-wavelet-transform
        # wavelet = 'db1'
        # wavelet = 'bior1.1'
        wavelet = 'haar' # deals well with harsh transitions
        level = 1
        wmode = "smooth"
        length = len(data)

        # de-trend the data
        n = data.size
        t = np.arange(0, n)
        p = np.polyfit(t, data, 1)  # find linear trend in data
        x_notrend = data - p[0] * t  # detrended data

        coeff = pywt.wavedec(x_notrend, wavelet, mode=wmode)

        # remove higher harmonics
        sigma = (1 / 0.6745) * self.madev(coeff[-level])
        uthresh = sigma * np.sqrt(2 * np.log(length))
        coeff[1:] = (pywt.threshold(i, value=uthresh, mode='hard') for i in coeff[1:])

        # inverse transform
        restored_sig = pywt.waverec(coeff, wavelet, mode=wmode)

        # re-trend the data
        model = restored_sig + p[0] * t

        return model

    def model(self, a: np.ndarray) -> np.float:
        #must return scalar, so just calculate prediction and take last value
        model = self.dwtModel(np.array(a))
        length = len(model)
        return model[length-1]

    def predict(self, a: np.ndarray) -> np.float:
        #must return scalar, so just calculate prediction and take last value
        # npredict = self.buy_dwt_lookahead.value
        npredict = self.dwt_lookahead

        y = self.dwtModel(np.array(a))
        length = len(y)
        if npredict == 0:
            predict = y[length-1]
        else:
            x = np.arange(length)
            f = scipy.interpolate.UnivariateSpline(x, y, k=3)

            predict = f(length-1+npredict)

        return predict


    ###################################

    """
    Buy Signal
    """


    def populate_buy_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = []
        dataframe.loc[:, 'buy_tag'] = ''

        # conditions.append(dataframe['volume'] > 0)

        # FFT triggers
        dwt_cond = (
                qtpylib.crossed_above(dataframe['dwt_predict_diff'], self.buy_dwt_diff.value)
        )

        conditions.append(dwt_cond)

        # DWTs will spike on big gains, so try to constrain
        spike_cond = (
                dataframe['dwt_predict_diff'] < 2.0 * self.buy_dwt_diff.value
        )
        conditions.append(spike_cond)

        # set buy tags
        dataframe.loc[dwt_cond, 'buy_tag'] += 'dwt_buy_1 '

        if conditions:
            dataframe.loc[reduce(lambda x, y: x & y, conditions), 'buy'] = 1

        return dataframe


    ###################################

    """
    Sell Signal
    """


    def populate_sell_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = []
        dataframe.loc[:, 'exit_tag'] = ''

        # FFT triggers
        dwt_cond = (
                qtpylib.crossed_below(dataframe['dwt_predict_diff'], self.sell_dwt_diff.value)
        )

        conditions.append(dwt_cond)

        # DWTs will spike on big gains, so try to constrain
        spike_cond = (
                dataframe['dwt_predict_diff'] > 2.0 * self.sell_dwt_diff.value
        )
        conditions.append(spike_cond)

        # set sell tags
        dataframe.loc[dwt_cond, 'exit_tag'] += 'dwt_sell_1 '

        if conditions:
            dataframe.loc[reduce(lambda x, y: x & y, conditions), 'sell'] = 1

        return dataframe
