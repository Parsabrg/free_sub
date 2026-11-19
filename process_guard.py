"""Kill a probe worker AND its Xray descendants when its deadline expires."""
from __future__ import annotations
import os
from pathlib import Path
import signal
import subprocess
import threading
import time


def _kill_on_close_job(process_handle):
    """A Windows job holding process_handle whose processes, and every descendant
    they start, die when the returned job handle closes. Returns (job, kernel32)."""
    import ctypes
    from ctypes import wintypes as w
    class BASIC(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong),
                    ('PerJobUserTimeLimit', ctypes.c_longlong), ('LimitFlags', w.DWORD),
                    ('MinimumWorkingSetSize', ctypes.c_size_t), ('MaximumWorkingSetSize', ctypes.c_size_t),
                    ('ActiveProcessLimit', w.DWORD), ('Affinity', ctypes.c_size_t),
                    ('PriorityClass', w.DWORD), ('SchedulingClass', w.DWORD)]
    class IO(ctypes.Structure):
        _fields_ = [(k, ctypes.c_ulonglong) for k in ('ReadOperationCount','WriteOperationCount',
                     'OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount')]
    class EXTENDED(ctypes.Structure):
        _fields_ = [('BasicLimitInformation', BASIC), ('IoInfo', IO),
                    ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                    ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    kernel.SetInformationJobObject.restype = w.BOOL
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.AssignProcessToJobObject.restype = w.BOOL
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise OSError('Cannot create probe job')
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)) or not kernel.AssignProcessToJobObject(handle, process_handle):
        kernel.CloseHandle(handle)
        raise OSError('Cannot contain probe process tree')
    return handle, kernel


_SELF_JOB = None


def contain_self():
    """Every process this one starts, and theirs, ends when this one ends, however it
    ends: a crash, Task Scheduler's time limit, taskkill. Without it a run killed while
    its source relay, egress check or verify.py was up left that Xray running."""
    global _SELF_JOB
    if os.name != 'nt' or _SELF_JOB is not None:
        return
    import ctypes
    try:
        # The handle is held, never closed: Windows closes it as this process exits.
        _SELF_JOB = _kill_on_close_job(ctypes.c_void_p(-1))  # GetCurrentProcess()
    except OSError:
        pass   # Still runs; only the after-death cleanup is lost.


def stray_xray(rows, binary):
    """PIDs of processes running [binary] whose parent is gone: left behind by a run
    that was killed before contain_self existed, or by a hand-run test. rows are
    (pid, parent_pid, created, path); a parent created after its child is a reused PID."""
    target = os.path.normcase(str(Path(binary).resolve()))
    created = {pid: when for pid, _, when, _ in rows}
    return [pid for pid, parent, when, path in rows
            if path and os.path.normcase(path) == target
            and not (parent in created and created[parent] <= when)]


def kill_stray_xray(binary):
    """Ends Xray processes of [binary] that no live process owns. Returns how many."""
    if os.name != 'nt':
        return 0
    script = ("Get-CimInstance Win32_Process|?{$_.CreationDate}|%{'{0}|{1}|{2}|{3}' -f "
              "$_.ProcessId,$_.ParentProcessId,$_.CreationDate.ToFileTimeUtc(),$_.ExecutablePath}")
    try:
        out = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
                             capture_output=True, text=True, timeout=30,
                             creationflags=subprocess.CREATE_NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    rows = []
    for line in out.splitlines():
        parts = line.split('|', 3)
        if len(parts) == 4 and all(p.isdigit() for p in parts[:3]):
            rows.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3]))
    killed = 0
    for pid in stray_xray(rows, binary):
        try:
            os.kill(pid, signal.SIGTERM)   # TerminateProcess on Windows
            killed += 1
        except OSError:
            pass
    return killed


class ProcessTree:
    def __init__(self, proc):
        self.proc = proc
        self.job = None
        if os.name != 'nt':
            return
        from ctypes import wintypes as w
        try:
            self.job, self.kernel = _kill_on_close_job(w.HANDLE(int(proc._handle)))
        except OSError:
            proc.kill()
            raise

    def close(self):
        if self.job:
            self.kernel.CloseHandle(self.job)
            self.job = None
        elif os.name != 'nt':
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=5)


class ProbeDeadline(subprocess.TimeoutExpired):
    def __init__(self, command, timeout, phase):
        super().__init__(command, timeout)
        self.phase = phase


def communicate_bounded(command, payload, timeout, *, ready_timeout=None):
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
                            start_new_session=os.name != 'nt',
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    tree = None
    exchange_thread = None
    try:
        tree = ProcessTree(proc)
        if ready_timeout is None:
            stdout, _ = proc.communicate(payload, timeout=timeout)
        else:
            # Start the short network deadline only after the worker's READY line.
            deadline = time.monotonic() + timeout
            ready_or_done = threading.Event()
            done = threading.Event()
            response = {}
            def exchange():
                try:
                    proc.stdin.write(payload)
                    proc.stdin.close()
                    first = proc.stdout.readline()
                    if first == 'READY\n':
                        response['ready_at'] = time.monotonic()
                        ready_or_done.set()
                        first = ''
                    response['stdout'] = first + proc.stdout.read()
                except Exception as exc:
                    response['error'] = exc
                finally:
                    done.set()
                    ready_or_done.set()
            exchange_thread = threading.Thread(target=exchange, daemon=True)
            exchange_thread.start()
            if not ready_or_done.wait(max(0, deadline-time.monotonic())):
                raise ProbeDeadline(command, timeout, 'startup')
            if 'ready_at' in response:
                deadline = min(deadline, response['ready_at'] + ready_timeout)
            if not done.wait(max(0, deadline-time.monotonic())):
                raise ProbeDeadline(command, ready_timeout, 'network')
            if 'error' in response:
                raise response['error']
            stdout = response['stdout']
            proc.wait(timeout=max(0.001, deadline-time.monotonic()))
        if proc.returncode:
            raise RuntimeError('ProbeWorkerFailed')
        return stdout
    finally:
        if tree is not None:
            tree.close()
        elif proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        if exchange_thread is not None:
            exchange_thread.join(timeout=5)
        for stream in (proc.stdin,proc.stdout,proc.stderr):
            if stream is not None:
                stream.close()


# One probe at its peak: a Python worker (~17 MB), its Xray (~21 MB) and the short DNS
# helper. Measured on Windows 11, Xray 26.3.27.
PROBE_MB = 45


def machine():
    """(total RAM in MB, logical processors) of this PC; RAM is None if unknown."""
    ram = None
    try:
        if os.name == 'nt':
            import ctypes
            class STATUS(ctypes.Structure):
                _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                            ('ullTotalPhys', ctypes.c_ulonglong)] + [
                           (k, ctypes.c_ulonglong) for k in ('ullAvailPhys', 'ullTotalPageFile', 'ullAvailPageFile',
                                                             'ullTotalVirtual', 'ullAvailVirtual', 'ullAvailExtendedVirtual')]
            status = STATUS(dwLength=ctypes.sizeof(STATUS))
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                ram = status.ullTotalPhys // 2**20
        else:
            ram = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') // 2**20
    except (OSError, ValueError, AttributeError):
        pass
    return ram, os.cpu_count() or 1


def auto_workers(ram_mb=None, threads=None):
    """Parallel tests for this PC: at most a quarter of its RAM and four tests per
    logical processor (they mostly wait on the network), from 4 to 64."""
    if ram_mb is None and threads is None:
        ram_mb, threads = machine()
    by_ram = (ram_mb or 4096) // 4 // PROBE_MB
    return max(4, min(64, by_ram, (threads or 1) * 4))
