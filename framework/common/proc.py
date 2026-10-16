"""Kill a process tree and find who listens on a port, on Windows and POSIX.

Windows keeps what the code always did: `taskkill /F /T /PID` and `netstat -ano`. POSIX has neither, so a tree is the
pid plus its descendants as psutil sees them at that moment (snapshot first, then SIGKILL each; a child that started its
own session is still found, it keeps its parent), and the listening ports come from `lsof` (psutil.net_connections needs
root on macOS). Stdlib only at import; psutil is imported when called.
"""
import os
import signal
import subprocess
import sys

NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def kill_tree(pid, tree=True, timeout=None):
    """Kill `pid` (and with `tree` everything under it) at once, like `taskkill /F [/T] /PID`. Never raises. `timeout` (s) bounds the
    Windows taskkill call."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F"] + (["/T"] if tree else []) + ["/PID", str(pid)], capture_output=True,
                           creationflags=NOWIN, timeout=timeout)
        except Exception:
            pass
        return
    try:
        import psutil
        root = psutil.Process(pid)
        procs = (root.children(recursive=True) if tree else []) + [root]
    except Exception:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        return
    for p in procs:
        try:
            p.kill()
        except Exception:
            pass
    try:
        psutil.wait_procs(procs, timeout=3)
    except Exception:
        pass


def listening(ports, hosts=("127.0.0.1", "0.0.0.0")):
    """{port: pid as str} for every port in `ports` that a process listens on (TCP, on one of `hosts`)."""
    ports = {int(p) for p in ports}
    held = {}
    if os.name == "nt":
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                             creationflags=NOWIN).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3] == "LISTENING":
                host, _, port = parts[1].rpartition(":")
                if port.isdigit() and int(port) in ports and host in hosts:
                    held[int(port)] = parts[4]
        return held
    if sys.platform != "darwin":        # Linux (and the containers, which have no lsof): psutil lists the sockets
        try:
            import psutil
            for c in psutil.net_connections(kind="tcp"):
                if (c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port in ports
                        and (c.laddr.ip in hosts or c.laddr.ip in ("::", "::1", "0.0.0.0"))):
                    held[c.laddr.port] = str(c.pid or "?")
            return held
        except Exception:
            held = {}
    try:
        out = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpn"], capture_output=True, text=True,
                             timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return held
    pid = None
    for line in out.splitlines():
        if line.startswith("p"):
            pid = line[1:]
        elif line.startswith("n") and pid:
            host, _, port = line[1:].rpartition(":")
            if port.isdigit() and int(port) in ports and host in hosts + ("*", "localhost"):
                held[int(port)] = pid
    return held
