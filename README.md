# Gashoks — IMC Prosperity 4

This repository contains our algorithmic trading submissions for **IMC Prosperity 4**. We competed as **Gashoks** and built a set of Python traders that evolved across the rounds as new products, signals, and market structures were introduced.

## Results

Our final result after **Phase 2**:

- **3rd place in Italy**
- **238th place globally**

Before the leaderboard reset after **Phase 1**, we were ranked:

- **74th globally**

## What is IMC Prosperity 4?

**IMC Prosperity 4** is IMC Trading's global trading competition. Teams compete in a simulated exchange by writing algorithmic traders and solving manual trading problems. The goal is to maximize profit and loss across several rounds while respecting product-specific position limits.

Each algorithmic submission implements a `Trader` class. At every timestamp, the trader receives the current market state, including:

- order books for all active products,
- recent own trades and market trades,
- current positions,
- observations,
- persistent `traderData` from previous timestamps.

The trader then returns orders for each product. The challenge is not only to find profitable signals, but also to quote safely, manage inventory, and avoid strategies that look good statistically but fail under the competition's fill model.

## Repository Structure

```text
.
├── datamodel.py        # IMC-provided data model and trading-state classes
├── trader_r1-2.py      # Trader used for Rounds 1 and 2
├── trader_r3-4.py      # Trader used for Rounds 3 and 4
├── trader_r5.py        # Trader used for Round 5 / Phase 2
└── README.md
```

## High-Level Strategy

Our approach changed throughout the competition. The early rounds used simple market making and product-specific directional logic. Later rounds required more structured models, including option pricing, counterparty-flow analysis, pair relationships, basket relationships, and broad cross-product market making.

The main principles across all traders were:

- estimate a fair value for every product,
- take liquidity only when the price had clear edge,
- quote passively around fair value,
- skew quotes based on current inventory,
- use persistent memory through `traderData`,
- exploit product-specific structure when it was reliable,
- disable ideas that looked good in theory but lost money in backtests.

## Rounds 1–2

Implemented in [`trader_r1-2.py`](./trader_r1-2.py).

The first trader handled two products:

- `INTARIAN_PEPPER_ROOT`
- `ASH_COATED_OSMIUM`

### INTARIAN_PEPPER_ROOT

`INTARIAN_PEPPER_ROOT` was treated as a stable product with a strong upward bias. The strategy was intentionally simple and aggressive:

1. Buy the cheapest available asks.
2. Continue accumulating until the position limit was reached.
3. If capacity remained, place passive buy orders at the best bid.

This was not a symmetric market-making strategy. It was a directional accumulation strategy based on the observed behavior of the product during the early rounds.

### ASH_COATED_OSMIUM

`ASH_COATED_OSMIUM` used a more traditional market-making approach.

The trader estimated fair value from the order book, especially from the outer walls of liquidity. It then used the previous wall midpoint to smooth the value and avoid reacting too aggressively to short-term book noise.

The strategy:

- calculated a dynamic fair value from the book,
- used previous fair values stored in memory,
- bought when asks were clearly cheap,
- sold when bids were clearly expensive,
- prioritized flattening inventory when carrying risk,
- posted passive quotes around fair value.

This gave us a robust baseline: earn spread when possible, but do not let inventory drift too far.

## Rounds 3–4

Implemented in [`trader_r3-4.py`](./trader_r3-4.py).

Rounds 3 and 4 introduced more complex products and relationships. The traded universe included:

- `VELVETFRUIT_EXTRACT`
- `HYDROGEL_PACK`
- `VEV_4000`
- `VEV_4500`
- `VEV_5000`
- `VEV_5100`
- `VEV_5200`
- `VEV_5300`
- `VEV_5400`
- `VEV_6000`
- `VEV_6500`

The `VEV_*` products behaved like call options on `VELVETFRUIT_EXTRACT`, with the number in the product name representing the strike.

### Option Pricing for VEV Products

For the `VEV_*` products, we implemented a Black-Scholes-style call option model.

Instead of separately estimating volatility and time to expiry, we calibrated the combined parameter:

```text
u = sigma * sqrt(T)
```

This simplified the model while still allowing us to infer option value from the underlying product.

The trader used:

- the midpoint of `VELVETFRUIT_EXTRACT` as the underlying price,
- the VEV strike from the product name,
- implied volatility-style calibration,
- theoretical option value,
- intrinsic value,
- delta estimates,
- order-book microprice,
- basis mean reversion.

This was useful because the visible order book alone was not always enough. The option chain contained structural information, and pricing the options relative to the underlying gave us better fair-value estimates.

Some products were traded more actively than others. For example, `VEV_5500` was disabled because it was the only loser in one of our backtests. This was a recurring pattern in the competition: we preferred removing weak components over keeping every theoretical signal.

### VELVETFRUIT_EXTRACT

`VELVETFRUIT_EXTRACT` was both a directly traded product and the underlying asset for the VEV option products.

The strategy combined:

- order-book midpoint,
- microprice,
- imbalance signals,
- fast and slow EMA estimates,
- mean-reversion against the EMA,
- counterparty-flow signals.

When the book showed strong and persistent imbalance, the trader shifted fair value in the direction of that pressure. When price moved too far from the slow EMA, the trader treated part of the move as mean-reverting.

### HYDROGEL_PACK

`HYDROGEL_PACK` was traded using a mix of market making, order-book imbalance, and a relationship with `VELVETFRUIT_EXTRACT`.

We tracked the spread:

```text
HYDROGEL_PACK - 1.9 * VELVETFRUIT_EXTRACT
```

The trader maintained an EMA of this spread and used deviations from the EMA as a fair-value adjustment. If Hydrogel became expensive relative to Velvetfruit Extract, the trader became more willing to sell Hydrogel. If it became cheap, the trader became more willing to buy.

### Counterparty Flow Signals

A major part of the Round 3–4 trader was tracking named counterparties from market trades.

The strategy assigned signals to trades involving participants such as:

- `Mark 01`
- `Mark 14`
- `Mark 22`
- `Mark 38`
- `Mark 49`
- `Mark 55`
- `Mark 67`

Some counterparties appeared to be informative for specific products. For example, a buy from one participant could be treated as bullish, while a sell from another could be treated as bullish or bearish depending on the historical behavior we observed.

These signals were not permanent. They decayed over time, so the trader reacted to recent flow without letting old information dominate future decisions.

### Inventory Management

The Round 3–4 trader adjusted both price and size based on current position.

When inventory was near zero, it could quote both sides more freely. When inventory became large, it became more conservative and prioritized reducing exposure. This mattered especially for the VEV products, because option-like instruments could move quickly when the underlying shifted.

## Round 5 / Phase 2

Implemented in [`trader_r5.py`](./trader_r5.py).

Round 5 expanded the universe heavily. The final trader covered many products across multiple groups:

- `GALAXY_SOUNDS_*`
- `SLEEP_POD_*`
- `MICROCHIP_*`
- `PEBBLES_*`
- `ROBOT_*`
- `UV_VISOR_*`
- `TRANSLATOR_*`
- `PANEL_*`
- `OXYGEN_SHAKE_*`
- `SNACKPACK_*`

At this stage, the safest approach was not to overfit every product individually. We used a reusable market-making framework for almost everything, then added pair overlays where they helped.

### Generic Product Market Maker

The base `ProductTrader` was a general market maker.

For each product, it:

1. Read the best bid and best ask.
2. Estimated a fair value from the order book.
3. Bought asks that were clearly below fair value.
4. Sold bids that were clearly above fair value.
5. Posted passive bid and ask quotes around fair value.
6. Skewed quotes according to current inventory.
7. Respected the position limit.

This framework was intentionally simple. In a large product universe, a stable market-making baseline was more valuable than a fragile product-specific model for every instrument.

### Pair Relative Trading

For selected products, we added a `PairRelativeTrader`. This did not fully replace market making. Instead, it slightly adjusted fair value using pressure from a related product.

The active pairs were:

```text
SNACKPACK_CHOCOLATE       / SNACKPACK_VANILLA
MICROCHIP_RECTANGLE       / MICROCHIP_SQUARE
MICROCHIP_OVAL            / MICROCHIP_TRIANGLE
SLEEP_POD_COTTON          / SLEEP_POD_POLYESTER
UV_VISOR_AMBER            / UV_VISOR_MAGENTA
ROBOT_IRONING             / ROBOT_MOPPING
PEBBLES_XL                / PEBBLES_XS
GALAXY_SOUNDS_BLACK_HOLES / OXYGEN_SHAKE_GARLIC
```

The pair overlay used the peer product's book pressure as a small signal. If the peer product moved in a way that historically correlated with the current product, the trader nudged fair value accordingly.

The best tested configuration used eight pairs and produced the strongest IMC-backtested result among the pair setups we tried. Some pairs had stronger statistical support than others, but the full set performed best in the IMC backtester.

### Pebbles Basket Logic

We also implemented a `PebblesTrader` for the `PEBBLES_*` products.

The idea was that the five Pebbles products behaved like a basket with an approximate invariant:

```text
PEBBLES_XS + PEBBLES_S + PEBBLES_M + PEBBLES_L + PEBBLES_XL ~= 50000
```

For any one Pebbles product, the trader could estimate its implied fair value from the other four:

```text
fair_value(product) = 50000 - sum(other_pebbles)
```

This let us detect when one Pebbles product was cheap or expensive relative to the basket.

In the final wiring, the general market maker with pair overlays was favored, while the basket logic remained available in the code as an explored strategy.

### Snackpack Experiments

The `SNACKPACK_*` products looked promising because they had basket-like and relative-value structure. We tested several strategies:

- snackpack basket residual trading,
- rolling basket-sum estimation,
- anchor-based relative-value trading,
- rolling mean and standard deviation estimates,
- target-position sizing based on residual strength,
- take-only mean-reversion logic.

However, these directional snackpack strategies performed badly under the IMC fill model. The problem was that the trader could build inventory against intraday drift faster than the residual mean-reverted.

Because of that, we did not activate the aggressive snackpack basket traders in the final version. We kept plain market making for snackpacks, with only the safer pair overlay for `SNACKPACK_CHOCOLATE` and `SNACKPACK_VANILLA`.

This was one of the clearest lessons from the competition: a relationship can be statistically real and still not be tradable after fills, spread, and inventory risk are included.

## Important Implementation Details

### `traderData`

We used `traderData` to store persistent state between calls to `run()`.

This included values such as:

- previous mid prices,
- previous wall midpoints,
- EMAs,
- basis estimates,
- implied-volatility estimates,
- counterparty-flow signals,
- basket rolling windows,
- last timestamp.

Without persistent memory, the trader would have had to treat every timestamp independently. That would have removed most of the useful time-series structure.

### Fair Value Models

Different products needed different fair-value logic:

| Product type | Fair-value method |
| --- | --- |
| Stable products | directional bias or simple book value |
| Market-making products | wall midpoint / order-book midpoint |
| VEV options | Black-Scholes-style theoretical value |
| Velvetfruit Extract | microprice, EMA, imbalance, flow signals |
| Hydrogel Pack | book value plus spread relationship to Velvetfruit |
| Pebbles | basket-implied value |
| Round 5 broad universe | generic market-making value plus pair overlay |

### Risk Management

Risk management was central to every trader.

The main controls were:

- hard position limits,
- inventory-aware quote skewing,
- reduced buying when already long,
- reduced selling when already short,
- flattening logic when possible,
- disabling unstable strategies,
- avoiding overfitted product-specific logic in the final large universe.

## What Worked Best

The most reliable components were:

1. **Simple market making** around a reasonable fair value.
2. **Inventory-aware quoting** instead of blindly posting both sides.
3. **Option pricing** for the VEV products.
4. **Counterparty-flow signals** when used with decay.
5. **Small pair overlays** rather than aggressive pair bets.
6. **Removing losing strategies** instead of trying to force every idea to work.

## What Did Not Work Well

Some ideas looked promising but were not robust enough:

- aggressive snackpack basket trading,
- overconfident mean reversion on drifting products,
- using statistical relationships without accounting for fills,
- keeping weak products active just because the theory looked clean,
- letting inventory build too quickly before the signal had time to revert.

## Lessons Learned

- A simple, stable market maker is a strong baseline.
- Fair value matters, but execution matters just as much.
- Inventory risk can destroy an otherwise correct signal.
- The IMC fill model can make some statistical edges unprofitable.
- Option products need a proper theoretical model.
- Counterparty information can be useful, but only if decayed and product-specific.
- Large universes reward robust generic logic more than overfitted per-product logic.

## Disclaimer

This code was written for IMC Prosperity 4's simulated exchange and synthetic product universe. It is not intended for real financial trading.
