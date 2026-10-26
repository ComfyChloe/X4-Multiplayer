//! Capture writer (format: see the module docs of [`crate::capture`]).

use std::io::{self, Write};

use super::{
    ColumnData, Dict, FrameRecord, Header, WriterStats, MAGIC, TAG_DICT, TAG_FRAM, TAG_HEAD, TAG_STAT, VERSION_MAJOR,
    VERSION_MINOR,
};
use crate::hash::Fnv64;

pub(crate) fn put_str(buf: &mut Vec<u8>, s: &str) {
    let len = u16::try_from(s.len()).expect("string longer than 65535 bytes");
    buf.extend_from_slice(&len.to_le_bytes());
    buf.extend_from_slice(s.as_bytes());
}

/// Encodes a record (tag, length, payload, checksum).
pub(crate) fn record(tag: u32, payload: &[u8]) -> Vec<u8> {
    let len = u32::try_from(payload.len()).expect("record too large");
    let mut out = Vec::with_capacity(payload.len() + 16);
    out.extend_from_slice(&tag.to_le_bytes());
    out.extend_from_slice(&len.to_le_bytes());
    out.extend_from_slice(payload);
    let mut h = Fnv64::new();
    h.write(&out);
    out.extend_from_slice(&h.finish().to_le_bytes());
    out
}

pub(crate) fn encode_header(h: &Header) -> Vec<u8> {
    let mut p = Vec::new();
    p.extend_from_slice(&VERSION_MAJOR.to_le_bytes());
    p.extend_from_slice(&VERSION_MINOR.to_le_bytes());
    p.push(h.pin.platform as u8);
    p.extend_from_slice(&[0, 0, 0]);
    p.extend_from_slice(&h.pin.steam_build.to_le_bytes());
    p.extend_from_slice(&h.pin.exe_sha256);
    p.extend_from_slice(&h.pin.pe_timestamp.to_le_bytes());
    p.extend_from_slice(&h.pin.image_size.to_le_bytes());
    p.extend_from_slice(&h.layout_sha256);
    p.extend_from_slice(&h.session_seed.to_le_bytes());
    p.extend_from_slice(&h.interval.to_le_bytes());
    p.extend_from_slice(&h.flags.to_le_bytes());
    put_str(&mut p, &h.producer);
    p.extend_from_slice(&u16::try_from(h.columns.len()).expect("too many columns").to_le_bytes());
    for c in &h.columns {
        p.extend_from_slice(&c.id.to_le_bytes());
        p.push(c.elem as u8);
        p.push(0);
        p.extend_from_slice(&c.count.to_le_bytes());
        put_str(&mut p, &c.name);
    }
    p
}

pub(crate) fn encode_frame(h: &Header, f: &FrameRecord) -> io::Result<Vec<u8>> {
    let mut p = Vec::with_capacity(
        32 + f.sectors.iter().map(|s| 24 + s.columns.iter().map(|c| 8 + c.bytes.len()).sum::<usize>()).sum::<usize>(),
    );
    p.extend_from_slice(&f.frame.to_le_bytes());
    p.extend_from_slice(&f.game_time.to_bits().to_le_bytes());
    p.extend_from_slice(&u32::try_from(f.sectors.len()).map_err(|_| invalid("too many sectors"))?.to_le_bytes());
    p.extend_from_slice(&f.flags.to_le_bytes());
    for s in &f.sectors {
        p.extend_from_slice(&s.root_uid.to_le_bytes());
        p.push(s.kind);
        p.push(0);
        p.extend_from_slice(&u16::try_from(s.columns.len()).map_err(|_| invalid("too many columns"))?.to_le_bytes());
        p.extend_from_slice(&s.rows.to_le_bytes());
        for c in &s.columns {
            check_column(h, s.rows, c)?;
            p.extend_from_slice(&c.id.to_le_bytes());
            p.extend_from_slice(&[0, 0]);
            p.extend_from_slice(&u32::try_from(c.bytes.len()).map_err(|_| invalid("column too large"))?.to_le_bytes());
            p.extend_from_slice(&c.bytes);
        }
    }
    Ok(p)
}

fn invalid(what: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, what.to_string())
}

fn check_column(h: &Header, rows: u32, c: &ColumnData) -> io::Result<()> {
    let desc = h.column(c.id).ok_or_else(|| invalid(&format!("column {:#x} not in the schema", c.id)))?;
    if c.bytes.len() != rows as usize * desc.row_bytes() {
        return Err(invalid(&format!(
            "column {} has {} bytes, expected {} rows x {}",
            desc.name,
            c.bytes.len(),
            rows,
            desc.row_bytes()
        )));
    }
    Ok(())
}

/// Streams a capture. Records are written whole; call [`CaptureWriter::finish`] to append
/// the STAT record and flush.
pub struct CaptureWriter<W: Write> {
    w: W,
    header: Header,
    stats: WriterStats,
}

impl<W: Write> CaptureWriter<W> {
    pub fn new(mut w: W, header: Header) -> io::Result<CaptureWriter<W>> {
        let mut start = Vec::with_capacity(16);
        start.extend_from_slice(&MAGIC);
        start.extend_from_slice(&VERSION_MAJOR.to_le_bytes());
        start.extend_from_slice(&VERSION_MINOR.to_le_bytes());
        start.extend_from_slice(&0u32.to_le_bytes());
        w.write_all(&start)?;
        let head = record(TAG_HEAD, &encode_header(&header));
        w.write_all(&head)?;
        let stats = WriterStats { bytes: (start.len() + head.len()) as u64, ..WriterStats::default() };
        Ok(CaptureWriter { w, header, stats })
    }

    pub fn header(&self) -> &Header {
        &self.header
    }

    /// Adds dictionary entries (accumulated by readers; later entries replace earlier ones).
    pub fn dict(&mut self, dict: Dict, entries: &[(u32, &str)]) -> io::Result<()> {
        let mut p = vec![dict as u8, 0, 0, 0];
        p.extend_from_slice(&u32::try_from(entries.len()).map_err(|_| invalid("too many entries"))?.to_le_bytes());
        for (index, value) in entries {
            p.extend_from_slice(&index.to_le_bytes());
            put_str(&mut p, value);
        }
        self.write_record(TAG_DICT, &p)
    }

    pub fn frame(&mut self, f: &FrameRecord) -> io::Result<()> {
        let p = encode_frame(&self.header, f)?;
        self.write_record(TAG_FRAM, &p)?;
        self.stats.frames += 1;
        Ok(())
    }

    /// Counts a frame that was not written (writer busy, filtered).
    pub fn note_dropped(&mut self) {
        self.stats.dropped += 1;
    }

    fn write_record(&mut self, tag: u32, payload: &[u8]) -> io::Result<()> {
        let r = record(tag, payload);
        self.w.write_all(&r)?;
        self.stats.bytes += r.len() as u64;
        Ok(())
    }

    pub fn stats(&self) -> WriterStats {
        self.stats
    }

    /// Writes STAT, flushes and returns the inner writer.
    pub fn finish(mut self) -> io::Result<W> {
        let mut p = Vec::with_capacity(32);
        let mut stats = self.stats;
        stats.bytes += 16 + 32;
        for v in [stats.frames, stats.dropped, stats.bytes, stats.walk_errors] {
            p.extend_from_slice(&v.to_le_bytes());
        }
        self.write_record(TAG_STAT, &p)?;
        self.w.flush()?;
        Ok(self.w)
    }
}
