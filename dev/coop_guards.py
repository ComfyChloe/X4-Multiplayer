"""Cooperative mode keeps ship IDs distinct from pilot slots and shares money."""
import xml.etree.ElementTree as E

def bubble(xml):
 root=E.fromstring(xml);setup=root.find('./cues/cue/actions')
 setup.insert(0,E.Element('set_value',name='$coop',exact='false'))
 for i in (1,2):
  a=root.find(f".//cue[@name='Shots{i}']/actions");legacy=a.find('do_if');legacy.tag='do_elseif'
  a.insert(1,E.fromstring(f'''<do_if value="$coop"><do_if value="player.occupiedship == $ships.{{{i}}}"><raise_lua_event name="'X4LAN_Coop.Shot'" param="{i}"/></do_if></do_if>'''))
 # Guest targets are held at 1% hull. Only a host death snapshot releases the
 # guard and destroys the replica; local damage may otherwise be reconciled.
 target=next(n for n in root.find(".//cue[@name='Apply']/actions").iter('do_if')
             if n.get('value')=='Arena.$targets.{$i}.exists')
 old=list(target)
 for n in old:target.remove(n)
 dead=E.SubElement(target,'do_if',value='Arena.$coop and $v.{5 + $i * 2} le 0')
 E.SubElement(dead,'set_object_min_hull',object='Arena.$targets.{$i}',exact='0')
 E.SubElement(dead,'destroy_object',object='Arena.$targets.{$i}',explosion='true')
 alive=E.SubElement(target,'do_else')
 for n in old:alive.append(n)
 return E.tostring(root,encoding='unicode')

def trade(xml):
 root=E.fromstring(xml)
 for node in root.iter():
  if node.tag=='transfer_money' and node.get('to')=='$arena.$proxy' and node.get('amount')=='100000Cr':
   node.set('amount','if $arena.$coop then 0Cr else 100000Cr')
  if node.tag=='set_value' and node.get('name')=='$account' and node.get('exact')=='if $slot == 1 then faction.player else $ship':
   node.set('exact','if Market.$arena.$coop or $slot == 1 then faction.player else $ship')
  if node.tag=='set_value' and node.get('name')=='$snapshot':
   value=node.get('exact','').replace('($arena.$remoteBalance / 1ct)i','((if $arena.$coop then player.money else $arena.$remoteBalance) / 1ct)i')
   node.set('exact',value)
 # The proxy account stays at zero in cooperative mode. The guest mirrors the
 # single host player balance, never a second spendable copy on the proxy ship.
 apply=root.find(".//cue[@name='Apply']/actions")
 for node in apply.iter('do_if'):
  if node.get('value')=='$object.isoperational':
   node.set('value','$object.isoperational and ($j gt 2 or not @md.X4LAN_Cargo.Setup.$wares.indexof.{$ware})')
  if node.get('value')=='$account.isoperational':node.set('value','$account.isoperational and (not Market.$arena.$coop or $j gt 1)')
 return E.tostring(root,encoding='unicode')
