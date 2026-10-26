"""Feature visibility for the relay and launcher (development rule, ENGINE-INSTRUMENTATION.txt rule 4).

Mirrors extension/x4_lan_lab/ui/features.lua. Every process logs one line at startup:
    FEATURES on=a,b off=c(reason) auto_disabled=d(reason)
and a feature that refuses or switches itself off logs exactly one
    FEATURE <name> DISABLED reason=...
per transition. The status JSON carries ``as_dict()`` under ``features``.
"""
from __future__ import annotations

import re

ON, OFF, AUTO = 'on', 'off', 'auto_disabled'


def _clean(reason):
    text = re.sub(r'\s+', ' ', str('unspecified' if reason is None else reason)).strip() or 'unspecified'
    return text if len(text) <= 160 else text[:157] + '...'


def _token(reason):
    # Summary tokens stay whitespace- and comma-free so the line remains machine-readable.
    text = re.sub(r'[\s,]+', '_', reason).replace('(', '').replace(')', '')
    return text if len(text) <= 60 else text[:57] + '...'


class Features:
    def __init__(self, log=print):
        self.items = {}
        self.log = log

    def declare(self, name, enabled, reason='config'):
        item = self.items.get(name)
        if item and item['state'] == AUTO:
            return False  # a refusal stays visible until resume()
        self.items[name] = {'state': ON, 'reason': None} if enabled else {'state': OFF, 'reason': _clean(reason)}
        return bool(enabled)

    def disable(self, name, reason):
        item = self.items.get(name)
        if item and item['state'] == AUTO:
            return False
        reason = _clean(reason)
        self.items[name] = {'state': AUTO, 'reason': reason}
        if self.log:
            self.log(f'FEATURE {name} DISABLED reason={reason}')
        return True

    def resume(self, name):
        item = self.items.get(name)
        if not item or item['state'] != AUTO:
            return False
        self.items[name] = {'state': ON, 'reason': None}
        if self.log:
            self.log(f'FEATURE {name} RESUMED')
        return True

    def as_dict(self):
        return {name: dict(self.items[name]) for name in sorted(self.items)}

    def summary(self):
        lists = {ON: [], OFF: [], AUTO: []}
        for name, item in sorted(self.items.items()):
            lists[item['state']].append(name if item['reason'] is None else f'{name}({_token(item["reason"])})')
        return ('FEATURES on=' + (','.join(lists[ON]) or '-') + ' off=' + (','.join(lists[OFF]) or '-')
                + ' auto_disabled=' + (','.join(lists[AUTO]) or '-'))


def relay_features(session, config=None, log=None):
    """Server-side features of a relay session, derived from the session type actually built."""
    from .bubble import BubbleSession
    from .coop import CoopSession
    from .docking import DockSession
    from .economy import EconomySession
    known = config is not None  # without a config, reasons name the session type only
    config = config or {}
    f = Features(log)
    # The relay always answers RTTPING; it starts measuring once a client with net_probe sends one.
    f.declare('netprobe', True)
    f.declare('bubble_actions', isinstance(session, BubbleSession), 'session type ' + type(session).__name__)
    f.declare('trading', isinstance(session, EconomySession), 'session type ' + type(session).__name__)
    coop = isinstance(session, CoopSession)
    why = 'config coop=false' if known and not config.get('coop') else 'session type ' + type(session).__name__
    for name in ('coop', 'health', 'shots', 'fire2', 'projectiles', 'quests', 'missions'):
        f.declare(name, coop, why)
    f.declare('coop_docks', isinstance(session, DockSession), 'requires coop' if not coop else 'session type ' + type(session).__name__)
    world = hasattr(session, 'universe')
    why = 'config world_sync=false' if known and not config.get('world_sync') else 'session type ' + type(session).__name__
    f.declare('world_sync', world, why)
    for name, attr in (('npc_combat', 'npc_combat'), ('cargo', 'cargo'), ('identity', 'identities')):
        f.declare(name, hasattr(session, attr), 'requires world_sync' if not world else 'missing in relay session')
    return f


# Client flags that internal packages leave off, with the recorded reason. A false flag in the
# generated internal config without an entry here is reported as "no recorded reason".
INTERNAL_OFF_REASONS = {
    'interaction_test': 'destructive test fixture; only when a test names it',
    'shot_undo': 'diagnostic only; replayed-hit undo delays shield recharge',
    'walking_avatars': 'room mapping not visually verified (walk_game.lua)',
}


def config_features(config, reasons=INTERNAL_OFF_REASONS, log=None):
    """Features of a generated client config.lua (a dict), for the launcher's FEATURES line."""
    f = Features(log)
    for key, value in config.items():
        # seed_verified is state (set by the world launcher after verification), not a feature.
        if key == 'seed_verified' or not (isinstance(value, bool) or key.endswith('_diagnostics')):
            continue
        f.declare(key, bool(value), reasons.get(key, 'no recorded reason'))
    return f
