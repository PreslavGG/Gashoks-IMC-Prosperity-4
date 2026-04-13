import json
import jsonpickle
from datamodel import Listing, Observation, OrderDepth, UserId, TradingState, Order, ProsperityEncoder, Symbol, Trade
from typing import Any, List, Optional


# ═══════════════════════════════════════════════════════════════════════════
# LOGGER
# ═══════════════════════════════════════════════════════════════════════════

class Logger:
    def __init__(self) -> None:
        self.logs = ""
        self.max_log_length = 3750

    def print(self, *objects: Any, sep: str = " ", end: str = "\n") -> None:
        self.logs += sep.join(map(str, objects)) + end

    def flush(self, state: TradingState, orders, conversions, trader_data):
        print(json.dumps({"logs": self.logs}))
        self.logs = ""

logger = Logger()


# ═══════════════════════════════════════════════════════════════════════════
# BASE PRODUCT TRADER
# ═══════════════════════════════════════════════════════════════════════════

class ProductTrader:

    PRODUCT: str
    POS_LIMIT: int

    MAKE_SIZE = 20
    MIN_EDGE = 3
    TAKE_EDGE = 2
    SKEW_THRESHOLD = 0.4
    MAX_SKEW_OFFSET = 3

    def __init__(self, state: TradingState) -> None:
        self.state = state
        self.order_depth = state.order_depths[self.PRODUCT]
        self.position = state.position.get(self.PRODUCT, 0)

        self.bids = self.order_depth.buy_orders
        self.asks = self.order_depth.sell_orders

        self.orders: List[Order] = []

        self.max_buy = self.POS_LIMIT - self.position
        self.max_sell = self.POS_LIMIT + self.position
        self.skew = self.position / self.POS_LIMIT if self.POS_LIMIT else 0

        self.best_bid = max(self.bids) if self.bids else None
        self.best_ask = min(self.asks) if self.asks else None

    # ─────────────────────────────────────────────
    # MICROPRICE (BIG UPGRADE)
    # ─────────────────────────────────────────────
    def microprice(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None

        bid_vol = self.bids[self.best_bid]
        ask_vol = abs(self.asks[self.best_ask])

        return (self.best_bid * ask_vol + self.best_ask * bid_vol) / (bid_vol + ask_vol)

    # ─────────────────────────────────────────────
    # REAL WALL DETECTION
    # ─────────────────────────────────────────────
    def biggest_wall(self):
        bid_wall = max(self.bids, key=lambda p: self.bids[p]) if self.bids else None
        ask_wall = min(self.asks, key=lambda p: abs(self.asks[p])) if self.asks else None
        return bid_wall, ask_wall

    # ─────────────────────────────────────────────
    def fair_value(self) -> Optional[float]:
        return self.microprice()

    # ─────────────────────────────────────────────
    def _buy(self, price: int, qty: int):
        qty = max(0, min(qty, self.max_buy))
        if qty > 0:
            self.orders.append(Order(self.PRODUCT, price, qty))
            self.max_buy -= qty

    def _sell(self, price: int, qty: int):
        qty = max(0, min(qty, self.max_sell))
        if qty > 0:
            self.orders.append(Order(self.PRODUCT, price, -qty))
            self.max_sell -= qty

    # ─────────────────────────────────────────────
    # TAKING LOGIC (IMPROVED EDGE CONTROL)
    # ─────────────────────────────────────────────
    def take_orders(self, fv: float):
        for ask_price in sorted(self.asks):
            ask_vol = abs(self.asks[ask_price])
            if ask_price <= fv - self.TAKE_EDGE:
                self._buy(ask_price, ask_vol)

        for bid_price in sorted(self.bids, reverse=True):
            bid_vol = abs(self.bids[bid_price])
            if bid_price >= fv + self.TAKE_EDGE:
                self._sell(bid_price, bid_vol)

    # ─────────────────────────────────────────────
    # MAKING LOGIC (IMPROVED)
    # ─────────────────────────────────────────────
    def make_orders(self, fv: float):
        if self.best_bid is None or self.best_ask is None:
            return

        spread = self.best_ask - self.best_bid

        # Skip bad market conditions
        if spread < 2:
            return

        # Base quotes
        my_bid = self.best_bid + 1
        my_ask = self.best_ask - 1

        # NONLINEAR SKEW
        offset = int((self.skew ** 3) * self.MAX_SKEW_OFFSET * 2)
        my_bid -= offset * 2
        my_ask -= offset

        # Enforce edge
        my_bid = min(my_bid, int(fv - self.MIN_EDGE))
        my_ask = max(my_ask, int(fv + self.MIN_EDGE))

        # ADAPTIVE SIZE
        size = int(self.MAKE_SIZE * (1 - abs(self.skew)))
        size = max(5, size)

        self._buy(my_bid, size)
        self._sell(my_ask, size)

    # ─────────────────────────────────────────────
    def get_orders(self) -> List[Order]:
        fv = self.fair_value()
        if fv is None:
            return self.orders

        self.take_orders(fv)
        self.make_orders(fv)
        return self.orders


# ═══════════════════════════════════════════════════════════════════════════
# EMERALDS (UNCHANGED BUT CLEAN)
# ═══════════════════════════════════════════════════════════════════════════

class EmeraldsTrader(ProductTrader):

    PRODUCT = "EMERALDS"
    POS_LIMIT = 80
    FAIR_VALUE = 10000

    MIN_EDGE = 1
    MAKE_SIZE = 40

    def fair_value(self):
        return self.FAIR_VALUE


# ═══════════════════════════════════════════════════════════════════════════
# TOMATOES (UPGRADED TREND MODEL)
# ═══════════════════════════════════════════════════════════════════════════

class TomatoesTrader(ProductTrader):

    PRODUCT = "TOMATOES"
    POS_LIMIT = 80

    EMA_FAST = 50
    EMA_SLOW = 200

    def __init__(self, state: TradingState, memory: dict):
        super().__init__(state)

        price = self.microprice() or 0

        prev_fast = memory.get("ema_fast", price)
        prev_slow = memory.get("ema_slow", price)

        k_fast = 2 / (self.EMA_FAST + 1)
        k_slow = 2 / (self.EMA_SLOW + 1)

        self.ema_fast = prev_fast + k_fast * (price - prev_fast)
        self.ema_slow = prev_slow + k_slow * (price - prev_slow)

        memory["ema_fast"] = self.ema_fast
        memory["ema_slow"] = self.ema_slow

    def fair_value(self):
        base = self.microprice()
        if base is None:
            return None

        # STRONGER TREND SIGNAL
        trend = self.ema_fast - self.ema_slow
        offset = int(trend / 2)
        offset = max(-5, min(5, offset))

        return base + offset


# ═══════════════════════════════════════════════════════════════════════════
# MAIN TRADER
# ═══════════════════════════════════════════════════════════════════════════

PRODUCT_TRADERS = {
    "EMERALDS": EmeraldsTrader,
    "TOMATOES": TomatoesTrader,
}


class Trader:

    def run(self, state: TradingState):
        try:
            memory = jsonpickle.decode(state.traderData)
            if not isinstance(memory, dict):
                memory = {}
        except:
            memory = {}

        result = {}

        for symbol, trader_cls in PRODUCT_TRADERS.items():
            if symbol in state.order_depths:
                try:
                    if symbol == "TOMATOES":
                        trader = trader_cls(state, memory)
                    else:
                        trader = trader_cls(state)

                    result[symbol] = trader.get_orders()

                except Exception as e:
                    logger.print(f"{symbol} error: {e}")

        trader_data = jsonpickle.encode(memory)

        logger.flush(state, result, 0, trader_data)
        return result, 0, trader_data