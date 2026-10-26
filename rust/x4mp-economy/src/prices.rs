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

#[cfg(test)]
mod tests {
    use super::*;

    // energycells: min 10, average 16, max 22 credits (wares.xml), in cents.
    const EC: WarePrices = WarePrices { avg: 1600, min: 1000, max: 2200 };

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
}
