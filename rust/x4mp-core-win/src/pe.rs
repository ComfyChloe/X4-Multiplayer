//! Platform-independent check of the in-memory PE headers of the main module against the
//! pinned `X4.exe` (unit-tested on any host).

use x4mp_core::pins::windows as pinned;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HeaderCheck {
    Ok,
    Malformed,
    Machine(u16),
    Timestamp(u32),
    SizeOfImage(u32),
}

fn u16_at(d: &[u8], at: usize) -> Option<u16> {
    Some(u16::from_le_bytes(d.get(at..at + 2)?.try_into().ok()?))
}
fn u32_at(d: &[u8], at: usize) -> Option<u32> {
    Some(u32::from_le_bytes(d.get(at..at + 4)?.try_into().ok()?))
}

/// `headers` = the first 4 KiB of the mapped image.
pub fn check_headers(headers: &[u8]) -> HeaderCheck {
    let parse = || -> Option<HeaderCheck> {
        if headers.get(..2)? != b"MZ" {
            return None;
        }
        let pe = u32_at(headers, 0x3c)? as usize;
        if pe > 0x800 || headers.get(pe..pe + 4)? != b"PE\0\0" {
            return None;
        }
        let machine = u16_at(headers, pe + 4)?;
        let timestamp = u32_at(headers, pe + 8)?;
        let optional = pe + 24;
        if u16_at(headers, optional)? != 0x20b {
            return None;
        }
        let size_of_image = u32_at(headers, optional + 56)?;
        Some(if machine != 0x8664 {
            HeaderCheck::Machine(machine)
        } else if timestamp != pinned::PE_TIMESTAMP {
            HeaderCheck::Timestamp(timestamp)
        } else if size_of_image != pinned::SIZE_OF_IMAGE {
            HeaderCheck::SizeOfImage(size_of_image)
        } else {
            HeaderCheck::Ok
        })
    };
    parse().unwrap_or(HeaderCheck::Malformed)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn headers(machine: u16, timestamp: u32, size: u32) -> Vec<u8> {
        let mut h = vec![0u8; 0x400];
        h[..2].copy_from_slice(b"MZ");
        h[0x3c..0x40].copy_from_slice(&0x100u32.to_le_bytes());
        h[0x100..0x104].copy_from_slice(b"PE\0\0");
        h[0x104..0x106].copy_from_slice(&machine.to_le_bytes());
        h[0x108..0x10c].copy_from_slice(&timestamp.to_le_bytes());
        h[0x118..0x11a].copy_from_slice(&0x20bu16.to_le_bytes());
        h[0x118 + 56..0x118 + 60].copy_from_slice(&size.to_le_bytes());
        h
    }

    #[test]
    fn exact_headers_only() {
        assert_eq!(check_headers(&headers(0x8664, pinned::PE_TIMESTAMP, pinned::SIZE_OF_IMAGE)), HeaderCheck::Ok);
        assert_eq!(
            check_headers(&headers(0x14c, pinned::PE_TIMESTAMP, pinned::SIZE_OF_IMAGE)),
            HeaderCheck::Machine(0x14c)
        );
        assert_eq!(check_headers(&headers(0x8664, 1, pinned::SIZE_OF_IMAGE)), HeaderCheck::Timestamp(1));
        assert_eq!(check_headers(&headers(0x8664, pinned::PE_TIMESTAMP, 2)), HeaderCheck::SizeOfImage(2));
        assert_eq!(check_headers(b"MZ"), HeaderCheck::Malformed);
        assert_eq!(check_headers(&[]), HeaderCheck::Malformed);
    }
}
