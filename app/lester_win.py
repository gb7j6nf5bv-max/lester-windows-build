"""Lester's window on Windows: the Windows twin of lester_app.js plus start_backend.sh.

setup_win.ps1 runs this with pymobiledevice3's own Python once the iPhone tools are installed.
  1. Starts the map server (lester.py) in the background, with no console window.
  2. Starts the iOS 17+ tunnel through the Windows admin prompt (tunnel_win.py).
  3. Keeps the PC awake, so the iPhone keeps its location.
  4. Shows the map in its own window (Microsoft Edge's app mode: no tabs or address bar).
Closing the window quits Lester; the map's power button closes the window.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("IPHONE_GPS_PORT", "8765"))
URL = f"http://127.0.0.1:{PORT}/"
STATE_DIR = Path(os.environ.get("LESTER_HOME") or
                 (Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "Lester"))
LOG = STATE_DIR / "lester.log"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW


def log(msg):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def alert(msg, title="Lester couldn't start"):
    log(f"alert: {msg}")
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, title, 0x10)
    else:
        print(f"{title}: {msg}", file=sys.stderr)


def server_up(timeout=2):
    try:
        with urllib.request.urlopen(URL + "api/ping", timeout=timeout) as r:
            return json.load(r).get("ok") is True
    except Exception:
        return False


def quit_server():
    """Same as the map's power button: the iPhone goes back to its real location."""
    req = urllib.request.Request(URL + "api/quit", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5).close()
    except Exception:
        pass


def tools():
    """pymobiledevice3's Python (this one) and its command, side by side in Scripts."""
    folder = Path(sys.executable).parent
    python = folder / "python.exe" if (folder / "python.exe").exists() else Path(sys.executable)
    pythonw = folder / "pythonw.exe" if (folder / "pythonw.exe").exists() else Path(sys.executable)
    pmd3 = folder / ("pymobiledevice3.exe" if os.name == "nt" else "pymobiledevice3")
    return python, pythonw, pmd3


def start_server(python, pmd3):
    env = dict(os.environ, NO_BROWSER="1", PYTHONUNBUFFERED="1", LESTER_PMD3=str(pmd3),
               LESTER_PYTHON=str(python), LESTER_HOME=str(STATE_DIR))
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    out = open(LOG, "ab")
    proc = subprocess.Popen([str(python), str(HERE / "lester.py")], stdin=subprocess.DEVNULL, stdout=out,
                            stderr=subprocess.STDOUT, env=env, cwd=str(HERE), creationflags=NO_WINDOW)
    (STATE_DIR / "server.pid").write_text(str(proc.pid))
    return proc


def start_tunnel(pythonw, server_pid):
    """Asks Windows for admin rights (the "allow this app to make changes" prompt).
    Returns False if the person said no."""
    script = HERE / "tunnel_win.py"
    tunnel_log = STATE_DIR / "tunneld.log"
    params = f'"{script}" {server_pid} "{tunnel_log}"'
    if os.name != "nt":
        return True
    import ctypes
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", str(pythonw), params, str(HERE), 0)
    return result > 32


def wait_for_tunnel(seconds=12):
    for _ in range(seconds):
        try:
            urllib.request.urlopen("http://127.0.0.1:49151/", timeout=1).close()
            return True
        except Exception:
            time.sleep(1)
    return False


def keep_awake(on=True):
    """Like caffeinate on the Mac: no sleep while Lester runs (Windows undoes it when we exit)."""
    if os.name != "nt":
        return
    import ctypes
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x00000001, 0x00000002
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED if on else 0)
    ctypes.windll.kernel32.SetThreadExecutionState(flags)


def find_edge():
    candidates = []
    for var in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if base:
            candidates.append(Path(base) / "Microsoft/Edge/Application/msedge.exe")
    if os.name == "nt":
        try:
            import winreg
            key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        candidates.insert(0, Path(winreg.QueryValue(k, None)))
                except OSError:
                    pass
        except ImportError:
            pass
    return next((p for p in candidates if p.exists()), None)


def window_command(edge):
    """Edge's app mode, with Lester's own profile so its window is a process of its own."""
    profile = STATE_DIR / "window"
    return [str(edge), f"--app={URL}", f"--user-data-dir={profile}", "--no-first-run",
            "--no-default-browser-check", "--disable-features=Translate", "--window-size=1280,860"]


def open_window():
    edge = find_edge()
    if not edge:
        webbrowser.open(URL)
        return None
    return subprocess.Popen(window_command(edge), creationflags=NO_WINDOW)


def window_processes():
    """Edge processes showing Lester's window (they all carry Lester's own Edge profile).
    Edge can hand the window over to another of its processes, so the one we started
    isn't a reliable sign the window is open."""
    import psutil
    profile = f"--user-data-dir={STATE_DIR / 'window'}".lower()
    found = []
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            if (p.info["name"] or "").lower() == "msedge.exe" and any(a.lower() == profile for a in p.info["cmdline"] or []):
                found.append(p)
        except (psutil.Error, TypeError):
            pass
    return found


def close_window():
    for p in window_processes():
        try:
            p.terminate()
        except Exception:
            pass


def run_window(server):
    """Waits until either the window is closed (then quits Lester) or Lester quits
    from the map's power button (then closes the window)."""
    window = open_window()
    if window is None:   # no Edge: the map is in the normal browser; the power button quits
        server.wait()
        return
    seen, gone_since = False, None
    while True:
        time.sleep(0.5)
        if server.poll() is not None:
            close_window()
            return
        if window_processes():
            seen, gone_since = True, None
            continue
        if not seen and window.poll() is None:
            continue   # still starting
        gone_since = gone_since or time.time()
        if time.time() - gone_since >= 2:   # closed (not just handed over between processes)
            quit_server()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
            return


def main():
    if server_up():
        log("already running: showing the window")
        open_window()
        return 0
    python, pythonw, pmd3 = tools()
    if not pmd3.exists():
        alert("The iPhone tools aren't installed. Open Lester again to finish setting it up.")
        return 1
    log("=== Lester starting ===")
    server = start_server(python, pmd3)
    for _ in range(40):
        if server_up(1) or server.poll() is not None:
            break
        time.sleep(0.25)
    if server.poll() is not None:
        alert("Lester's map didn't start. Another app may be using port 8765. Restart the PC and try again.")
        return 1
    if not start_tunnel(pythonw, server.pid):
        quit_server()
        alert("Lester needs admin rights to connect to your iPhone. Open Lester again and click Yes.")
        return 1
    log("tunnel up (TCP)" if wait_for_tunnel() else "warning: tunnel not answering yet")
    keep_awake(True)
    try:
        run_window(server)
    finally:
        keep_awake(False)
        try:
            (STATE_DIR / "server.pid").unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
