
from typing import Optional, TypeVar, Type, cast
from driftpy.types import (
    PositionDirection,
    StateAccount,
    MarketsAccount,
    FundingPaymentHistoryAccount,
    FundingRateHistoryAccount,
    TradeHistoryAccount,
    LiquidationHistoryAccount,
    DepositHistoryAccount,
    ExtendedCurveHistoryAccount,
    User,
    UserPositions,
)
from driftpy.constants.markets import MARKETS
from driftpy.constants.numeric_constants import MARK_PRICE_PRECISION, QUOTE_PRECISION
from driftpy.clearing_house_user import ClearingHouseUser
import asyncio

import pandas as pd
import numpy as np

#todo
AMM_PRECISION = 1e13

async def load_position_table(drift_acct):
    async def async_apply(ser, lambd):
        prices = []
        for x in ser.values.tolist():
            price = await lambd(x) 
            prices.append(price)
        return prices

    drift_user = ClearingHouseUser(drift_acct, drift_acct.program.provider.wallet.public_key)
    drift_user_acct = await (drift_user.get_user_account())
    position_acct = await (drift_acct.program.account["UserPositions"].fetch(
                        drift_user_acct.positions
                    ))

    # load user positions
    positions = cast(
                    UserPositions,
                    position_acct,
                )
    positions_df = pd.DataFrame(positions.positions)[['market_index', 'base_asset_amount', 'quote_asset_amount']]
    positions_df['base_asset_amount'] /= AMM_PRECISION
    positions_df['quote_asset_amount'] /= QUOTE_PRECISION
    positions_df = pd.DataFrame(MARKETS)[['symbol', 'market_index']].merge(positions_df)
    positions_df.loc[positions_df.base_asset_amount==0,:] = np.nan
    positions_df['notional'] = np.array(await (async_apply(positions_df['market_index'], drift_user.get_position_value)))/QUOTE_PRECISION
    positions_df['entry_price'] = (positions_df['quote_asset_amount'] / positions_df['base_asset_amount']).abs()
    positions_df['exit_price'] = (positions_df['notional'] / positions_df['base_asset_amount']).abs()
    positions_df['pnl'] = (positions_df['notional'] -  positions_df['quote_asset_amount'])*positions_df['base_asset_amount'].pipe(np.sign)
    return positions_df



from datetime import datetime, timedelta
from pythclient.pythaccounts import PythPriceAccount
from pythclient.solana import (SolanaClient, SolanaPublicKey, SOLANA_DEVNET_HTTP_ENDPOINT, SOLANA_DEVNET_WS_ENDPOINT,
SOLANA_MAINNET_HTTP_ENDPOINT, SOLANA_MAINNET_HTTP_ENDPOINT)

async def get_pyth_price(address):
    # devnet DOGE/USD price account key (available on pyth.network website)
    account_key = SolanaPublicKey(address)
    solana_client = SolanaClient(endpoint=SOLANA_MAINNET_HTTP_ENDPOINT, ws_endpoint=SOLANA_MAINNET_HTTP_ENDPOINT)
    price: PythPriceAccount = PythPriceAccount(account_key, solana_client)

    await price.update()
    # print(dir(price.derivations))
    twap_result = (price.derivations.get('TWAPVALUE'), price.derivations.get('TWACVALUE'))
    price_result = (price.aggregate_price, price.aggregate_price_confidence_interval)
    print(twap_result)
    return price
    # # print(price.aggregate_price, "±", price.aggregate_price_confidence_interval)
    # return result

HOURS_IN_DAY = 24

def calculate_capped_funding_rate(markets_summary):
    next_funding = (markets_summary['last_mark_price_twap'] \
                         - markets_summary['last_oracle_price_twap'])/HOURS_IN_DAY
    fd1 =  next_funding/MARK_PRICE_PRECISION \
    * (markets_summary['base_asset_amount']/AMM_PRECISION)
    fd2_m = (((markets_summary['total_fee_minus_distributions']
                             - markets_summary['total_fee']/2)*.66666)/QUOTE_PRECISION)
    
    fd2_u =  next_funding/MARK_PRICE_PRECISION \
    * (markets_summary['base_asset_amount_long'])
    
    
    est_fee_pool_funding_revenue = markets_summary["base_asset_amount"]/AMM_PRECISION * next_funding/MARK_PRICE_PRECISION

    drift_capped_fund_rate = (markets_summary[["base_asset_amount_long", "base_asset_amount_short"]].abs().min(axis=1)  \
                       * next_funding + fd2_m*MARK_PRICE_PRECISION*AMM_PRECISION) \
    / markets_summary[["base_asset_amount_long", "base_asset_amount_short"]].abs().max(axis=1)

    capped_funding = drift_capped_fund_rate/markets_summary['last_oracle_price_twap'] * 100
    capped_funding[fd2_m+est_fee_pool_funding_revenue>0] = np.nan
    
    return capped_funding
    

async def calculate_market_summary(markets):
    FUNDING_PRECISION = 1e4
    
    markets_summary = pd.concat([
        pd.DataFrame(MARKETS).iloc[:,:3],
    pd.DataFrame(markets.markets),
    pd.DataFrame([x.amm for x in markets.markets]),           
              ],axis=1).dropna(subset=['symbol'])

    last_funding_ts = pd.to_datetime(markets.markets[0].amm.last_funding_rate_ts*1e9)
    next_funding_ts = last_funding_ts + timedelta(hours=1)
    next_funding_ts

    summary = {}
    
    next_funding = (markets_summary['last_mark_price_twap'] \
                         - markets_summary['last_oracle_price_twap'])/24
    summary['next_funding_rate(%)'] = next_funding\
        /markets_summary['last_oracle_price_twap'] * 100
    
    
    summary['next_funding_rate_capped(%)']  = calculate_capped_funding_rate(markets_summary)
    
    summary['next_funding_rate(%APR)'] = (summary['next_funding_rate(%)'] * 24 * 365.25).round(2)
    
    # next_est_capped_funding_revenue = \
    # markets_summary[["base_asset_amount_long", "base_asset_amount_short"]].abs().min(axis=1) * next_funding \
    # - markets_summary[["base_asset_amount_long", "base_asset_amount_short"]].abs().max(axis=1) * drift_capped_fund_rate
    
    summary['mark_price'] = (markets_summary['quote_asset_reserve'] \
                         /markets_summary['base_asset_reserve'])\
    *markets_summary['peg_multiplier']/1e3
    
    prices = []
    twaps = []
    confs = []
    twacs = []
    for x in markets_summary['oracle'].values.tolist():
        price = await (get_pyth_price(str(x)))
        prices.append(price.aggregate_price)
        confs.append(price.aggregate_price_confidence_interval)        
    summary['oracle_price'] = prices
    summary['oracle_conf'] = confs
    
    df = pd.concat([pd.DataFrame(MARKETS).iloc[:,:3], pd.DataFrame(summary)],axis=1)
    
    # df.loc[fd2_m+est_fee_pool_funding_revenue>0,
    #      ['next_funding_rate_capped(%)']] = np.nan
    
    return df


# oracle_mark_spread = market_summary['mark_price']-market_summary['oracle_price']

# funding_whatif = np.sign(oracle_mark_spread)*abs(oracle_mark_spread - market_summary['oracle_conf'])\
# /market_summary['oracle_price']
# pd.concat([funding_whatif, market_summary['next_funding_rate']],axis=1)
# (funding_whatif - market_summary['next_funding_rate'])#/market_summary['next_funding_rate']