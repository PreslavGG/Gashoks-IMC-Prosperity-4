import json
import jsonpickle
from datamodel import OrderDepth, UserId, TradingState, Order
from typing import List, Dict


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
        self.bids = self.order_depth.buy_orders    # {price: +volume}
        self.asks = self.order_depth.sell_orders    # {price: -volume}
        self.orders: List[Order] = []
        self.best_bid, self.best_ask = self.get_best_bid_ask()
        self.bid_wall, self.ask_wall, self.wall_mid = self.get_walls()
        self.max_buy = self.POS_LIMIT - self.position
        self.max_sell = self.POS_LIMIT + self.position
        self.skew = self.position / self.POS_LIMIT if self.POS_LIMIT else 0
        self.log("pos", self.position)
        self.log("skew", round(self.skew, 2))

    def log(self, kind: str, message):
        logger.log(self.product, kind, message)

    def get_best_bid_ask(self):
        best_bid = max(self.bids.keys()) if self.bids else None
        best_ask = min(self.asks.keys()) if self.asks else None
        return best_bid, best_ask

    def get_walls(self):
        """
        The 'wall' is the deepest (outermost) price level in the order book,
        where the market-maker bots post large resting volume.
        Wall mid = (bid_wall + ask_wall) / 2 is our fair value proxy.
        """
        bid_wall = min(self.bids.keys()) if self.bids else None
        ask_wall = max(self.asks.keys()) if self.asks else None
        wall_mid = None
        if bid_wall is not None and ask_wall is not None:
            wall_mid = (bid_wall + ask_wall) / 2
        return bid_wall, ask_wall, wall_mid

    def get_orders(self) -> List[Order]:
        raise NotImplementedError


# ═══════════════════════════════════════════════════════════════════════════
#  EMERALDS — Static asset pinned at 10,000
#  Strategy: Frankfurt Hedgehogs StaticTrader style
#  - Take anything crossing the known fair value (wall_mid)
#  - Flatten inventory at wall_mid (0-edge but reduces risk)
#  - Post passive quotes inside the walls with overbidding/undercutting
# ═══════════════════════════════════════════════════════════════════════════

class EmeraldsTrader(ProductTrader):

    PRODUCT = "EMERALDS"
    POS_LIMIT = 80

    def __init__(self, state: TradingState) -> None:
        super().__init__(state, self.PRODUCT)

    def get_orders(self) -> List[Order]:
        if self.wall_mid is None or self.bid_wall is None or self.ask_wall is None:
            return self.orders

        # ── 1. TAKING ──────────────────────────────────────────────────────
        for ask_price in sorted(self.asks.keys()):
            ask_vol = abs(self.asks[ask_price])
            if ask_price <= self.wall_mid - 1 and self.max_buy > 0:
                qty = min(ask_vol, self.max_buy)
                self.orders.append(Order(self.product, ask_price, qty))
                self.max_buy -= qty
            elif ask_price <= self.wall_mid and self.position < 0:
                qty = min(ask_vol, abs(self.position), self.max_buy)
                if qty > 0:
                    self.orders.append(Order(self.product, ask_price, qty))
                    self.max_buy -= qty

        for bid_price in sorted(self.bids.keys(), reverse=True):
            bid_vol = abs(self.bids[bid_price])
            if bid_price >= self.wall_mid + 1 and self.max_sell > 0:
                qty = min(bid_vol, self.max_sell)
                self.orders.append(Order(self.product, bid_price, -qty))
                self.max_sell -= qty
            elif bid_price >= self.wall_mid and self.position > 0:
                qty = min(bid_vol, self.position, self.max_sell)
                if qty > 0:
                    self.orders.append(Order(self.product, bid_price, -qty))
                    self.max_sell -= qty

        # ── 2. MAKING ─────────────────────────────────────────────────────
        bid_price = int(self.bid_wall + 1)
        ask_price = int(self.ask_wall - 1)

        for bp in sorted(self.bids.keys(), reverse=True):
            bv = abs(self.bids[bp])
            overbid = bp + 1
            if bv > 1 and overbid < self.wall_mid:
                bid_price = max(bid_price, overbid)
                break
            elif bp < self.wall_mid:
                bid_price = max(bid_price, bp)
                break

        for sp in sorted(self.asks.keys()):
            sv = abs(self.asks[sp])
            undercut = sp - 1
            if sv > 1 and undercut > self.wall_mid:
                ask_price = min(ask_price, undercut)
                break
            elif sp > self.wall_mid:
                ask_price = min(ask_price, sp)
                break

        if self.max_buy > 0:
            self.orders.append(Order(self.product, bid_price, self.max_buy))
        if self.max_sell > 0:
            self.orders.append(Order(self.product, ask_price, -self.max_sell))

        self.log("wall_mid", round(self.wall_mid, 1))
        self.log("mm_bid", bid_price)
        self.log("mm_ask", ask_price)

        return self.orders


# ═══════════════════════════════════════════════════════════════════════════
#  TOMATOES — Dynamic asset (random walk with drift + mean-reverting ticks)
#
#  Analysis summary:
#    - Wall spread: consistently 16 ticks (walls at level 2, ~20 lots each)
#    - Inner spread: 13-14 ticks (level 1, ~7 lots, sits 1 tick inside walls)
#    - Wall mid drifts like a random walk (no trend to exploit)
#    - Tick returns autocorrelation: -0.21 (mean reversion at tick level)
#    - Trade quantities: 2-5 (uniform), no informed-bot signature found
#
#  Strategy (adapted from Frankfurt Hedgehogs' Kelp + Static approaches):
#    1. TAKING: Frankfurt-style — take anything crossing wall_mid +/- 1,
#       flatten inventory at wall_mid (position-aware)
#    2. MAKING: Post passive quotes inside the spread, using overbidding/
#       undercutting with inventory skew to manage drift risk.
#       Unlike Emeralds, we can't assume a fixed fair value, so all quotes
#       are anchored to wall_mid which moves each tick.
#
#  Backtested PnL (Round 0):
#    Day -2: ~9,900   Day -1: ~6,300   Total: ~16,200
#    Combined with Emeralds: ~31,100  (Sharpe ~7.1)
# ═══════════════════════════════════════════════════════════════════════════

class TomatoesTrader(ProductTrader):

    PRODUCT = "TOMATOES"
    POS_LIMIT = 80

    # ── Tunable parameters ─────────────────────────────────────────────
    SKEW_THRESHOLD = 0.4     # start skewing quotes at 40% of limit (pos ~32)
    AGGR_THRESHOLD = 0.75    # aggressively flush at 75% (pos ~60)
    MAX_SKEW_OFFSET = 2      # max ticks to shift quotes for inventory
    MAKE_SIZE = 20           # size per passive quote side
    MIN_EDGE = 3             # minimum ticks of edge from wall_mid to post

    def __init__(self, state: TradingState) -> None:
        super().__init__(state, self.PRODUCT)

    def get_orders(self) -> List[Order]:
        if self.wall_mid is None or self.bid_wall is None or self.ask_wall is None:
            return self.orders

        self.log("wall_mid", round(self.wall_mid, 1))
        self.log("wall_spread", int(self.ask_wall - self.bid_wall))

        # ══════════════════════════════════════════════════════════════════
        #  1. MARKET TAKING  (Frankfurt Hedgehogs style)
        #
        #  wall_mid is our fair value proxy. Any order book level that
        #  crosses wall_mid +/- 1 is free edge — take it immediately.
        #  If we have inventory, flatten at wall_mid (0 edge, risk reduction).
        # ══════════════════════════════════════════════════════════════════

        for ask_price in sorted(self.asks.keys()):
            ask_vol = abs(self.asks[ask_price])
            if ask_price <= self.wall_mid - 1 and self.max_buy > 0:
                qty = min(ask_vol, self.max_buy)
                self.log("take_buy", f"{qty}@{ask_price}")
                self.orders.append(Order(self.product, ask_price, qty))
                self.max_buy -= qty
            elif ask_price <= self.wall_mid and self.position < 0:
                qty = min(ask_vol, abs(self.position), self.max_buy)
                if qty > 0:
                    self.log("flatten_buy", f"{qty}@{ask_price}")
                    self.orders.append(Order(self.product, ask_price, qty))
                    self.max_buy -= qty

        for bid_price in sorted(self.bids.keys(), reverse=True):
            bid_vol = abs(self.bids[bid_price])
            if bid_price >= self.wall_mid + 1 and self.max_sell > 0:
                qty = min(bid_vol, self.max_sell)
                self.log("take_sell", f"{qty}@{bid_price}")
                self.orders.append(Order(self.product, bid_price, -qty))
                self.max_sell -= qty
            elif bid_price >= self.wall_mid and self.position > 0:
                qty = min(bid_vol, self.position, self.max_sell)
                if qty > 0:
                    self.log("flatten_sell", f"{qty}@{bid_price}")
                    self.orders.append(Order(self.product, bid_price, -qty))
                    self.max_sell -= qty

        # ══════════════════════════════════════════════════════════════════
        #  2. MARKET MAKING
        #
        #  The Tomatoes book typically has:
        #    ask_wall (level 2)  ~20 lots
        #    best_ask (level 1)  ~7 lots     ← 1 tick inside wall
        #    ──── ~13 tick inner spread ────
        #    best_bid (level 1)  ~7 lots     ← 1 tick inside wall
        #    bid_wall (level 2)  ~20 lots
        #
        #  We overbid/undercut to get queue priority, then apply inventory
        #  skew to lean toward flattening. MIN_EDGE=3 ensures we always
        #  have at least 3 ticks of edge from the fair value proxy.
        # ══════════════════════════════════════════════════════════════════

        if self.best_bid is None or self.best_ask is None:
            return self.orders

        # ── Aggressive inventory flush ────────────────────────────────
        if abs(self.skew) > self.AGGR_THRESHOLD:
            if self.skew > 0 and self.max_sell > 0:
                qty = min(self.MAKE_SIZE, self.max_sell)
                self.log("aggr_sell", f"{qty}@{self.best_bid}")
                self.orders.append(Order(self.product, self.best_bid, -qty))
                self.max_sell -= qty
            elif self.skew < 0 and self.max_buy > 0:
                qty = min(self.MAKE_SIZE, self.max_buy)
                self.log("aggr_buy", f"{qty}@{self.best_ask}")
                self.orders.append(Order(self.product, self.best_ask, qty))
                self.max_buy -= qty
            return self.orders

        # ── Compute inventory skew offset ────────────────────────────
        if abs(self.skew) > self.SKEW_THRESHOLD:
            skew_offset = round(self.skew * self.MAX_SKEW_OFFSET)
        else:
            skew_offset = 0

        # ── Base quote prices: overbid / undercut ────────────────────
        base_bid = int(self.bid_wall + 1)
        base_ask = int(self.ask_wall - 1)

        for bp in sorted(self.bids.keys(), reverse=True):
            bv = abs(self.bids[bp])
            overbid = bp + 1
            if bv > 1 and overbid < self.wall_mid:
                base_bid = max(base_bid, overbid)
                break
            elif bp < self.wall_mid:
                base_bid = max(base_bid, bp)
                break

        for sp in sorted(self.asks.keys()):
            sv = abs(self.asks[sp])
            undercut = sp - 1
            if sv > 1 and undercut > self.wall_mid:
                base_ask = min(base_ask, undercut)
                break
            elif sp > self.wall_mid:
                base_ask = min(base_ask, sp)
                break

        # ── Apply skew offset ────────────────────────────────────────
        my_bid = base_bid - skew_offset
        my_ask = base_ask - skew_offset

        # ── Safety: enforce minimum edge from wall_mid ───────────────
        max_bid = int(self.wall_mid - self.MIN_EDGE)
        min_ask = int(self.wall_mid + self.MIN_EDGE)
        if self.wall_mid % 1 == 0.5:
            max_bid = int(self.wall_mid - 0.5) - self.MIN_EDGE + 1
            min_ask = int(self.wall_mid + 0.5) + self.MIN_EDGE - 1

        my_bid = min(my_bid, max_bid)
        my_ask = max(my_ask, min_ask)

        # Ensure we never cross our own quotes
        if my_bid >= my_ask:
            my_bid = int(self.wall_mid) - 1
            my_ask = int(self.wall_mid) + 1
            if my_bid >= my_ask:
                my_ask = my_bid + 2

        # ── Post passive quotes ──────────────────────────────────────
        if self.max_buy > 0:
            qty = min(self.MAKE_SIZE, self.max_buy)
            self.log("mm_bid", f"{qty}@{my_bid}")
            self.orders.append(Order(self.product, my_bid, qty))

        if self.max_sell > 0:
            qty = min(self.MAKE_SIZE, self.max_sell)
            self.log("mm_ask", f"{qty}@{my_ask}")
            self.orders.append(Order(self.product, my_ask, -qty))

        self.log("edge_bid", round(self.wall_mid - my_bid, 1))
        self.log("edge_ask", round(my_ask - self.wall_mid, 1))

        return self.orders


# ═══════════════════════════════════════════════════════════════════════════
#  PRODUCT REGISTRY & MAIN TRADER
# ═══════════════════════════════════════════════════════════════════════════

PRODUCT_TRADERS = {
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