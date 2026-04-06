from datamodel import OrderDepth, UserId, TradingState, Order
from typing import List
import string

POSITION_LIMITS = {
    "TOMATOES": 80,
    "EMERALDS": 80,
}

class ProductTrader:
    def __init__(self, state: TradingState) -> None:
        self.product = "EMERALDS"
        self.state = state
        self.order_depth = state.order_depths[self.product]
        self.position = self.state.position.get(self.product, 0)
        self.bids = self.order_depth.buy_orders
        self.asks = self.order_depth.sell_orders
        self.orders: List[Order] = []
        self.mid = self.calculate_mid()
        self.best_bid, self.best_ask = self.get_best_bid_ask()

    def calculate_mid(self) -> float:
        avg_bid, avg_ask = 0, 0
        bid_vol, ask_vol = 0, 0

        for price, volume in self.bids.items():
            avg_bid += price*volume
            bid_vol += volume
        
        avg_bid /= bid_vol

        for price, volume in self.asks.items():
            avg_ask += price*volume
            ask_vol += volume
        
        avg_ask /= (-ask_vol)

        return (avg_bid + avg_ask) / 2
    
    def get_best_bid_ask(self):
        
        best_bid = best_ask = None

        try:
            if len(self.bids) > 0:
                best_bid = max(self.bids.keys())
            if len(self.asks) > 0:
                best_ask = min(self.asks.keys())
        except: pass

        return best_bid, best_ask

class EmeraldsTrader(ProductTrader):

    FAIR_VALUE = 10000
    POS_LIMIT = 80

    def __init__(self, state: TradingState) -> None:
        super(EmeraldsTrader, self).__init__(state)
        self.max_buy = self.POS_LIMIT - self.position
        self.max_sell = self.POS_LIMIT + self.position

    def run(self) -> List[Order]:
        ## Market Making ##
        best_bid, best_ask = self.best_bid, self.best_ask

        if best_bid is None or best_ask is None:
            return []

        # current spread
        spread = best_ask - best_bid

        # only trade if spread is wide enough
        if spread <= 1:
               return []

        # improve the market
        my_bid = best_bid + 1
        my_ask = best_ask - 1

        # BUY (place bid)
        if self.max_buy > 0:
            qty = min(10, self.max_buy)
            print(f"BID {qty} @ {my_bid}")
            self.orders.append(Order(self.product, my_bid, qty))

        # SELL (place ask)
        if self.max_sell > 0:
            qty = min(10, self.max_sell)
            print(f"ASK {qty} @ {my_ask}")
            self.orders.append(Order(self.product, my_ask, -qty))

        return self.orders


class Trader:

    def bid(self):
        return 15
    
    def run(self, state: TradingState):
        """Only method required. It takes all buy and sell orders for all
        symbols as an input, and outputs a list of orders to be sent."""

        print("traderData: " + state.traderData)
        print("Observations: " + str(state.observations))

        result: dict[str, List[Order]] = {}
        if "EMERALDS" in state.order_depths:
            result["EMERALDS"] = EmeraldsTrader(state).run()
    
        # String value holding Trader state data required. 
        # It will be delivered as TradingState.traderData on next execution.
        traderData = "SAMPLE" 
        
        # Sample conversion request. Check more details below. 
        conversions = 1
        return result, conversions, traderData