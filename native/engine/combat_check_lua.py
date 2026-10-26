"""Load x4mp_combat.dll in a standalone Lua process (never X4) and check its Lua ABI and refusals.

    python native/engine/combat_check_lua.py --lua-runtime <lua51_64.dll> [--module rust/target/release/x4mp_combat.dll]

Checks: the module table, refusal outside the pinned X4 process, refusal on a coroutine,
argument validation. Mirrors check_lua_abi.py for the fixed-tick module.
"""
import argparse
import ctypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--lua-runtime', required=True, type=Path)
    parser.add_argument('--module', type=Path, default=ROOT / 'rust' / 'target' / 'release' / 'x4mp_combat.dll')
    args = parser.parse_args()
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
    try:
        lua.luaL_openlibs(state)
        path = ''.join('\\%03d' % b for b in str(args.module.resolve()).encode('utf-8'))
        source = f'''
          local module=assert(package.loadlib("{path}","luaopen_x4mp_combat"))()
          assert(type(module.start)=='function')
          local h,why=module.start(-1)
          assert(h==nil and why=='invalid capacity',tostring(why))
          h,why=module.start(3e6)
          assert(h==nil and why=='invalid capacity',tostring(why))
          h,why=module.start(1000,true,true)
          assert(h==nil and why=='unsupported executable (headers)',tostring(why))
          h,why=module.start()
          assert(h==nil and why=='unsupported executable (headers)',tostring(why))
          local thread=coroutine.create(function()
            local h2,why2=module.start(10)
            assert(h2==nil and why2=='main Lua thread required',tostring(why2))
          end)
          local ok,err=coroutine.resume(thread)
          assert(ok,tostring(err))
          assert(type(module.census)=='function')
          h,why=module.census(0)
          assert(h==nil and why=='invalid frame capacity',tostring(why))
          h,why=module.census(1.5)
          assert(h==nil and why=='invalid frame capacity',tostring(why))
          h,why=module.census(1000)
          assert(h==nil and why=='unsupported executable (headers)',tostring(why))
          thread=coroutine.create(function()
            local h2,why2=module.census(10)
            assert(h2==nil and why2=='main Lua thread required',tostring(why2))
          end)
          ok,err=coroutine.resume(thread)
          assert(ok,tostring(err))
        '''.encode('utf-8')
        result = lua.luaL_loadstring(state, source)
        if not result:
            result = lua.lua_pcall(state, 0, 0, 0)
        if result:
            raise RuntimeError(lua.lua_tolstring(state, -1, None).decode('utf-8', errors='replace'))
    finally:
        lua.lua_close(state)
    print('PASS: x4mp_combat Lua ABI, argument validation, coroutine rejection, non-X4 process rejection')


if __name__ == '__main__':
    main()
