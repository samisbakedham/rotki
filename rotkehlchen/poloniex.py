#!/usr/bin/env python
import csv
import datetime
import hashlib
import hmac
import logging
import os
import time
import traceback
from typing import Dict, List, Optional, Tuple, Union, cast
from urllib.parse import urlencode

from rotkehlchen import typing
from rotkehlchen.errors import PoloniexError, RemoteError
from rotkehlchen.exchange import Exchange
from rotkehlchen.fval import FVal
from rotkehlchen.inquirer import Inquirer
from rotkehlchen.logging import RotkehlchenLogsAdapter
from rotkehlchen.order_formatting import AssetMovement, Trade
from rotkehlchen.utils import (
    cache_response_timewise,
    createTimeStamp,
    get_pair_position,
    retry_calls,
    rlk_jsonloads,
)

logger = logging.getLogger(__name__)
log = RotkehlchenLogsAdapter(logger)


def tsToDate(s):
    return datetime.datetime.fromtimestamp(s).strftime('%Y-%m-%d %H:%M:%S')


def trade_from_poloniex(poloniex_trade, pair):
    """Turn a poloniex trade returned from poloniex trade history to our common trade
    history format"""

    trade_type = poloniex_trade['type']
    amount = FVal(poloniex_trade['amount'])
    rate = FVal(poloniex_trade['rate'])
    perc_fee = FVal(poloniex_trade['fee'])
    base_currency = get_pair_position(pair, 'first')
    quote_currency = get_pair_position(pair, 'second')
    timestamp = createTimeStamp(poloniex_trade['date'], formatstr="%Y-%m-%d %H:%M:%S"),
    if trade_type == 'buy':
        cost = rate * amount
        cost_currency = base_currency
        fee = amount * perc_fee
        fee_currency = quote_currency
    elif trade_type == 'sell':
        cost = amount * rate
        cost_currency = base_currency
        fee = cost * perc_fee
        fee_currency = base_currency
    else:
        raise ValueError('Got unexpected trade type "{}" for poloniex trade'.format(trade_type))

    if poloniex_trade['category'] == 'settlement':
        trade_type = "settlement_%s" % trade_type

    log.debug(
        'Processing poloniex Trade',
        sensitive_log=True,
        timestamp=timestamp,
        order_type=trade_type,
        pair=pair,
        base_currency=base_currency,
        quote_currency=quote_currency,
        amount=amount,
        cost=cost,
        fee=fee,
        rate=rate,
    )

    return Trade(
        timestamp=timestamp,
        pair=pair,
        type=trade_type,
        rate=rate,
        cost=cost,
        cost_currency=cost_currency,
        fee=fee,
        fee_currency=fee_currency,
        amount=amount,
        location='poloniex'
    )


class Poloniex(Exchange):

    def __init__(
            self,
            api_key: typing.ApiKey,
            secret: typing.ApiSecret,
            inquirer: Inquirer,
            data_dir: typing.FilePath,
    ):
        super(Poloniex, self).__init__('poloniex', api_key, secret, data_dir)

        self.uri = 'https://poloniex.com/'
        self.public_uri = self.uri + 'public?command='
        self.usdprice: Dict[typing.BlockchainAsset, FVal] = {}
        self.inquirer = inquirer
        self.session.headers.update({
            'Key': self.api_key,
        })

    def first_connection(self):
        if self.first_connection_made:
            return

        fees_resp = self.returnFeeInfo()
        with self.lock:
            self.maker_fee = FVal(fees_resp['makerFee'])
            self.taker_fee = FVal(fees_resp['takerFee'])
            self.first_connection_made = True
        # Also need to do at least a single pass of the market watcher for the ticker
        self.market_watcher()

    def validate_api_key(self) -> Tuple[bool, str]:
        try:
            self.returnFeeInfo()
        except ValueError as e:
            error = str(e)
            if 'Invalid API key/secret pair' in error:
                return False, 'Provided API Key or secret is invalid'
            else:
                raise
        return True, ''

    def post_process(self, before: Dict) -> Dict:
        after = before

        # Add timestamps if there isnt one but is a datetime
        if('return' in after):
            if(isinstance(after['return'], list)):
                for x in range(0, len(after['return'])):
                    if(isinstance(after['return'][x], dict)):
                        if('datetime' in after['return'][x] and
                           'timestamp' not in after['return'][x]):
                            after['return'][x]['timestamp'] = float(
                                createTimeStamp(after['return'][x]['datetime'])
                            )

        return after

    def api_query(self, command: str, req: Optional[Dict] = None) -> Dict:
        result = retry_calls(5, 'poloniex', command, self._api_query, command, req)
        if 'error' in result:
            raise PoloniexError(
                'Poloniex query for "{}" returned error: {}'.format(
                    command,
                    result['error']
                ))
        return result

    def _api_query(self, command: str, req: Optional[Dict] = None) -> Dict:
        if req is None:
            req = {}

        if command == "returnTicker":
            log.debug('Querying poloniex for returnTicker')
            ret = self.session.get(self.public_uri + command)
        else:
            req['command'] = command

            with self.lock:
                # Protect this region with a lock since poloniex will reject
                # non-increasing nonces. So if two greenlets come in here at
                # the same time one of them will fail
                req['nonce'] = int(time.time() * 1000)
                post_data = str.encode(urlencode(req))

                sign = hmac.new(self.secret, post_data, hashlib.sha512).hexdigest()
                self.session.headers.update({'Sign': sign})

                log.debug(
                    'Poloniex private API query',
                    command=command,
                    post_data=req,
                )
                ret = self.session.post('https://poloniex.com/tradingApi', req)

            result = rlk_jsonloads(ret.text)
            return self.post_process(result)

        return rlk_jsonloads(ret.text)

    def returnTicker(self) -> Dict:
        return self.api_query("returnTicker")

    def returnFeeInfo(self) -> Dict:
        return self.api_query("returnFeeInfo")

    def returnLendingHistory(
            self,
            start_ts: Optional[typing.Timestamp] = None,
            end_ts: Optional[typing.Timestamp] = None,
            limit: Optional[int] = None,
    ) -> Dict:
        """Default limit for this endpoint seems to be 500 when I tried.
        So to be sure all your loans are included put a very high limit per call
        and also check if the limit was reached after each call.

        Also maximum limit seems to be 12660
        """
        req: Dict[str, Union[int, typing.Timestamp]] = dict()
        if start_ts is not None:
            req['start'] = start_ts
        if end_ts is not None:
            req['end'] = end_ts
        if limit is not None:
            req['limit'] = limit
        return self.api_query("returnLendingHistory", req)

    def returnTradeHistory(
            self,
            currencyPair: str,
            start: typing.Timestamp,
            end: typing.Timestamp,
    ) -> Union[Dict, List]:
        """If `currencyPair` is all, then it returns a dictionary with each key
        being a pair and each value a list of trades. If `currencyPair` is a specific
        pair then a list is returned"""
        return self.api_query('returnTradeHistory', {
            "currencyPair": currencyPair,
            'start': start,
            'end': end,
            'limit': 10000,
        })

    def returnDepositsWithdrawals(
            self,
            start_ts: typing.Timestamp,
            end_ts: typing.Timestamp,
    ) -> Dict:
        return self.api_query('returnDepositsWithdrawals', {'start': start_ts, 'end': end_ts})

    def market_watcher(self):
        self.ticker = self.returnTicker()
        with self.lock:
            self.usdprice['BTC'] = FVal(self.ticker['USDT_BTC']['last'])
            self.usdprice['ETH'] = FVal(self.ticker['USDT_ETH']['last'])
            self.usdprice['DASH'] = FVal(self.ticker['USDT_DASH']['last'])
            self.usdprice['XMR'] = FVal(self.ticker['USDT_XMR']['last'])
            self.usdprice['LTC'] = FVal(self.ticker['USDT_LTC']['last'])
            self.usdprice['MAID'] = FVal(self.ticker['BTC_MAID']['last']) * self.usdprice['BTC']
            self.usdprice['FCT'] = FVal(self.ticker['BTC_FCT']['last']) * self.usdprice['BTC']

    def main_logic(self):
        if not self.first_connection_made:
            return

        try:
            self.market_watcher()

        except PoloniexError as e:
            log.error('Poloniex error at main loop', error=str(e))
        except Exception as e:
            log.error(
                "\nException at main loop: {}\n{}\n".format(
                    str(e), traceback.format_exc())
            )

    # ---- General exchanges interface ----
    @cache_response_timewise()
    def query_balances(self) -> Tuple[Optional[dict], str]:
        try:
            resp = self.api_query('returnCompleteBalances', {"account": "all"})
        except (RemoteError, PoloniexError) as e:
            msg = (
                'Poloniex API request failed. Could not reach poloniex due '
                'to {}'.format(e)
            )
            log.error(msg)
            return None, msg

        balances = dict()
        for currency, v in resp.items():
            available = FVal(v['available'])
            on_orders = FVal(v['onOrders'])
            if (available != FVal(0) or on_orders != FVal(0)):
                entry = {}
                entry['amount'] = available + on_orders
                usd_price = self.inquirer.find_usd_price(
                    asset=currency,
                    asset_btc_price=None
                )
                usd_value = entry['amount'] * usd_price
                entry['usd_value'] = usd_value
                balances[currency] = entry

                log.debug(
                    'Poloniex balance query',
                    sensitive_log=True,
                    currency=currency,
                    amount=entry['amount'],
                    usd_value=usd_value,
                )

        return balances, ''

    def query_trade_history(
            self,
            start_ts: typing.Timestamp,
            end_ts: typing.Timestamp,
            end_at_least_ts: typing.Timestamp,
    ) -> Dict:
        with self.lock:
            cache = self.check_trades_cache(start_ts, end_at_least_ts)
        if cache is not None:
            assert isinstance(cache, Dict), 'Poloniex trade history should be a dict'
            return cache

        result = self.returnTradeHistory(
            currencyPair='all',
            start=start_ts,
            end=end_ts
        )
        # we know that returnTradeHistory returns a dict with currencyPair=all
        result = cast(Dict, result)

        results_length = 0
        for r, v in result.items():
            results_length += len(v)

        log.debug('Poloniex trade history query', results_num=results_length)

        if results_length >= 10000:
            raise ValueError(
                'Poloniex api has a 10k limit to trade history. Have not implemented'
                ' a solution for more than 10k trades at the moment'
            )

        with self.lock:
            self.update_trades_cache(result, start_ts, end_ts)
        return result

    def parseLoanCSV(self) -> List:
        """Parses (if existing) the lendingHistory.csv and returns the history in a list

        It can throw OSError, IOError if the file does not exist and csv.Error if
        the file is not proper CSV"""
        # the default filename, and should be (if at all) inside the data directory
        path = os.path.join(self.data_dir, "lendingHistory.csv")
        lending_history = list()
        with open(path, 'r') as csvfile:
            history = csv.reader(csvfile, delimiter=',', quotechar='|')
            next(history)  # skip header row
            for row in history:
                lending_history.append({
                    'currency': row[0],
                    'earned': FVal(row[6]),
                    'amount': FVal(row[2]),
                    'fee': FVal(row[5]),
                    'open': row[7],
                    'close': row[8]
                })
        return lending_history

    def query_loan_history(
            self,
            start_ts: typing.Timestamp,
            end_ts: typing.Timestamp,
            end_at_least_ts: typing.Timestamp,
            from_csv: Optional[bool] = False,
    ) -> List:
        """
        WARNING: Querying from returnLendingHistory endpoing instead of reading from
        the CSV file can potentially  return unexpected/wrong results.

        That is because the `returnLendingHistory` endpoint has a hidden limit
        of 12660 results. In our code we use the limit of 12000 but poloniex may change
        the endpoint to have a lower limit at which case this code will break.

        To be safe compare results of both CSV and endpoint to make sure they agree!
        """
        try:
            if from_csv:
                return self.parseLoanCSV()
        except (OSError, IOError, csv.Error):
            pass

        with self.lock:
            # We know Loan history cache is a list
            cache = cast(
                List,
                self.check_trades_cache(start_ts, end_at_least_ts, special_name='loan_history'),
            )
        if cache is not None:
            return cache

        loans_query_return_limit = 12000
        result = self.returnLendingHistory(
            start_ts=start_ts,
            end_ts=end_ts,
            limit=loans_query_return_limit
        )
        data = list(result)
        log.debug('Poloniex loan history query', results_num=len(data))

        # since I don't think we have any guarantees about order of results
        # using a set of loan ids is one way to make sure we get no duplicates
        # if poloniex can guarantee me that the order is going to be ascending/descending
        # per open/close time then this can be improved
        id_set = set()

        while len(result) == loans_query_return_limit:
            # Find earliest timestamp to re-query the next batch
            min_ts = end_ts
            for loan in result:
                ts = createTimeStamp(loan['close'], formatstr="%Y-%m-%d %H:%M:%S")
                min_ts = min(min_ts, ts)
                id_set.add(loan['id'])

            result = self.returnLendingHistory(
                start_ts=start_ts,
                end_ts=min_ts,
                limit=loans_query_return_limit
            )
            log.debug('Poloniex loan history query', results_num=len(result))
            for loan in result:
                if loan['id'] not in id_set:
                    data.append(loan)

        with self.lock:
            self.update_trades_cache(data, start_ts, end_ts, special_name='loan_history')
        return data

    def query_deposits_withdrawals(
            self,
            start_ts: typing.Timestamp,
            end_ts: typing.Timestamp,
            end_at_least_ts: typing.Timestamp,
    ) -> List:
        with self.lock:
            cache = self.check_trades_cache(
                start_ts,
                end_at_least_ts,
                special_name='deposits_withdrawals'
            )
            cache = cast(Dict, cache)
        if cache is None:
            result = self.returnDepositsWithdrawals(start_ts, end_ts)
            with self.lock:
                self.update_trades_cache(
                    result,
                    start_ts,
                    end_ts,
                    special_name='deposits_withdrawals'
                )
        else:
            result = cache

        log.debug(
            'Poloniex deposits/withdrawal query',
            results_num=len(result['withdrawals']) + len(result['deposits']),
        )

        movements = list()
        for withdrawal in result['withdrawals']:
            movements.append(AssetMovement(
                exchange='poloniex',
                category='withdrawal',
                timestamp=withdrawal['timestamp'],
                asset=withdrawal['currency'],
                amount=FVal(withdrawal['amount']),
                fee=FVal(withdrawal['fee'])
            ))

        for deposit in result['deposits']:
            movements.append(AssetMovement(
                exchange='poloniex',
                category='deposit',
                timestamp=deposit['timestamp'],
                asset=deposit['currency'],
                amount=FVal(deposit['amount']),
                fee=0
            ))

        return movements
