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



# ---------------------------------------------------------------------------------------
# network endpoints (session mode: a server must stay on loopback) and job process lists

iphlpapi = ctypes.WinDLL('iphlpapi')
AF_INET = 2
AF_INET6 = 23
TCP_TABLE_OWNER_PID_ALL = 5
UDP_TABLE_OWNER_PID = 1
ERROR_INSUFFICIENT_BUFFER = 122
JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
TCP_STATES = {1: 'CLOSED', 2: 'LISTEN', 3: 'SYN_SENT', 4: 'SYN_RCVD', 5: 'ESTABLISHED', 6: 'FIN_WAIT1',
              7: 'FIN_WAIT2', 8: 'CLOSE_WAIT', 9: 'CLOSING', 10: 'LAST_ACK', 11: 'TIME_WAIT', 12: 'DELETE_TCB'}


class MIB_TCPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [('dwState', wt.DWORD), ('dwLocalAddr', wt.DWORD), ('dwLocalPort', wt.DWORD),
                ('dwRemoteAddr', wt.DWORD), ('dwRemotePort', wt.DWORD), ('dwOwningPid', wt.DWORD)]


class MIB_UDPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [('dwLocalAddr', wt.DWORD), ('dwLocalPort', wt.DWORD), ('dwOwningPid', wt.DWORD)]


class MIB_TCP6ROW_OWNER_PID(ctypes.Structure):
    _fields_ = [('ucLocalAddr', ctypes.c_ubyte * 16), ('dwLocalScopeId', wt.DWORD), ('dwLocalPort', wt.DWORD),
                ('ucRemoteAddr', ctypes.c_ubyte * 16), ('dwRemoteScopeId', wt.DWORD), ('dwRemotePort', wt.DWORD),
                ('dwState', wt.DWORD), ('dwOwningPid', wt.DWORD)]


class MIB_UDP6ROW_OWNER_PID(ctypes.Structure):
    _fields_ = [('ucLocalAddr', ctypes.c_ubyte * 16), ('dwLocalScopeId', wt.DWORD), ('dwLocalPort', wt.DWORD),
                ('dwOwningPid', wt.DWORD)]


for _name in ('GetExtendedTcpTable', 'GetExtendedUdpTable'):
    getattr(iphlpapi, _name).restype = wt.DWORD
    getattr(iphlpapi, _name).argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.DWORD), wt.BOOL, wt.ULONG,
                                         ctypes.c_int, wt.ULONG]


def _port(value) -> int:
    return ((value & 0xFF) << 8) | ((value >> 8) & 0xFF)


def _ipv4(value) -> str:
    import socket
    import struct
    return socket.inet_ntoa(struct.pack('<I', value))


def _ipv6(raw) -> str:
    import socket
    return socket.inet_ntop(socket.AF_INET6, bytes(raw))


def _rows(function, family, table_class, row_type):
    size = wt.DWORD(0)
    function(None, ctypes.byref(size), False, family, table_class, 0)
    for _ in range(6):
        buffer = ctypes.create_string_buffer(size.value + 8192)
        size = wt.DWORD(len(buffer))
        status = function(buffer, ctypes.byref(size), False, family, table_class, 0)
        if status == 0:
            count = ctypes.c_uint32.from_buffer(buffer).value
            return list((row_type * count).from_buffer(buffer, 4))
        if status != ERROR_INSUFFICIENT_BUFFER:
            raise OSError(status, f'{function.__name__} failed')
    raise OSError(ERROR_INSUFFICIENT_BUFFER, 'endpoint table kept growing')


def endpoints(pids) -> list:
    """TCP (all states) and UDP endpoints owned by the given processes, IPv4 and IPv6."""
    pids = {int(p) for p in pids}
    out = []
    for row in _rows(iphlpapi.GetExtendedTcpTable, AF_INET, TCP_TABLE_OWNER_PID_ALL, MIB_TCPROW_OWNER_PID):
        if row.dwOwningPid in pids:
            out.append({'proto': 'tcp', 'pid': row.dwOwningPid, 'local': _ipv4(row.dwLocalAddr),
                        'lport': _port(row.dwLocalPort), 'remote': _ipv4(row.dwRemoteAddr),
                        'rport': _port(row.dwRemotePort), 'state': TCP_STATES.get(row.dwState, str(row.dwState))})
    for row in _rows(iphlpapi.GetExtendedTcpTable, AF_INET6, TCP_TABLE_OWNER_PID_ALL, MIB_TCP6ROW_OWNER_PID):
        if row.dwOwningPid in pids:
            out.append({'proto': 'tcp6', 'pid': row.dwOwningPid, 'local': _ipv6(row.ucLocalAddr),
                        'lport': _port(row.dwLocalPort), 'remote': _ipv6(row.ucRemoteAddr),
                        'rport': _port(row.dwRemotePort), 'state': TCP_STATES.get(row.dwState, str(row.dwState))})
    for row in _rows(iphlpapi.GetExtendedUdpTable, AF_INET, UDP_TABLE_OWNER_PID, MIB_UDPROW_OWNER_PID):
        if row.dwOwningPid in pids:
            out.append({'proto': 'udp', 'pid': row.dwOwningPid, 'local': _ipv4(row.dwLocalAddr),
                        'lport': _port(row.dwLocalPort)})
    for row in _rows(iphlpapi.GetExtendedUdpTable, AF_INET6, UDP_TABLE_OWNER_PID, MIB_UDP6ROW_OWNER_PID):
        if row.dwOwningPid in pids:
            out.append({'proto': 'udp6', 'pid': row.dwOwningPid, 'local': _ipv6(row.ucLocalAddr),
                        'lport': _port(row.dwLocalPort)})
    return out


def _job_pids(self) -> list:
    """PIDs of all processes currently in this job (a server and the processes it started)."""
    capacity = 256
    for _ in range(4):
        class LIST(ctypes.Structure):
            _fields_ = [('NumberOfAssignedProcesses', wt.DWORD), ('NumberOfProcessIdsInList', wt.DWORD),
                        ('ProcessIdList', ctypes.c_size_t * capacity)]
        data = LIST()
        if kernel32.QueryInformationJobObject(self.handle, JOB_OBJECT_BASIC_PROCESS_ID_LIST, ctypes.byref(data),
                                              ctypes.sizeof(data), None):
            return [int(data.ProcessIdList[i]) for i in range(data.NumberOfProcessIdsInList)]
        if data.NumberOfAssignedProcesses > capacity:
            capacity = data.NumberOfAssignedProcesses + 16
            continue
        raise OSError(ctypes.get_last_error(), 'QueryInformationJobObject(process list) failed')
    return []


kernel32.TerminateJobObject.argtypes = [HANDLE, wt.UINT]


def _job_terminate(self, code=1) -> bool:
    return bool(self.handle and kernel32.TerminateJobObject(self.handle, code))


KillOnCloseJob.pids = _job_pids
KillOnCloseJob.terminate = _job_terminate


user32.IsHungAppWindow.argtypes = [wt.HWND]


def window_hung(pid: int) -> bool:
    """True if a visible top-level window of the process does not respond (IsHungAppWindow)."""
    return any(user32.IsHungAppWindow(w['hwnd']) for w in main_windows(pid))


# ---------------------------------------------------------------------------------------
# named object presence without opening it (protected X4 clients must never be disturbed)

class OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = [('Length', wt.ULONG), ('RootDirectory', HANDLE), ('ObjectName', ctypes.POINTER(UNICODE_STRING)),
                ('Attributes', wt.ULONG), ('SecurityDescriptor', ctypes.c_void_p),
                ('SecurityQualityOfService', ctypes.c_void_p)]


class OBJECT_DIRECTORY_INFORMATION(ctypes.Structure):
    _fields_ = [('Name', UNICODE_STRING), ('TypeName', UNICODE_STRING)]


DIRECTORY_QUERY = 0x0001
STATUS_MORE_ENTRIES = 0x00000105
STATUS_NO_MORE_ENTRIES = 0x8000001A
ntdll.NtOpenDirectoryObject.restype = ctypes.c_long
ntdll.NtOpenDirectoryObject.argtypes = [ctypes.POINTER(HANDLE), wt.DWORD, ctypes.POINTER(OBJECT_ATTRIBUTES)]
ntdll.NtQueryDirectoryObject.restype = ctypes.c_long
ntdll.NtQueryDirectoryObject.argtypes = [HANDLE, ctypes.c_void_p, wt.ULONG, wt.BOOLEAN, wt.BOOLEAN,
                                         ctypes.POINTER(wt.ULONG), ctypes.POINTER(wt.ULONG)]
ntdll.NtClose.argtypes = [HANDLE]
kernel32.ProcessIdToSessionId.argtypes = [wt.DWORD, ctypes.POINTER(wt.DWORD)]
kernel32.GetCurrentProcessId.restype = wt.DWORD


def named_object_exists(name: str, session=None) -> bool:
    r"""True if \Sessions\<session>\BaseNamedObjects\<name> exists. Lists the object directory only;
    the object itself is never opened, so no handle of ours can keep it alive or race a release."""
    if session is None:
        value = wt.DWORD(0)
        kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(value))
        session = value.value
    path = rf'\Sessions\{session}\BaseNamedObjects' if session else r'\BaseNamedObjects'
    text = ctypes.create_unicode_buffer(path)
    us = UNICODE_STRING(len(path) * 2, len(path) * 2 + 2, ctypes.cast(text, ctypes.c_void_p).value)
    attributes = OBJECT_ATTRIBUTES(ctypes.sizeof(OBJECT_ATTRIBUTES), None, ctypes.pointer(us), 0x40, None, None)
    directory = HANDLE()
    status = ntdll.NtOpenDirectoryObject(ctypes.byref(directory), DIRECTORY_QUERY, ctypes.byref(attributes))
    if status != 0:
        raise OSError(status & 0xFFFFFFFF, f'NtOpenDirectoryObject({path}) failed')
    try:
        context, returned = wt.ULONG(0), wt.ULONG(0)
        buffer = ctypes.create_string_buffer(64 * 1024)
        restart = True
        while True:
            status = ntdll.NtQueryDirectoryObject(directory, buffer, len(buffer), False, restart,
                                                  ctypes.byref(context), ctypes.byref(returned)) & 0xFFFFFFFF
            restart = False
            if status == STATUS_NO_MORE_ENTRIES:
                return False
            if status not in (0, STATUS_MORE_ENTRIES):
                raise OSError(status, 'NtQueryDirectoryObject failed')
            size = ctypes.sizeof(OBJECT_DIRECTORY_INFORMATION)
            for index in range(len(buffer) // size):
                entry = OBJECT_DIRECTORY_INFORMATION.from_buffer(buffer, index * size)
                if not entry.Name.Length:
                    break
                if ctypes.wstring_at(entry.Name.Buffer, entry.Name.Length // 2) == name:
                    return True
            if status == 0:
                return False
    finally:
        ntdll.NtClose(directory)
