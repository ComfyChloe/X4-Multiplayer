//! Script-visible values as the behaviours and the X4AITRACE format see them.
//!
//! Floats compare by bits everywhere (the exactness policy: discrete data exact, floating point
//! bit-identical). Text encoding (one token, no tabs):
//!
//! | value | encoding | example |
//! |---|---|---|
//! | null | `n` | `n` |
//! | bool | `b:0` / `b:1` | `b:1` |
//! | integer | `i:<decimal>` | `i:-3` |
//! | f32 | `f:<8 hex bits>` (writer appends `~<decimal>`), or `F:<decimal>` (hand-written) | `f:3f800000~1` |
//! | f64 | `d:<16 hex bits>` (writer appends `~<decimal>`), or `D:<decimal>` | `D:0.001` |
//! | component | `c:0x<hex>` (engine UniverseID, 0 = null component) | `c:0x1a2b` |
//! | order | `o:0x<hex>` (AI order handle; 0 = null) | `o:0x51` |
//! | raw u64 | `x:<hex>` (RNG states, masks) | `x:f3da54e488294596` |
//! | position | `p:<x>,<y>,<z>` with each part an f32 token without prefix: 8 hex bits or `F<decimal>` | `p:F1,F0,F-2.5` |
//! | keyword | `k:<token>` (classes, enums, order ids, script names) | `k:zone` |
//! | string | `s:<text>` with `\t \n \\ \; \[ \]` escaped | `s:Patrol` |
//! | list | `l:[<v>;<v>;...]` | `l:[c:0x10;c:0x11]` |

use std::fmt;

/// Engine component id (`U::UniverseID`, the payload of script value tag 10). 0 is the null component.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord, Default)]
pub struct Uid(pub u64);

impl Uid {
    pub const NULL: Uid = Uid(0);
    pub const fn is_null(self) -> bool {
        self.0 == 0
    }
}

/// Script position (XLib vector of three f32).
#[derive(Clone, Copy, Debug, Default)]
pub struct Pos {
    pub x: f32,
    pub y: f32,
    pub z: f32,
}

impl Pos {
    pub const fn new(x: f32, y: f32, z: f32) -> Pos {
        Pos { x, y, z }
    }
    pub fn bits(&self) -> [u32; 3] {
        [self.x.to_bits(), self.y.to_bits(), self.z.to_bits()]
    }
}

impl PartialEq for Pos {
    fn eq(&self, o: &Pos) -> bool {
        self.bits() == o.bits()
    }
}
impl Eq for Pos {}

/// Game time in seconds (`player.age`, TLS+0x300, f64).
pub type Time = f64;

/// A script value.
#[derive(Clone, Debug)]
pub enum Val {
    Null,
    Bool(bool),
    Int(i64),
    F32(f32),
    F64(f64),
    Uid(Uid),
    Order(OrderId),
    U64(u64),
    Pos(Pos),
    Key(String),
    Str(String),
    List(Vec<Val>),
}

/// Handle of an AI order (script value tag 0x10, printed `order<0x..>`). 0 is the null order.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord, Default)]
pub struct OrderId(pub u64);

impl OrderId {
    pub const NULL: OrderId = OrderId(0);
    pub const fn is_null(self) -> bool {
        self.0 == 0
    }
}

impl PartialEq for Val {
    fn eq(&self, o: &Val) -> bool {
        use Val::*;
        match (self, o) {
            (Null, Null) => true,
            (Bool(a), Bool(b)) => a == b,
            (Int(a), Int(b)) => a == b,
            (F32(a), F32(b)) => a.to_bits() == b.to_bits(),
            (F64(a), F64(b)) => a.to_bits() == b.to_bits(),
            (Uid(a), Uid(b)) => a == b,
            (Order(a), Order(b)) => a == b,
            (U64(a), U64(b)) => a == b,
            (Pos(a), Pos(b)) => a == b,
            (Key(a), Key(b)) => a == b,
            (Str(a), Str(b)) => a == b,
            (List(a), List(b)) => a == b,
            _ => false,
        }
    }
}

impl Val {
    pub fn key(s: &str) -> Val {
        Val::Key(s.to_string())
    }
    pub fn uid(u: Uid) -> Val {
        if u.is_null() {
            Val::Null
        } else {
            Val::Uid(u)
        }
    }
    pub fn opt_uid(u: Option<Uid>) -> Val {
        match u {
            Some(u) => Val::uid(u),
            None => Val::Null,
        }
    }
    pub fn uids(v: &[Uid]) -> Val {
        Val::List(v.iter().map(|u| Val::Uid(*u)).collect())
    }

    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Val::Bool(b) => Some(*b),
            _ => None,
        }
    }
    pub fn as_uid(&self) -> Option<Uid> {
        match self {
            Val::Uid(u) => Some(*u),
            Val::Null => Some(Uid::NULL),
            _ => None,
        }
    }
    pub fn order(o: OrderId) -> Val {
        if o.is_null() {
            Val::Null
        } else {
            Val::Order(o)
        }
    }
    pub fn as_order(&self) -> Option<OrderId> {
        match self {
            Val::Order(o) => Some(*o),
            Val::Null => Some(OrderId::NULL),
            _ => None,
        }
    }
    pub fn as_u64(&self) -> Option<u64> {
        match self {
            Val::U64(v) => Some(*v),
            _ => None,
        }
    }
    pub fn as_f32(&self) -> Option<f32> {
        match self {
            Val::F32(v) => Some(*v),
            _ => None,
        }
    }
    pub fn as_f64(&self) -> Option<f64> {
        match self {
            Val::F64(v) => Some(*v),
            _ => None,
        }
    }
    pub fn as_int(&self) -> Option<i64> {
        match self {
            Val::Int(v) => Some(*v),
            _ => None,
        }
    }
    pub fn as_pos(&self) -> Option<Pos> {
        match self {
            Val::Pos(p) => Some(*p),
            _ => None,
        }
    }
    pub fn as_key(&self) -> Option<&str> {
        match self {
            Val::Key(k) => Some(k),
            _ => None,
        }
    }
    pub fn as_list(&self) -> Option<&[Val]> {
        match self {
            Val::List(l) => Some(l),
            _ => None,
        }
    }
    pub fn as_uid_list(&self) -> Option<Vec<Uid>> {
        let l = self.as_list()?;
        l.iter().map(|v| v.as_uid()).collect()
    }

    /// Parse one encoded token (see the module table).
    pub fn parse(t: &str) -> Result<Val, String> {
        if t == "n" {
            return Ok(Val::Null);
        }
        let (tag, body) = t.split_once(':').ok_or_else(|| format!("value without tag: {t:?}"))?;
        match tag {
            "b" => match body {
                "0" => Ok(Val::Bool(false)),
                "1" => Ok(Val::Bool(true)),
                _ => Err(format!("bad bool {t:?}")),
            },
            "i" => body.parse::<i64>().map(Val::Int).map_err(|e| format!("bad int {t:?}: {e}")),
            "f" => parse_f32_bits(body).map(Val::F32),
            "F" => body.parse::<f32>().map(Val::F32).map_err(|e| format!("bad float {t:?}: {e}")),
            "d" => {
                let h = body.split('~').next().unwrap_or("");
                u64::from_str_radix(h, 16)
                    .map(|b| Val::F64(f64::from_bits(b)))
                    .map_err(|e| format!("bad double {t:?}: {e}"))
            }
            "D" => body.parse::<f64>().map(Val::F64).map_err(|e| format!("bad double {t:?}: {e}")),
            "c" => {
                let h = body.strip_prefix("0x").ok_or_else(|| format!("component needs 0x: {t:?}"))?;
                u64::from_str_radix(h, 16).map(|v| Val::uid(Uid(v))).map_err(|e| format!("bad component {t:?}: {e}"))
            }
            "o" => {
                let h = body.strip_prefix("0x").ok_or_else(|| format!("order needs 0x: {t:?}"))?;
                u64::from_str_radix(h, 16).map(|v| Val::order(OrderId(v))).map_err(|e| format!("bad order {t:?}: {e}"))
            }
            "x" => u64::from_str_radix(body, 16).map(Val::U64).map_err(|e| format!("bad u64 {t:?}: {e}")),
            "p" => {
                let parts: Vec<&str> = body.split(',').collect();
                if parts.len() != 3 {
                    return Err(format!("position needs 3 parts: {t:?}"));
                }
                let mut c = [0f32; 3];
                for (i, p) in parts.iter().enumerate() {
                    c[i] = if let Some(d) = p.strip_prefix('F') {
                        d.parse::<f32>().map_err(|e| format!("bad position part {p:?}: {e}"))?
                    } else {
                        parse_f32_bits(p)?
                    };
                }
                Ok(Val::Pos(Pos::new(c[0], c[1], c[2])))
            }
            "k" => Ok(Val::Key(body.to_string())),
            "s" => Ok(Val::Str(unescape(body))),
            "l" => {
                let inner = body
                    .strip_prefix('[')
                    .and_then(|b| b.strip_suffix(']'))
                    .ok_or_else(|| format!("list needs [..]: {t:?}"))?;
                let mut out = Vec::new();
                for item in split_list(inner) {
                    out.push(Val::parse(&item)?);
                }
                Ok(Val::List(out))
            }
            _ => Err(format!("unknown value tag {tag:?} in {t:?}")),
        }
    }
}

fn parse_f32_bits(body: &str) -> Result<f32, String> {
    let h = body.split('~').next().unwrap_or("");
    if h.len() != 8 {
        return Err(format!("f32 bits need 8 hex digits: {body:?}"));
    }
    u32::from_str_radix(h, 16).map(f32::from_bits).map_err(|e| format!("bad f32 bits {body:?}: {e}"))
}

/// Split a list body at top-level `;` (nested lists and escapes respected).
fn split_list(s: &str) -> Vec<String> {
    let mut out = Vec::new();
    if s.is_empty() {
        return out;
    }
    let mut depth = 0i32;
    let mut cur = String::new();
    let mut esc = false;
    for ch in s.chars() {
        if esc {
            cur.push('\\');
            cur.push(ch);
            esc = false;
            continue;
        }
        match ch {
            '\\' => esc = true,
            '[' => {
                depth += 1;
                cur.push(ch)
            }
            ']' => {
                depth -= 1;
                cur.push(ch)
            }
            ';' if depth == 0 => out.push(std::mem::take(&mut cur)),
            _ => cur.push(ch),
        }
    }
    out.push(cur);
    out
}

fn escape(s: &str) -> String {
    let mut o = String::with_capacity(s.len());
    for ch in s.chars() {
        match ch {
            '\t' => o.push_str("\\t"),
            '\n' => o.push_str("\\n"),
            '\\' => o.push_str("\\\\"),
            ';' => o.push_str("\\;"),
            '[' => o.push_str("\\["),
            ']' => o.push_str("\\]"),
            _ => o.push(ch),
        }
    }
    o
}

fn unescape(s: &str) -> String {
    let mut o = String::with_capacity(s.len());
    let mut it = s.chars();
    while let Some(ch) = it.next() {
        if ch == '\\' {
            match it.next() {
                Some('t') => o.push('\t'),
                Some('n') => o.push('\n'),
                Some(c) => o.push(c),
                None => o.push('\\'),
            }
        } else {
            o.push(ch);
        }
    }
    o
}

impl fmt::Display for Val {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Val::Null => write!(f, "n"),
            Val::Bool(b) => write!(f, "b:{}", *b as u8),
            Val::Int(i) => write!(f, "i:{i}"),
            Val::F32(v) => write!(f, "f:{:08x}~{v}", v.to_bits()),
            Val::F64(v) => write!(f, "d:{:016x}~{v}", v.to_bits()),
            Val::Uid(u) => write!(f, "c:0x{:x}", u.0),
            Val::Order(o) => write!(f, "o:0x{:x}", o.0),
            Val::U64(v) => write!(f, "x:{v:016x}"),
            Val::Pos(p) => write!(f, "p:{:08x},{:08x},{:08x}", p.x.to_bits(), p.y.to_bits(), p.z.to_bits()),
            Val::Key(k) => write!(f, "k:{k}"),
            Val::Str(s) => write!(f, "s:{}", escape(s)),
            Val::List(l) => {
                write!(f, "l:[")?;
                for (i, v) in l.iter().enumerate() {
                    if i > 0 {
                        write!(f, ";")?;
                    }
                    write!(f, "{v}")?;
                }
                write!(f, "]")
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trip() {
        let vals = vec![
            Val::Null,
            Val::Bool(true),
            Val::Int(-42),
            Val::F32(1.5),
            Val::F32(-0.0),
            Val::F64(0.001),
            Val::Uid(Uid(0x1a2b)),
            Val::Order(OrderId(0x51)),
            Val::U64(0xf3da54e488294596),
            Val::Pos(Pos::new(1.0, -2.5, 3.25)),
            Val::key("zone"),
            Val::Str("a;b[c]\td".into()),
            Val::List(vec![Val::Uid(Uid(1)), Val::List(vec![Val::Int(2), Val::Null]), Val::Str("x;y".into())]),
        ];
        for v in vals {
            let s = v.to_string();
            assert_eq!(Val::parse(&s).unwrap(), v, "{s}");
        }
    }

    #[test]
    fn hand_written_forms() {
        assert_eq!(Val::parse("F:2000").unwrap(), Val::F32(2000.0));
        assert_eq!(Val::parse("D:0.001").unwrap(), Val::F64(0.001));
        assert_eq!(Val::parse("p:F1,F0,F-2").unwrap(), Val::Pos(Pos::new(1.0, 0.0, -2.0)));
        assert_eq!(Val::parse("c:0x0").unwrap(), Val::Null);
        assert_eq!(Val::parse("l:[]").unwrap(), Val::List(vec![]));
        assert!(Val::F32(0.0) != Val::F32(-0.0), "floats compare by bits");
    }
}
