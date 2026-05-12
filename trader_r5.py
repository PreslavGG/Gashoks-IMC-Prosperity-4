from datamodel import OrderDepth, TradingState, Order
from typing import Dict, List
import json
import math


class ProductTrader:
    def __init__(self, product: str, position_limit: int = 10):
        self.product = product
        self.position_limit = position_limit

    def extract_book(self, od: OrderDepth):
        if not od.buy_orders or not od.sell_orders:
            return None

        buy_orders = dict(sorted(od.buy_orders.items(), key=lambda x: x[0], reverse=True))
        sell_orders = dict(sorted(od.sell_orders.items(), key=lambda x: x[0]))

        return {
            "buy_orders": {int(price): abs(int(volume)) for price, volume in buy_orders.items()},
            "sell_orders": {int(price): abs(int(volume)) for price, volume in sell_orders.items()},
            "best_bid": int(max(buy_orders.keys())),
            "best_ask": int(min(sell_orders.keys())),
            "bid_wall": int(min(buy_orders.keys())),
            "ask_wall": int(max(sell_orders.keys())),
        }

    def wall_mid(self, book: Dict) -> float:
        return (book["bid_wall"] + book["ask_wall"]) / 2.0

    def best_mid(self, book: Dict) -> float:
        return (book["best_bid"] + book["best_ask"]) / 2.0

    def fair_value(self, book: Dict, state: TradingState, trader_data: Dict) -> float:
        return self.wall_mid(book)

    def compress_orders(self, orders: List[Order]) -> List[Order]:
        merged: Dict[int, int] = {}
        for order in orders:
            if order.quantity == 0:
                continue
            merged[order.price] = merged.get(order.price, 0) + order.quantity

        out = []
        for price in sorted(merged.keys()):
            qty = merged[price]
            if qty != 0:
                out.append(Order(self.product, price, qty))
        return out

    def market_make(self, book: Dict, fair: float, position: int) -> List[Order]:
        orders: List[Order] = []

        best_bid = book["best_bid"]
        best_ask = book["best_ask"]

        max_buy = max(0, self.position_limit - position)
        max_sell = max(0, self.position_limit + position)

        # Hedgehogs-style flatten at fair
        if position > 0 and best_bid >= fair:
            qty = min(position, max_sell)
            if qty > 0:
                orders.append(Order(self.product, best_bid, -qty))
            return self.compress_orders(orders)

        if position < 0 and best_ask <= fair:
            qty = min(abs(position), max_buy)
            if qty > 0:
                orders.append(Order(self.product, best_ask, qty))
            return self.compress_orders(orders)

        # Take obvious edge
        for ask_price, ask_volume in book["sell_orders"].items():
            if max_buy <= 0:
                break
            if ask_price <= fair - 1:
                qty = min(ask_volume, max_buy)
                if qty > 0:
                    orders.append(Order(self.product, ask_price, qty))
                    max_buy -= qty
                    position += qty
            elif ask_price <= fair and position < 0:
                qty = min(ask_volume, max_buy, abs(position))
                if qty > 0:
                    orders.append(Order(self.product, ask_price, qty))
                    max_buy -= qty
                    position += qty

        for bid_price, bid_volume in book["buy_orders"].items():
            if max_sell <= 0:
                break
            if bid_price >= fair + 1:
                qty = min(bid_volume, max_sell)
                if qty > 0:
                    orders.append(Order(self.product, bid_price, -qty))
                    max_sell -= qty
                    position -= qty
            elif bid_price >= fair and position > 0:
                qty = min(bid_volume, max_sell, position)
                if qty > 0:
                    orders.append(Order(self.product, bid_price, -qty))
                    max_sell -= qty
                    position -= qty

        max_buy = max(0, self.position_limit - position)
        max_sell = max(0, self.position_limit + position)

        overbid = best_bid + 1
        undercut = best_ask - 1

        quote_bid = min(overbid, best_ask - 1, math.floor(fair - 1))
        quote_ask = max(undercut, best_bid + 1, math.ceil(fair + 1))

        if max_buy > 0 and quote_bid < best_ask:
            orders.append(Order(self.product, int(quote_bid), int(max_buy)))

        if max_sell > 0 and quote_ask > best_bid:
            orders.append(Order(self.product, int(quote_ask), int(-max_sell)))

        return self.compress_orders(orders)

    def get_orders(self, state: TradingState, trader_data: Dict) -> List[Order]:
        if self.product not in state.order_depths:
            return []

        book = self.extract_book(state.order_depths[self.product])
        if book is None:
            return []

        fair = self.fair_value(book, state, trader_data)
        position = state.position.get(self.product, 0)
        return self.market_make(book, fair, position)


class PairRelativeTrader(ProductTrader):
    """Pure MM with a peer book-pressure overlay. Bias own fair_value by
    the peer's (best_mid - wall_mid) signed by historical correlation.
    Adjustment is capped at OWN half-spread so the overlay can actually
    move quotes without crossing the touch.

    Rolling-correlation auto-disable was tested and lost -96k by damping
    the high-PnL Pebbles pair. The frozen magnitude is what's working in
    IMC's fill model; we use the simpler approach and rely on offline
    pair selection (manually drop pairs that bleed).
    """

    def __init__(
        self,
        product: str,
        peer: str,
        corr: float,
        position_limit: int = 10,
        overlay_strength: float = 1.0,
    ):
        super().__init__(product, position_limit)
        self.peer = peer
        self.corr = corr
        self.overlay_strength = overlay_strength

    def fair_value(self, book: Dict, state: TradingState, trader_data: Dict) -> float:
        base_fair = self.wall_mid(book)

        if self.peer not in state.order_depths:
            return base_fair
        peer_book = self.extract_book(state.order_depths[self.peer])
        if peer_book is None:
            return base_fair

        # Peer book pressure: positive when inner book leans bid-side.
        signal = self.best_mid(peer_book) - self.wall_mid(peer_book)
        adjustment = self.overlay_strength * self.corr * signal

        cap = max(1.0, (book["best_ask"] - book["best_bid"]) / 2.0)
        if adjustment > cap:
            adjustment = cap
        elif adjustment < -cap:
            adjustment = -cap

        return base_fair + adjustment


class PebblesTrader(ProductTrader):
    PEBBLES = [
        "PEBBLES_XS",
        "PEBBLES_S",
        "PEBBLES_M",
        "PEBBLES_L",
        "PEBBLES_XL",
    ]

    def fair_value(self, book: Dict, state: TradingState, trader_data: Dict) -> float:
        mids = {}
        for p in self.PEBBLES:
            if p not in state.order_depths:
                return self.wall_mid(book)
            other_book = self.extract_book(state.order_depths[p])
            if other_book is None:
                return self.wall_mid(book)
            mids[p] = self.wall_mid(other_book)

        return 50000.0 - sum(mids[p] for p in self.PEBBLES if p != self.product)

    def get_orders(self, state: TradingState, trader_data: Dict) -> List[Order]:
        if self.product not in state.order_depths:
            return []

        book = self.extract_book(state.order_depths[self.product])
        if book is None:
            return []

        fair = self.fair_value(book, state, trader_data)
        position = state.position.get(self.product, 0)
        best_mid = self.best_mid(book)
        residual = best_mid - fair

        return self.market_make_pebbles(book, fair, residual, position)

    def market_make_pebbles(self, book: Dict, fair: float, residual: float, position: int) -> List[Order]:
        orders: List[Order] = []

        best_bid = book["best_bid"]
        best_ask = book["best_ask"]

        max_buy = max(0, self.position_limit - position)
        max_sell = max(0, self.position_limit + position)

        # Flatten full inventory at fair
        if position > 0 and best_bid >= fair:
            qty = min(position, max_sell)
            if qty > 0:
                orders.append(Order(self.product, best_bid, -qty))
            return self.compress_orders(orders)

        if position < 0 and best_ask <= fair:
            qty = min(abs(position), max_buy)
            if qty > 0:
                orders.append(Order(self.product, best_ask, qty))
            return self.compress_orders(orders)

        # Take obvious edge first
        for ask_price, ask_volume in book["sell_orders"].items():
            if max_buy <= 0:
                break
            if ask_price <= fair - 1:
                qty = min(ask_volume, max_buy)
                if qty > 0:
                    orders.append(Order(self.product, ask_price, qty))
                    max_buy -= qty
                    position += qty
            elif ask_price <= fair and position < 0:
                qty = min(ask_volume, max_buy, abs(position))
                if qty > 0:
                    orders.append(Order(self.product, ask_price, qty))
                    max_buy -= qty
                    position += qty

        for bid_price, bid_volume in book["buy_orders"].items():
            if max_sell <= 0:
                break
            if bid_price >= fair + 1:
                qty = min(bid_volume, max_sell)
                if qty > 0:
                    orders.append(Order(self.product, bid_price, -qty))
                    max_sell -= qty
                    position -= qty
            elif bid_price >= fair and position > 0:
                qty = min(bid_volume, max_sell, position)
                if qty > 0:
                    orders.append(Order(self.product, bid_price, -qty))
                    max_sell -= qty
                    position -= qty

        max_buy = max(0, self.position_limit - position)
        max_sell = max(0, self.position_limit + position)

        shift = 0
        if residual > 0:
            shift = 1
        elif residual < 0:
            shift = -1

        overbid = best_bid + 1
        undercut = best_ask - 1

        base_bid = min(overbid, best_ask - 1, math.floor(fair - 1))
        base_ask = max(undercut, best_bid + 1, math.ceil(fair + 1))

        quote_bid = base_bid - shift
        quote_ask = base_ask - shift

        if max_buy > 0 and quote_bid < best_ask:
            orders.append(Order(self.product, int(quote_bid), int(max_buy)))

        if max_sell > 0 and quote_ask > best_bid:
            orders.append(Order(self.product, int(quote_ask), int(-max_sell)))

        return self.compress_orders(orders)


class SnackpackBasketTrader(PebblesTrader):
    """Mirrors the Pebbles basket strategy on Snackpacks. Difference from
    Pebbles: snackpack sum is *not* a hard 50000 invariant (it drifts
    daily ~50,041 -> 50,326 -> 50,296), so the anchor must be learned online
    via a rolling-window mean of the basket sum. Basket residual autocorr is
    -0.22 across all 3 days, i.e. there IS exploitable mean reversion in the
    sum even though individual product rel-offsets are near-random-walk.

    Opt-in: not wired into Trader.__init__ by default. To activate, set the
    snackpack branch in __init__ to use this class and add a call to
    Trader._update_snack_basket_state(state, trader_data) in run().
    """

    PEBBLES = [
        "SNACKPACK_CHOCOLATE",
        "SNACKPACK_VANILLA",
        "SNACKPACK_PISTACHIO",
        "SNACKPACK_STRAWBERRY",
        "SNACKPACK_RASPBERRY",
    ]

    def fair_value(self, book: Dict, state: TradingState, trader_data: Dict) -> float:
        snack_state = trader_data.get("snack_basket_state", {})
        sum_mean = snack_state.get("sum_mean")
        if sum_mean is None:
            return self.wall_mid(book)

        mids = {}
        for p in self.PEBBLES:
            if p == self.product:
                continue
            if p not in state.order_depths:
                return self.wall_mid(book)
            other_book = self.extract_book(state.order_depths[p])
            if other_book is None:
                return self.wall_mid(book)
            mids[p] = self.wall_mid(other_book)

        return sum_mean - sum(mids.values())


class SnackpackAnchorTrader(ProductTrader):
    SNACKS = [
        "SNACKPACK_CHOCOLATE",
        "SNACKPACK_VANILLA",
        "SNACKPACK_PISTACHIO",
        "SNACKPACK_STRAWBERRY",
        "SNACKPACK_RASPBERRY",
    ]

    def fair_value(self, book: Dict, state: TradingState, trader_data: Dict) -> float:
        snack_state = trader_data.get("snack_anchor_state", {})
        group_mean = snack_state.get("group_mean")
        rolling_means = snack_state.get("rolling_mean", {})
        n_obs = snack_state.get("rolling_n", {}).get(self.product, 0)

        if group_mean is None or n_obs < 2 or self.product not in rolling_means:
            return self.wall_mid(book)

        # Rolling-window mean of (mid/group_mean - 1) is our online estimate of
        # this product's equilibrium relative offset within the current session.
        return group_mean * (1.0 + rolling_means[self.product])

    def get_orders(self, state: TradingState, trader_data: Dict) -> List[Order]:
        if self.product not in state.order_depths:
            return []

        book = self.extract_book(state.order_depths[self.product])
        if book is None:
            return []

        fair = self.fair_value(book, state, trader_data)
        position = state.position.get(self.product, 0)

        snack_state = trader_data.get("snack_anchor_state", {})
        group_mean = snack_state.get("group_mean") or self.wall_mid(book)
        rel_std = snack_state.get("rolling_std", {}).get(self.product, 0.0)
        rel_slope = snack_state.get("rolling_slope", {}).get(self.product, 0.0)
        n_obs = snack_state.get("rolling_n", {}).get(self.product, 0)

        # Warm-up: not enough rolling-window data to make a decision.
        if n_obs < 4 or rel_std <= 0.0:
            return []

        # ---- Regime detection from data alone --------------------------------
        # |slope| / rolling_std measures how strong the recent directional
        # shift is relative to typical noise (both in rel-offset units, so the
        # ratio is dimensionless). Slope > 1 std => significant trend.
        slope_strength = abs(rel_slope) / rel_std

        if slope_strength > 1.0:
            # Trending regime. The data shows snackpack rel-offsets follow
            # near-random-walks with persistent drift, so fading them loses.
            # Trade *with* the slope: target a directional position scaled by
            # how far the trend exceeds noise (capped at position_limit when
            # slope >= 2 stds).
            scale = min(slope_strength - 1.0, 1.0)  # 0..1, no hardcoded knobs
            sign = 1 if rel_slope > 0 else -1
            target_position = int(round(sign * scale * self.position_limit))
            return self.move_toward_target(book, position, target_position)

        # Quiet regime: cross-sectional offsets stable enough to mean-revert.
        entry_threshold = max(1, int(math.ceil(rel_std * group_mean)))
        return self.mean_revert_take_only(book, fair, position, entry_threshold)

    def move_toward_target(
        self, book: Dict, position: int, target_position: int
    ) -> List[Order]:
        """Aggressive directional sizing — cross the spread to reach target."""
        orders: List[Order] = []
        delta = target_position - position
        max_buy = max(0, self.position_limit - position)
        max_sell = max(0, self.position_limit + position)

        if delta > 0:
            need = min(delta, max_buy)
            for ask_price in sorted(book["sell_orders"].keys()):
                if need <= 0:
                    break
                qty = min(book["sell_orders"][ask_price], need)
                if qty > 0:
                    orders.append(Order(self.product, ask_price, qty))
                    need -= qty
        elif delta < 0:
            need = min(-delta, max_sell)
            for bid_price in sorted(book["buy_orders"].keys(), reverse=True):
                if need <= 0:
                    break
                qty = min(book["buy_orders"][bid_price], need)
                if qty > 0:
                    orders.append(Order(self.product, bid_price, -qty))
                    need -= qty

        return self.compress_orders(orders)

    def mean_revert_take_only(self, book: Dict, fair: float, position: int, entry_threshold: int) -> List[Order]:
        orders: List[Order] = []

        best_bid = book["best_bid"]
        best_ask = book["best_ask"]

        max_buy = max(0, self.position_limit - position)
        max_sell = max(0, self.position_limit + position)

        # Flatten at fair
        if position > 0 and best_bid >= fair:
            qty = min(position, max_sell)
            if qty > 0:
                orders.append(Order(self.product, best_bid, -qty))
            return self.compress_orders(orders)

        if position < 0 and best_ask <= fair:
            qty = min(abs(position), max_buy)
            if qty > 0:
                orders.append(Order(self.product, best_ask, qty))
            return self.compress_orders(orders)

        # Mean-reversion taking only
        for ask_price, ask_volume in book["sell_orders"].items():
            if max_buy <= 0:
                break
            if ask_price <= fair - entry_threshold:
                qty = min(ask_volume, max_buy)
                if qty > 0:
                    orders.append(Order(self.product, ask_price, qty))
                    max_buy -= qty
                    position += qty

        for bid_price, bid_volume in book["buy_orders"].items():
            if max_sell <= 0:
                break
            if bid_price >= fair + entry_threshold:
                qty = min(bid_volume, max_sell)
                if qty > 0:
                    orders.append(Order(self.product, bid_price, -qty))
                    max_sell -= qty
                    position -= qty

        return self.compress_orders(orders)


class Trader:
    PEBBLES = [
        "PEBBLES_XS",
        "PEBBLES_S",
        "PEBBLES_M",
        "PEBBLES_L",
        "PEBBLES_XL",
    ]

    SNACKS = [
        "SNACKPACK_CHOCOLATE",
        "SNACKPACK_VANILLA",
        "SNACKPACK_PISTACHIO",
        "SNACKPACK_STRAWBERRY",
        "SNACKPACK_RASPBERRY",
    ]

    ALL_PRODUCTS = [
        "GALAXY_SOUNDS_DARK_MATTER",
        "GALAXY_SOUNDS_BLACK_HOLES",
        "GALAXY_SOUNDS_PLANETARY_RINGS",
        "GALAXY_SOUNDS_SOLAR_WINDS",
        "GALAXY_SOUNDS_SOLAR_FLAMES",
        "SLEEP_POD_SUEDE",
        "SLEEP_POD_LAMB_WOOL",
        "SLEEP_POD_POLYESTER",
        "SLEEP_POD_NYLON",
        "SLEEP_POD_COTTON",
        "MICROCHIP_CIRCLE",
        "MICROCHIP_OVAL",
        "MICROCHIP_SQUARE",
        "MICROCHIP_RECTANGLE",
        "MICROCHIP_TRIANGLE",
        "PEBBLES_XS",
        "PEBBLES_S",
        "PEBBLES_M",
        "PEBBLES_L",
        "PEBBLES_XL",
        "ROBOT_VACUUMING",
        "ROBOT_MOPPING",
        "ROBOT_DISHES",
        "ROBOT_LAUNDRY",
        "ROBOT_IRONING",
        "UV_VISOR_YELLOW",
        "UV_VISOR_AMBER",
        "UV_VISOR_ORANGE",
        "UV_VISOR_RED",
        "UV_VISOR_MAGENTA",
        "TRANSLATOR_SPACE_GRAY",
        "TRANSLATOR_ASTRO_BLACK",
        "TRANSLATOR_ECLIPSE_CHARCOAL",
        "TRANSLATOR_GRAPHITE_MIST",
        "TRANSLATOR_VOID_BLUE",
        "PANEL_1X2",
        "PANEL_2X2",
        "PANEL_1X4",
        "PANEL_2X4",
        "PANEL_4X4",
        "OXYGEN_SHAKE_MORNING_BREATH",
        "OXYGEN_SHAKE_EVENING_BREATH",
        "OXYGEN_SHAKE_MINT",
        "OXYGEN_SHAKE_CHOCOLATE",
        "OXYGEN_SHAKE_GARLIC",
        "SNACKPACK_CHOCOLATE",
        "SNACKPACK_VANILLA",
        "SNACKPACK_PISTACHIO",
        "SNACKPACK_STRAWBERRY",
        "SNACKPACK_RASPBERRY",
    ]

    # Pairs for PairRelativeTrader. The 8-pair config below was the best
    # IMC-backtested setup (+564,726). Diff-correlation analysis says only
    # SNACK_CHOC/VAN (-0.92) and PEBBLES_XL/XS (-0.50) have real tick-level
    # co-movement; the others have ~0 diff-corr but are net-positive in the
    # IMC backtester anyway (likely capturing book-microstructure rather than
    # peer-prediction). Kept all 8 pending rolling-corr auto-disable logic.
    PAIRS = [
        ("SNACKPACK_CHOCOLATE",       "SNACKPACK_VANILLA",         -0.925873),
        ("MICROCHIP_RECTANGLE",       "MICROCHIP_SQUARE",          -0.882298),
        ("MICROCHIP_OVAL",            "MICROCHIP_TRIANGLE",         0.870459),
        ("SLEEP_POD_COTTON",          "SLEEP_POD_POLYESTER",        0.875237),
        ("UV_VISOR_AMBER",            "UV_VISOR_MAGENTA",          -0.867276),
        ("ROBOT_IRONING",             "ROBOT_MOPPING",             -0.815181),
        ("PEBBLES_XL",                "PEBBLES_XS",                -0.827570),
        ("GALAXY_SOUNDS_BLACK_HOLES", "OXYGEN_SHAKE_GARLIC",        0.885000),
    ]

    def __init__(self):
        self.traders: Dict[str, ProductTrader] = {}

        # Build pair lookup (symmetric: A->B and B->A both registered).
        pair_lookup: Dict[str, tuple] = {}
        for a, b, corr in self.PAIRS:
            pair_lookup[a] = (b, corr)
            pair_lookup[b] = (a, corr)

        # Pure market-making baseline for everything, with a small peer-pressure
        # overlay on the |corr| pairs. Snackpacks DO have a real statistical
        # mean-reverting basket residual (diff-corr -0.226, half-life 46 ticks)
        # but every directional snackpack strategy I've tried lost catastrophic
        # PnL in the IMC fill model — the strategy builds inventory against
        # intraday drift faster than the residual mean-reverts. Plain MM stays
        # for snacks; SnackpackBasketTrader / PebblesTrader / SnackpackAnchor
        # remain defined but NOT activated.
        for product in self.ALL_PRODUCTS:
            if product in pair_lookup:
                peer, corr = pair_lookup[product]
                self.traders[product] = PairRelativeTrader(
                    product=product,
                    peer=peer,
                    corr=corr,
                    position_limit=10,
                    overlay_strength=1.0,
                )
            else:
                self.traders[product] = ProductTrader(product, position_limit=10)

    def bid(self):
        return 15

    def run(self, state: TradingState):
        trader_data = self._load_state(state.traderData)

        result: Dict[str, List[Order]] = {product: [] for product in state.order_depths}
        conversions = 0

        for product, trader in self.traders.items():
            if product not in state.order_depths:
                continue
            result[product].extend(trader.get_orders(state, trader_data))

        return result, conversions, self._dump_state(trader_data)

    # Rolling window length for online estimates of equilibrium and noise.
    # Only structural parameter — no trading constants are hardcoded.
    SNACK_ROLLING_WINDOW = 200

    # Window for the SnackpackBasketTrader's online sum_mean. Same idea as
    # Pebbles' hardcoded 50000 invariant, but learned online because the
    # snackpack sum drifts daily.
    SNACK_BASKET_WINDOW = 200

    def _update_snack_basket_state(self, state: TradingState, trader_data: Dict) -> Dict:
        """Online rolling-mean of the snackpack basket sum. Reset per day so
        each session learns its own anchor. Call from run() ONLY when
        SnackpackBasketTrader is wired into __init__."""
        if "snack_basket_state" not in trader_data:
            trader_data["snack_basket_state"] = {}
        snack_state = trader_data["snack_basket_state"]

        last_ts = trader_data.get("last_timestamp")
        new_day = last_ts is None or state.timestamp < last_ts
        if new_day:
            snack_state["sum_window"] = []
            snack_state["sum_mean"] = None

        mids: Dict[str, float] = {}
        for p in self.SNACKS:
            od = state.order_depths.get(p)
            if od is None or not od.buy_orders or not od.sell_orders:
                continue
            mids[p] = (max(od.buy_orders.keys()) + min(od.sell_orders.keys())) / 2.0

        if len(mids) == len(self.SNACKS):
            cur_sum = sum(mids.values())
            window = snack_state.setdefault("sum_window", [])
            window.append(cur_sum)
            if len(window) > self.SNACK_BASKET_WINDOW:
                del window[: len(window) - self.SNACK_BASKET_WINDOW]
            if len(window) >= 2:
                snack_state["sum_mean"] = sum(window) / len(window)

        trader_data["last_timestamp"] = state.timestamp
        return trader_data

    def _update_snack_anchor_state(self, state: TradingState, trader_data: Dict) -> Dict:
        if "snack_anchor_state" not in trader_data:
            trader_data["snack_anchor_state"] = {}

        snack_state = trader_data["snack_anchor_state"]
        last_timestamp = trader_data.get("last_timestamp")

        new_day = last_timestamp is None or state.timestamp < last_timestamp
        if new_day:
            # Reset rolling windows so each day learns its own regime fresh.
            snack_state["rel_offset_window"] = {p: [] for p in self.SNACKS}
            snack_state["rolling_mean"] = {p: 0.0 for p in self.SNACKS}
            snack_state["rolling_std"] = {p: 0.0 for p in self.SNACKS}
            snack_state["rolling_slope"] = {p: 0.0 for p in self.SNACKS}
            snack_state["rolling_n"] = {p: 0 for p in self.SNACKS}

        # Pull current mid_prices for every snackpack (need the full set to
        # build a clean cross-sectional group mean).
        mids: Dict[str, float] = {}
        for product in self.SNACKS:
            od = state.order_depths.get(product)
            if od is None or not od.buy_orders or not od.sell_orders:
                continue
            best_bid = max(od.buy_orders.keys())
            best_ask = min(od.sell_orders.keys())
            mids[product] = (best_bid + best_ask) / 2.0

        if len(mids) == len(self.SNACKS):
            group_mean = sum(mids.values()) / len(self.SNACKS)
            snack_state["group_mean"] = group_mean

            windows = snack_state.setdefault(
                "rel_offset_window", {p: [] for p in self.SNACKS}
            )
            rolling_means: Dict[str, float] = {}
            rolling_stds: Dict[str, float] = {}
            rolling_slopes: Dict[str, float] = {}
            rolling_n: Dict[str, int] = {}

            for p in self.SNACKS:
                # Relative offset of this product vs the cross-sectional group
                # mean — strips out common drift, keeps idiosyncratic signal.
                rel_offset = mids[p] / group_mean - 1.0

                window = windows.setdefault(p, [])
                window.append(rel_offset)
                if len(window) > self.SNACK_ROLLING_WINDOW:
                    del window[: len(window) - self.SNACK_ROLLING_WINDOW]

                n = len(window)
                rolling_n[p] = n
                if n >= 2:
                    m = sum(window) / n
                    var = sum((x - m) * (x - m) for x in window) / n
                    rolling_means[p] = m
                    rolling_stds[p] = math.sqrt(var)
                else:
                    rolling_means[p] = rel_offset
                    rolling_stds[p] = 0.0

                # Slope = (mean of latter half) - (mean of former half), in
                # rel-offset units. Same units as rolling_std so they're
                # directly comparable for "is the trend significant?" tests.
                if n >= 4:
                    h = n // 2
                    first_avg = sum(window[:h]) / h
                    last_avg = sum(window[h:]) / (n - h)
                    rolling_slopes[p] = last_avg - first_avg
                else:
                    rolling_slopes[p] = 0.0

            snack_state["rolling_mean"] = rolling_means
            snack_state["rolling_std"] = rolling_stds
            snack_state["rolling_slope"] = rolling_slopes
            snack_state["rolling_n"] = rolling_n

        trader_data["last_timestamp"] = state.timestamp
        return trader_data

    def _load_state(self, trader_data: str) -> Dict:
        if not trader_data:
            return {}
        try:
            obj = json.loads(trader_data)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    def _dump_state(self, state_obj: Dict) -> str:
        try:
            return json.dumps(state_obj, separators=(",", ":"))
        except Exception:
            return "{}"