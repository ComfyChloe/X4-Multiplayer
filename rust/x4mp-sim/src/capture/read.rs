//! Capture reader (format: see the module docs of [`crate::capture`]).

use std::collections::BTreeMap;
use std::io::{self, Read};

use super::{
    BuildPin, CapError, ColumnData, ColumnDesc, Dict, Elem, FrameRecord, Header, Platform, SectorRecord, WriterStats,
    MAGIC, MAX_RECORD, TAG_DICT, TAG_FRAM, TAG_HEAD, TAG_STAT, VERSION_MAJOR,
};
use crate::hash::Fnv64;

struct Cur<'a> {
    b: &'a [u8],
    pos: usize,
    base: u64,
}

impl<'a> Cur<'a> {
    fn new(b: &'a [u8], base: u64) -> Cur<'a> {
        Cur { b, pos: 0, base }
    }

    fn err(&self, what: &str) -> CapError {
        CapError::Malformed { offset: self.base + self.pos as u64, what: what.to_string() }
    }

    fn take(&mut self, n: usize) -> Result<&'a [u8], CapError> {
        if self.b.len() - self.pos < n {
            return Err(self.err("payload too short"));
        }
        let s = &self.b[self.pos..self.pos + n];
        self.pos += n;
        Ok(s)
    }

    fn u8(&mut self) -> Result<u8, CapError> {
        Ok(self.take(1)?[0])
    }
    fn u16(&mut self) -> Result<u16, CapError> {
        Ok(u16::from_le_bytes(self.take(2)?.try_into().expect("2")))
    }
    fn u32(&mut self) -> Result<u32, CapError> {
        Ok(u32::from_le_bytes(self.take(4)?.try_into().expect("4")))
    }
    fn u64(&mut self) -> Result<u64, CapError> {
        Ok(u64::from_le_bytes(self.take(8)?.try_into().expect("8")))
    }
    fn arr32(&mut self) -> Result<[u8; 32], CapError> {
        Ok(self.take(32)?.try_into().expect("32"))
    }
    fn str(&mut self) -> Result<String, CapError> {
        let n = self.u16()? as usize;
        let b = self.take(n)?;
        String::from_utf8(b.to_vec()).map_err(|_| self.err("string is not UTF-8"))
    }
    fn done(&self) -> Result<(), CapError> {
        if self.pos != self.b.len() {
            return Err(self.err("trailing bytes in record"));
        }
        Ok(())
    }
}

fn read_exact_or_eof<R: Read>(r: &mut R, buf: &mut [u8]) -> io::Result<usize> {
    let mut got = 0;
    while got < buf.len() {
        match r.read(&mut buf[got..]) {
            Ok(0) => break,
            Ok(n) => got += n,
            Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
            Err(e) => return Err(e),
        }
    }
    Ok(got)
}

fn parse_header(p: &[u8], base: u64, version: (u16, u16)) -> Result<Header, CapError> {
    let mut c = Cur::new(p, base);
    if (c.u16()?, c.u16()?) != version {
        return Err(c.err("file version differs from the HEAD copy"));
    }
    let platform = Platform::from_u8(c.u8()?).ok_or_else(|| c.err("unknown platform"))?;
    c.take(3)?;
    let steam_build = c.u32()?;
    let exe_sha256 = c.arr32()?;
    let pe_timestamp = c.u32()?;
    let image_size = c.u32()?;
    let layout_sha256 = c.arr32()?;
    let session_seed = c.u64()?;
    let interval = c.u32()?;
    let flags = c.u32()?;
    let producer = c.str()?;
    let ncols = c.u16()?;
    let mut columns: Vec<ColumnDesc> = Vec::with_capacity(ncols as usize);
    for _ in 0..ncols {
        let id = c.u16()?;
        let elem = Elem::from_u8(c.u8()?).ok_or_else(|| c.err("unknown element type"))?;
        c.u8()?;
        let count = c.u16()?;
        let name = c.str()?;
        if count == 0 || columns.iter().any(|d| d.id == id) {
            return Err(CapError::Schema(format!("column {id:#x} duplicated or empty")));
        }
        columns.push(ColumnDesc { id, elem, count, name });
    }
    c.done()?;
    Ok(Header {
        version,
        pin: BuildPin { platform, steam_build, exe_sha256, pe_timestamp, image_size },
        layout_sha256,
        session_seed,
        interval,
        flags,
        producer,
        columns,
    })
}

fn parse_frame(h: &Header, p: &[u8], base: u64) -> Result<FrameRecord, CapError> {
    let mut c = Cur::new(p, base);
    let frame = c.u64()?;
    let game_time = f64::from_bits(c.u64()?);
    let nsectors = c.u32()?;
    let flags = c.u32()?;
    let mut sectors = Vec::with_capacity(nsectors.min(1 << 16) as usize);
    for _ in 0..nsectors {
        let root_uid = c.u64()?;
        let kind = c.u8()?;
        c.u8()?;
        let ncols = c.u16()?;
        let rows = c.u32()?;
        let mut columns: Vec<ColumnData> = Vec::with_capacity(ncols as usize);
        for _ in 0..ncols {
            let id = c.u16()?;
            c.u16()?;
            let nbytes = c.u32()? as usize;
            let desc = h
                .column(id)
                .ok_or_else(|| CapError::Schema(format!("frame {frame}: column {id:#x} not in the header")))?;
            if nbytes != rows as usize * desc.row_bytes() {
                return Err(CapError::Schema(format!(
                    "frame {frame}: column {} has {nbytes} bytes for {rows} rows (expected {})",
                    desc.name,
                    rows as usize * desc.row_bytes()
                )));
            }
            if columns.iter().any(|x| x.id == id) {
                return Err(CapError::Schema(format!("frame {frame}: column {} repeated", desc.name)));
            }
            columns.push(ColumnData { id, bytes: c.take(nbytes)?.to_vec() });
        }
        sectors.push(SectorRecord { root_uid, kind, rows, columns });
    }
    c.done()?;
    Ok(FrameRecord { frame, game_time, flags, sectors })
}

/// Streams a capture: header on open, then frames; DICT and STAT records are absorbed on the
/// way, unknown tags skipped (counted).
pub struct CaptureReader<R: Read> {
    r: R,
    offset: u64,
    header: Header,
    dicts: BTreeMap<(Dict, u32), String>,
    stats: Option<WriterStats>,
    unknown_records: u64,
}

impl<R: Read> CaptureReader<R> {
    pub fn open(mut r: R) -> Result<CaptureReader<R>, CapError> {
        let mut start = [0u8; 16];
        if read_exact_or_eof(&mut r, &mut start)? != 16 || start[..8] != MAGIC {
            return Err(CapError::BadMagic);
        }
        let major = u16::from_le_bytes([start[8], start[9]]);
        let minor = u16::from_le_bytes([start[10], start[11]]);
        if major != VERSION_MAJOR {
            return Err(CapError::Version { major, minor });
        }
        if start[12..16] != [0, 0, 0, 0] {
            return Err(CapError::Malformed { offset: 12, what: "reserved file header word is not zero".into() });
        }
        let mut reader = CaptureReader {
            r,
            offset: 16,
            header: Header::synthetic("", 0),
            dicts: BTreeMap::new(),
            stats: None,
            unknown_records: 0,
        };
        let (tag, payload, base) = reader.next_record()?.ok_or(CapError::Truncated { offset: 16 })?;
        if tag != TAG_HEAD {
            return Err(CapError::Malformed { offset: base, what: "first record is not HEAD".into() });
        }
        reader.header = parse_header(&payload, base, (major, minor))?;
        Ok(reader)
    }

    /// (tag, payload, payload offset); `None` at a clean end of file.
    fn next_record(&mut self) -> Result<Option<(u32, Vec<u8>, u64)>, CapError> {
        let start = self.offset;
        let mut head = [0u8; 8];
        match read_exact_or_eof(&mut self.r, &mut head)? {
            0 => return Ok(None),
            8 => {}
            _ => return Err(CapError::Truncated { offset: start }),
        }
        let tag = u32::from_le_bytes(head[..4].try_into().expect("4"));
        let len = u32::from_le_bytes(head[4..].try_into().expect("4"));
        if len > MAX_RECORD {
            return Err(CapError::Malformed { offset: start, what: format!("record length {len}") });
        }
        let mut payload = vec![0u8; len as usize];
        if read_exact_or_eof(&mut self.r, &mut payload)? != payload.len() {
            return Err(CapError::Truncated { offset: start });
        }
        let mut check = [0u8; 8];
        if read_exact_or_eof(&mut self.r, &mut check)? != 8 {
            return Err(CapError::Truncated { offset: start });
        }
        let mut h = Fnv64::new();
        h.write(&head);
        h.write(&payload);
        if h.finish() != u64::from_le_bytes(check) {
            return Err(CapError::Checksum { offset: start, tag });
        }
        self.offset = start + 16 + u64::from(len);
        Ok(Some((tag, payload, start + 8)))
    }

    pub fn header(&self) -> &Header {
        &self.header
    }

    /// Refuses captures of another build.
    pub fn require_pin(&self, pin: &BuildPin) -> Result<(), CapError> {
        if self.header.pin != *pin {
            return Err(CapError::Pin(format!(
                "capture is {}, expected {}",
                self.header.pin.describe(),
                pin.describe()
            )));
        }
        Ok(())
    }

    /// Refuses captures made with another probe offset table.
    pub fn require_layout(&self, sha256: &[u8; 32]) -> Result<(), CapError> {
        if self.header.layout_sha256 != *sha256 {
            return Err(CapError::Pin(format!(
                "layout {} expected {}",
                x4mp_core::sha256::hex(&self.header.layout_sha256),
                x4mp_core::sha256::hex(sha256)
            )));
        }
        Ok(())
    }

    /// Next frame; `Ok(None)` at the end.
    pub fn next_frame(&mut self) -> Result<Option<FrameRecord>, CapError> {
        loop {
            let Some((tag, payload, base)) = self.next_record()? else { return Ok(None) };
            match tag {
                TAG_FRAM => return parse_frame(&self.header, &payload, base).map(Some),
                TAG_DICT => self.absorb_dict(&payload, base)?,
                TAG_STAT => {
                    let mut c = Cur::new(&payload, base);
                    self.stats = Some(WriterStats {
                        frames: c.u64()?,
                        dropped: c.u64()?,
                        bytes: c.u64()?,
                        walk_errors: c.u64()?,
                    });
                    c.done()?;
                }
                TAG_HEAD => return Err(CapError::Malformed { offset: base, what: "second HEAD".into() }),
                _ => self.unknown_records += 1,
            }
        }
    }

    fn absorb_dict(&mut self, p: &[u8], base: u64) -> Result<(), CapError> {
        let mut c = Cur::new(p, base);
        let kind = c.u8()?;
        c.take(3)?;
        let n = c.u32()?;
        let dict = match kind {
            1 => Dict::Faction,
            _ => {
                self.unknown_records += 1;
                return Ok(());
            }
        };
        for _ in 0..n {
            let index = c.u32()?;
            let value = c.str()?;
            self.dicts.insert((dict, index), value);
        }
        c.done()
    }

    pub fn dict_value(&self, dict: Dict, index: u32) -> Option<&str> {
        self.dicts.get(&(dict, index)).map(|s| s.as_str())
    }

    /// All entries of a dictionary read so far.
    pub fn dict(&self, dict: Dict) -> Vec<(u32, &str)> {
        self.dicts.iter().filter(|((d, _), _)| *d == dict).map(|((_, i), v)| (*i, v.as_str())).collect()
    }

    pub fn stats(&self) -> Option<WriterStats> {
        self.stats
    }

    pub fn unknown_records(&self) -> u64 {
        self.unknown_records
    }

    /// Byte offset of the next record.
    pub fn offset(&self) -> u64 {
        self.offset
    }
}
