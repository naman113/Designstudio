"""Windows job object: tie every process this process starts to its own life.

Kept in framework/common so the studio and the orchestrator use the same code (a service imports only framework/common).
"""
import os

_JOB_HANDLE = None


def tie_children_to_me():
    """Windows: put THIS process in a job object that kills every process in it when the last handle
    closes, i.e. when this process dies by any means (taskkill /F without /T, a crash). Its children
    (and their trees) join the job on creation, so a killed or restarted
    server can never orphan a child process. POSIX: see _tie_posix. Returns True when applied."""
    global _JOB_HANDLE
    if os.name != "nt":
        return _tie_posix()
    if _JOB_HANDLE is not None:
        return True
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IOC(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("ro", "wo", "oo", "rb", "wb", "ob")]

        class EXT(ctypes.Structure):
            _fields_ = [("Basic", BASIC), ("Io", IOC), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return False
        info = EXT()
        info.Basic.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            return False
        if not k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
            return False
        _JOB_HANDLE = job
        return True
    except Exception:
        return False


_POSIX_TIED = False


def _kill_descendants():
    try:
        import psutil
        kids = psutil.Process().children(recursive=True)
    except Exception:
        return
    for k in kids:
        try:
            k.kill()
        except Exception:
            pass


kill_descendants = _kill_descendants     # public name (a service that handles SIGTERM itself ends its children with it)


def _tie_posix():
    """POSIX stand-in for the job object: kill every descendant (psutil snapshot) when this
    process exits normally (atexit) or on SIGTERM / SIGHUP (then the signal's default action runs, so the exit status
    is unchanged). The gap against Windows: SIGKILL or a hard crash runs no handler, so the children are orphaned;
    macOS has no PR_SET_PDEATHSIG. The callers' stop paths (services.py stop, the supervisor) kill whole
    trees themselves, which covers the normal cases. A handler someone else installed is left alone."""
    global _POSIX_TIED
    if _POSIX_TIED:
        return True
    import atexit
    import signal

    def _on_signal(signum, _frame):
        _kill_descendants()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    atexit.register(_kill_descendants)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            if signal.getsignal(sig) in (signal.SIG_DFL, None):
                signal.signal(sig, _on_signal)
        except (ValueError, OSError):       # not the main thread: atexit alone
            pass
    _POSIX_TIED = True
    return True
