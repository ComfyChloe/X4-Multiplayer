//! Flight-model captures written by the native probe (native/engine/physics_flight.h, `FLIGHTCAP`) and
//! their offline replay against the reimplemented kernels.
//!
//! File: magic `X4PFCAP1`; twelve u32: version 1, model class (0 forced .. 5 steering), record size,
//! model bytes, agent offset (0x340), agent bytes (0x60), slot bytes (0x1a0), records, dropped,
//! PE timestamp, SizeOfImage, record header size (32); then the records. A record: header
//! {u32 model, u32 flags (1 cheap, 2 P(owner), 4 owner thread), f32 dt, f32 max forward (agent+0x364),
//! f64 t, u64 frame}, the model object's first `model bytes`, the agent bytes, the slot before and after
//! the original Update, padded to 16 bytes.

use std::path::Path;

use crate::flight::{forced_update, CrtMath, ForcedModel, Slot};
use x4mp_core::pins::windows as pinned;

pub const MAGIC: &[u8; 8] = b"X4PFCAP1";
pub const MODEL_NAMES: [&str; 6] = ["forced", "highway", "linear", "masstraffic", "spline", "steering"];

#[derive(Clone, Debug, PartialEq)]
pub struct Header {
    pub version: u32,
    pub model: u32,
    pub record_size: u32,
    pub model_bytes: u32,
    pub agent_from: u32,
    pub agent_bytes: u32,
    pub slot_bytes: u32,
    pub records: u32,
    pub dropped: u32,
    pub pe_timestamp: u32,
    pub size_of_image: u32,
    pub head_bytes: u32,
}

/// One recorded call.
#[derive(Clone, Debug)]
pub struct Record<'a> {
    pub model: u32,
    pub flags: u32,
    pub dt: f32,
    pub max_forward: f32,
    pub t: f64,
    pub frame: u64,
    pub model_bytes: &'a [u8],
    pub agent: &'a [u8],
    pub before: &'a [u8],
    pub after: &'a [u8],
}

pub struct FlightCapture {
    pub header: Header,
    data: Vec<u8>,
}

fn u32_at(d: &[u8], at: usize) -> u32 {
    u32::from_le_bytes(d[at..at + 4].try_into().unwrap())
}

impl FlightCapture {
    pub fn read(path: &Path) -> Result<FlightCapture, String> {
        let data = std::fs::read(path).map_err(|e| format!("{}: {e}", path.display()))?;
        FlightCapture::parse(data)
    }

    pub fn parse(data: Vec<u8>) -> Result<FlightCapture, String> {
        if data.len() < 56 || &data[..8] != MAGIC {
            return Err("not a flight capture (magic)".into());
        }
        let h: Vec<u32> = (0..12).map(|i| u32_at(&data, 8 + 4 * i)).collect();
        let header = Header {
            version: h[0],
            model: h[1],
            record_size: h[2],
            model_bytes: h[3],
            agent_from: h[4],
            agent_bytes: h[5],
            slot_bytes: h[6],
            records: h[7],
            dropped: h[8],
            pe_timestamp: h[9],
            size_of_image: h[10],
            head_bytes: h[11],
        };
        if header.version != 1 || header.head_bytes != 32 || header.slot_bytes != 0x1a0 || header.model > 5 {
            return Err(format!("unsupported capture layout {header:?}"));
        }
        if header.pe_timestamp != pinned::PE_TIMESTAMP || header.size_of_image != pinned::SIZE_OF_IMAGE {
            return Err("capture is not from the pinned build".into());
        }
        let need = header.head_bytes + header.model_bytes + header.agent_bytes + 2 * header.slot_bytes;
        if header.record_size < need || header.record_size % 16 != 0 {
            return Err("record size does not fit the layout".into());
        }
        if data.len() != 56 + header.records as usize * header.record_size as usize {
            return Err(format!("file size {} does not match {} records", data.len(), header.records));
        }
        Ok(FlightCapture { header, data })
    }

    pub fn record(&self, index: usize) -> Record<'_> {
        let h = &self.header;
        let at = 56 + index * h.record_size as usize;
        let r = &self.data[at..at + h.record_size as usize];
        let mut off = h.head_bytes as usize;
        let mut take = |n: u32| {
            let s = &r[off..off + n as usize];
            off += n as usize;
            s
        };
        let model_bytes = take(h.model_bytes);
        let agent = take(h.agent_bytes);
        let before = take(h.slot_bytes);
        let after = take(h.slot_bytes);
        Record {
            model: u32_at(r, 0),
            flags: u32_at(r, 4),
            dt: f32::from_le_bytes(r[8..12].try_into().unwrap()),
            max_forward: f32::from_le_bytes(r[12..16].try_into().unwrap()),
            t: f64::from_le_bytes(r[16..24].try_into().unwrap()),
            frame: u64::from_le_bytes(r[24..32].try_into().unwrap()),
            model_bytes,
            agent,
            before,
            after,
        }
    }

    pub fn records(&self) -> impl Iterator<Item = Record<'_>> {
        (0..self.header.records as usize).map(move |i| self.record(i))
    }
}

/// Serialise records in the probe's format (tests and tools; `records` = (head fields, model, agent,
/// before, after)).
#[allow(clippy::type_complexity)]
pub fn write(model: u32, model_bytes: u32, dropped: u32,
             records: &[((u32, f32, f32, f64, u64), Vec<u8>, Vec<u8>, Vec<u8>, Vec<u8>)]) -> Vec<u8> {
    let record_size = (32 + model_bytes + 0x60 + 2 * 0x1a0 + 15) & !15;
    let mut out = MAGIC.to_vec();
    for v in [1, model, record_size, model_bytes, 0x340, 0x60, 0x1a0, records.len() as u32, dropped,
              pinned::PE_TIMESTAMP, pinned::SIZE_OF_IMAGE, 32] {
        out.extend_from_slice(&u32::to_le_bytes(v));
    }
    for ((flags, dt, vfwd, t, frame), m, a, b, af) in records {
        let start = out.len();
        out.extend_from_slice(&model.to_le_bytes());
        out.extend_from_slice(&flags.to_le_bytes());
        out.extend_from_slice(&dt.to_le_bytes());
        out.extend_from_slice(&vfwd.to_le_bytes());
        out.extend_from_slice(&t.to_le_bytes());
        out.extend_from_slice(&frame.to_le_bytes());
        out.extend_from_slice(m);
        out.extend_from_slice(a);
        out.extend_from_slice(b);
        out.extend_from_slice(af);
        out.resize(start + record_size as usize, 0);
    }
    out
}

/// Outcome of a replay: `REPLAYCAP <name> records= mismatched= first=[record@offset]`.
#[derive(Debug, Default)]
pub struct ReplayReport {
    pub records: usize,
    pub mismatched: usize,
    pub first: Option<(usize, usize)>,
}

impl ReplayReport {
    pub fn line(&self, name: &str) -> String {
        let first = self.first.map(|(r, o)| format!("record{r}@slot+{o:#x}")).unwrap_or_default();
        format!("REPLAYCAP {name} records={} mismatched={} first=[{first}]", self.records, self.mismatched)
    }
}

fn v4(b: &[u8], at: usize) -> [f32; 4] {
    let f = |i: usize| f32::from_le_bytes(b[at + 4 * i..at + 4 * i + 4].try_into().unwrap());
    [f(0), f(1), f(2), f(3)]
}

/// Replay a forced-model capture: each record's slot after the original vs `forced_update` on the slot
/// before, all 0x1a0 bytes compared.
pub fn replay_forced(cap: &FlightCapture, math: &impl CrtMath) -> ReplayReport {
    let mut report = ReplayReport::default();
    for (index, r) in cap.records().enumerate() {
        report.records += 1;
        let model = ForcedModel {
            target_pos: v4(r.model_bytes, 0x20),
            target_rot: [v4(r.model_bytes, 0x30), v4(r.model_bytes, 0x40), v4(r.model_bytes, 0x50)],
        };
        let mut slot = Slot::from_bytes(r.before);
        forced_update(&model, &mut slot, r.dt, r.max_forward, math);
        let mut ours = r.before.to_vec();
        slot.write_bytes(&mut ours);
        if ours != r.after {
            report.mismatched += 1;
            if report.first.is_none() {
                report.first = Some((index, (0..ours.len()).find(|&i| ours[i] != r.after[i]).unwrap()));
            }
        }
    }
    report
}

/// Replay any supported capture.
pub fn replay(cap: &FlightCapture, math: &impl CrtMath) -> Result<ReplayReport, String> {
    match cap.header.model {
        0 => Ok(replay_forced(cap, math)),
        m => Err(format!("no reimplementation for the {} model yet", MODEL_NAMES[m as usize])),
    }
}
