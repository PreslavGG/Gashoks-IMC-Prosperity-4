from datamodel import OrderDepth, UserId, TradingState, Order
from typing import List
import string

POSITION_LIMITS = {
    "TOMATOES": 80,
    "EMERALDS": 80,
}

class EmeraldsTrader:

    FAIR_VALUE = 10000
    POS_LIMIT = 80

    def __init__(self, state: TradingState) -> None:
        self.product = "EMERALDS"
        self.state = state
        self.order_depth = state.order_depths[self.product]
        self.position = self.state.position.get(self.product, 0)
        self.bids = self.order_depth.buy_orders
        self.asks = self.order_depth.sell_orders
        self.orders: List[Order] = []
        self.max_buy = self.POS_LIMIT - self.position
        self.max_sell = self.POS_LIMIT + self.position

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
    
    def run(self) -> List[Order]:
        mid = self.calculate_mid()

        for price, volume in self.asks.items():
            if price < mid and self.max_buy > 0:
                qty = min(abs(volume), self.max_buy)
                self.orders.append(Order(self.product, price, qty))
                self.max_buy -= qty
        
        for price, volume in self.bids.items():
            if price > mid and self.max_sell > 0:
                qty = min(volume, self.max_sell)
                self.orders.append(Order(self.product, price, -qty))
                self.max_sell -= qty
        
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