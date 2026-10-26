"""Windows-only helpers of the native X4 test lanes (ctypes, no third-party modules).

Read-only process inspection (image path, command line, creation time), window and dialog
lookup for one process, a kill-on-close job object for the X4 the runner starts, memory
and Steam client state. Nothing here writes to another process except WM_CLOSE/BM_CLICK
messages to windows of the process the runner started itself.
"""
from __future__ import annotations

import ctypes
import dataclasses
import os
import time
from ctypes import wintypes as wt

if os.name != 'nt':  # pragma: no cover - imported lazily by dev/x4lane.py on Windows only
    raise ImportError('lanewin requires Windows')

kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
user32 = ctypes.WinDLL('user32', use_last_error=True)
ntdll = ctypes.WinDLL('ntdll')
psapi = ctypes.WinDLL('psapi', use_last_error=True)

HANDLE = wt.HANDLE
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010
SYNCHRONIZE = 0x00100000
STILL_ACTIVE = 259
TH32CS_SNAPPROCESS = 0x00000002
PROCESS_COMMAND_LINE_INFORMATION = 60
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
WM_CLOSE = 0x0010
WM_GETTEXT = 0x000D
BM_CLICK = 0x00F5
SMTO_ABORTIFHUNG = 0x0002
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9


class FILETIME(ctypes.Structure):
    _fields_ = [('lo', wt.DWORD), ('hi', wt.DWORD)]

    @property
    def value(self) -> int:
        return (self.hi << 32) | self.lo


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [('dwSize', wt.DWORD), ('cntUsage', wt.DWORD), ('th32ProcessID', wt.DWORD),
                ('th32DefaultHeapID', ctypes.c_size_t), ('th32ModuleID', wt.DWORD),
                ('cntThreads', wt.DWORD), ('th32ParentProcessID', wt.DWORD),
                ('pcPriClassBase', ctypes.c_long), ('dwFlags', wt.DWORD),
                ('szExeFile', ctypes.c_wchar * 260)]


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [('Length', wt.USHORT), ('MaximumLength', wt.USHORT), ('Buffer', ctypes.c_void_p)]


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [('dwLength', wt.DWORD), ('dwMemoryLoad', wt.DWORD),
                ('ullTotalPhys', ctypes.c_uint64), ('ullAvailPhys', ctypes.c_uint64),
                ('ullTotalPageFile', ctypes.c_uint64), ('ullAvailPageFile', ctypes.c_uint64),
                ('ullTotalVirtual', ctypes.c_uint64), ('ullAvailVirtual', ctypes.c_uint64),
                ('ullAvailExtendedVirtual', ctypes.c_uint64)]


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [('cb', wt.DWORD), ('PageFaultCount', wt.DWORD),
                ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t)]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                ('LimitFlags', wt.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wt.DWORD),
                ('Affinity', ctypes.c_size_t), ('PriorityClass', wt.DWORD), ('SchedulingClass', wt.DWORD)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
        'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', JOBOBJECT_BASIC_LIMIT_INFORMATION), ('IoInfo', IO_COUNTERS),
                ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]


WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

kernel32.OpenProcess.restype = HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.CloseHandle.argtypes = [HANDLE]
kernel32.CreateToolhelp32Snapshot.restype = HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
kernel32.Process32FirstW.argtypes = [HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.argtypes = [HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.QueryFullProcessImageNameW.argtypes = [HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.GetProcessTimes.argtypes = [HANDLE] + [ctypes.POINTER(FILETIME)] * 4
kernel32.GetExitCodeProcess.argtypes = [HANDLE, ctypes.POINTER(wt.DWORD)]
kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
kernel32.CreateJobObjectW.restype = HANDLE
kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
kernel32.SetInformationJobObject.argtypes = [HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
kernel32.QueryInformationJobObject.argtypes = [HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD,
                                               ctypes.POINTER(wt.DWORD)]
kernel32.AssignProcessToJobObject.argtypes = [HANDLE, HANDLE]
kernel32.GetCurrentProcess.restype = HANDLE
ntdll.NtQueryInformationProcess.restype = ctypes.c_long
ntdll.NtQueryInformationProcess.argtypes = [HANDLE, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                                            ctypes.POINTER(ctypes.c_ulong)]
psapi.GetProcessMemoryInfo.argtypes = [HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wt.DWORD]
user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
user32.EnumChildWindows.argtypes = [wt.HWND, WNDENUMPROC, wt.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
user32.SendMessageTimeoutW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM, wt.UINT, wt.UINT,
                                       ctypes.POINTER(ctypes.c_size_t)]


class Handle:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self.value:
            kernel32.CloseHandle(self.value)
            self.value = None


def open_process(pid: int, access=PROCESS_QUERY_LIMITED_INFORMATION):
    handle = kernel32.OpenProcess(access, False, int(pid))
    return Handle(handle) if handle else None


def image_path(handle) -> str | None:
    size = wt.DWORD(32768)
    buffer = ctypes.create_unicode_buffer(size.value)
    if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
        return None
    return buffer.value


def command_line(handle) -> str | None:
    length = 8192
    for _ in range(4):
        buffer = ctypes.create_string_buffer(length)
        returned = ctypes.c_ulong(0)
        status = ntdll.NtQueryInformationProcess(handle, PROCESS_COMMAND_LINE_INFORMATION, buffer,
                                                 length, ctypes.byref(returned)) & 0xFFFFFFFF
        if status == STATUS_INFO_LENGTH_MISMATCH:
            length = max(length * 2, returned.value + 64)
            continue
        if status != 0:
            return None
        text = UNICODE_STRING.from_buffer(buffer)
        if not text.Buffer or not text.Length:
            return ''
        return ctypes.wstring_at(text.Buffer, text.Length // 2)
    return None


def creation_time(handle) -> int | None:
    created, exited, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
    if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                    ctypes.byref(kernel), ctypes.byref(user)):
        return None
    return created.value


def exit_code(handle) -> int | None:
    code = wt.DWORD(0)
    if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
        return None
    return code.value


def process_created(pid: int) -> int | None:
    handle = open_process(pid)
    if not handle:
        return None
    with handle as h:
        return creation_time(h)


def pid_alive(pid: int, created=None) -> bool:
    handle = open_process(pid)
    if not handle:
        return False
    with handle as h:
        if exit_code(h) != STILL_ACTIVE:
            return False
        if created not in (None, '', 'None'):
            try:
                return creation_time(h) == int(created)
            except (TypeError, ValueError):
                return True
        return True


def memory_counters(handle) -> dict:
    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
        return {}
    return {'working_set_gb': round(counters.WorkingSetSize / 2**30, 2),
            'peak_working_set_gb': round(counters.PeakWorkingSetSize / 2**30, 2),
            'peak_commit_gb': round(counters.PeakPagefileUsage / 2**30, 2)}


@dataclasses.dataclass
class Proc:
    pid: int
    exe: str | None
    cmdline: str | None
    created: int | None
    parent: int


def processes(name='X4.exe') -> list:
    """All processes with the given image file name (read-only inspection)."""
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        raise OSError(ctypes.get_last_error(), 'CreateToolhelp32Snapshot failed')
    found = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == name.lower():
                found.append((entry.th32ProcessID, entry.th32ParentProcessID))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    out = []
    for pid, parent in found:
        handle = open_process(pid)
        if not handle:
            out.append(Proc(pid, None, None, None, parent))
            continue
        with handle as h:
            out.append(Proc(pid, image_path(h), command_line(h), creation_time(h), parent))
    return out


def memory_status() -> tuple:
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(status)
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError(ctypes.get_last_error(), 'GlobalMemoryStatusEx failed')
    return status.ullTotalPhys, status.ullAvailPhys


def _window_text(hwnd, timeout_ms=500) -> str:
    buffer = ctypes.create_unicode_buffer(1024)
    result = ctypes.c_size_t(0)
    ok = user32.SendMessageTimeoutW(hwnd, WM_GETTEXT, 1024, ctypes.cast(buffer, ctypes.c_void_p).value,
                                    SMTO_ABORTIFHUNG, timeout_ms, ctypes.byref(result))
    if ok:
        return buffer.value
    user32.GetWindowTextW(hwnd, buffer, 1024)
    return buffer.value


def _class_name(hwnd) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def windows_of(pid: int) -> list:
    """Top-level windows of one process: dicts with hwnd, class, title, visible."""
    out = []

    def visit(hwnd, _param):
        owner = wt.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, 512)
            out.append({'hwnd': hwnd, 'class': _class_name(hwnd), 'title': title.value,
                        'visible': bool(user32.IsWindowVisible(hwnd))})
        return True

    user32.EnumWindows(WNDENUMPROC(visit), 0)
    return out


def child_controls(hwnd) -> list:
    out = []

    def visit(child, _param):
        out.append({'hwnd': child, 'class': _class_name(child), 'text': _window_text(child)})
        return True

    user32.EnumChildWindows(hwnd, WNDENUMPROC(visit), 0)
    return out


def dialogs_of(pid: int) -> list:
    """Visible standard dialogs (#32770, e.g. MessageBox) of one process with their texts."""
    out = []
    for window in windows_of(pid):
        if window['class'] == '#32770' and window['visible']:
            texts = [c['text'] for c in child_controls(window['hwnd']) if c['text']]
            out.append({'hwnd': window['hwnd'], 'title': window['title'], 'texts': texts})
    return out


def close_dialog(hwnd) -> None:
    """Dismiss a message box: WM_CLOSE, then click its buttons if it is still there."""
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    time.sleep(1.0)
    for control in child_controls(hwnd):
        if control['class'].lower() == 'button':
            user32.PostMessageW(control['hwnd'], BM_CLICK, 0, 0)


def main_windows(pid: int) -> list:
    return [w for w in windows_of(pid) if w['visible'] and w['class'] != '#32770']


def post_close(pid: int) -> int:
    """WM_CLOSE to the visible top-level windows of one process; returns how many."""
    count = 0
    for window in main_windows(pid):
        if user32.PostMessageW(window['hwnd'], WM_CLOSE, 0, 0):
            count += 1
    return count


class KillOnCloseJob:
    """A job object that terminates its processes when the runner's handle closes (also on
    a crash of the runner), so an X4 started by a lane run never outlives its runner."""

    def __init__(self):
        self.handle = kernel32.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), 'CreateJobObjectW failed')
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(self.handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                                                ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.get_last_error()
            self.close()
            raise OSError(error, 'SetInformationJobObject failed')

    def assign(self, process_handle) -> None:
        if not kernel32.AssignProcessToJobObject(self.handle, process_handle):
            raise OSError(ctypes.get_last_error(), 'AssignProcessToJobObject failed')

    def peak_memory_gb(self) -> float | None:
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        if not kernel32.QueryInformationJobObject(self.handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                                                  ctypes.byref(info), ctypes.sizeof(info), None):
            return None
        return round(info.PeakProcessMemoryUsed / 2**30, 2)

    def close(self):
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = None


def steam_status() -> dict:
    """HKCU\\Software\\Valve\\Steam\\ActiveProcess: a live steam.exe with a logged-in user."""
    import winreg
    out = {'running': False, 'pid': None, 'user': None, 'exe': None}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam\ActiveProcess') as key:
            out['pid'] = int(winreg.QueryValueEx(key, 'pid')[0])
            out['user'] = int(winreg.QueryValueEx(key, 'ActiveUser')[0])
    except OSError:
        return out
    handle = open_process(out['pid']) if out['pid'] else None
    if handle:
        with handle as h:
            out['exe'] = image_path(h)
            alive = exit_code(h) == STILL_ACTIVE
        out['running'] = bool(alive and out['exe'] and out['exe'].lower().endswith('\\steam.exe') and out['user'])
    return out


kernel32.TerminateProcess.argtypes = [HANDLE, wt.UINT]


def terminate(pid: int, expected_exe) -> bool:
    """TerminateProcess, only if the process image is exactly expected_exe."""
    from pathlib import PureWindowsPath
    handle = open_process(pid, PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE)
    if not handle:
        return False
    with handle as h:
        path = image_path(h)
        if not path or PureWindowsPath(path) != PureWindowsPath(str(expected_exe)):
            return False
        return bool(kernel32.TerminateProcess(h, 1))
