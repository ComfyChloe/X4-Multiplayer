"""Check x4lan_xform_64.dll in a standalone Lua process (real lua51_64.dll); never starts or attaches to X4.

    python native/engine/xform_check_lua.py --lua-runtime <lua51_64.dll copy> --module .local/engine/x4lan_xform_64.dll
"""
import argparse
import ctypes
from pathlib import Path
import tempfile


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--lua-runtime', required=True, type=Path)
    ap.add_argument('--module', required=True, type=Path)
    args = ap.parse_args()
    lua = ctypes.WinDLL(str(args.lua_runtime.resolve()))
    lua.luaL_newstate.restype = ctypes.c_void_p
    lua.luaL_openlibs.argtypes = [ctypes.c_void_p]
    lua.luaL_loadstring.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lua.lua_pcall.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    lua.lua_tolstring.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    lua.lua_tolstring.restype = ctypes.c_char_p
    lua.lua_close.argtypes = [ctypes.c_void_p]
    state = lua.luaL_newstate()
    if not state:
        raise RuntimeError('luaL_newstate failed')
    with tempfile.TemporaryDirectory() as tmp:
        esc = lambda s: ''.join('\\%03d' % b for b in s.encode('utf-8'))  # decimal escapes: paths are data
        module, folder = esc(str(args.module.resolve())), esc(tmp.replace('\\', '/'))
        source = f'''
          local m=assert(package.loadlib("{module}","luaopen_x4lan_xform"))()
          assert(type(m.start)=='function')
          local usage='usage: start(dir, first_s, every_s, count, trace_every)'
          local function refused(expected,...)
            local h,why=m.start(...)
            assert(h==nil and why==expected,tostring(why))
          end
          refused(usage)
          refused(usage,"{folder}",1,2,3)
          refused(usage,123,1,2,3,4)
          refused(usage,"{folder}",1,"2",3,4)
          refused('invalid arguments',"{folder}",1,-1,3,4)
          refused('invalid arguments',"{folder}",1,2,0,4)
          refused('invalid arguments',"{folder}",1,2,1001,4)
          refused('invalid arguments',"{folder}",1,2,3,-1)
          refused('invalid arguments',"",1,2,3,4)
          refused('capture directory missing',"{folder}/missing",1,2,3,4)
          refused('unsupported executable (PE header)',"{folder}",1,2,3,4)
          refused(usage,"{folder}",1,2,3,4,1)
          refused(usage,"{folder}",1,2,3,4,"1","x.dll")
          refused(usage,"{folder}",1,2,3,4,1,2)
          refused('invalid arguments',"{folder}",1,2,3,4,-1,"x.dll")
          refused('invalid arguments',"{folder}",1,2,3,4,1001,"x.dll")
          refused('unsupported executable (PE header)',"{folder}",1,2,3,4,1,"x.dll")
          local co=coroutine.create(function() refused('main Lua thread required',"{folder}",1,2,3,4) end)
          local ok,err=coroutine.resume(co)
          assert(ok,err)
        '''.encode('utf-8')
        try:
            lua.luaL_openlibs(state)
            result = lua.luaL_loadstring(state, source)
            if not result:
                result = lua.lua_pcall(state, 0, 0, 0)
            if result:
                raise RuntimeError(lua.lua_tolstring(state, -1, None).decode('utf-8', errors='replace'))
        finally:
            lua.lua_close(state)
    print('PASS: real Lua ABI, argument validation, missing directory, coroutine rejection, non-X4 process rejection')


if __name__ == '__main__':
    main()
