import json
import jsonpickle
from datamodel import Listing, Observation, OrderDepth, UserId, TradingState, Order, ProsperityEncoder, Symbol, Trade
from typing import Any, List, Optional


# ═══════════════════════════════════════════════════════════════════════════
#  LOGGER  (visualizer-compatible)
# ═══════════════════════════════════════════════════════════════════════════

class Logger:
    def __init__(self) -> None:
        self.logs = ""
        self.max_log_length = 3750

    def print(self, *objects: Any, sep: str = " ", end: str = "\n") -> None:
        self.logs += sep.join(map(str, objects)) + end

    def flush(self, state: TradingState, orders: dict[Symbol, list[Order]], conversions: int, trader_data: str) -> None:
        base_length = len(self.to_json([self.compress_state(state, ""), self.compress_orders(orders), conversions, "", ""]))
        max_item_length = (self.max_log_length - base_length) // 3
        print(self.to_json([
            self.compress_state(state, self.truncate(state.traderData, max_item_length)),
            self.compress_orders(orders),
            conversions,
            self.truncate(trader_data, max_item_length),
            self.truncate(self.logs, max_item_length),
        ]))
        self.logs = ""

    def compress_state(self, state: TradingState, trader_data: str) -> list[Any]:
        return [state.timestamp, trader_data, self.compress_listings(state.listings),
                self.compress_order_depths(state.order_depths), self.compress_trades(state.own_trades),
                self.compress_trades(state.market_trades), state.position, self.compress_observations(state.observations)]

    def compress_listings(self, listings: dict[Symbol, Listing]) -> list[list[Any]]:
        return [[l.symbol, l.product, l.denomination] for l in listings.values()]

    def compress_order_depths(self, order_depths: dict[Symbol, OrderDepth]) -> dict[Symbol, list[Any]]:
        return {s: [od.buy_orders, od.sell_orders] for s, od in order_depths.items()}

    def compress_trades(self, trades: dict[Symbol, list[Trade]]) -> list[list[Any]]:
        return [[t.symbol, t.price, t.quantity, t.buyer, t.seller, t.timestamp]
                for arr in trades.values() for t in arr]

    def compress_observations(self, observations: Observation) -> list[Any]:
        conversion_observations = {
            p: [o.bidPrice, o.askPrice, o.transportFees, o.exportTariff, o.importTariff, o.sugarPrice, o.sunlightIndex]
            for p, o in observations.conversionObservations.items()
        }
        return [observations.plainValueObservations, conversion_observations]

    def compress_orders(self, orders: dict[Symbol, list[Order]]) -> list[list[Any]]:
        return [[o.symbol, o.price, o.quantity] for arr in orders.values() for o in arr]

    def to_json(self, value: Any) -> str:
        return json.dumps(value, cls=ProsperityEncoder, separators=(",", ":"))

    def truncate(self, value: str, max_length: int) -> str:
        lo, hi, out = 0, min(len(value), max_length), ""
        while lo <= hi:
            mid = (lo + hi) // 2
            candidate = value[:mid] + ("..." if mid < len(value) else "")
            if len(json.dumps(candidate)) <= max_length:
                out = candidate
                lo = mid + 1
            else:
                hi = mid - 1
        return out

logger = Logger()


# ═══════════════════════════════════════════════════════════════════════════
#  BASE PRODUCT TRADER
#  Subclasses configure via class constants and override fair_value() only.
#  Adding a new product = new subclass + config + optional fair_value().
# ═══════════════════════════════════════════════════════════════════════════

class ProductTrader:

    # ── Must define in each subclass ──────────────────────────────────────
    PRODUCT: str
    POS_LIMIT: int

    # ── Market-making params (override per product as needed) ─────────────
    MAKE_SIZE: int          = 20    # lots per passive quote side
    MIN_EDGE: int           = 3     # min ticks of edge from fair value
    SKEW_THRESHOLD: float   = 0.4   # skew starts at this fraction of limit
    MAX_SKEW_OFFSET: int    = 2     # max tick shift for inventory skew

    def __init__(self, state: TradingState) -> None:
        self.state = state
        self.order_depth = state.order_depths[self.PRODUCT]
        self.position = state.position.get(self.PRODUCT, 0)

        # Sort order book levels explicitly (not guaranteed in live engine)
        self.bids: dict[int, int] = self.order_depth.buy_orders   # price → +qty
        self.asks: dict[int, int] = self.order_depth.sell_orders  # price → -qty

        self.orders: List[Order] = []
        self.max_buy  = self.POS_LIMIT - self.position
        self.max_sell = self.POS_LIMIT + self.position
        self.skew     = self.position / self.POS_LIMIT if self.POS_LIMIT else 0

        self.best_bid: Optional[int]
        self.best_ask: Optional[int]
        self.best_bid, self.best_ask = self._best_bid_ask()

        bid_wall, ask_wall = self._walls()
        self.bid_wall: Optional[int] = bid_wall
        self.ask_wall: Optional[int] = ask_wall
        self.wall_mid: Optional[float] = (
            (bid_wall + ask_wall) / 2 if bid_wall is not None and ask_wall is not None else None
        )

        self._log_state()

    # ── Logging helpers ───────────────────────────────────────────────────

    def log(self, kind: str, message):
        logger.print(f"[{self.PRODUCT}] {kind}: {message}")

    def _log_state(self):
        self.log("pos",  self.position)
        self.log("skew", round(self.skew, 2))
        bid_wall, ask_wall, wall_mid = self.bid_wall, self.ask_wall, self.wall_mid
        if wall_mid is not None and bid_wall is not None and ask_wall is not None:
            self.log("wall_mid", round(wall_mid, 1))
            self.log("wall_spread", int(ask_wall - bid_wall))

    # ── Order book helpers ────────────────────────────────────────────────

    def _best_bid_ask(self) -> tuple[Optional[int], Optional[int]]:
        best_bid = max(self.bids) if self.bids else None
        best_ask = min(self.asks) if self.asks else None
        return best_bid, best_ask

    def _walls(self) -> tuple[Optional[int], Optional[int]]:
        """Outermost (deepest) price levels — used as fair value anchors."""
        bid_wall = min(self.bids) if self.bids else None
        ask_wall = max(self.asks) if self.asks else None
        return bid_wall, ask_wall

    # ── Fair value ────────────────────────────────────────────────────────

    def fair_value(self) -> Optional[float]:
        """
        Override in subclasses to use a different fair value estimate.
        Default: wall_mid (outermost bid/ask average).
        """
        return self.wall_mid

    # ── Capacity helpers ──────────────────────────────────────────────────

    def _buy(self, price: int, qty: int, tag: str = "buy"):
        """Place a buy order and decrement remaining capacity."""
        qty = max(0, min(qty, self.max_buy))
        if qty > 0:
            self.orders.append(Order(self.PRODUCT, price, qty))
            self.max_buy -= qty
            self.log(tag, f"{qty}@{price}")

    def _sell(self, price: int, qty: int, tag: str = "sell"):
        """Place a sell order and decrement remaining capacity."""
        qty = max(0, min(qty, self.max_sell))
        if qty > 0:
            self.orders.append(Order(self.PRODUCT, price, -qty))
            self.max_sell -= qty
            self.log(tag, f"{qty}@{price}")

    FLATTEN_THRESHOLD: float = 0.25  # only flatten at fv when |skew| exceeds this

    # ── Shared taking logic ───────────────────────────────────────────────

    def take_orders(self, fv: float):
        """
        Take any order book level that crosses fair value.
        Also flatten inventory at fair value when position is large enough.
        """
        for ask_price in sorted(self.asks):
            ask_vol = abs(self.asks[ask_price])
            if ask_price <= fv - 1:
                self._buy(ask_price, ask_vol, "take_buy")
            elif ask_price <= fv and self.position < 0 and abs(self.skew) > self.FLATTEN_THRESHOLD:
                qty = min(ask_vol, abs(self.position))
                self._buy(ask_price, qty, "flatten_buy")

        for bid_price in sorted(self.bids, reverse=True):
            bid_vol = abs(self.bids[bid_price])
            if bid_price >= fv + 1:
                self._sell(bid_price, bid_vol, "take_sell")
            elif bid_price >= fv and self.position > 0 and abs(self.skew) > self.FLATTEN_THRESHOLD:
                qty = min(bid_vol, self.position)
                self._sell(bid_price, qty, "flatten_sell")

    # ── Shared making logic ───────────────────────────────────────────────

    def make_orders(self, fv: float):
        """Post passive quotes around fair value with inventory skew."""
        if self.best_bid is None or self.best_ask is None:
            return
        bid_wall, ask_wall, wall_mid = self.bid_wall, self.ask_wall, self.wall_mid
        if bid_wall is None or ask_wall is None or wall_mid is None:
            return

        # Base quotes: overbid best bid / undercut best ask for queue priority
        my_bid = int(bid_wall + 1)
        my_ask = int(ask_wall - 1)

        for bp in sorted(self.bids, reverse=True):
            overbid = bp + 1
            if abs(self.bids[bp]) > 1 and overbid < wall_mid:
                my_bid = max(my_bid, overbid)
                break
            elif bp < wall_mid:
                my_bid = max(my_bid, bp)
                break

        for sp in sorted(self.asks):
            undercut = sp - 1
            if abs(self.asks[sp]) > 1 and undercut > wall_mid:
                my_ask = min(my_ask, undercut)
                break
            elif sp > wall_mid:
                my_ask = min(my_ask, sp)
                break

        # Inventory skew: shift quotes asymmetrically toward flattening position.
        # If long: push ask down (easier to sell) and bid down more (harder to buy more).
        # If short: push bid up (easier to buy) and ask up more (harder to sell more).
        if abs(self.skew) > self.SKEW_THRESHOLD:
            offset = round(self.skew * self.MAX_SKEW_OFFSET)
            my_bid -= offset * 2  # harder to add to position
            my_ask -= offset      # easier to reduce position

        # Enforce minimum edge from fair value
        my_bid = min(my_bid, int(fv - self.MIN_EDGE))
        my_ask = max(my_ask, int(fv + self.MIN_EDGE))

        self._buy(my_bid,  self.MAKE_SIZE, "mm_bid")
        self._sell(my_ask, self.MAKE_SIZE, "mm_ask")

        self.log("edge_bid", round(fv - my_bid, 1))
        self.log("edge_ask", round(my_ask - fv, 1))

    # ── Entry point ───────────────────────────────────────────────────────

    def get_orders(self) -> List[Order]:
        fv = self.fair_value()
        if fv is None:
            return self.orders
        self.take_orders(fv)
        self.make_orders(fv)
        return self.orders


# ═══════════════════════════════════════════════════════════════════════════
#  EMERALDS — Stable asset, fixed fair value at 10,000
# ═══════════════════════════════════════════════════════════════════════════

class EmeraldsTrader(ProductTrader):

    PRODUCT   = "EMERALDS"
    POS_LIMIT = 80
    FAIR_VALUE_FIXED = 10_000

    # Tighter edge is fine given the fixed fair value
    MIN_EDGE  = 1
    MAKE_SIZE = 40

    def fair_value(self) -> float:
        return self.FAIR_VALUE_FIXED


# ═══════════════════════════════════════════════════════════════════════════
#  TOMATOES — Dynamic asset, fair value tracks wall_mid
# ═══════════════════════════════════════════════════════════════════════════

class TomatoesTrader(ProductTrader):

    PRODUCT   = "TOMATOES"
    POS_LIMIT = 80

    MAKE_SIZE        = 20
    MIN_EDGE         = 3  # enforced in base make_orders against fair_value(); inner spread can compress to ~5 ticks so 3 covers the worst case
    SKEW_THRESHOLD   = 0.4
    MAX_SKEW_OFFSET  = 2

    EMA_FAST = 50
    EMA_SLOW = 200

    def __init__(self, state: TradingState, memory: dict) -> None:
        super().__init__(state)
        price = self.wall_mid if self.wall_mid is not None else 0.0

        prev_fast = memory.get("tom_ema_fast", price)
        prev_slow = memory.get("tom_ema_slow", price)

        k_fast = 2 / (self.EMA_FAST + 1)
        k_slow = 2 / (self.EMA_SLOW + 1)

        self.ema_fast = prev_fast + k_fast * (price - prev_fast)
        self.ema_slow = prev_slow + k_slow * (price - prev_slow)

        memory["tom_ema_fast"] = self.ema_fast
        memory["tom_ema_slow"] = self.ema_slow

        self.log("ema_fast", round(self.ema_fast, 2))
        self.log("ema_slow", round(self.ema_slow, 2))
        self.log("trend", "up" if self.ema_fast > self.ema_slow else "down")

    def fair_value(self) -> Optional[float]:
        if self.wall_mid is None:
            return None
        # Shift fair value in the direction of the trend by MAX_SKEW_OFFSET ticks
        # Uptrend: FV shifts up → ask harder to fill, bid easier (we want to buy)
        # Downtrend: FV shifts down → bid harder to fill, ask easier (we want to sell)
        trend_offset = self.MAX_SKEW_OFFSET if self.ema_fast > self.ema_slow else -self.MAX_SKEW_OFFSET
        return self.wall_mid + trend_offset


# ═══════════════════════════════════════════════════════════════════════════
#  PRODUCT REGISTRY & MAIN TRADER
# ═══════════════════════════════════════════════════════════════════════════

PRODUCT_TRADERS: dict[str, type[ProductTrader]] = {
    "EMERALDS": EmeraldsTrader,
    "TOMATOES": TomatoesTrader,
}


class Trader:

    def bid(self):
        return 15

    def run(self, state: TradingState):
        try:
            memory = jsonpickle.decode(state.traderData)
            if not isinstance(memory, dict):
                memory = {}
        except Exception:
            memory = {}

        result: dict[str, list[Order]] = {}

        for symbol, trader_cls in PRODUCT_TRADERS.items():
            if symbol in state.order_depths:
                try:
                    if symbol == "TOMATOES":
                        trader = TomatoesTrader(state, memory)
                    else:
                        trader = trader_cls(state)
                    result[symbol] = trader.get_orders()
                except Exception as e:
                    logger.print(f"[{symbol}] error: {e}")

        memory["last_timestamp"] = state.timestamp
        trader_data = jsonpickle.encode(memory) or ""

        logger.flush(state, result, 0, trader_data)
        return result, 0, trader_data