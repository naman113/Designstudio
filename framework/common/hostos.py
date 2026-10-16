"""Per-OS defaults and two process helpers, so the services run on Windows, macOS and Linux.

Each default is overridden by its environment variable. The call sites keep their Windows commands (`netstat`, `taskkill`)
behind `os.name == "nt"`; only the other branch calls the helpers below.

    name                 override        Windows                                               macOS and Linux
    VENV_PY_DEFAULT      MECH_PYTHON     C:\\venvs\\buildcore\\Scripts\\python.exe                 ~/venvs/buildcore/bin/python

Named `hostos`, not `platform`: a module of that name would shadow the standard library's for any script run from this
folder. Standard library only at import time (a plain system Python imports modules that import this); psutil is
imported inside the functions that need it.
"""
import os
import shutil
import signal
import subprocess
import sys

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"


if IS_WIN:
    VENV_PY_DEFAULT = r"C:\venvs\buildcore\Scripts\python.exe"
else:                                   # macOS and Linux
    VENV_PY_DEFAULT = os.path.join(os.path.expanduser("~"), "venvs", "buildcore", "bin", "python")


def venv_python():
    return os.environ.get("MECH_PYTHON") or VENV_PY_DEFAULT


def bash_exe():
    """A bash for shell helper scripts: Git for Windows' on Windows when it is
    installed (not the WSL launcher on PATH), else the first bash on PATH."""
    if IS_WIN:
        git = "C:/Program Files/Git/bin/bash.exe"
        if os.path.exists(git):
            return git
    return shutil.which("bash") or "bash"


def node_path(given=None):
    """The folder with puppeteer-core that the headless-Chrome page checks need: the --node-path given, else env
    BC_NODE_PATH. Says in one sentence when neither is set."""
    p = given or os.environ.get("BC_NODE_PATH")
    if not p:
        raise SystemExit("This page check needs a folder with puppeteer-core: set BC_NODE_PATH (or pass --node-path).")
    return p


def kill_tree(pid):
    """End `pid` and everything under it, by this pid only (never by name). Not Windows: the callers keep
    `taskkill /F /T /PID` there. Uses psutil (descendants first, then the pid, SIGKILL); without psutil, the process group
    when `pid` leads one, else the pid alone. Returns how many processes were signalled."""
    pid = int(pid)
    try:
        import psutil
        root = psutil.Process(pid)
        procs = root.children(recursive=True) + [root]
    except ImportError:
        procs = None
    except Exception:                   # gone already
        return 0
    if procs is None:
        try:
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
            return 1
        except OSError:
            return 0
    for p in procs:
        try:
            p.kill()
        except Exception:
            pass
    try:
        psutil.wait_procs(procs, timeout=5)
    except Exception:
        pass
    return len(procs)


def listening(ports):
    """{port: pid} of the given TCP ports that something listens on at 127.0.0.1 or every address. Not Windows: the
    callers keep `netstat -ano` there. macOS: `lsof` (psutil cannot list other processes' sockets without root there);
    Linux: psutil, else lsof."""
    want = {int(p) for p in ports}
    held = {}
    if not IS_MAC:
        try:
            import psutil
            for c in psutil.net_connections(kind="tcp"):
                if (c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port in want
                        and c.laddr.ip in ("127.0.0.1", "0.0.0.0", "::")):
                    held[c.laddr.port] = str(c.pid or "?")
            return held
        except Exception:
            pass
    try:
        out = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-F", "pn"], capture_output=True, text=True,
                             timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return held
    pid = "?"
    for line in out.splitlines():
        if line.startswith("p"):
            pid = line[1:]
        elif line.startswith("n"):
            host, _, port = line[1:].rpartition(":")
            if port.isdigit() and int(port) in want and host in ("127.0.0.1", "*", "0.0.0.0", "[::]"):
                held[int(port)] = pid
    return held
