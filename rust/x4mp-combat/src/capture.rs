//! Capture files of joint steps and their offline replay.
//!
//! A capture holds the inputs and outputs of `IKValue_RotateToward` calls as the original
//! produced them: from a native lane run (an in-process recorder, see
//! reports/REWRITE-COMBAT.md) or from the offline oracle. [`replay`] runs
//! [`crate::ikjoint::rotate_toward`] on every record and compares all outputs bit for bit.
//!
//! Format (little endian): a 32-byte header, then fixed 96-byte records.
//!
//! | header | bytes |
//! |---|---|
//! | magic `X4CJSTEP` | 8 |
//! | version (1) | u32 |
//! | Steam build (23660954) | u32 |
//! | MXCSR of the recording thread | u32 |
//! | joint mode byte (0x3d19b14) | u8 |
//! | source: 0 native, 1 oracle, 2 synthetic | u8 |
//! | reserved | 10 |
//!
//! | record | offset | type |
//! |---|---|---|
//! | component id (weapon/turret, 0 if unknown) | 0 | u64 |
//! | chain index, joint index | 8, 10 | u16, u16 |
//! | flags: bit 0 wrap, bit 1 snap | 12 | u16 |
//! | result | 14 | u8 (+1 pad) |
//! | before: value, velocity, target | 16, 20, 24 | f32 |
//! | command target | 28 | f32 |
//! | before: time | 32 | f64 |
//! | command min, max, max_speed, accel, feed_forward | 40..60 | f32 |
//! | after: value, velocity, target | 60, 64, 68 | f32 |
//! | after: time | 72 | f64 |
//! | clock now, previous | 80, 88 | f64 |
//!
//! Floats are stored as their raw bits, so a record round-trips exactly.

use std::io::{self, Read, Write};

use crate::ikjoint::{rotate_toward, FrameClock, JointCommand, JointState};

pub const MAGIC: &[u8; 8] = b"X4CJSTEP";
pub const VERSION: u32 = 1;
pub const HEADER_SIZE: usize = 32;
pub const RECORD_SIZE: usize = 96;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Source {
    Native = 0,
    Oracle = 1,
    Synthetic = 2,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Header {
    pub steam_build: u32,
    pub mxcsr: u32,
    pub joint_mode: u8,
    pub source: Source,
}

/// One recorded call.
#[derive(Debug, Clone, Copy, Default)]
pub struct Record {
    pub component: u64,
    pub chain: u16,
    pub joint: u16,
    pub before: JointState,
    pub command: JointCommand,
    pub clock: FrameClock,
    pub after: JointState,
    pub result: bool,
}

impl Header {
    pub fn encode(&self) -> [u8; HEADER_SIZE] {
        let mut out = [0u8; HEADER_SIZE];
        out[..8].copy_from_slice(MAGIC);
        out[8..12].copy_from_slice(&VERSION.to_le_bytes());
        out[12..16].copy_from_slice(&self.steam_build.to_le_bytes());
        out[16..20].copy_from_slice(&self.mxcsr.to_le_bytes());
        out[20] = self.joint_mode;
        out[21] = self.source as u8;
        out
    }

    pub fn decode(bytes: &[u8]) -> io::Result<Header> {
        let bad = |what: &str| io::Error::new(io::ErrorKind::InvalidData, what.to_string());
        if bytes.len() < HEADER_SIZE || &bytes[..8] != MAGIC {
            return Err(bad("not a joint capture (magic)"));
        }
        let word = |at: usize| u32::from_le_bytes(bytes[at..at + 4].try_into().unwrap());
        if word(8) != VERSION {
            return Err(bad("unsupported capture version"));
        }
        let source = match bytes[21] {
            0 => Source::Native,
            1 => Source::Oracle,
            2 => Source::Synthetic,
            _ => return Err(bad("unknown capture source")),
        };
        Ok(Header { steam_build: word(12), mxcsr: word(16), joint_mode: bytes[20], source })
    }
}

fn put32(out: &mut [u8], at: usize, v: f32) {
    out[at..at + 4].copy_from_slice(&v.to_bits().to_le_bytes());
}
fn put64(out: &mut [u8], at: usize, v: f64) {
    out[at..at + 8].copy_from_slice(&v.to_bits().to_le_bytes());
}
fn get32(b: &[u8], at: usize) -> f32 {
    f32::from_bits(u32::from_le_bytes(b[at..at + 4].try_into().unwrap()))
}
fn get64(b: &[u8], at: usize) -> f64 {
    f64::from_bits(u64::from_le_bytes(b[at..at + 8].try_into().unwrap()))
}

impl Record {
    pub fn encode(&self) -> [u8; RECORD_SIZE] {
        let mut out = [0u8; RECORD_SIZE];
        out[0..8].copy_from_slice(&self.component.to_le_bytes());
        out[8..10].copy_from_slice(&self.chain.to_le_bytes());
        out[10..12].copy_from_slice(&self.joint.to_le_bytes());
        let flags = self.before.wrap as u16 | (self.command.snap as u16) << 1;
        out[12..14].copy_from_slice(&flags.to_le_bytes());
        out[14] = self.result as u8;
        put32(&mut out, 16, self.before.value);
        put32(&mut out, 20, self.before.velocity);
        put32(&mut out, 24, self.before.target);
        put32(&mut out, 28, self.command.target);
        put64(&mut out, 32, self.before.time);
        put32(&mut out, 40, self.command.min);
        put32(&mut out, 44, self.command.max);
        put32(&mut out, 48, self.command.max_speed);
        put32(&mut out, 52, self.command.accel);
        put32(&mut out, 56, self.command.feed_forward);
        put32(&mut out, 60, self.after.value);
        put32(&mut out, 64, self.after.velocity);
        put32(&mut out, 68, self.after.target);
        put64(&mut out, 72, self.after.time);
        put64(&mut out, 80, self.clock.now);
        put64(&mut out, 88, self.clock.previous);
        out
    }

    pub fn decode(b: &[u8]) -> Record {
        let flags = u16::from_le_bytes([b[12], b[13]]);
        let wrap = flags & 1 != 0;
        Record {
            component: u64::from_le_bytes(b[0..8].try_into().unwrap()),
            chain: u16::from_le_bytes([b[8], b[9]]),
            joint: u16::from_le_bytes([b[10], b[11]]),
            result: b[14] != 0,
            before: JointState {
                value: get32(b, 16),
                velocity: get32(b, 20),
                target: get32(b, 24),
                time: get64(b, 32),
                wrap,
            },
            command: JointCommand {
                target: get32(b, 28),
                min: get32(b, 40),
                max: get32(b, 44),
                max_speed: get32(b, 48),
                accel: get32(b, 52),
                feed_forward: get32(b, 56),
                snap: flags & 2 != 0,
            },
            after: JointState {
                value: get32(b, 60),
                velocity: get32(b, 64),
                target: get32(b, 68),
                time: get64(b, 72),
                wrap,
            },
            clock: FrameClock { now: get64(b, 80), previous: get64(b, 88) },
        }
    }
}

pub fn write_capture(mut out: impl Write, header: &Header, records: &[Record]) -> io::Result<()> {
    out.write_all(&header.encode())?;
    for record in records {
        out.write_all(&record.encode())?;
    }
    out.flush()
}

pub fn read_capture(mut input: impl Read) -> io::Result<(Header, Vec<Record>)> {
    let mut bytes = Vec::new();
    input.read_to_end(&mut bytes)?;
    let header = Header::decode(&bytes)?;
    let body = &bytes[HEADER_SIZE..];
    if body.len() % RECORD_SIZE != 0 {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "truncated record"));
    }
    Ok((header, body.chunks_exact(RECORD_SIZE).map(Record::decode).collect()))
}

/// Which outputs differ (bitwise) between the capture and the reimplementation.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Diff {
    pub value: bool,
    pub velocity: bool,
    pub target: bool,
    pub time: bool,
    pub result: bool,
}

impl Diff {
    pub fn any(&self) -> bool {
        self.value || self.velocity || self.target || self.time || self.result
    }
}

/// Compare one record's outputs with a reimplementation result, bit for bit.
pub fn diff(record: &Record, after: &JointState, result: bool) -> Diff {
    Diff {
        value: record.after.value.to_bits() != after.value.to_bits(),
        velocity: record.after.velocity.to_bits() != after.velocity.to_bits(),
        target: record.after.target.to_bits() != after.target.to_bits(),
        time: record.after.time.to_bits() != after.time.to_bits(),
        result: record.result != result,
    }
}

#[derive(Debug, Clone, Default)]
pub struct ReplayReport {
    pub records: usize,
    pub mismatched: usize,
    pub value: usize,
    pub velocity: usize,
    pub target: usize,
    pub time: usize,
    pub result: usize,
    /// Index and diff of the first mismatches (at most 16).
    pub first: Vec<(usize, Diff)>,
}

impl ReplayReport {
    pub fn exact(&self) -> bool {
        self.mismatched == 0
    }
}

/// Replay every record through [`rotate_toward`] on the calling thread.
pub fn replay(records: &[Record]) -> ReplayReport {
    let mut report = ReplayReport { records: records.len(), ..Default::default() };
    for (index, record) in records.iter().enumerate() {
        let mut joint = record.before;
        let result = rotate_toward(&mut joint, &record.command, record.clock);
        let d = diff(record, &joint, result);
        if d.any() {
            report.mismatched += 1;
            report.value += d.value as usize;
            report.velocity += d.velocity as usize;
            report.target += d.target as usize;
            report.time += d.time as usize;
            report.result += d.result as usize;
            if report.first.len() < 16 {
                report.first.push((index, d));
            }
        }
    }
    report
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> Record {
        let before = JointState { value: 0.25, time: 99.5, velocity: -0.5, target: 1.0, wrap: true };
        let command = JointCommand {
            target: -2.0,
            min: 0.0,
            max: 0.0,
            max_speed: 1.5,
            accel: 3.0,
            feed_forward: 0.1,
            snap: false,
        };
        let clock = FrameClock { now: 99.516, previous: 99.5 };
        let mut after = before;
        let result = rotate_toward(&mut after, &command, clock);
        Record { component: 0x1234_5678_9abc, chain: 1, joint: 2, before, command, clock, after, result }
    }

    #[test]
    fn record_round_trips_bitwise() {
        let mut record = sample();
        record.before.value = f32::from_bits(0x7fc0_0001); // a NaN payload survives
        let decoded = Record::decode(&record.encode());
        assert_eq!(decoded.encode(), record.encode());
        assert_eq!(decoded.before.value.to_bits(), 0x7fc0_0001);
    }

    #[test]
    fn capture_round_trip_and_replay() {
        let header = Header { steam_build: 23_660_954, mxcsr: 0x1f80, joint_mode: 0, source: Source::Synthetic };
        let records = vec![sample(), sample()];
        let mut bytes = Vec::new();
        write_capture(&mut bytes, &header, &records).unwrap();
        assert_eq!(bytes.len(), HEADER_SIZE + 2 * RECORD_SIZE);
        let (read_header, read) = read_capture(&bytes[..]).unwrap();
        assert_eq!(read_header, header);
        let report = replay(&read);
        assert!(report.exact(), "{report:?}");
    }

    #[test]
    fn replay_reports_a_corrupted_field() {
        let mut record = sample();
        record.after.velocity = f32::from_bits(record.after.velocity.to_bits() ^ 1);
        let report = replay(&[record]);
        assert_eq!((report.mismatched, report.velocity, report.value), (1, 1, 0));
        assert!(report.first[0].1.velocity);
    }

    #[test]
    fn rejects_foreign_files() {
        assert!(read_capture(&b"NOTACAPTURE....................."[..]).is_err());
        let header = Header { steam_build: 1, mxcsr: 0, joint_mode: 0, source: Source::Native };
        let mut bytes = header.encode().to_vec();
        bytes.extend_from_slice(&[0u8; 10]);
        assert!(read_capture(&bytes[..]).is_err());
    }
}
