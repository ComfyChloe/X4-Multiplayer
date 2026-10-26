//! host.py `Relay`: the non-blocking UDP loop around a [`Session`].

use std::io::ErrorKind;
use std::net::{SocketAddr, UdpSocket};

use crate::session::{Session, TICK_HZ};
use crate::wire::MAX_PACKET;

pub struct Relay {
    pub session: Session,
    pub socket: UdpSocket,
    next_tick: f64,
    pub received_packets: u64,
    pub received_bytes: u64,
    pub sent_packets: u64,
    pub sent_bytes: u64,
    pub send_errors: u64,
    /// Receive errors other than "would block"/"connection reset". The Python relay
    /// lets these escape and stops (see README: Windows WSAEMSGSIZE); this port counts
    /// and continues.
    pub receive_errors: u64,
}

#[cfg(windows)]
pub(crate) fn disable_connreset(socket: &UdpSocket) -> std::io::Result<()> {
    use std::os::windows::io::AsRawSocket;
    #[link(name = "ws2_32")]
    extern "system" {
        fn WSAIoctl(
            s: usize,
            code: u32,
            input: *const core::ffi::c_void,
            input_len: u32,
            output: *mut core::ffi::c_void,
            output_len: u32,
            returned: *mut u32,
            overlapped: *mut core::ffi::c_void,
            routine: *mut core::ffi::c_void,
        ) -> i32;
    }
    const SIO_UDP_CONNRESET: u32 = 0x9800_000C;
    let disabled: i32 = 0;
    let mut returned = 0u32;
    // SAFETY: a valid socket handle and a 4-byte BOOL input, as documented for the ioctl.
    let status = unsafe {
        WSAIoctl(
            socket.as_raw_socket() as usize,
            SIO_UDP_CONNRESET,
            (&disabled as *const i32).cast(),
            4,
            std::ptr::null_mut(),
            0,
            &mut returned,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
        )
    };
    if status == 0 {
        Ok(())
    } else {
        Err(std::io::Error::last_os_error())
    }
}

impl Relay {
    pub fn bind(bind: &str, port: u16, session: Session) -> std::io::Result<Relay> {
        let socket = UdpSocket::bind((bind, port))?;
        // Windows otherwise surfaces a departed UDP peer's ICMP reply on recv.
        #[cfg(windows)]
        disable_connreset(&socket)?;
        socket.set_nonblocking(true)?;
        Ok(Relay {
            session,
            socket,
            next_tick: 0.0,
            received_packets: 0,
            received_bytes: 0,
            sent_packets: 0,
            sent_bytes: 0,
            send_errors: 0,
            receive_errors: 0,
        })
    }

    pub fn step(&mut self, now: f64) {
        let mut buffer = [0u8; MAX_PACKET + 1];
        // Bound the work per loop, including under a packet flood.
        for _ in 0..128 {
            let (count, address) = match self.socket.recv_from(&mut buffer) {
                Ok(received) => received,
                Err(e) if e.kind() == ErrorKind::WouldBlock => break,
                // A departed Windows UDP endpoint is not a relay failure.
                Err(e) if e.kind() == ErrorKind::ConnectionReset => continue,
                Err(_) => {
                    self.receive_errors += 1;
                    continue;
                }
            };
            self.received_packets += 1;
            self.received_bytes += count as u64;
            for reply in self.session.receive(&buffer[..count], address, now) {
                self.send(&reply, address);
            }
        }
        if now >= self.next_tick {
            for (address, packet) in self.session.snapshots(now) {
                self.send(&packet, address);
            }
            self.next_tick = now + 1.0 / f64::from(TICK_HZ);
        }
    }

    fn send(&mut self, packet: &[u8], address: SocketAddr) {
        match self.socket.send_to(packet, address) {
            Ok(count) => {
                self.sent_packets += 1;
                self.sent_bytes += count as u64;
            }
            // Successful sends do not prove remote delivery.
            Err(_) => self.send_errors += 1,
        }
    }
}
