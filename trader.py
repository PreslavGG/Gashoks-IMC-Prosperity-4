import json
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
# ═══════════════════════════════════════════════════════════════════════════

class ProductTrader:

    # ── Must define in each subclass ──────────────────────────────────────
    PRODUCT: str
    POS_LIMIT: int

    def __init__(self, state: TradingState, memory) -> None:
        self.memory = memory
        self.state = state
        self.order_depth = state.order_depths[self.PRODUCT]
        self.position = state.position.get(self.PRODUCT, 0)

        # Order book — store ask volumes as POSITIVE (Hedgehogs pattern),
        # sorted explicitly (not guaranteed in live engine).
        self.bids: dict[int, int] = {
            bp: abs(bv) for bp, bv in sorted(
                self.order_depth.buy_orders.items(), key=lambda x: x[0], reverse=True
            )
        }
        self.asks: dict[int, int] = {
            sp: abs(sv) for sp, sv in sorted(
                self.order_depth.sell_orders.items(), key=lambda x: x[0]
            )
        }

        self.bid_vol = sum(self.bids.values())
        self.ask_vol = sum(self.asks.values())

        self.orders: List[Order] = []

        # Remaining capacity — decremented when we place orders
        self.max_buy  = self.POS_LIMIT - self.position
        self.max_sell = self.POS_LIMIT + self.position

        # Projected position after our orders are placed
        self.proj_position = self.position

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
        self.log("pos", self.position)
        if self.wall_mid is not None and self.bid_wall is not None and self.ask_wall is not None:
            self.log("wall_mid", round(self.wall_mid, 1))
            self.log("wall_spread", int(self.ask_wall - self.bid_wall))

    def _log_trades(self):
        own = self.state.own_trades.get(self.PRODUCT, [])
        mkt = self.state.market_trades.get(self.PRODUCT, [])

        if own:
            summary = [f"{t.price}x{t.quantity}({t.buyer}<<{t.seller})" for t in own]
            self.log("own_trades", f"n={len(own)} {summary}")

        if mkt:
            summary = [f"{t.price}x{t.quantity}({t.buyer}<<{t.seller})" for t in mkt]
            self.log("mkt_trades", f"n={len(mkt)} {summary}")

            # Cumulative bot activity tracker (buys, sells, volumes) per bot ID
            stats = self.memory.setdefault("bot_stats", {})
            for t in mkt:
                for bot_id, side in [(t.buyer, "buy"), (t.seller, "sell")]:
                    if not bot_id:
                        continue
                    entry = stats.setdefault(bot_id, {"buy_qty": 0, "sell_qty": 0, "buys": 0, "sells": 0})
                    entry[f"{side}s"] += 1
                    entry[f"{side}_qty"] += t.quantity

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
        return self.wall_mid
 

    # ── Order placement ───────────────────────────────────────────────────

    def _buy(self, price: int, qty: int, tag: str = "buy"):
        """Place a buy order, clamped to remaining buy capacity."""
        qty = max(0, min(int(qty), self.max_buy))
        if qty > 0:
            self.orders.append(Order(self.PRODUCT, int(price), qty))
            self.max_buy -= qty
            self.proj_position += qty
            self.log(tag, f"{qty}@{price}")

    def _sell(self, price: int, qty: int, tag: str = "sell"):
        """Place a sell order, clamped to remaining sell capacity."""
        qty = max(0, min(int(qty), self.max_sell))
        if qty > 0:
            self.orders.append(Order(self.PRODUCT, int(price), -qty))
            self.max_sell -= qty
            self.proj_position -= qty
            self.log(tag, f"{qty}@{price}")

    # ── Shared taking logic ───────────────────────────────────────────────
    # Hedgehogs pattern: take everything strictly through fair value,
    # flatten (cross at fair value itself) when holding opposite inventory.

    def take_orders(self, fv: float):
        for ask_price in list(self.asks):
            ask_vol = self.asks[ask_price]
            if ask_price <= fv - 1:
                self._buy(ask_price, ask_vol, "take_buy")
            elif ask_price <= fv and self.position < 0:
                self._buy(ask_price, min(ask_vol, -self.position), "flatten_buy")

        for bid_price in list(self.bids):
            bid_vol = self.bids[bid_price]
            if bid_price >= fv + 1:
                self._sell(bid_price, bid_vol, "take_sell")
            elif bid_price >= fv and self.position > 0:
                self._sell(bid_price, min(bid_vol, self.position), "flatten_sell")

    # ── Shared making logic ───────────────────────────────────────────────
    # Hedgehogs pattern: overbid/undercut inside the walls, full remaining
    # capacity per side. Capacity asymmetry naturally skews inventory.

    def make_orders(self, fv: float):
        if self.bid_wall is None or self.ask_wall is None or self.wall_mid is None:
            return

        # Base: one tick inside the walls
        my_bid = int(self.bid_wall + 1)
        my_ask = int(self.ask_wall - 1)

        # Overbid: step up behind the best bid that sits below wall_mid
        for bp, bv in self.bids.items():
            overbid = bp + 1
            if bv > 1 and overbid < self.wall_mid:
                my_bid = max(my_bid, overbid)
                break
            elif bp < self.wall_mid:
                my_bid = max(my_bid, bp)
                break

        # Undercut: step down in front of the best ask that sits above wall_mid
        for sp, sv in self.asks.items():
            undercut = sp - 1
            if sv > 1 and undercut > self.wall_mid:
                my_ask = min(my_ask, undercut)
                break
            elif sp > self.wall_mid:
                my_ask = min(my_ask, sp)
                break

        # Dump full remaining capacity on each side — no fixed MAKE_SIZE,
        # no MIN_EDGE clamp. Remaining capacity after takes encodes skew.
        self._buy(my_bid,  self.max_buy,  "mm_bid")
        self._sell(my_ask, self.max_sell, "mm_ask")

        self.log("quote", f"{my_bid}/{my_ask}")

    # ── Entry point ───────────────────────────────────────────────────────

    def get_orders(self) -> List[Order]:
        fv = self.fair_value()
        if fv is None or self.best_bid is None or self.best_ask is None:
            return self.orders
        self.take_orders(fv)
        self.make_orders(fv)
        return self.orders


# ═══════════════════════════════════════════════════════════════════════════
#  INTARIAN_PEPPER_ROOT — Stable growing asset
# ═══════════════════════════════════════════════════════════════════════════

class StableTrader(ProductTrader):
    PRODUCT   = "INTARIAN_PEPPER_ROOT"
    POS_LIMIT = 80

    def get_orders(self) -> List[Order]:
        if self.asks and self.max_buy > 0:
            cheapest = min(self.asks)
            vol = self.asks[cheapest]
            self._buy(cheapest, min(vol, self.max_buy), "take_cheap")
        
        # Rest the rest at best_bid
        if self.max_buy > 0 and self.best_bid is not None:
            self._buy(self.best_bid, self.max_buy, "rest")
        
        return self.orders

# ═══════════════════════════════════════════════════════════════════════════
#  ASH_COATED_OSMIUM — Pure market-making, flatten taking only
# ═══════════════════════════════════════════════════════════════════════════

class DynamicTrader(ProductTrader):
    PRODUCT   = "ASH_COATED_OSMIUM"
    POS_LIMIT = 80

    def fair_value(self) -> Optional[float]:
        return self.wall_mid
    
    def get_orders(self) -> List[Order]:
        fv = self.fair_value()
        if fv is None:
            return self.orders
        self.take_orders(fv)
        self.make_orders(fv)
        return self.orders

# ═══════════════════════════════════════════════════════════════════════════
#  PRODUCT REGISTRY & MAIN TRADER
# ═══════════════════════════════════════════════════════════════════════════

PRODUCT_TRADERS: dict[str, type[ProductTrader]] = {
    "INTARIAN_PEPPER_ROOT": StableTrader,
    "ASH_COATED_OSMIUM": DynamicTrader,
}


class Trader:

    def bid(self):
        return 15

    def run(self, state: TradingState):
        # Lightweight json state (replaces jsonpickle)
        try:
            memory = json.loads(state.traderData) if state.traderData else {}
            if not isinstance(memory, dict):
                memory = {}
        except Exception:
            memory = {}

        result: dict[str, list[Order]] = {}

        for symbol, trader_cls in PRODUCT_TRADERS.items():
            if symbol in state.order_depths:
                try:
                    trader = trader_cls(state, memory)
                    result[symbol] = trader.get_orders()
                except Exception as e:
                    logger.print(f"[{symbol}] error: {e}")

        memory["last_timestamp"] = state.timestamp
        try:
            trader_data = json.dumps(memory)
        except Exception:
            trader_data = ""

        logger.flush(state, result, 0, trader_data)
        return result, 0, trader_data