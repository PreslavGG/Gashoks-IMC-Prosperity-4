# IMC Prosperity 4 — Round 1

This repo holds our algorithm for [IMC Prosperity 4](https://prosperity.imc.com/) and the reasoning behind it. The goal of this writeup is to explain *why* the strategy looks the way it does and why it works, what alternatives we tried.

Heavy credit to **Frankfurt Hedgehogs** (2nd place in [Prosperity 3](https://github.com/TimoDiehm/imc-prosperity-3)). Their writeup and algorithm was key for the development of our algorithm.

---

## Round 1 products

Round 1 introduces two products, both with a position limit of 80.

![Round 1 bid/ask](round1_bidask.png)

- `ASH_COATED_OSMIUM` — noisy but stable, mean-reverting around ~10,000 (similar to the tutorial round EMERALDS, but instead of always being stationary, it deviated more from the FV).
- `INTARIAN_PEPPER_ROOT` — deterministic upward drift (with small slope) each day, resets at day boundary, very tight noise around the trend.

---

## ASH_COATED_OSMIUM — `DynamicTrader`

### Observations

- The price hovers around 10,000 with little drift — a textbook mean-reverting asset.
- The book has multiple levels per side. The outermost ("wall") bid and ask are sticky across consecutive ticks (however are missing on some timestamps and orderbook is imbalanced at times, skewing the FV); the inside best bid/ask flicker as smaller bots rotate quotes.
- The inside spread can be much tighter than the wall spread when bots quote smaller quantities between the walls.

### Strategy

We anchor fair value at the **wall midpoint**. We found that this is the best way to calculate the FV of the asset. However; the inside flickers, which would inject noise into our quotes if we anchored there. This is the Hedgehogs trick from their Rainforest Resin / Kelp logic from last year. This ensures that even if the bids and asks are missing or change rapidly between ticks, our mid value is still fairly stable.

Around that anchor we run a two-step strategy:

**1. Take.** Sweep any ask priced `≤ wall_mid − 1` and any bid priced `≥ wall_mid + 1`. These are mispriced bot orders giving away free edge versus our fair value. Position-limit clamping makes sure we never overshoot. Only downside we found was that becasue of the noisy orderbook with missing quotes, the algorithm misprises the FV on some occasions. Furthermore, our taking strategy consisted of flatten buy and sell trades at fair value if our position became to skewed, which minimised any risk from underlying asset movements. However, taking still worked for us as we tested it rigorously on multiple backtesters.

**2. Make.** Quote with full remaining capacity on each side, one tick inside the bid_wall and ask_wall. This strategy implements a simple overbidding and undercutting the wall orders, and with +-1 we found that we got the most fills, which strengthened our thesis. Instead of skewing by parameters about when to skew and by how much, we just lean on the fact that position sizing already consumes capacity asymmetrically.

This is mathematically the same as a traditional inventory-skew rule, but it falls out of the position-limit accounting for free.

### What we ruled out

- **Pure passive market making at mid ± edge.** Works, but is risky to overfitting leaves money on the table whenever the inside spread crosses fair value. Adding the take step is a strict improvement.
- **EMA-based fair value.** The wall mid is already a stable, low-noise anchor and it's free to compute. An EMA of the inside mid would just be a noisier version of the same thing.
- **Explicit skew parameter (`fv − k * position`).** Duplicates what capacity tracking already does. Adding it on top would either over-skew or require careful tuning. We skipped it.

---

## INTARIAN_PEPPER_ROOT — `StableTrader`

### Observations

- Strong, near-deterministic upward drift within each day, with very little noise around the trend.
- Day boundaries reset the price level (visible as the jagged drops in the bottom panel of the chart above), but within a day the trend is monotonic.
- The asset is essentially a one-way market: it goes up.

### Strategy

When an asset has a known directional drift and tiny noise, market making against it is a structural loser. A symmetric quote around the rising mid will:
- Get its bid filled when the price dips, then the price keeps rising (small win).
- Get its ask filled when the price rips, then the price keeps rising (loss — we sold and watched it climb away).

Net of fees and rebates, you bleed. The right play is the obvious one: **load max long as cheaply as possible and hold.**

Our implementation is one-sided:

1. Take the cheapest available ask up to remaining buy capacity.
2. Rest any leftover capacity at the best bid (free option to fill cheaper if anyone hits us).
3. Never sell. Holding +80 across the day captures the drift directly.

There is no fair-value calculation, no make/take split, no inventory management. It's intentionally minimal because the data shows there's nothing else worth doing.

### What we ruled out

- **Symmetric market making.** As above — structurally adverse on a trending asset.
- **Slope-aware market making** (skew quotes upward by the observed slope). Tested in spirit. The slope is so small per tick and the inside spread so often tight that the math degenerates to "bid more aggressively, never sell" — which is just what the final strategy does, with less code.
- **Trying to time day-boundary resets.** Possible in principle but risky: a single missed reset wipes the day's PnL. Out of scope for Round 1.
- **Selling near end of day to flatten.** Considered. Tabled — the bot price near close still reflects the drifted level, so holding through the reset is fine; the "loss" is purely a mark-to-market artifact, not a realized loss.

---

## Architecture

```
ProductTrader (base)
├── _buy / _sell                — capacity-clamped order placement
├── take_orders(fv)             — sweep mispriced bot levels
├── make_orders(fv)             — overbid / undercut, dump remaining capacity
└── fair_value()                — overridable per product

DynamicTrader   (Osmium)        — fair_value = wall_mid, take + make
StableTrader    (Pepper Root)   — overrides get_orders entirely: take cheapest, rest at bid
```

A few engineering details worth flagging:

- **Order book volumes are stored as positive on both sides** (Hedgehogs convention). Sign handling is the most common bug source in Prosperity, and normalizing at construction time eliminates it everywhere downstream.
- **Order book levels are explicitly sorted** (`bids` desc, `asks` asc). The live engine does not guarantee key order even though the visualizer makes it look that way.
- **`traderData` uses plain `json` instead of `jsonpickle`.** Faster, and it dodges the `"SAMPLE"` default-string deserialization issue cleanly with a single `isinstance` guard.
- **The `Logger` class** is the standard visualizer-compatible one for use with [jmerle/imc-prosperity-3-visualizer](https://github.com/jmerle/imc-prosperity-3-visualizer).

---

## Files

- `r1_trader.py` — submission
- `prices_round_1_day_*.csv`, `trades_round_1_day_*.csv` — sample data
- `round1_bidask.png` — bid/ask plot for both products across the three sample days
