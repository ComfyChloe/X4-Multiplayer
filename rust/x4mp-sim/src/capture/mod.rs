//! Capture format v1: compact, versioned, build-pinned per-frame snapshots of engine state.
//!
//! Written by the native mirror probe (`native/engine/mirror.h`) and by Rust
//! ([`CaptureWriter`]); read by [`CaptureReader`]; loaded into a [`crate::Universe`] by
//! [`mirror()`]. All integers are little endian. Layout:
//!
//! ```text
//! file    := magic "X4MPCAP\0" (8) | u16 major=1 | u16 minor | u32 reserved (must be 0) | record*
//! record  := u32 tag | u32 len | payload[len] | u64 check
//!            check = FNV-1a 64 over (tag_le, len_le, payload)
//! tags    := "HEAD" (exactly once, first) | "DICT"* | "FRAM"* | "STAT" (optional, last)
//! HEAD    := u16 major | u16 minor (copy of the file start, so it is checksummed)
//!            | u8 platform (0 synthetic, 1 windows, 2 linux) | u8[3] 0 | u32 steam_build
//!            | u8[32] exe_sha256 | u32 pe_timestamp | u32 image_size | u8[32] layout_sha256
//!            | u64 session_seed | u32 interval | u32 flags | str producer
//!            | u16 ncols | ncols x (u16 id | u8 elem | u8 0 | u16 count | str name)
//! DICT    := u8 dict (1 faction) | u8[3] 0 | u32 n | n x (u32 index | str value)
//! FRAM    := u64 frame | u64 game_time_bits | u32 nsectors | u32 flags
//!            | nsectors x (u64 root_uid | u8 kind | u8 0 | u16 ncols | u32 nrows
//!                          | ncols x (u16 id | u16 0 | u32 nbytes | bytes))
//! STAT    := u64 frames | u64 dropped | u64 bytes | u64 walk_errors
//! str     := u16 len | UTF-8 bytes
//! ```
//!
//! A column's bytes are `nrows x count` elements of its type, densely packed in row order
//! (tree order). Every column id in a frame must appear in the header schema, and `nbytes`
//! must equal `nrows * count * size(elem)`. Readers skip unknown record tags; a higher minor
//! version is accepted, a different major version is refused. The layout hash pins the
//! probe's offset table (`native/engine/mirror_layout.py`); captures with an unexpected hash
//! are refused by [`CaptureReader::require_layout`].

use std::fmt;

pub mod check;
pub mod mirror;
pub mod read;
pub mod write;

pub use mirror::{capture_universe, mirror, MirrorReport};
pub use read::CaptureReader;
pub use write::CaptureWriter;

pub const MAGIC: [u8; 8] = *b"X4MPCAP\0";
pub const VERSION_MAJOR: u16 = 1;
pub const VERSION_MINOR: u16 = 0;

pub const TAG_HEAD: u32 = u32::from_le_bytes(*b"HEAD");
pub const TAG_DICT: u32 = u32::from_le_bytes(*b"DICT");
pub const TAG_FRAM: u32 = u32::from_le_bytes(*b"FRAM");
pub const TAG_STAT: u32 = u32::from_le_bytes(*b"STAT");

/// Upper bound for one record's payload (corruption guard).
pub const MAX_RECORD: u32 = 1 << 30;

/// FRAM flag: the probe hit its row limit; the frame is incomplete.
pub const FRAME_PARTIAL: u32 = 1;
/// FRAM flag: frames were dropped (writer busy) since the previous record.
pub const FRAME_AFTER_DROP: u32 = 2;

/// Element type of a column.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum Elem {
    U8 = 1,
    U16 = 2,
    U32 = 3,
    U64 = 4,
    I8 = 5,
    I16 = 6,
    I32 = 7,
    I64 = 8,
    F32 = 9,
    F64 = 10,
}

impl Elem {
    pub fn from_u8(v: u8) -> Option<Elem> {
        Some(match v {
            1 => Elem::U8,
            2 => Elem::U16,
            3 => Elem::U32,
            4 => Elem::U64,
            5 => Elem::I8,
            6 => Elem::I16,
            7 => Elem::I32,
            8 => Elem::I64,
            9 => Elem::F32,
            10 => Elem::F64,
            _ => return None,
        })
    }

    pub fn size(self) -> usize {
        match self {
            Elem::U8 | Elem::I8 => 1,
            Elem::U16 | Elem::I16 => 2,
            Elem::U32 | Elem::I32 | Elem::F32 => 4,
            Elem::U64 | Elem::I64 | Elem::F64 => 8,
        }
    }
}

/// One column of the header schema.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ColumnDesc {
    pub id: u16,
    pub elem: Elem,
    /// Elements per row.
    pub count: u16,
    pub name: String,
}

impl ColumnDesc {
    pub fn new(id: u16, elem: Elem, count: u16, name: &str) -> ColumnDesc {
        ColumnDesc { id, elem, count, name: name.to_string() }
    }

    pub fn row_bytes(&self) -> usize {
        self.elem.size() * usize::from(self.count)
    }
}

/// Core column ids (v1). Ids >= 0x100 are reserved for system agents; register new ids in
/// `rewrite-coord/interfaces.md` first.
pub mod col {
    /// `u64` UniverseId (`Component+0x08`).
    pub const UID: u16 = 0x01;
    /// `u8` class id (GetClassID equivalent; 0xff = unknown vtable).
    pub const CLASS: u16 = 0x02;
    /// `u64` parent UniverseId (`+0x70` -> `+0x08`; 0 = none).
    pub const PARENT: u16 = 0x03;
    /// `u16` depth below the record root (root = 0).
    pub const DEPTH: u16 = 0x04;
    /// `i32` attention (`+0x1ec`).
    pub const ATTENTION: u16 = 0x05;
    /// `u8` flags: bit0 registered (`+0xd1`), bits 1-2 death (`+0xd3`), bit3 mode (`+0x68`)
    /// != 0, bit4 known vtable.
    pub const FLAGS: u16 = 0x06;
    /// `u32` primary vtable RVA (0 when outside the image).
    pub const VTABLE: u16 = 0x07;
    /// `u8` controller kind ([`crate::comp::CtrlKind`]).
    pub const CTRL: u16 = 0x08;
    /// `u32` controller primary vtable RVA.
    pub const CTRL_VTABLE: u16 = 0x09;
    /// `f32 x16` local pose per controller kind (zero when not captured).
    pub const POSE: u16 = 0x0a;
    /// `f32 x8` linear4 + angular4 velocity (navigation only).
    pub const VEL: u16 = 0x0b;
    /// `u64` pathing slot reference component (`+0xb8`).
    pub const POSE_REF: u16 = 0x0c;
    /// `f32 x16` NavigationController cached mat4 (`controller+0x60`).
    pub const NAV_CACHE: u16 = 0x0d;
    /// `f64` hull (`Destructible+0x280`; NaN when not destructible).
    pub const HULL: u16 = 0x0e;
    /// `u16` true owner faction (dictionary index; 0xffff none).
    pub const OWNER: u16 = 0x0f;
    /// `u16` cover owner faction.
    pub const COVER: u16 = 0x10;
    /// `u32 x2` order vector length, current order index (Controllable).
    pub const ORDERS: u16 = 0x11;
}

/// The v1 core schema in id order.
pub fn core_schema() -> Vec<ColumnDesc> {
    use Elem::*;
    vec![
        ColumnDesc::new(col::UID, U64, 1, "uid"),
        ColumnDesc::new(col::CLASS, U8, 1, "class"),
        ColumnDesc::new(col::PARENT, U64, 1, "parent"),
        ColumnDesc::new(col::DEPTH, U16, 1, "depth"),
        ColumnDesc::new(col::ATTENTION, I32, 1, "attention"),
        ColumnDesc::new(col::FLAGS, U8, 1, "flags"),
        ColumnDesc::new(col::VTABLE, U32, 1, "vtable"),
        ColumnDesc::new(col::CTRL, U8, 1, "ctrl"),
        ColumnDesc::new(col::CTRL_VTABLE, U32, 1, "ctrl_vtable"),
        ColumnDesc::new(col::POSE, F32, 16, "pose"),
        ColumnDesc::new(col::VEL, F32, 8, "vel"),
        ColumnDesc::new(col::POSE_REF, U64, 1, "pose_ref"),
        ColumnDesc::new(col::NAV_CACHE, F32, 16, "nav_cache"),
        ColumnDesc::new(col::HULL, F64, 1, "hull"),
        ColumnDesc::new(col::OWNER, U16, 1, "owner"),
        ColumnDesc::new(col::COVER, U16, 1, "cover"),
        ColumnDesc::new(col::ORDERS, U32, 2, "orders"),
    ]
}

/// Dictionaries carried by DICT records.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
#[repr(u8)]
pub enum Dict {
    Faction = 1,
}

/// Which platform produced the capture.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum Platform {
    Synthetic = 0,
    Windows = 1,
    Linux = 2,
}

impl Platform {
    pub fn from_u8(v: u8) -> Option<Platform> {
        match v {
            0 => Some(Platform::Synthetic),
            1 => Some(Platform::Windows),
            2 => Some(Platform::Linux),
            _ => None,
        }
    }
}

/// The engine build a capture belongs to.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BuildPin {
    pub platform: Platform,
    pub steam_build: u32,
    pub exe_sha256: [u8; 32],
    pub pe_timestamp: u32,
    pub image_size: u32,
}

fn hex32(s: &str) -> [u8; 32] {
    let mut out = [0u8; 32];
    for (i, o) in out.iter_mut().enumerate() {
        *o = u8::from_str_radix(&s[2 * i..2 * i + 2], 16).expect("pinned hex");
    }
    out
}

impl BuildPin {
    /// The one supported Windows build (values from `x4mp_core::pins`).
    pub fn windows() -> BuildPin {
        use x4mp_core::pins;
        BuildPin {
            platform: Platform::Windows,
            steam_build: pins::STEAM_BUILD,
            exe_sha256: hex32(pins::windows::FILE_SHA256),
            pe_timestamp: pins::windows::PE_TIMESTAMP,
            image_size: pins::windows::SIZE_OF_IMAGE,
        }
    }

    /// Synthetic (test) captures: no engine build.
    pub fn synthetic() -> BuildPin {
        BuildPin { platform: Platform::Synthetic, steam_build: 0, exe_sha256: [0; 32], pe_timestamp: 0, image_size: 0 }
    }

    pub fn describe(&self) -> String {
        format!(
            "{:?} build={} sha256={} pe_ts={:#x} image={:#x}",
            self.platform,
            self.steam_build,
            x4mp_core::sha256::hex(&self.exe_sha256),
            self.pe_timestamp,
            self.image_size
        )
    }
}

/// HEAD record.
#[derive(Debug, Clone, PartialEq)]
pub struct Header {
    pub version: (u16, u16),
    pub pin: BuildPin,
    pub layout_sha256: [u8; 32],
    pub session_seed: u64,
    /// Frames between captured frames (1 = every frame).
    pub interval: u32,
    pub flags: u32,
    pub producer: String,
    pub columns: Vec<ColumnDesc>,
}

impl Header {
    /// A synthetic header with the core schema.
    pub fn synthetic(producer: &str, session_seed: u64) -> Header {
        Header {
            version: (VERSION_MAJOR, VERSION_MINOR),
            pin: BuildPin::synthetic(),
            layout_sha256: [0; 32],
            session_seed,
            interval: 1,
            flags: 0,
            producer: producer.to_string(),
            columns: core_schema(),
        }
    }

    pub fn column(&self, id: u16) -> Option<&ColumnDesc> {
        self.columns.iter().find(|c| c.id == id)
    }
}

/// One column's raw values in a sector record.
#[derive(Debug, Clone, PartialEq)]
pub struct ColumnData {
    pub id: u16,
    pub bytes: Vec<u8>,
}

macro_rules! typed {
    ($name:ident, $from:ident, $t:ty, $n:expr) => {
        pub fn $name(&self) -> Vec<$t> {
            self.bytes.chunks_exact($n).map(|c| <$t>::from_le_bytes(c.try_into().expect("chunk"))).collect()
        }
        pub fn $from(id: u16, values: &[$t]) -> ColumnData {
            let mut bytes = Vec::with_capacity(values.len() * $n);
            for v in values {
                bytes.extend_from_slice(&v.to_le_bytes());
            }
            ColumnData { id, bytes }
        }
    };
}

impl ColumnData {
    typed!(u8s, from_u8s, u8, 1);
    typed!(u16s, from_u16s, u16, 2);
    typed!(u32s, from_u32s, u32, 4);
    typed!(u64s, from_u64s, u64, 8);
    typed!(i32s, from_i32s, i32, 4);
    typed!(f32s, from_f32s, f32, 4);
    typed!(f64s, from_f64s, f64, 8);
}

/// One world root (sector / cluster-level / galaxy-level) in a frame.
#[derive(Debug, Clone, PartialEq)]
pub struct SectorRecord {
    pub root_uid: u64,
    /// [`crate::world::WorldKind`] as u8.
    pub kind: u8,
    pub rows: u32,
    pub columns: Vec<ColumnData>,
}

impl SectorRecord {
    pub fn column(&self, id: u16) -> Option<&ColumnData> {
        self.columns.iter().find(|c| c.id == id)
    }
}

/// One captured frame.
#[derive(Debug, Clone, PartialEq)]
pub struct FrameRecord {
    pub frame: u64,
    pub game_time: f64,
    pub flags: u32,
    pub sectors: Vec<SectorRecord>,
}

impl FrameRecord {
    pub fn rows(&self) -> u64 {
        self.sectors.iter().map(|s| u64::from(s.rows)).sum()
    }
}

/// STAT record written at close.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct WriterStats {
    pub frames: u64,
    pub dropped: u64,
    pub bytes: u64,
    pub walk_errors: u64,
}

/// Capture read/validation errors.
#[derive(Debug)]
pub enum CapError {
    Io(std::io::Error),
    BadMagic,
    Version { major: u16, minor: u16 },
    Truncated { offset: u64 },
    Checksum { offset: u64, tag: u32 },
    Malformed { offset: u64, what: String },
    Schema(String),
    Pin(String),
}

impl fmt::Display for CapError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CapError::Io(e) => write!(f, "io: {e}"),
            CapError::BadMagic => write!(f, "not an X4MPCAP file"),
            CapError::Version { major, minor } => write!(f, "unsupported format version {major}.{minor}"),
            CapError::Truncated { offset } => write!(f, "truncated record at offset {offset}"),
            CapError::Checksum { offset, tag } => {
                write!(f, "checksum mismatch in record {} at offset {offset}", tag_name(*tag))
            }
            CapError::Malformed { offset, what } => write!(f, "malformed record at offset {offset}: {what}"),
            CapError::Schema(s) => write!(f, "schema: {s}"),
            CapError::Pin(s) => write!(f, "build pin: {s}"),
        }
    }
}

impl std::error::Error for CapError {}

impl From<std::io::Error> for CapError {
    fn from(e: std::io::Error) -> CapError {
        CapError::Io(e)
    }
}

pub fn tag_name(tag: u32) -> String {
    tag.to_le_bytes().iter().map(|b| if b.is_ascii_graphic() { *b as char } else { '?' }).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn schema_ids_unique_and_sized() {
        let s = core_schema();
        for (i, c) in s.iter().enumerate() {
            assert!(s[i + 1..].iter().all(|d| d.id != c.id && d.name != c.name), "{c:?}");
        }
        assert_eq!(s.iter().map(|c| c.row_bytes()).sum::<usize>(), 221);
        assert_eq!(tag_name(TAG_FRAM), "FRAM");
    }

    #[test]
    fn windows_pin_matches_core_pins() {
        let p = BuildPin::windows();
        assert_eq!(p.steam_build, 23_660_954);
        assert_eq!(p.exe_sha256[0], 0x19);
        assert_eq!(p.exe_sha256[31], 0x91);
        assert_eq!(p.pe_timestamp, 0x6a1e_aded);
        assert_eq!(p.image_size, 0x06fa_9000);
    }

    #[test]
    fn typed_column_roundtrip() {
        let c = ColumnData::from_f32s(7, &[1.5, -0.0, f32::MAX]);
        assert_eq!(
            c.f32s().iter().map(|v| v.to_bits()).collect::<Vec<_>>(),
            vec![1.5f32.to_bits(), 0x8000_0000, f32::MAX.to_bits()]
        );
        assert_eq!(ColumnData::from_u64s(1, &[u64::MAX, 3]).u64s(), vec![u64::MAX, 3]);
    }
}
