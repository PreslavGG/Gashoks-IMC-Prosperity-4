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
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return None
    
    def get_best_bid_ask(self):
        best_bid = max(self.bids.keys()) if self.bids else None
        best_ask = min(self.asks.keys()) if self.asks else None
        return best_bid, best_ask

class EmeraldsTrader(ProductTrader):

    FAIR_VALUE = 10000
    POS_LIMIT = 80

    def __init__(self, state: TradingState) -> None:
        super().__init__(state)
        self.max_buy = self.POS_LIMIT - self.position
        self.max_sell = self.POS_LIMIT + self.position

    def run(self) -> List[Order]:
        if self.best_bid is None or self.best_ask is None:
            return []

        spread = self.best_ask - self.best_bid
        edge = 1

        # === TAKE mispriced orders ===
        if self.best_ask < self.FAIR_VALUE - edge and self.max_buy > 0:
            qty = min(abs(self.asks[self.best_ask]), self.max_buy)
            self.orders.append(Order(self.product, self.best_ask, qty))

        if self.best_bid > self.FAIR_VALUE + edge and self.max_sell > 0:
            qty = min(self.bids[self.best_bid], self.max_sell)
            self.orders.append(Order(self.product, self.best_bid, -qty))

        # === MARKET MAKE ===
        if spread > 1:
            inventory_skew = int(self.position * 0.1)

            my_bid = self.best_bid + 1 - inventory_skew
            my_ask = self.best_ask - 1 - inventory_skew

            if self.max_buy > 0:
                qty = min(10, self.max_buy)
                self.orders.append(Order(self.product, my_bid, qty))

            if self.max_sell > 0:
                qty = min(10, self.max_sell)
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