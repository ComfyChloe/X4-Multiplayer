//! Trade offer prices, bit for bit (X4 9.00 build 23660954; reports/REWRITE-ECONOMY.md §6).
//!
//! Money is an `i64` in cents. A ware's reference prices are `WareClass+0xc8` (average),
//! `+0xd0` (minimum) and `+0xd8` (maximum). An offer stores its unit price (self-relative
//! tag at `Trade+0x30`); these functions are the engine's conversions of that unit price:
//!
//! - [`trade_price`] = 0x891d00 `(trade, amount, modifier)`: total price for an amount with a
//!   price modifier (commission/discount of the trade partner, computed by 0x8918b0);
//! - [`relative_price`] = 0x891b20: position of the unit price on the min/average/max scale,
//!   -2..2 (scripts: `relativeprice`, filters `TradeFilterByRelativePrice`);
//! - [`quantity_factor`] = 0x891de0: unit price / average.
//!
//! The precision of every step follows the disassembly (the decompiler shows these as float
//! throughout; they are not): see the comments.

/// Reference prices of a ware in cents (`WareClass+0xc8/+0xd0/+0xd8`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct WarePrices {
    pub avg: i64,
    pub min: i64,
    pub max: i64,
}

/// x86 `cvttsd2si r64`: truncation, `i64::MIN` for NaN and out-of-range values.
#[inline]
fn cvttsd2si64(x: f64) -> i64 {
    if !(-9.223_372_036_854_776e18..9.223_372_036_854_776e18).contains(&x) {
        i64::MIN
    } else {
        x as i64
    }
}

/// 0x891d00: total price of `amount` units at `unit` cents with modifier `m`.
///
/// For `|m| < 1e-4f` the result is `unit * amount` (i64, wrapping like `imul`). Otherwise
/// `max(a, b + (unit - avg) * amount)` clamped at 0, with
/// `a = (i64)((double)(m + 1.0f) * (double)unit * (double)amount + 0.5)` and
/// `b = (i64)((double)(((float)(max - min) / (float)avg) * m + 1.0f) * (double)avg * (double)amount + 0.5)`.
pub fn trade_price(unit: i64, amount: i32, m: f32, w: &WarePrices) -> i64 {
    let amount64 = amount as i64;
    if !(m.abs() >= 1e-4_f32) {
        return unit.wrapping_mul(amount64);
    }
    let a = cvttsd2si64(((m + 1.0f32) as f64) * (unit as f64) * (amount as f64) + 0.5);
    let spread = (w.max.wrapping_sub(w.min)) as f32; // cvtsi2ss of the i64 difference
    let x = (spread / (w.avg as f32)) * m + 1.0f32; // divss, mulss, addss
    let b = cvttsd2si64((x as f64) * (w.avg as f64) * (amount as f64) + 0.5);
    let b = b.wrapping_add(unit.wrapping_sub(w.avg).wrapping_mul(amount64));
    let r = if b < a { a } else { b };
    if r > 0 {
        r
    } else {
        0
    }
}

/// 0x891b20: relative price of a unit price, all in f32.
pub fn relative_price(unit: i64, w: &WarePrices) -> f32 {
    let p = unit as f32;
    let avg = w.avg as f32;
    if !(p >= avg) {
        let min = w.min as f32;
        if !(p >= min) {
            let half = min * 0.5f32;
            if !((half - min).abs() >= 1e-4_f32) {
                return -1.5;
            }
            if !(p > half) {
                return -2.0;
            }
            return (p - half) / (min - half) - 2.0;
        }
        if !((min - avg).abs() >= 1e-4_f32) {
            return -0.5;
        }
        if !(p > min) {
            return -1.0;
        }
        return (p - min) / (avg - min) - 1.0;
    }
    if !(p > avg) {
        return 0.0;
    }
    let max = w.max as f32;
    if !(p > max) {
        if !((avg - max).abs() >= 1e-4_f32) {
            return 0.5;
        }
        if !(p < max) {
            return 1.0;
        }
        return (p - avg) / (max - avg);
    }
    let twice = max + max;
    if !((max - twice).abs() >= 1e-4_f32) {
        return 1.5;
    }
    if !(p < twice) {
        return 2.0;
    }
    (p - max) / max + 1.0
}

/// 0x891de0: unit price / average price (f32).
pub fn quantity_factor(unit: i64, w: &WarePrices) -> f32 {
    (unit as f32) / (w.avg as f32)
}

/// x86 `cvttsd2si r64` for the stock price: truncation toward zero.
#[inline]
fn trunc_i64(x: f64) -> i64 {
    cvttsd2si64(x)
}

/// 0x34cde0: a station's unit price for a ware from its stock and its target amount, in cents.
///
/// The S-shaped price curve of station trade offers: average price at half the target,
/// the ware's maximum price at an empty stock, its minimum price at the target.
/// - `stock > target`: minimum price;
/// - `stock <= 0` or `target <= 0` (and not above): maximum price;
/// - otherwise, with `h = target * 0.5`, `d = clamp(stock - h, -h, h)` (and the average when
///   `|stock - h| < 1e-7`), `s = avg - min` for `d >= 0` else `max - avg`, `k = -s * (1/0.7)`:
///   `trunc(((-s - k) / (target^2 * (h * 0.25))) * (d * d * d) + (k * d) / h + avg)`.
///
/// All in f64 in the engine's operation order (`target^2 * (h * 0.25)`, `(d * d) * d`).
pub fn stock_price(w: &WarePrices, stock: i32, target: i32) -> i64 {
    if stock > target {
        return w.min;
    }
    if stock <= 0 || target <= 0 {
        return w.max;
    }
    let t = target as f64;
    let h = t * 0.5;
    let x = stock as f64 - h;
    if !(x.abs() >= 1e-7) {
        return w.avg;
    }
    // minsd/maxsd order of the engine: min(x, h), then max(.., -h); no NaN can occur here.
    let d = if x <= h { x } else { h };
    let d = if d > -h { d } else { -h };
    let span = if d >= 0.0 { w.avg.wrapping_sub(w.min) } else { w.max.wrapping_sub(w.avg) };
    let neg = -(span as f64);
    let k = neg * 1.428_571_428_571_428_4;
    let denom = (t * t) * (h * 0.25);
    let cubic = ((neg - k) / denom) * ((d * d) * d);
    trunc_i64(cubic + (k * d) / h + w.avg as f64)
}

/// 0x14dd240: clamped linear interpolation from `(x0, y0)` to `(x1, y1)` at `x` (f64); the
/// mean of `y0` and `y1` when `|x0 - x1| < 1e-4`.
pub fn lerp_clamped(x0: f64, x1: f64, y0: f64, y1: f64, x: f64) -> f64 {
    if !((x0 - x1).abs() >= 1e-4) {
        return (y0 + y1) * 0.5;
    }
    if x0 < x {
        if x1 <= x {
            return y1;
        }
        return ((x - x0) * (y1 - y0)) / (x1 - x0) + y0;
    }
    y0
}

/// 0x34ccf0: production cycles of a method with cycle time `cycle` seconds in `horizon`
/// seconds, as f32 (0 for a cycle time below 1e-7).
pub fn cycles_in(cycle: f64, horizon: f64) -> f32 {
    if cycle.abs() >= 1e-7 {
        (horizon / cycle) as f32
    } else {
        0.0
    }
}

/// Final buy/sell separation in `GetContainerWarePrice` (tail of 0x403a60, near 0x405d01).
/// When both sides are eligible, automatic prices keep a one-credit (100-cent) gap.
/// Prefer reducing the automatic buy price, respecting the ware minimum; raise the
/// automatic sell price when the buy side is fixed or the sell side is near the minimum.
/// Two explicitly configured prices are preserved. These flags describe the engine's
/// price calculation, not whether an offer currently has a nonzero amount.
///
/// This isolated arithmetic is static-analysis/synthetic validated. Captures do not yet
/// supply all eligibility/override inputs, so it is not used to claim native price replay.
pub fn separate_offer_prices(
    mut buy: i64,
    mut sell: i64,
    minimum: i64,
    buy_enabled: bool,
    sell_enabled: bool,
    automatic_buy: bool,
    automatic_sell: bool,
) -> (i64, i64) {
    let below_sell = sell.wrapping_sub(100);
    if buy_enabled && sell_enabled && (automatic_buy || automatic_sell) && below_sell < buy {
        if automatic_sell && (!automatic_buy || sell < minimum.wrapping_add(100)) {
            sell = sell.max(buy.wrapping_add(100));
        } else if automatic_buy {
            buy = buy.min(below_sell.max(minimum));
        }
    }
    (buy, sell)
}

#[cfg(test)]
mod tests {
    use super::*;

    // energycells: min 10, average 16, max 22 credits (wares.xml), in cents.
    const EC: WarePrices = WarePrices { avg: 1600, min: 1000, max: 2200 };

    #[test]
    fn automatic_offer_gap_preserves_fixed_prices_and_minimum() {
        let gap =
            |buy, sell, auto_buy, auto_sell| separate_offer_prices(buy, sell, 4300, true, true, auto_buy, auto_sell);
        // At the minimum the buy price cannot drop: raise the automatic sell price.
        assert_eq!(gap(4300, 4300, true, true), (4300, 4400));
        assert_eq!(gap(4500, 4500, true, true), (4400, 4500));
        assert_eq!(gap(4500, 4500, false, true), (4500, 4600));
        assert_eq!(gap(4500, 4500, true, false), (4400, 4500));
        assert_eq!(gap(4500, 4500, false, false), (4500, 4500));
        assert_eq!(gap(4300, 4300, true, false), (4300, 4300));
        assert_eq!(gap(4300, 4400, true, true), (4300, 4400));
        assert_eq!(separate_offer_prices(4300, 4300, 4300, false, true, true, true), (4300, 4300));
        assert_eq!(separate_offer_prices(4300, 4300, 4300, true, false, true, true), (4300, 4300));
    }

    #[test]
    fn no_modifier_is_unit_times_amount() {
        assert_eq!(trade_price(1734, 500, 0.0, &EC), 867_000);
        assert_eq!(trade_price(1734, 500, 5e-5, &EC), 867_000);
    }

    #[test]
    fn modifier_takes_the_larger_of_both_forms() {
        // a = (0.9 as f32 + 1.0) * 1734 * 100 + 0.5 = 329_460(.5..) ; b uses the ware spread.
        let a = (((-0.1f32 + 1.0f32) as f64) * 1734.0 * 100.0 + 0.5) as i64;
        let x = ((1200.0f32 / 1600.0f32) * -0.1f32 + 1.0f32) as f64;
        let b = (x * 1600.0 * 100.0 + 0.5) as i64 + (1734 - 1600) * 100;
        assert_eq!(trade_price(1734, 100, -0.1, &EC), a.max(b).max(0));
        assert!(trade_price(10, 1, -5.0, &EC) == 0, "clamped at zero");
    }

    #[test]
    fn relative_price_scale() {
        assert_eq!(relative_price(1600, &EC), 0.0);
        assert_eq!(relative_price(2200, &EC), 1.0);
        assert_eq!(relative_price(1000, &EC), -1.0);
        assert_eq!(relative_price(1300, &EC), (1300.0f32 - 1000.0) / (1600.0 - 1000.0) - 1.0);
        assert_eq!(relative_price(500, &EC), -2.0);
        assert_eq!(relative_price(4400, &EC), 2.0);
        assert_eq!(relative_price(3300, &EC), (3300.0f32 - 2200.0) / 2200.0 + 1.0);
        assert_eq!(quantity_factor(2400, &EC), 1.5);
    }

    #[test]
    fn stock_price_curve() {
        // Edges: above target -> min; empty or no target -> max; half target -> average.
        assert_eq!(stock_price(&EC, 101, 100), 1000);
        assert_eq!(stock_price(&EC, 0, 100), 2200);
        assert_eq!(stock_price(&EC, 0, 0), 2200);
        assert_eq!(stock_price(&EC, -5, 100), 2200);
        assert_eq!(stock_price(&EC, 50, 100), 1600);
        // At the target the cubic reaches the minimum (up to the f64 rounding of 1/0.7).
        let at_target = stock_price(&EC, 100, 100);
        assert!((999..=1000).contains(&at_target), "{at_target}");
        // Monotonic falling between empty and full.
        let mut last = i64::MAX;
        for s in 1..=1000 {
            let p = stock_price(&EC, s, 1000);
            assert!(p <= last, "stock {s}: {p} > {last}");
            assert!((999..=2200).contains(&p));
            last = p;
        }
        // One value by hand in the engine's operation order: stock 25 of 100.
        let (t, h) = (100.0f64, 50.0f64);
        let d = 25.0f64 - h;
        let neg = -600.0f64;
        let k = neg * 1.428_571_428_571_428_4;
        let v = ((neg - k) / ((t * t) * (h * 0.25))) * ((d * d) * d) + (k * d) / h + 1600.0;
        assert_eq!(stock_price(&EC, 25, 100), v as i64);
        assert_eq!(stock_price(&EC, 25, 100), 1996); // 1600 + 428.57 - 32.14
    }

    #[test]
    fn interpolation_and_cycles() {
        assert_eq!(lerp_clamped(0.0, 10.0, 1.0, 3.0, 5.0), 2.0);
        assert_eq!(lerp_clamped(0.0, 10.0, 1.0, 3.0, -1.0), 1.0);
        assert_eq!(lerp_clamped(0.0, 10.0, 1.0, 3.0, 12.0), 3.0);
        assert_eq!(lerp_clamped(5.0, 5.00001, 1.0, 3.0, 0.0), 2.0);
        assert_eq!(cycles_in(300.0, 3600.0), 12.0);
        assert_eq!(cycles_in(0.0, 3600.0), 0.0);
    }
}
