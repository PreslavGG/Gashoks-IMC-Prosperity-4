from typing import Any, List
import json
import math
from datamodel import Listing, Observation, Order, OrderDepth, ProsperityEncoder, Symbol, Trade, TradingState


# ─────────────────────────────────────────────────────────────────────────────
#  BS parameterized by u = σ * sqrt(T)
#  d1 = log(S/K)/u + u/2;  call = S*N(d1) - K*N(d1 - u)
#  We never store σ or T separately — we calibrate u from the live option chain.
# ─────────────────────────────────────────────────────────────────────────────


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call_u(S, K, u):
    """Call price using u = σ√T directly. Robust to u=0 (returns intrinsic)."""
    if S <= 0 or u <= 1e-9:
        return max(S - K, 0.0)
    log_sk = math.log(S / K)
    d1 = log_sk / u + 0.5 * u
    d2 = d1 - u
    return S * _norm_cdf(d1) - K * _norm_cdf(d2)


def bs_delta_u(S, K, u):
    if S <= 0 or u <= 1e-9:
        return 1.0 if S > K else 0.0
    log_sk = math.log(S / K)
    d1 = log_sk / u + 0.5 * u
    return _norm_cdf(d1)


def implied_u(C, S, K, lo=1e-4, hi=2.0, max_iter=40):
    """Bisection: solve bs_call_u(S, K, u) = C for u. Returns None if no fit."""
    intrinsic = max(S - K, 0.0)
    if C < intrinsic - 0.01 or C > S:
        return None
    f_lo = bs_call_u(S, K, lo) - C
    f_hi = bs_call_u(S, K, hi) - C
    if f_lo * f_hi > 0:
        return None
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        f_mid = bs_call_u(S, K, mid) - C
        if abs(f_mid) < 1e-4 or (hi - lo) < 1e-6:
            return mid
        if f_mid * f_lo < 0:
            hi = mid
            f_hi = f_mid
        else:
            lo = mid
            f_lo = f_mid
    return 0.5 * (lo + hi)


# ─────────────────────────────────────────────────────────────────────────────
#  Trader
# ─────────────────────────────────────────────────────────────────────────────


class Logger:
    def __init__(self) -> None:
        self.logs = ""
    def print(self, *objects: Any, sep: str = " ", end: str = "\n") -> None:
        self.logs += sep.join(map(str, objects)) + end
    def flush(self, state, orders, conversions, trader_data):
        pass


logger = Logger()


class Trader:
    def run(self, state: TradingState):
        if state.traderData == "":
            data = {}
        else:
            try:
                data = json.loads(state.traderData)
            except json.JSONDecodeError:
                data = {}

        data.setdefault("ema", {})
        data.setdefault("ema_fast", {})
        data.setdefault("basis_ema", {})
        data.setdefault("last_mid", {})
        data.setdefault("pair_ema", 0)
        data.setdefault("id_sig", {})
        data.setdefault("imb_pos_streak", 0)
        data.setdefault("imb_neg_streak", 0)
        data.setdefault("m22_hyd_sig", 0.0)
        data.setdefault("vex_ema", 0.0)
        data.setdefault("vex_ema_init", False)
        data.setdefault("u_ema", 0.0)             # calibrated total vol u = σ√T
        data.setdefault("u_ema_init", False)

        for k in list(data["id_sig"].keys()):
            data["id_sig"][k] *= 0.80
            if abs(data["id_sig"][k]) < 0.35:
                del data["id_sig"][k]

        def add_sig(prod: str, val: float):
            data["id_sig"][prod] = data["id_sig"].get(prod, 0.0) + val

        # ─── Mark 22 hydrogel slow-decay signal ───────────────────────────
        data["m22_hyd_sig"] *= 0.985
        if abs(data["m22_hyd_sig"]) < 0.5:
            data["m22_hyd_sig"] = 0.0
        for t in state.market_trades.get("HYDROGEL_PACK", []):
            q = abs(t.quantity)
            if q > 4:
                continue
            if t.buyer == "Mark 22":
                data["m22_hyd_sig"] -= 8.0
            elif t.seller == "Mark 22":
                data["m22_hyd_sig"] += 4.0
        data["m22_hyd_sig"] = max(-25.0, min(25.0, data["m22_hyd_sig"]))

        # ─── Counterparty flow signal ─────────────────────────────────────
        for product, trades in state.market_trades.items():
            for t in trades:
                q = abs(t.quantity)
                if product == 'HYDROGEL_PACK':
                    if t.buyer == 'Mark 14': add_sig(product, +0.22 * q)
                    if t.seller == 'Mark 14': add_sig(product, -0.22 * q)
                    if t.buyer == 'Mark 38': add_sig(product, -0.22 * q)
                    if t.seller == 'Mark 38': add_sig(product, +0.22 * q)
                elif product == 'VELVETFRUIT_EXTRACT':
                    if t.buyer == 'Mark 01': add_sig(product, +0.16 * q)
                    if t.seller == 'Mark 01': add_sig(product, -0.16 * q)
                    if t.buyer == 'Mark 14': add_sig(product, +0.14 * q)
                    if t.seller == 'Mark 14': add_sig(product, -0.14 * q)
                    if t.buyer == 'Mark 67': add_sig(product, +0.22 * q)
                    if t.buyer == 'Mark 55': add_sig(product, -0.18 * q)
                    if t.seller == 'Mark 55': add_sig(product, +0.18 * q)
                    if t.seller == 'Mark 49': add_sig(product, +0.12 * q)
                elif product == 'VEV_4000':
                    if t.buyer == 'Mark 14': add_sig(product, +0.20 * q)
                    if t.seller == 'Mark 14': add_sig(product, -0.20 * q)
                    if t.buyer == 'Mark 38': add_sig(product, -0.20 * q)
                    if t.seller == 'Mark 38': add_sig(product, +0.20 * q)
                elif product == 'VEV_5200':
                    if t.buyer == 'Mark 14': add_sig(product, +0.12 * q)
                    if t.seller == 'Mark 22': add_sig(product, +0.10 * q)
                elif product == 'VEV_5300':
                    if t.buyer == 'Mark 01': add_sig(product, +0.10 * q)
                    if t.buyer == 'Mark 14': add_sig(product, +0.08 * q)
                    if t.seller == 'Mark 22': add_sig(product, +0.10 * q)

        result = {}

        CONFIG = {
            'VEV_4000':            {'LIMIT': 300, 'MEAN': 1250, 'EDGE': 4, 'THRESH': 10, 'TAKE': 80, 'ALPHA': 0.01,  'WIDE': 10, 'IMB_W': 0.40, 'STRIKE': 4000},
            'VEV_4500':            {'LIMIT': 300, 'MEAN': 750,  'EDGE': 3, 'THRESH': 16, 'TAKE': 80, 'ALPHA': 0.01,  'WIDE': 8,  'IMB_W': 0.20, 'STRIKE': 4500},
            'VEV_5000':            {'LIMIT': 300, 'MEAN': 255,  'EDGE': 1, 'THRESH': 9,  'TAKE': 100,'ALPHA': 0.015, 'WIDE': 4,  'IMB_W': 0.05, 'STRIKE': 5000},
            'VEV_5100':            {'LIMIT': 300, 'MEAN': 167,  'EDGE': 1, 'THRESH': 9,  'TAKE': 100,'ALPHA': 0.01,  'WIDE': 3,  'IMB_W': 0.05, 'STRIKE': 5100},
            'VEV_5200':            {'LIMIT': 300, 'MEAN': 96,   'EDGE': 1, 'THRESH': 6,  'TAKE': 80, 'ALPHA': 0.01,  'WIDE': 2,  'IMB_W': 0.05, 'STRIKE': 5200},
            'VEV_5300':            {'LIMIT': 300, 'MEAN': 47,   'EDGE': 1, 'THRESH': 3,  'TAKE': 80, 'ALPHA': 0.01,  'WIDE': 2,  'IMB_W': 0.05, 'STRIKE': 5300},
            'VEV_5400':            {'LIMIT': 300, 'MEAN': 16,   'EDGE': 1, 'THRESH': 3,  'TAKE': 30, 'ALPHA': 0.01,  'WIDE': 2,  'IMB_W': 0.05, 'STRIKE': 5400},
            # 'VEV_5500' disabled — only loser in backtest (-$646 over 3 days)
            #'VEV_5500':            {'LIMIT': 300, 'MEAN': 7,    'EDGE': 1, 'THRESH': 2,  'TAKE': 30, 'ALPHA': 0.01,  'WIDE': 2,  'IMB_W': 0.05, 'STRIKE': 5500},
            'VEV_6000':            {'LIMIT': 300, 'MEAN': 0.5,  'EDGE': 0, 'THRESH': 1,  'TAKE': 0,  'ALPHA': 0.01,  'WIDE': 1,  'IMB_W': 0.00, 'STRIKE': 6000},
            'VEV_6500':            {'LIMIT': 300, 'MEAN': 0.5,  'EDGE': 0, 'THRESH': 1,  'TAKE': 0,  'ALPHA': 0.01,  'WIDE': 1,  'IMB_W': 0.00, 'STRIKE': 6500},
            'VELVETFRUIT_EXTRACT': {'LIMIT': 200, 'MEAN': 5250, 'EDGE': 1, 'THRESH': 15, 'TAKE': 45, 'ALPHA': 0.015, 'WIDE': 3,  'IMB_W': 0.05, 'STRIKE': None},
            'HYDROGEL_PACK':       {'LIMIT': 200, 'MEAN': 9991, 'EDGE': 2, 'THRESH': 20, 'TAKE': 60, 'ALPHA': 0.01,  'WIDE': 8,  'IMB_W': 0.10, 'STRIKE': None},
        }

        # ─── Mids ─────────────────────────────────────────────────────────
        mids = {}
        for product, depth in state.order_depths.items():
            if depth.buy_orders and depth.sell_orders:
                mids[product] = (max(depth.buy_orders.keys()) + min(depth.sell_orders.keys())) / 2

        vfx_mid = mids.get('VELVETFRUIT_EXTRACT', 5250)
        vfx_last = data["last_mid"].get('VELVETFRUIT_EXTRACT', vfx_mid)
        vfx_reversion = -0.07 * (vfx_mid - vfx_last)
        data["last_mid"]['VELVETFRUIT_EXTRACT'] = vfx_mid

        # ─── Calibrate u = σ√T from live option chain ─────────────────────
        #
        # For each option strike with delta in [0.2, 0.8] (mid-curve, where IV is most
        # stable), invert BS to recover u from the market mid. Average across strikes,
        # smooth with EMA across iterations.
        #
        # Why mid-curve only:
        #   - Deep ITM (delta ≈ 1): option mid ≈ intrinsic, u almost unidentifiable.
        #   - Deep OTM (delta < 0.1): option mid pinned at floor (e.g. 0.5), no info.
        #   - Mid (delta 0.2-0.8): pure time-value, IV identifiable.
        #
        # Self-calibrating: σ shifts day to day, TTE shrinks intra-day → u falls
        # naturally. No hardcoded constants.
        #
        # Initial seed: if we have no calibration yet, use 0.07 (equiv to σ=0.012, T=4d).
        u_samples = []
        for sym, cfg in CONFIG.items():
            if cfg['STRIKE'] is None:
                continue
            K = cfg['STRIKE']
            if sym not in mids:
                continue
            opt_mid = mids[sym]
            # Crude delta estimate to filter mid-curve
            moneyness = vfx_mid / K  # >1 = ITM
            # Skip clearly deep ITM/OTM
            if moneyness < 0.97 or moneyness > 1.05:
                continue
            u = implied_u(opt_mid, vfx_mid, K)
            if u is not None and 0.001 < u < 1.0:
                u_samples.append(u)

        if u_samples:
            current_u = sum(u_samples) / len(u_samples)
            if not data["u_ema_init"]:
                data["u_ema"] = current_u
                data["u_ema_init"] = True
            else:
                data["u_ema"] = 0.05 * current_u + 0.95 * data["u_ema"]

        # Fallback if calibration ever fails
        if not data["u_ema_init"] or data["u_ema"] <= 0:
            data["u_ema"] = 0.07

        u = data["u_ema"]

        # ─── VEX EMA for mean-reversion gate ──────────────────────────────
        if not data["vex_ema_init"]:
            data["vex_ema"] = vfx_mid
            data["vex_ema_init"] = True
        else:
            data["vex_ema"] = 0.01 * vfx_mid + 0.99 * data["vex_ema"]
        vex_dev = vfx_mid - data["vex_ema"]
        VEX_DEV_THRESH = 8.0
        if vex_dev > VEX_DEV_THRESH:
            mr_signal = -1
        elif vex_dev < -VEX_DEV_THRESH:
            mr_signal = +1
        else:
            mr_signal = 0

        # ─── VEX imbalance + streak overlay ───────────────────────────────
        extract_depth = state.order_depths.get('VELVETFRUIT_EXTRACT')
        extract_imb = 0
        if extract_depth and extract_depth.buy_orders and extract_depth.sell_orders:
            v_b = extract_depth.buy_orders[max(extract_depth.buy_orders.keys())]
            v_a = abs(extract_depth.sell_orders[min(extract_depth.sell_orders.keys())])
            extract_imb = (v_b - v_a) / (v_b + v_a + 1e-6)

        if extract_imb > 0.60:
            data["imb_pos_streak"] += 1
            data["imb_neg_streak"] = 0
        elif extract_imb < -0.60:
            data["imb_neg_streak"] += 1
            data["imb_pos_streak"] = 0
        else:
            data["imb_pos_streak"] = 0
            data["imb_neg_streak"] = 0

        if data["imb_pos_streak"] >= 2:
            add_sig('VELVETFRUIT_EXTRACT', 4.0)
        if data["imb_neg_streak"] >= 2:
            add_sig('VELVETFRUIT_EXTRACT', -4.0)

        extract_pred_shift = (extract_imb * 0.5) + vfx_reversion

        hydrogel_mid = mids.get('HYDROGEL_PACK', 9991)
        pair_bias_h, pair_bias_e = 0, 0
        if vfx_mid and hydrogel_mid:
            current_pair_spread = hydrogel_mid - 1.9 * vfx_mid
            data["pair_ema"] = 0.02 * current_pair_spread + (1 - 0.02) * data["pair_ema"]
            pair_signal = current_pair_spread - data["pair_ema"]
            pair_bias_h = -pair_signal * 0.08 + (extract_pred_shift * 0.9)
            pair_bias_e = pair_signal * 0.08 / 1.9

        # ─── Per-product loop ─────────────────────────────────────────────
        for product in state.order_depths:
            if product not in CONFIG:
                continue
            cfg = CONFIG[product]
            depth = state.order_depths[product]
            if not depth.buy_orders or not depth.sell_orders:
                continue

            LIMIT = cfg['LIMIT']
            HISTORICAL_MEAN = cfg['MEAN']
            BASE_EDGE = cfg['EDGE']
            REVERSION_THRESH = cfg['THRESH']
            MAX_TAKE = cfg['TAKE']
            EMA_ALPHA = cfg['ALPHA']
            WIDE_THRESH = cfg['WIDE']
            IMB_WEIGHT = cfg['IMB_W']
            STRIKE = cfg['STRIKE']

            best_bid = max(depth.buy_orders.keys())
            best_ask = min(depth.sell_orders.keys())
            v1_b = depth.buy_orders[best_bid]
            v1_a = abs(depth.sell_orders[best_ask])

            if product == 'HYDROGEL_PACK':
                sorted_bids = sorted(depth.buy_orders.items(), key=lambda x: x[0], reverse=True)
                sorted_asks = sorted(depth.sell_orders.items(), key=lambda x: x[0])
                p2_b = sorted_bids[1][0] if len(sorted_bids) > 1 else best_bid
                p2_a = sorted_asks[1][0] if len(sorted_asks) > 1 else best_ask
                micro_price = ((best_bid + best_ask) / 2) * 0.7 + ((p2_b + p2_a) / 2) * 0.3
                imb = (v1_b - v1_a) / (v1_b + v1_a + 1e-6)
            else:
                micro_price = (best_ask * v1_b + best_bid * v1_a) / (v1_b + v1_a)
                imb = (v1_b - v1_a) / (v1_b + v1_a)

            spread = best_ask - best_bid

            # ─── Fair value ───────────────────────────────────────────────
            if STRIKE is not None and product not in ('VEV_6000', 'VEV_6500'):
                # BS fair via calibrated u
                intrinsic = max(vfx_mid - STRIKE, 0.0)
                bs_fair = bs_call_u(vfx_mid, STRIKE, u)
                option_fair = max(intrinsic, bs_fair)
                delta = bs_delta_u(vfx_mid, STRIKE, u)

                # More BS weight when far from ATM (where micro-price lies less);
                # less weight near ATM (micro-price more reliable).
                bs_weight = 0.30 + 0.50 * abs(delta - 0.5) * 2
                bs_weight = max(0.0, min(1.0, bs_weight))
                fair_value = bs_weight * option_fair + (1 - bs_weight) * micro_price
                fair_value += imb * spread * IMB_WEIGHT
            else:
                fair_value = micro_price + imb * spread * IMB_WEIGHT
                delta = 1.0 if product == 'VELVETFRUIT_EXTRACT' else 0.0

            # ─── Basis EMA (delta-residual mean reversion for options) ────
            delta_bias = 0
            if STRIKE is not None and product not in ('VEV_6000', 'VEV_6500'):
                basis = fair_value - (vfx_mid * delta)
                if product not in data["basis_ema"]:
                    data["basis_ema"][product] = basis
                data["basis_ema"][product] = 0.08 * basis + (1 - 0.08) * data["basis_ema"][product]
                delta_bias = -(basis - data["basis_ema"][product]) * 0.25 + (extract_pred_shift * delta * 0.4)

            if product not in data["ema"]:
                data["ema"][product] = HISTORICAL_MEAN
            data["ema"][product] = round(EMA_ALPHA * fair_value + (1 - EMA_ALPHA) * data["ema"][product])
            if product not in data["ema_fast"]:
                data["ema_fast"][product] = HISTORICAL_MEAN
            data["ema_fast"][product] = round(0.1 * fair_value + (1 - 0.1) * data["ema_fast"][product])

            adaptive_mean = data["ema"][product]
            fast_mean = data["ema_fast"][product]
            trending = abs(fast_mean - adaptive_mean) > REVERSION_THRESH * 0.3

            position = state.position.get(product, 0)
            buy_cap, sell_cap = LIMIT - position, LIMIT + position
            pos_ratio = position / LIMIT

            total_bias = 0
            if STRIKE is not None and product not in ('VEV_6000', 'VEV_6500'):
                total_bias = delta_bias
            elif product == 'HYDROGEL_PACK':
                total_bias = pair_bias_h
            elif product == 'VELVETFRUIT_EXTRACT':
                total_bias = pair_bias_e + vfx_reversion

            id_sig = data["id_sig"].get(product, 0.0)
            vev_sig = data["id_sig"].get('VELVETFRUIT_EXTRACT', 0.0)

            if product == 'HYDROGEL_PACK':
                total_bias += 0.55 * max(-10.0, min(10.0, id_sig))
                total_bias += 0.80 * data["m22_hyd_sig"]
            elif product == 'VELVETFRUIT_EXTRACT':
                total_bias += 0.45 * max(-12.0, min(12.0, id_sig))
            elif product == 'VEV_4000':
                total_bias += 0.70 * max(-8.0, min(8.0, id_sig))
            elif product == 'VEV_5200':
                total_bias += 0.35 * max(-6.0, min(6.0, id_sig)) + 0.20 * max(-12.0, min(12.0, vev_sig)) * delta
            elif product == 'VEV_5300':
                total_bias += 0.30 * max(-6.0, min(6.0, id_sig)) + 0.20 * max(-12.0, min(12.0, vev_sig)) * delta

            # Mid-curve mean-reversion overlay
            if product in ('VEV_5000', 'VEV_5100', 'VEV_5200', 'VEV_5300') and mr_signal != 0:
                flow_against = (mr_signal > 0 and (id_sig + vev_sig) < -8) or \
                               (mr_signal < 0 and (id_sig + vev_sig) > 8)
                if not flow_against:
                    mr_strength = 1.0 - abs(delta - 0.5) * 2
                    total_bias += mr_signal * 1.5 * mr_strength

            # Far OTM: floor-quoter
            if product in ('VEV_6000', 'VEV_6500'):
                order_list = []
                bid_qty = min(buy_cap, 35)
                ask_qty = min(sell_cap, 35)
                if position > 120:
                    bid_qty, ask_qty = min(buy_cap, 5), min(sell_cap, 60)
                elif position < -120:
                    bid_qty, ask_qty = min(buy_cap, 60), min(sell_cap, 5)
                elif position > 40:
                    bid_qty, ask_qty = min(buy_cap, 15), min(sell_cap, 45)
                elif position < -40:
                    bid_qty, ask_qty = min(buy_cap, 45), min(sell_cap, 15)
                if bid_qty > 0:
                    order_list.append(Order(product, 0, bid_qty))
                if ask_qty > 0:
                    order_list.append(Order(product, 1, -ask_qty))
                result[product] = order_list
                continue

            skew = pos_ratio * 2.0
            our_bid = round((best_bid + 1 if spread > WIDE_THRESH else fair_value - BASE_EDGE) - skew + total_bias)
            our_ask = round((best_ask - 1 if spread > WIDE_THRESH else fair_value + BASE_EDGE) - skew + total_bias)
            our_bid = min(our_bid, best_ask - 1)
            our_ask = max(our_ask, best_bid + 1)
            if our_ask <= our_bid:
                our_ask = our_bid + 1

            base_qty = max(10, int(50 * (1 - abs(pos_ratio) ** 0.6 * 0.7)))
            dist = fair_value - adaptive_mean
            take_thresh = REVERSION_THRESH if not trending else int(REVERSION_THRESH * 1.5)

            if product == 'VELVETFRUIT_EXTRACT':
                dist += 0.35 * max(-12.0, min(12.0, id_sig))
                if abs(id_sig) > 8:
                    take_thresh = max(4, take_thresh - 2)
            elif product == 'HYDROGEL_PACK':
                dist += 0.20 * max(-10.0, min(10.0, id_sig))
                dist += 0.50 * data["m22_hyd_sig"]
                if abs(data["m22_hyd_sig"]) > 6 or abs(id_sig) > 8:
                    take_thresh = max(6, take_thresh - 4)
            elif product == 'VEV_4000':
                dist += 0.30 * max(-8.0, min(8.0, id_sig))
                if abs(id_sig) > 6:
                    take_thresh = max(4, take_thresh - 1)
            elif product in ('VEV_5200', 'VEV_5300'):
                merged = 0.5 * max(-6.0, min(6.0, id_sig)) + 0.3 * max(-12.0, min(12.0, vev_sig))
                dist += 0.20 * merged * delta
                if abs(merged) > 5:
                    take_thresh = max(3, take_thresh - 1)

            if product in ('VEV_5000', 'VEV_5100', 'VEV_5200', 'VEV_5300') and mr_signal != 0:
                flow_against = (mr_signal > 0 and (id_sig + vev_sig) < -8) or \
                               (mr_signal < 0 and (id_sig + vev_sig) > 8)
                if not flow_against:
                    mr_strength = 1.0 - abs(delta - 0.5) * 2
                    dist += mr_signal * 2.0 * mr_strength

            order_list = []
            if dist <= -take_thresh and buy_cap > 0:
                take_qty = min(buy_cap, MAX_TAKE) if pos_ratio <= 0.3 else min(buy_cap, max(10, int(MAX_TAKE * 0.5)))
                if take_qty > 0:
                    order_list.append(Order(product, best_ask, take_qty))
                if buy_cap - take_qty > 0:
                    order_list.append(Order(product, our_bid, min(buy_cap - take_qty, base_qty)))
            elif dist >= take_thresh and sell_cap > 0:
                take_qty = min(sell_cap, MAX_TAKE) if pos_ratio >= -0.3 else min(sell_cap, max(10, int(MAX_TAKE * 0.5)))
                if take_qty > 0:
                    order_list.append(Order(product, best_bid, -take_qty))
                if sell_cap - take_qty > 0:
                    order_list.append(Order(product, our_ask, -min(sell_cap - take_qty, base_qty)))
            else:
                if position > LIMIT * 0.4 and sell_cap > 0:
                    order_list.append(Order(product, max(best_bid + 1, our_ask - 1), -min(sell_cap, int(position * 0.25), 30)))
                elif position < -LIMIT * 0.4 and buy_cap > 0:
                    order_list.append(Order(product, min(best_ask - 1, our_bid + 1), min(buy_cap, int(abs(position) * 0.25), 30)))

                bid_post = min(buy_cap, base_qty)
                ask_post = min(sell_cap, base_qty)

                if product in ('HYDROGEL_PACK', 'VELVETFRUIT_EXTRACT', 'VEV_4000', 'VEV_5200', 'VEV_5300'):
                    if id_sig > 8:
                        ask_post = min(ask_post, max(10, base_qty // 2))
                    elif id_sig < -8:
                        bid_post = min(bid_post, max(10, base_qty // 2))

                if product == 'HYDROGEL_PACK':
                    if data["m22_hyd_sig"] < -6:
                        bid_post = min(bid_post, max(5, base_qty // 4))
                    elif data["m22_hyd_sig"] > 6:
                        ask_post = min(ask_post, max(5, base_qty // 4))

                if buy_cap > 0:
                    order_list.append(Order(product, our_bid, bid_post))
                if sell_cap > 0:
                    order_list.append(Order(product, our_ask, -ask_post))

            result[product] = order_list

        return result, 0, json.dumps(data)