"""Bounded STATEHASH narrowing and sector-baseline resync coordination."""
from dataclasses import dataclass

BUCKETS = ('identity', 'pose', 'health', 'rng', 'time')
MAX_SAMPLES = 128
MAX_MISMATCHES = 256
MAX_COOLDOWNS = 64
RETRY = .25
TIMEOUT = 15
COOLDOWN = 1


def bucket_text(values):
    values = tuple(values)
    if not values or len(set(values)) != len(values) or any(v not in BUCKETS for v in values):
        raise ValueError('invalid hash buckets')
    return ','.join(v for v in BUCKETS if v in values)


@dataclass
class Repair:
    epoch: str
    sector: str
    request: int
    after: int
    buckets: str
    next_send: float
    deadline: float
    attempts: int = 0


class HashReconciler:
    """Compares aligned complete samples; only sector baselines can repair state."""
    def __init__(self):
        self.samples = {'p1': {}, 'p2': {}}
        self.mismatches = {}
        self.repairs = {}
        self.cooldowns = {}
        self.next_request = 0

    def observe(self, slot, epoch, frame, sector, hashes, now):
        """Return newly detected (reported sector, differing buckets) pairs."""
        key = (epoch, frame, sector)
        old = self.samples[slot].get(key)
        if old is not None:
            return () if old == hashes else None
        newest = max((k[1] for k in self.samples[slot] if k[0] == epoch), default=0)
        if frame + MAX_SAMPLES <= newest:
            return ()
        self.samples[slot][key] = hashes
        while len(self.samples[slot]) > MAX_SAMPLES:
            del self.samples[slot][next(iter(self.samples[slot]))]
        other = self.samples['p2' if slot == 'p1' else 'p1'].get(key)
        if other is None:
            return ()
        differing = tuple(name for name in BUCKETS if hashes.get(name) != other.get(name))
        if not differing:
            return ()
        mismatch = (epoch, frame, sector)
        if mismatch in self.mismatches:
            return ()
        self.mismatches[mismatch] = differing
        while len(self.mismatches) > MAX_MISMATCHES:
            del self.mismatches[next(iter(self.mismatches))]
        return ((sector, differing),)

    def request_repair(self, epoch, sector, after, buckets, now):
        key = (epoch, sector)
        if key in self.repairs or now < self.cooldowns.get(key, 0) or self.next_request >= 2147483647:
            return None
        self.next_request += 1
        repair = Repair(epoch, sector, self.next_request, after, bucket_text(buckets), now, now + TIMEOUT)
        self.repairs[key] = repair
        self.cooldowns[key] = now + COOLDOWN
        while len(self.cooldowns) > MAX_COOLDOWNS:
            del self.cooldowns[next(iter(self.cooldowns))]
        return repair

    def committed(self, baseline):
        """A fresh commit satisfies the capture request, not RNG/time equality."""
        repair = self.repairs.get((baseline.epoch, baseline.sector))
        if repair and baseline.revision > repair.after:
            del self.repairs[(baseline.epoch, baseline.sector)]
            return True
        return False

    def packet(self, lease, repair):
        return (f'URESYNC|1|{lease}|{repair.epoch}|{repair.request}|{repair.sector}|'
                f'{repair.after}|{repair.buckets}').encode('ascii')

    def packets(self, host, guest, interest, now):
        result = []
        for key, repair in list(self.repairs.items()):
            if (now >= repair.deadline or repair.epoch != host.lease or not interest or
                    interest[0] != guest.lease or interest[2] != repair.sector):
                del self.repairs[key]
                continue
            if now < repair.next_send:
                continue
            if repair.attempts >= 60:
                del self.repairs[key]
                continue
            repair.attempts += 1
            repair.next_send = now + RETRY
            result.append((host.address, self.packet(host.lease, repair)))
        return result
