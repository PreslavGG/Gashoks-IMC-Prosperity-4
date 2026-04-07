import json
import jsonpickle
from datamodel import OrderDepth, UserId, TradingState, Order
from typing import List


class Logger:
    def __init__(self):
        self.prints = {}

    def log(self, product: str, kind: str, message):
        group = self.prints.get(product, {})
        group[kind] = message
        self.prints[product] = group

    def flush(self):
        print(json.dumps(self.prints))
        self.prints = {}

logger = Logger()


class ProductTrader:

    POS_LIMIT: int  # must be defined in each subclass

    def __init__(self, state: TradingState, product: str) -> None:
        self.product = product
        self.state = state
        self.order_depth = state.order_depths[self.product]
        self.position = state.position.get(self.product, 0)
        self.bids = self.order_depth.buy_orders
        self.asks = self.order_depth.sell_orders
        self.orders: List[Order] = []
        self.mid = self.calculate_mid()
        self.best_bid, self.best_ask = self.get_best_bid_ask()
        self.max_buy = self.POS_LIMIT - self.position
        self.max_sell = self.POS_LIMIT + self.position
        self.skew = self.position / self.POS_LIMIT  # -1 to +1
        self.log("pos", self.position)
        self.log("skew", round(self.skew, 2))
        self.log("mid", round(self.mid, 2))

    def log(self, kind: str, message):
        logger.log(self.product, kind, message)

    def calculate_mid(self) -> float:
        avg_bid, avg_ask = 0.0, 0.0
        bid_vol, ask_vol = 0, 0

        for price, volume in self.bids.items():
            avg_bid += price * volume
            bid_vol += volume

        for price, volume in self.asks.items():
            avg_ask += price * volume
            ask_vol += volume

        avg_bid /= bid_vol
        avg_ask /= (-ask_vol)

        return (avg_bid + avg_ask) / 2

    def get_best_bid_ask(self):
        best_bid = max(self.bids.keys()) if self.bids else None
        best_ask = min(self.asks.keys()) if self.asks else None
        return best_bid, best_ask

    def get_orders(self) -> List[Order]:
        raise NotImplementedError


class EmeraldsTrader(ProductTrader):

    PRODUCT = "EMERALDS"
    FAIR_VALUE = 10000
    POS_LIMIT = 80

    # inventory thresholds
    SKEW_THRESHOLD = 0.5    # start skewing quotes at 50% of limit (pos=40)
    AGGR_THRESHOLD = 0.75   # aggressively flush at 75% of limit (pos=60)
    QUOTE_SIZE = 10

    def __init__(self, state: TradingState) -> None:
        super().__init__(state, self.PRODUCT)

    def get_orders(self) -> List[Order]:

        ## MARKET TAKING ##
        for ask_price, ask_vol in sorted(self.asks.items()):
            if ask_price < self.FAIR_VALUE and self.max_buy > 0:
                qty = min(-ask_vol, self.max_buy)
                self.log("take_buy", f"{qty} @ {ask_price}")
                self.orders.append(Order(self.product, ask_price, qty))
                self.max_buy -= qty

        for bid_price, bid_vol in sorted(self.bids.items(), reverse=True):
            if bid_price > self.FAIR_VALUE and self.max_sell > 0:
                qty = min(bid_vol, self.max_sell)
                self.log("take_sell", f"{qty} @ {bid_price}")
                self.orders.append(Order(self.product, bid_price, -qty))
                self.max_sell -= qty

        ## MARKET MAKING ##
        best_bid, best_ask = self.best_bid, self.best_ask

        if best_bid is None or best_ask is None:
            return []

        spread = best_ask - best_bid
        self.log("spread", spread)

        if spread <= 1:
            return []

        # ── Aggressive inventory flush ────────────────────────────────────────
        # If position is too long, hit the bot's bid to offload inventory.
        # If position is too short, hit the bot's ask to cover.
        # We give up edge on these trades but it keeps us near flat.

        if self.skew > self.AGGR_THRESHOLD:
            # too long — sell aggressively at best bid
            qty = min(self.QUOTE_SIZE, self.max_sell)
            if qty > 0:
                self.log("aggressive_sell", f"{qty} @ {best_bid}")
                self.orders.append(Order(self.product, best_bid, -qty))
                return self.orders

        elif self.skew < -self.AGGR_THRESHOLD:
            # too short — buy aggressively at best ask
            qty = min(self.QUOTE_SIZE, self.max_buy)
            if qty > 0:
                self.log("aggressive_buy", f"{qty} @ {best_ask}")
                self.orders.append(Order(self.product, best_ask, qty))
                return self.orders

        # ── Passive quotes with inventory skew ───────────────────────────────
        # Shift both quotes in the direction that mean-reverts position.
        # skew > 0 (long)  → lower bid and ask → lean toward selling
        # skew < 0 (short) → raise bid and ask → lean toward buying

        skew_offset = round(self.skew * 2) if abs(self.skew) > self.SKEW_THRESHOLD else 0  # max ±2 ticks of skew
        my_bid = best_bid + 1 - skew_offset
        my_ask = best_ask - 1 - skew_offset

        # ensure we never cross our own quotes
        if my_bid >= my_ask:
            my_bid = self.FAIR_VALUE - 1
            my_ask = self.FAIR_VALUE + 1

        if self.max_buy > 0:
            qty = min(self.QUOTE_SIZE, self.max_buy)
            self.log("bid", f"{qty} @ {my_bid}")
            self.orders.append(Order(self.product, my_bid, qty))

        if self.max_sell > 0:
            qty = min(self.QUOTE_SIZE, self.max_sell)
            self.log("ask", f"{qty} @ {my_ask}")
            self.orders.append(Order(self.product, my_ask, -qty))

        return self.orders
    

PRODUCT_TRADERS = {
    "EMERALDS": EmeraldsTrader,
}

class Trader:

    def bid(self):
        return 15

    def run(self, state: TradingState):
        try:
            memory = jsonpickle.decode(state.traderData)
            if not isinstance(memory, dict):
                memory = {}
        except:
            memory = {}

        result: dict[str, List[Order]] = {}

        for symbol, trader_cls in PRODUCT_TRADERS.items():
            if symbol in state.order_depths:
                try:
                    trader = trader_cls(state)
                    result[symbol] = trader.get_orders()
                except Exception as e:
                    logger.log(symbol, "error", str(e))

        memory["last_timestamp"] = state.timestamp
        traderData = jsonpickle.encode(memory)
        conversions = 0

        logger.flush()
        return result, conversions, traderData