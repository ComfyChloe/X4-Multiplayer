"""Create a private lane extension from a retained station manifest and plans.

Input/save-derived plan data and generated extension remain outside Git. Never
launches X4. Pass the output to the released x4lane tool as --extension.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import re
import shutil
import xml.etree.ElementTree as ET


def atom(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise ValueError(f"invalid identifier: {value!r}")
    return value


def build(manifest, plans_path, out):
    if out.exists():
        raise ValueError("output exists; use a fresh label to preserve evidence")
    rows = json.loads(manifest.read_text())["stations"]
    plans = {p.get("id"): p for p in ET.parse(plans_path).getroot().iter("plan")}
    if not 1 <= len(rows) <= 32:
        raise ValueError("requires 1..32 station plans")
    expected, actions = [], []
    for i, row in enumerate(rows, 1):
        plan = atom(row["plan"])
        macro, owner = atom(row["macro"]), atom(row["owner"])
        entries = list(plans[plan].findall("entry"))
        counts = Counter(atom(e.get("macro")) for e in entries)
        if len(entries) != row["entries"] or not entries:
            raise ValueError("manifest and construction plan count differ")
        expected.append({"plan": plan, "macros": dict(counts)})
        actions.append(f'''<create_station name="$s" macro="macro.{macro}" owner="faction.{owner}" sector="player.sector" state="componentstate.operational" constructionplan="'{plan}'">
          <position x="{50000+i*10000}m" y="0m" z="50000m"/>
        </create_station>
        <do_if value="$s">
          <raise_lua_event name="'DLCOR.Station'" param="'%s|%s|%s'.[{i},'{plan}',$s]"/>
        </do_if>
        <do_else><debug_text text="'DLCOR FAIL create index={i}'" filter="error"/></do_else>''')
    lua = "DLCOR_EXPECTED = {\n" + ",\n".join(
        "{plan=" + json.dumps(r["plan"]) + ",macros={" + ",".join(
            "["+json.dumps(k)+"]="+str(v) for k, v in r["macros"].items()) + "}}"
        for r in expected) + "\n}\n"
    md = '''<?xml version="1.0" encoding="utf-8"?>
<mdscript name="DLCOR" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="md.xsd">
 <cues><cue name="Data"><actions><set_value name="$started" exact="false"/></actions><cues>
 <cue name="Begin" instantiate="true"><conditions><event_ui_triggered screen="'DLCOR'" control="'Start'"/></conditions><actions>
 <do_if value="Data.$started"><debug_text text="'DLCOR FAIL repeated_start UI_reload'" filter="error"/></do_if>
 <do_else><set_value name="Data.$started" exact="true"/>''' + "\n".join(actions) + '''
 </do_else></actions></cue></cues></cue></cues></mdscript>'''
    ET.fromstring(md)
    for sub in ("md", "ui", "libraries"):
        (out/sub).mkdir(parents=True, exist_ok=False)
    (out/"content.xml").write_text('<content id="x4mp_dynload_correlation" name="Dynamic load station correlation" version="1" save="false"><dependency version="900"/></content>')
    (out/"ui.xml").write_text('<addon name="x4mp_dynload_correlation"><environment type="menus"><dependency name="ego_detailmonitor"/><file name="ui/expected.lua"/><file name="ui/probe.lua"/></environment></addon>')
    (out/"ui/expected.lua").write_text(lua)
    (out/"ui/probe.lua").write_text(Path(__file__).with_name("dynload_station_probe.lua").read_text())
    (out/"md/correlation.xml").write_text(md)
    shutil.copyfile(plans_path, out/"libraries/constructionplans.xml")
    (out/"expected.json").write_text(json.dumps(expected, indent=2))
    return expected


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("manifest", type=Path)
    ap.add_argument("plans", type=Path)
    ap.add_argument("out", type=Path)
    a = ap.parse_args()
    print(json.dumps({"stations": len(build(a.manifest, a.plans, a.out)), "extension": str(a.out)}))
