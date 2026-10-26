//! The non-blocking UDP loop around a [`V2Session`].

use std::io::ErrorKind;
use std::net::{SocketAddr, UdpSocket};

use x4mp_proto::MAX_DATAGRAM;

use super::{Packet, V2Session};

pub struct V2Relay {
    pub session: V2Session,
    pub socket: UdpSocket,
    next_tick_us: u64,
    pub received_packets: u64,
    pub sent_packets: u64,
    pub send_errors: u64,
    pub receive_errors: u64,
}

impl V2Relay {
    pub fn bind(bind: &str, port: u16, session: V2Session) -> std::io::Result<V2Relay> {
        let socket = UdpSocket::bind((bind, port))?;
        #[cfg(windows)]
        crate::relay::disable_connreset(&socket)?;
        socket.set_nonblocking(true)?;
        Ok(V2Relay {
            session,
            socket,
            next_tick_us: 0,
            received_packets: 0,
            sent_packets: 0,
            send_errors: 0,
            receive_errors: 0,
        })
    }

    pub fn local_addr(&self) -> std::io::Result<SocketAddr> {
        self.socket.local_addr()
    }

    /// Drain pending datagrams (bounded), then run a tick if one is due. `now_us` is the
    /// caller's monotonic clock, so tests can drive the relay with simulated time.
    pub fn step(&mut self, now_us: u64) {
        // One spare byte: an oversized datagram is seen as such and rejected.
        let mut buffer = [0u8; MAX_DATAGRAM + 1];
        for _ in 0..256 {
            let (count, address) = match self.socket.recv_from(&mut buffer) {
                Ok(received) => received,
                Err(e) if e.kind() == ErrorKind::WouldBlock => break,
                Err(e) if e.kind() == ErrorKind::ConnectionReset => continue,
                Err(_) => {
                    self.receive_errors += 1;
                    continue;
                }
            };
            self.received_packets += 1;
            let out = self.session.receive(&buffer[..count], address, now_us);
            self.send_all(out);
        }
        if now_us >= self.next_tick_us {
            let out = self.session.tick(now_us);
            self.send_all(out);
            let interval = 1_000_000 / u64::from(self.session.config().tick_hz);
            self.next_tick_us = now_us + interval;
        }
    }

    fn send_all(&mut self, packets: Vec<Packet>) {
        for (address, bytes) in packets {
            match self.socket.send_to(&bytes, address) {
                Ok(_) => self.sent_packets += 1,
                Err(_) => self.send_errors += 1,
            }
        }
    }
}
