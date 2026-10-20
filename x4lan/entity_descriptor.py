"""Bounded native ship equipment descriptors, independent of transport/handles.

This codec does not authorize creation or claim crew, inventory or combat state.
Slot indices use the native preset API's zero-based convention. Group paths and
names are explicit: a bag of equivalent macros loses physical slot assignments.
"""
import re

MAX_BYTES = 131072
MAX_ROWS = 1024
KINDS = ('engine', 'shield', 'turret', 'weapon')
ATOM = re.compile(r'[A-Za-z0-9_.-]{1,96}\Z')
FIELDS = {'macro','owner','slots','groups','software','thruster'}


def atom(value, macro=False):
    if not isinstance(value,str) or not ATOM.fullmatch(value) or (macro and not value.endswith('_macro')):
        raise ValueError('Invalid descriptor identifier')
    return value


def integer(value, maximum, minimum=0):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('Invalid descriptor integer')
    return value


def path(value):
    if not isinstance(value,str) or len(value)>192 or any(not 32<=ord(c)<=126 for c in value):
        raise ValueError('Invalid native group path/name')
    return value.encode('ascii').hex()


def encode(descriptor):
    if not isinstance(descriptor,dict) or set(descriptor)!=FIELDS:
        raise ValueError('Incomplete or unsupported descriptor fields')
    for field in ('slots','groups','software'):
        if not isinstance(descriptor[field],list) or len(descriptor[field])>MAX_ROWS:
            raise ValueError('Invalid descriptor collection')
    rows=['X4SHIP1|'+atom(descriptor['macro'],True)+'|'+atom(descriptor['owner'])]
    slots={};groups={}
    for row in descriptor['slots']:
        if not isinstance(row,dict) or set(row)!={'kind','index','macro'} or row['kind'] not in KINDS:
            raise ValueError('Invalid equipment slot')
        key=(row['kind'],integer(row['index'],511))
        if key in slots:raise ValueError('Duplicate equipment slot')
        slots[key]='S|'+key[0]+'|'+str(key[1])+'|'+atom(row['macro'],True)
    rows.extend(slots[key] for key in sorted(slots))
    for row in descriptor['groups']:
        if (not isinstance(row,dict) or set(row)!={'kind','path','group','macro','count'}
                or row['kind'] not in ('shield','turret')):
            raise ValueError('Invalid equipment group')
        encoded_path,encoded_group=path(row['path']),path(row['group'])
        if not row['path'] or (row['path']=='..' and not row['group']):
            raise ValueError('Ungrouped hardware belongs in slots')
        key=(row['kind'],row['path'],row['group'])
        if key in groups:raise ValueError('Duplicate equipment group')
        groups[key]='|'.join(('G',key[0],encoded_path,encoded_group,atom(row['macro'],True),str(integer(row['count'],512,1))))
    rows.extend(groups[key] for key in sorted(groups))
    software=[atom(v) for v in descriptor['software']]
    if len(software)>64 or len(set(software))!=len(software):
        raise ValueError('Invalid/duplicate software')
    rows.extend('W|'+ware for ware in sorted(software))
    thruster=descriptor['thruster']
    rows.append('V|'+('none' if thruster is None else atom(thruster,True)))
    value='\n'.join(rows)
    if len(rows)>MAX_ROWS or len(value)>MAX_BYTES:
        raise ValueError('Descriptor exceeds bounded size')
    return value


def decode(value):
    if not isinstance(value,str) or not value.isascii() or not 1<=len(value)<=MAX_BYTES:
        raise ValueError('Invalid descriptor encoding/size')
    lines=value.split('\n')
    if not 2<=len(lines)<=MAX_ROWS:
        raise ValueError('Invalid descriptor row count')
    header=lines[0].split('|')
    if len(header)!=3 or header[0]!='X4SHIP1':raise ValueError('Unsupported descriptor header')
    result=dict(macro=header[1],owner=header[2],slots=[],groups=[],software=[])
    def number(text):
        if not re.fullmatch(r'0|[1-9][0-9]{0,8}',text):raise ValueError('Noncanonical descriptor number')
        return int(text)
    def unhex(text):
        if len(text)%2 or len(text)>384 or re.search('[^0-9a-f]',text):raise ValueError('Invalid group string encoding')
        try:return bytes.fromhex(text).decode('ascii')
        except (ValueError,UnicodeError) as e:raise ValueError('Invalid group string') from e
    for line in lines[1:]:
        f=line.split('|')
        if f[0]=='S' and len(f)==4:
            result['slots'].append(dict(kind=f[1],index=number(f[2]),macro=f[3]))
        elif f[0]=='G' and len(f)==6:
            result['groups'].append(dict(kind=f[1],path=unhex(f[2]),group=unhex(f[3]),macro=f[4],count=number(f[5])))
        elif f[0]=='W' and len(f)==2:result['software'].append(f[1])
        elif f[0]=='V' and len(f)==2 and 'thruster' not in result:
            result['thruster']=None if f[1]=='none' else f[1]
        else:raise ValueError('Invalid/duplicate descriptor row')
    if encode(result)!=value:raise ValueError('Descriptor is not canonical')
    return result
