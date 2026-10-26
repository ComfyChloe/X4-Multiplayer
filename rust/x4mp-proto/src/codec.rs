//! Bounds-checked little-endian primitives. Every read is checked; nothing here can
//! panic on hostile input.

use std::fmt;

/// Why a datagram was rejected. Receivers drop the datagram; none of these is fatal.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DecodeError {
    /// The datagram or a field ends early.
    TooShort,
    /// The datagram exceeds [`crate::MAX_DATAGRAM`].
    TooLong,
    /// Not a v2 datagram (wrong magic).
    BadMagic,
    /// A v2-family datagram with another major version.
    UnsupportedMajor(u8),
    /// Unknown message kind.
    UnknownKind(u8),
    /// The header's body length differs from the datagram's.
    LengthMismatch,
    /// Reserved bits set in a datagram of a minor version this decoder knows.
    ReservedBits,
    /// Bytes after the last field in a datagram of a minor version this decoder knows.
    TrailingBytes,
    /// A field holds a value outside its domain.
    Invalid(&'static str),
}

impl fmt::Display for DecodeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            DecodeError::TooShort => f.write_str("datagram too short"),
            DecodeError::TooLong => f.write_str("datagram too long"),
            DecodeError::BadMagic => f.write_str("not a v2 datagram"),
            DecodeError::UnsupportedMajor(v) => write!(f, "unsupported major version {v}"),
            DecodeError::UnknownKind(k) => write!(f, "unknown message kind {k}"),
            DecodeError::LengthMismatch => f.write_str("body length mismatch"),
            DecodeError::ReservedBits => f.write_str("reserved bits set"),
            DecodeError::TrailingBytes => f.write_str("trailing bytes"),
            DecodeError::Invalid(field) => write!(f, "invalid {field}"),
        }
    }
}

impl std::error::Error for DecodeError {}

/// Why a message could not be encoded.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EncodeError {
    /// The encoded datagram would exceed [`crate::MAX_DATAGRAM`].
    TooLarge,
    /// A field value cannot be represented on the wire.
    Invalid(&'static str),
}

impl fmt::Display for EncodeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            EncodeError::TooLarge => f.write_str("message exceeds the datagram limit"),
            EncodeError::Invalid(field) => write!(f, "cannot encode {field}"),
        }
    }
}

impl std::error::Error for EncodeError {}

/// Zero-copy cursor over a received datagram.
#[derive(Clone, Debug)]
pub struct Reader<'a> {
    buf: &'a [u8],
    pos: usize,
}

impl<'a> Reader<'a> {
    pub fn new(buf: &'a [u8]) -> Self {
        Reader { buf, pos: 0 }
    }

    pub fn remaining(&self) -> usize {
        self.buf.len() - self.pos
    }

    pub fn position(&self) -> usize {
        self.pos
    }

    pub fn take(&mut self, n: usize) -> Result<&'a [u8], DecodeError> {
        let end = self.pos.checked_add(n).ok_or(DecodeError::TooShort)?;
        let slice = self.buf.get(self.pos..end).ok_or(DecodeError::TooShort)?;
        self.pos = end;
        Ok(slice)
    }

    pub fn array<const N: usize>(&mut self) -> Result<[u8; N], DecodeError> {
        let mut out = [0u8; N];
        out.copy_from_slice(self.take(N)?);
        Ok(out)
    }

    pub fn u8(&mut self) -> Result<u8, DecodeError> {
        Ok(self.take(1)?[0])
    }

    pub fn u16(&mut self) -> Result<u16, DecodeError> {
        Ok(u16::from_le_bytes(self.array()?))
    }

    pub fn u32(&mut self) -> Result<u32, DecodeError> {
        Ok(u32::from_le_bytes(self.array()?))
    }

    pub fn u64(&mut self) -> Result<u64, DecodeError> {
        Ok(u64::from_le_bytes(self.array()?))
    }

    pub fn i16(&mut self) -> Result<i16, DecodeError> {
        Ok(i16::from_le_bytes(self.array()?))
    }

    pub fn i32(&mut self) -> Result<i32, DecodeError> {
        Ok(i32::from_le_bytes(self.array()?))
    }

    /// Everything not yet read.
    pub fn rest(&mut self) -> &'a [u8] {
        let out = &self.buf[self.pos..];
        self.pos = self.buf.len();
        out
    }
}

/// Little-endian writer into a growable buffer.
#[derive(Default, Debug)]
pub struct Writer {
    pub buf: Vec<u8>,
}

impl Writer {
    pub fn with_capacity(n: usize) -> Self {
        Writer { buf: Vec::with_capacity(n) }
    }

    pub fn len(&self) -> usize {
        self.buf.len()
    }

    pub fn is_empty(&self) -> bool {
        self.buf.is_empty()
    }

    pub fn bytes(&mut self, data: &[u8]) {
        self.buf.extend_from_slice(data);
    }

    pub fn u8(&mut self, v: u8) {
        self.buf.push(v);
    }

    pub fn u16(&mut self, v: u16) {
        self.bytes(&v.to_le_bytes());
    }

    pub fn u32(&mut self, v: u32) {
        self.bytes(&v.to_le_bytes());
    }

    pub fn u64(&mut self, v: u64) {
        self.bytes(&v.to_le_bytes());
    }

    pub fn i16(&mut self, v: i16) {
        self.bytes(&v.to_le_bytes());
    }

    pub fn i32(&mut self, v: i32) {
        self.bytes(&v.to_le_bytes());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reader_never_reads_past_the_end() {
        let data = [1u8, 2, 3];
        let mut r = Reader::new(&data);
        assert_eq!(r.u16(), Ok(0x0201));
        assert_eq!(r.u16(), Err(DecodeError::TooShort));
        assert_eq!(r.u8(), Ok(3));
        assert_eq!(r.u8(), Err(DecodeError::TooShort));
        assert_eq!(r.take(usize::MAX), Err(DecodeError::TooShort));
        assert!(r.rest().is_empty());
    }
}
