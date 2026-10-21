"""Independent raw-log gate for the native production dock importer experiment."""
import math
import re


def capture(text, mode, seed):
    rows, fingerprints, queries, queried = {}, {}, {}, {}
    pending = None
    total = None
    complete = False
    for line in text.splitlines():
        marker = '[X4LAN] DOCK_IMPORT '
        if marker not in line:
            continue
        value = line.split(marker, 1)[1]
        if complete:
            raise ValueError('dock events after completion')
        if value.startswith('event '):
            match = re.fullmatch(r'event mode=(capture|load) kind=(Begin|Row|End|Error) value=(.*)', value)
            if not match or match[1] != mode or match[2] == 'Error':
                raise ValueError('invalid native dock event')
            kind, fields = match[2], match[3].split('|')
            if len(fields) < 2 or fields[0] != seed or not re.fullmatch(r'0|[1-9][0-9]*', fields[1]):
                raise ValueError('wrong native dock seed/serial')
            serial = int(fields[1])
            if kind == 'Begin':
                if pending or len(fields) != 5:
                    raise ValueError('overlapping dock batch')
                first, last, count = map(int, fields[2:])
                if not 1 <= first <= last <= count <= 250000 or total not in (None, count):
                    raise ValueError('dock batch bounds changed')
                total = count
                if serial == 0:
                    if first != len(rows)+1 or last != min(first+63, total) or queries:
                        raise ValueError('nonsequential dock import')
                elif len(rows) != total or last != first or serial != len(queries)+1 or serial > 2:
                    raise ValueError('native query before complete import or wrong order')
                pending = dict(serial=serial, first=first, last=last, next=first, row=None)
            elif kind == 'Row':
                if not pending or len(fields) != 12 or serial != pending['serial']:
                    raise ValueError('unrequested native dock row')
                index = int(fields[2])
                if index != pending['next'] or index > pending['last']:
                    raise ValueError('missing/reordered/duplicate native dock row')
                if any(not re.fullmatch(r'0x[0-9a-fA-F]{1,16}', h) or int(h,16)==0 for h in fields[3:6]):
                    raise ValueError('invalid native dock handles')
                if any(not re.fullmatch(r'[a-z][a-z0-9_]*_macro', m) for m in fields[6:8]):
                    raise ValueError('invalid dock macro')
                if fields[8] not in ('0','1') or fields[9] not in ('0','1') or fields[8:10] == ['1','1']:
                    raise ValueError('inconsistent dock type')
                capacity, error = int(fields[10]), float(fields[11])
                if not 0 <= capacity <= 2147483647 or not math.isfinite(error) or not 0 <= error <= .05:
                    raise ValueError('invalid native dock capacity/relative pose')
                row = tuple(fields[3:11])
                if serial == 0:
                    rows[index] = (row, error)
                elif row != rows[index][0]:
                    raise ValueError('native query witness changed')
                pending['row'] = (index, row)
                pending['next'] += 1
            else:
                if not pending or len(fields) != 6 or fields[5] != '1' or list(map(int,fields[1:5])) != [
                    pending['serial'],pending['first'],pending['last'],total] or pending['next'] != pending['last']+1:
                    raise ValueError('incomplete native dock batch')
                if serial:
                    queries[serial] = pending['row']
                pending = None
        elif value.startswith('fingerprint '):
            match = re.fullmatch(r'fingerprint mode=(capture|load) index=([1-9][0-9]*) value=(\S+)', value)
            if not match or match[1] != mode or pending or total is None or len(rows) != total or queries:
                raise ValueError('fingerprint before completed import')
            index = int(match[2])
            if index != len(fingerprints)+1:
                raise ValueError('missing/reordered native fingerprint')
            fingerprints[index] = match[3]
        elif value.startswith('queried '):
            match = re.fullmatch(r'queried mode=(capture|load) index=([1-9][0-9]*) state=(EXTERNAL|STORED) fingerprint=(\S+)',value)
            if not match or match[1] != mode or pending or len(queries) != len(queried)+1 or len(fingerprints) != total:
                raise ValueError('query callback before complete readback')
            index, state = int(match[2]), match[3]
            if queries[len(queries)][0] != index or match[4] != fingerprints[index] or state in queried:
                raise ValueError('query callback identity changed')
            if rows[index][0][5 if state == 'EXTERNAL' else 6] != '1':
                raise ValueError('query callback type changed')
            queried[state] = index
        else:
            match = re.fullmatch(r'complete mode=(capture|load) seed=([a-f0-9]{32}) count=([1-9][0-9]*)',value)
            if not match or match[1] != mode or match[2] != seed or pending or int(match[3]) != total or len(rows) != total or len(fingerprints) != total or set(queried) != {'EXTERNAL','STORED'}:
                raise ValueError('incomplete native dock importer')
            complete = True
    if not complete:
        raise ValueError('native dock importer did not complete')
    nodes = {}
    expected = {}
    handles = set()
    def node(handle):
        value = int(handle,16)
        if value not in nodes:
            nodes[value] = len(nodes)+1
        return nodes[value]
    for index, (row, error) in rows.items():
        obj,parent,container,macro,container_macro,external,storage,capacity = row
        if int(obj,16) in handles or obj in (parent,container):
            raise ValueError('aliased native dock graph')
        handles.add(int(obj,16))
        labels = node(obj),node(container),node(parent)
        expected[index] = ':'.join(map(str,(seed,index,macro,container_macro,external,storage,capacity,*labels)))
    if fingerprints != expected:
        raise ValueError('native Lua fingerprints differ from independent component graph')
    return dict(rows=rows,fingerprints=fingerprints,count=total,queried=queried,
                max_relative_error=max(error for row,error in rows.values()))


def validate(before, after, seed):
    original, restored = capture(before,'capture',seed), capture(after,'load',seed)
    if original['fingerprints'] != restored['fingerprints'] or original['queried'] != restored['queried']:
        raise ValueError('saved dock identity/topology changed across reload')
    return dict(passed=True,count=original['count'],
                max_relative_error=max(original['max_relative_error'],restored['max_relative_error']),
                local_handles_renumbered=sum(original['rows'][i][0][0]!=restored['rows'][i][0][0] for i in original['rows']),
                queried_states=sorted(original['queried']), gameplay_acceptance=False)
