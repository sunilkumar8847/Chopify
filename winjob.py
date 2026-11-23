"""Tie child processes to Studio's lifetime (Windows Job Object).

When Studio is closed normally it stops its workers itself. When it is
killed (Task Manager, power loss of the console, a crash), Windows does not
stop child processes on its own, so a worker could keep running and a
restarted Studio would start the same task again. Every worker and ffmpeg
process Studio starts is therefore added to one Job Object created with
``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``: when Studio's process ends for any
reason, Windows closes the job handle and ends every process in it.

Elsewhere (Linux/macOS) ``attach`` does nothing; the scheduler's restart
recovery still stops leftovers it can identify.
"""
import sys
import threading

_job = None
_lock = threading.Lock()


def _create():
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = 0x2000      # KILL_ON_JOB_CLOSE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                 ctypes.c_void_p, wintypes.DWORD]
    if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        kernel32.CloseHandle(job)
        return None
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    return job, kernel32


def attach(process):
    """Add a subprocess.Popen to Studio's job. Returns True when attached."""
    global _job
    if sys.platform != "win32" or process is None:
        return False
    with _lock:
        if _job is None:
            try:
                _job = _create() or False
            except (OSError, AttributeError):
                _job = False
        if not _job:
            return False
        job, kernel32 = _job
        try:
            return bool(kernel32.AssignProcessToJobObject(job, int(process._handle)))
        except (OSError, AttributeError):
            return False
