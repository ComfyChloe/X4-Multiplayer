"""Check the DLL in a standalone Lua process; never attach to or start X4."""
import argparse
import ctypes
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lua-runtime', required=True, type=Path)
    parser.add_argument('--module', required=True, type=Path)
    parser.add_argument('--symbol', default='luaopen_x4lan_simtick',
                        choices=['luaopen_x4lan_simtick', 'luaopen_x4lan_fixedtick'])
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
        # Decimal byte escapes avoid treating paths as executable Lua text.
        path = ''.join('\\%03d' % b for b in str(args.module.resolve()).encode('utf-8'))
        argument = '' if args.symbol == 'luaopen_x4lan_fixedtick' else 'function() end'
        source = f'''
          local module=assert(package.loadlib("{path}","{args.symbol}"))()
          assert(type(module.start)=='function')
          local ok,result=pcall(module.start,123)
          assert(not ok or result==nil)
          local handle,reason=module.start({argument})
          assert(handle==nil and reason=='unsupported executable or modified engine signatures',reason)
          local thread=coroutine.create(function()
            local h,why=module.start({argument})
            assert(h==nil and why=='main Lua thread required',why)
          end)
          assert(coroutine.resume(thread))
        '''.encode('utf-8')
        result = lua.luaL_loadstring(state, source)
        if not result:
            result = lua.lua_pcall(state, 0, 0, 0)
        if result:
            raise RuntimeError(lua.lua_tolstring(state, -1, None).decode('utf-8', errors='replace'))
    finally:
        lua.lua_close(state)
    print('PASS: actual Lua ABI, argument validation, coroutine rejection, non-X4 process rejection')


if __name__ == '__main__':
    main()
