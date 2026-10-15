"""Kill a probe worker AND its Xray descendants when its deadline expires."""
from __future__ import annotations
import os
import signal
import subprocess


class ProcessTree:
    def __init__(self, proc):
        self.proc = proc
        self.job = None
        if os.name != 'nt':
            return
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
            proc.kill()
            raise OSError('Cannot create probe job')
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)) or not kernel.AssignProcessToJobObject(handle, w.HANDLE(int(proc._handle))):
            kernel.CloseHandle(handle)
            proc.kill()
            raise OSError('Cannot contain probe process tree')
        self.job, self.kernel = handle, kernel

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


def communicate_bounded(command, payload, timeout):
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
                            start_new_session=os.name != 'nt',
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    tree = None
    try:
        tree = ProcessTree(proc)
        stdout, _ = proc.communicate(payload, timeout=timeout)
        if proc.returncode:
            raise RuntimeError('ProbeWorkerFailed')
        return stdout
    finally:
        if tree is not None:
            tree.close()
        elif proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        for stream in (proc.stdin,proc.stdout,proc.stderr):
            if stream is not None:
                stream.close()
