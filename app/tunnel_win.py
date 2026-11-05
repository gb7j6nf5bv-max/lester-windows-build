"""Runs the iOS 17+ tunnel on Windows, with admin rights, for as long as Lester runs.

The Windows twin of the tunnel part of start_backend.sh. Lester.exe starts this through the
Windows "allow this app to make changes" prompt, because the tunnel adds a network adapter.
  - Any tunnel left over from an older Lester (found by its port, 49151) is stopped first.
  - TCP: iOS 18.2+ refuses QUIC, so Wi-Fi only works over TCP.
  - It stops the tunnel by itself once the Lester server (SERVER_PID) exits.

Runs with pymobiledevice3's own Python. Usage: pythonw tunnel_win.py SERVER_PID LOG_FILE
"""
import os
import subprocess
import sys
import time

TUNNEL_PORT = 49151
NO_WINDOW = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW


def listeners(port, psutil):
    pids = set()
    try:
        for c in psutil.net_connections(kind="tcp"):
            if c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN and c.pid:
                pids.add(c.pid)
    except (psutil.Error, OSError):
        pass
    return pids


def stop_old(psutil, port=TUNNEL_PORT, wait=8.0):
    """Asks an old tunnel to stop, then forces it if it hangs."""
    old = listeners(port, psutil)
    for pid in old:
        try:
            psutil.Process(pid).terminate()
        except psutil.Error:
            pass
    deadline = time.time() + wait
    while old and listeners(port, psutil) and time.time() < deadline:
        time.sleep(0.5)
    for pid in listeners(port, psutil):
        try:
            psutil.Process(pid).kill()
        except psutil.Error:
            pass


def tunnel_command(python):
    return [python, "-m", "pymobiledevice3", "remote", "tunneld", "--protocol", "tcp"]


def python_for_children():
    """python.exe rather than pythonw.exe, so the tunnel's output goes to the log."""
    exe = sys.executable
    folder, name = os.path.split(exe)
    if name.lower() == "pythonw.exe":
        console = os.path.join(folder, "python.exe")
        if os.path.exists(console):
            return console
    return exe


def run(server_pid, log_path, psutil, poll=2.0):
    stop_old(psutil)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1")
    with open(log_path, "ab") as log:
        tunnel = subprocess.Popen(tunnel_command(python_for_children()), stdin=subprocess.DEVNULL,
                                  stdout=log, stderr=subprocess.STDOUT, env=env, creationflags=NO_WINDOW)
        try:
            while psutil.pid_exists(server_pid) and tunnel.poll() is None:
                time.sleep(poll)
        finally:
            if tunnel.poll() is None:
                tunnel.terminate()
                try:
                    tunnel.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    tunnel.kill()
    return tunnel.returncode


def main(argv):
    import psutil
    if len(argv) != 3:
        return 2
    run(int(argv[1]), argv[2], psutil)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
