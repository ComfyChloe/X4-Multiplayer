"""Player scope: inert defaults, host adapter contract and script-patch structure.

The vanilla scripts are not in the checkout; dev/check_player_scope_diff.py applies the
patch to locally extracted game files. Native X4 behaviour remains unverified here.
"""
import re
import unittest
import xml.etree.ElementTree as E
from pathlib import Path
import test_coop_adapter
from test_game_adapter import LuaRuntime

EXT = Path(__file__).resolve().parents[1] / 'extension/x4_lan_lab'
UI = EXT / 'ui'
MD = EXT / 'md/x4lan_player_scope.xml'
AI = EXT / 'aiscripts/move.seekenemies.xml'
FLAG = '$x4lan_guestplayer'


class PlayerScopeScriptTests(unittest.TestCase):
    def test_source_config_is_inert_and_adapter_registered(self):
        self.assertIn('player_scope = false', (UI / 'config.lua').read_text())
        self.assertIn('ui/player_scope_game.lua', (EXT / 'ui.xml').read_text())
        self.assertIn('playerScope.frame(now)', (UI / 'bubble_game.lua').read_text())

    def test_md_session_requires_internal_host_coop_setup(self):
        session = E.parse(MD).getroot().find("cues/cue[@name='Session']")
        ui = session.find('conditions/event_ui_triggered')
        self.assertEqual((ui.get('screen'), ui.get('control')), ("'X4LAN_PlayerScope'", "'Setup'"))
        check = session.find('conditions/check_value').get('value')
        for term in ("player.module == 'x4lan_internal'", 'Arena.$host', 'Arena.$coop'):
            self.assertIn(term, check)

    def test_md_has_no_world_altering_actions(self):
        tags = {e.tag for e in E.parse(MD).iter()}
        for forbidden in ('destroy_object', 'set_owner', 'add_cargo', 'remove_cargo', 'drop_cargo', 'reward_player',
                          'add_faction_relation', 'set_faction_relation', 'add_relation_boost', 'create_order',
                          'create_ship', 'warp', 'transfer_money'):
            self.assertNotIn(forbidden, tags)

    def test_halt_only_answers_for_marked_guest_and_never_for_local_player(self):
        root = E.parse(MD).getroot()
        halt = root.find(".//cue[@name='Halt']")
        event = halt.find('conditions/event_object_signalled')
        self.assertEqual(event.get('param'), "'halt'")
        check = halt.find('conditions/check_value').get('value')
        for term in ('event.object == Session.$guest', 'event.object != player.occupiedship', "'policehalt'", "'piratehalt'"):
            self.assertIn(term, check)
        signals = halt.findall('.//signal_objects')
        self.assertEqual(len(signals), 2)
        for s in signals:
            self.assertEqual(s.get('param'), "'player halt response'")
            self.assertEqual(s.get('delay'), '1s')  # inspector must already wait for the answer
        responses = {s.get('param2') for s in signals}
        self.assertEqual(responses, {'$response'})
        values = [e.get('exact') for e in root.iter('set_value') if e.get('name') in ('$policeResponse', '$response')]
        self.assertEqual(set(values) - {'Session.$policeResponse'}, {"'comply'", "'wait'"})

    def test_guest_mark_excludes_local_ship_and_is_cleared(self):
        guest = E.parse(MD).getroot().find(".//cue[@name='Guest']")
        text = E.tostring(guest, encoding='unicode')
        self.assertIn('$candidate != player.occupiedship and $candidate != player.container', text)
        self.assertIn('name="Session.$guest.' + FLAG + '"', text)
        self.assertIn('name="$next.' + FLAG + '"', text)

    def test_ai_patch_extends_stock_expressions_with_guest_flag_only(self):
        root = E.parse(AI).getroot()
        self.assertEqual(root.tag, 'diff')
        ops = list(root)
        self.assertEqual([o.tag for o in ops], ['add', 'replace'])
        add, replace = ops
        self.assertEqual(add.get('sel'), "//check_value[@value='@$detected.{$i} == player.occupiedship']")
        self.assertEqual(add.get('pos'), 'after')
        self.assertEqual([c.get('value') for c in add], ['@$detected.{$i}.' + FLAG])
        stock = re.fullmatch(r"//do_elseif\[@value='([^']*)'\]/@value", replace.get('sel')).group(1)
        self.assertIn('($locobject != player.occupiedship)', stock)
        self.assertEqual(replace.text, stock + ' and not @$locobject.' + FLAG)
        # No blocking actions may be added to an AI script by diff.
        for tag in ('wait', 'move_to', 'run_script'):
            self.assertIsNone(root.find('.//' + tag))


@unittest.skipUnless(LuaRuntime, 'LuaJIT unavailable')
class PlayerScopeAdapterTests(unittest.TestCase):
    def make(self, slot='p1', enabled=True):
        l = test_coop_adapter.CoopAdapterTests().make(slot)
        l.execute('X4LAN_CONFIG.player_scope=%s' % ('true' if enabled else 'false'))
        l.execute('function last(name) for i=#requests,1,-1 do if requests[i][2]==name then return requests[i][3] end end end')
        l.execute((UI / 'player_scope_game.lua').read_text())
        return l

    def test_disabled_or_guest_side_adapter_is_absent(self):
        for slot, enabled in (('p1', False), ('p2', True)):
            l = self.make(slot, enabled)
            l.execute('assert(X4LAN_BUBBLE_API.playerScope==nil)')

    def test_setup_then_guest_index_follows_authority_and_resends(self):
        l = self.make()
        l.execute('''
         local p=X4LAN_BUBBLE_API.playerScope
         p.frame(now);p.frame(now);assert(countRequest('Setup')>=1)
         local before=countRequest('Guest');p.frame(now);assert(countRequest('Guest')==before)
         events['X4LAN_PlayerScope.Ready']()
         p.frame(now);assert(last('Guest')==2)
         local n=countRequest('Guest');p.frame(now);assert(countRequest('Guest')==n)
         now=now+1.1;p.frame(now);assert(countRequest('Guest')==n+1)
         auth(2,'p2',2,'p2','p1',2,'p1');p.frame(now);assert(last('Guest')==1)
         auth(3,'p2',2,'0','p1',2,'p1');p.frame(now);assert(last('Guest')==0)
         paused=true;n=countRequest('Guest');auth(4,'p2',2,'p2','p1',2,'p1');p.frame(now);assert(countRequest('Guest')==n)
        ''')

    def test_halt_notices_are_validated_and_bounded(self):
        l = self.make()
        l.execute('''
         local p=X4LAN_BUBBLE_API.playerScope
         events['X4LAN_PlayerScope.Halt'](nil,'policehalt|comply|ABC-123|argon|30')
         events['X4LAN_PlayerScope.Halt'](nil,'piratehalt|attack|ABC-123|scaleplate|30')
         events['X4LAN_PlayerScope.Halt'](nil,'policehalt|comply|ABC|argon')
         assert(#p.notices==1 and p.notices[1].owner=='argon' and p.notices[1].grace==30)
         for i=1,40 do events['X4LAN_PlayerScope.Halt'](nil,'piratehalt|wait|X-'..i..'|scaleplate|30') end
         assert(#p.notices==32 and p.notices[32].inspector=='X-40')
        ''')


if __name__ == '__main__':
    unittest.main()
