"""Checks for Lester for Windows. Runs ON WINDOWS only (GitHub's Windows build machine or a PC),
with pymobiledevice3's own Python:  python tests/test_windows.py
No iPhone needed. It never changes a phone's location."""
import json
import os
import plistlib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
sys.path.insert(0, str(APP))
failures = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(name)


assert os.name == "nt", "This test is for Windows"

# 1. The pairing script: pymobiledevice3 still has what it uses, and the key format is right
import lester_pair
try:
    from pymobiledevice3.exceptions import RemotePairingCompletedError  # noqa: F401
    from pymobiledevice3.lockdown import create_using_usbmux  # noqa: F401
    from pymobiledevice3.remote.tunnel_service import RemotePairingLockdownService
    check("pairing: pymobiledevice3 parts exist", hasattr(RemotePairingLockdownService, "create")
          and hasattr(RemotePairingLockdownService, "save_pair_record"))
except Exception as e:
    check("pairing: pymobiledevice3 parts exist", False, repr(e))
hid = lester_pair.host_identifier("Lester")
check("pairing: host id in idevice's form (lowercase UUID)", len(hid) == 36 and hid == hid.lower())
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
k = Ed25519PrivateKey.generate()
rec = {"public_key": k.public_key().public_bytes_raw(), "private_key": k.private_bytes_raw(), "peer_alt_irk": os.urandom(16)}
out = plistlib.loads(lester_pair.pairing_file_bytes(rec, lester_pair.host_identifier("Lester")))
check("pairing: file has idevice's fields", set(out) == {"public_key", "private_key", "identifier", "alt_irk"})
tmp = Path(tempfile.mkdtemp()) / "key.plist"
lester_pair.write_private(str(tmp), b"x")
check("pairing: key file written", tmp.read_bytes() == b"x")

# 2. The tunnel starter's pieces
import psutil
import tunnel_win
srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(); port = srv.getsockname()[1]
check("tunnel: finds who listens on a port", os.getpid() in tunnel_win.listeners(port, psutil))
srv.close()
check("tunnel: command", tunnel_win.tunnel_command("py")[-4:] == ["remote", "tunneld", "--protocol", "tcp"])
r = subprocess.run([sys.executable, "-m", "pymobiledevice3", "remote", "tunneld", "--help"], capture_output=True, text=True, timeout=120)
check("tunnel: pymobiledevice3 tunneld exists", r.returncode == 0 and "--protocol" in r.stdout, r.stderr[-300:])

# 3. The window
import lester_win
edge = lester_win.find_edge()
check("window: Microsoft Edge found", edge is not None)
check("window: app mode", any(a.startswith("--app=http://127.0.0.1:") for a in lester_win.window_command(edge or "msedge.exe")))

# 4. The server in Windows mode (real pymobiledevice3, no phone attached)
state = Path(tempfile.mkdtemp())
pmd3 = Path(sys.executable).parent / "pymobiledevice3.exe"
check("server: pymobiledevice3.exe next to python", pmd3.exists())
env = dict(os.environ, LESTER_HOME=str(state), LESTER_PMD3=str(pmd3), LESTER_PYTHON=sys.executable,
           NO_BROWSER="1", IPHONE_GPS_PORT="8795", LESTER_USBMUX_PORT="1")
server = subprocess.Popen([sys.executable, str(APP / "lester.py") if (APP / "lester.py").exists() else str(ROOT / "lester.py")],
                          env=env, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
base = "http://127.0.0.1:8795/"


def get(path):
    with urllib.request.urlopen(base + path, timeout=60) as r:
        return r.read()


def post(path, body=b"{}"):
    req = urllib.request.Request(base + path, data=body, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


for _ in range(60):
    try:
        get("api/ping"); break
    except Exception:
        time.sleep(0.5)
try:
    page = get("").decode("utf-8")
    check("server: page says PC, not Mac", "this PC" in page and "this Mac" not in page)
    check("server: page knows it's Windows", 'window.LESTER_OS = "windows"' in page)
    # The guide holds both sets of instructions and shows Windows users the PowerShell ones
    check("server: PowerShell instructions", "Open <b>PowerShell</b>" in page and "IS_WIN ?" in page
          and "window.LESTER_OS === 'windows'" in page)
    st = json.loads(get("api/status"))
    check("server: status works with no iPhone", st.get("devices") == [] or "error" in st, str(st)[:200])
    check("server: notices Apple Devices is missing", st.get("appleDevicesMissing") is True, str(st)[:200])
    phones = json.loads(get("api/usb-phones"))
    check("server: pairing available on Windows", phones.get("pairing_available") is True)
    setup = json.loads(get("api/iphone-setup?udid=00000000-0000000000000000"))
    check("server: setup guide answers", setup.get("os") == "windows" and setup.get("connected") is False)
    check("server: Developer Mode command for PowerShell", setup.get("revealCommand", "").startswith('& "'))
    post("api/quit")
    server.wait(timeout=20)
    check("server: quits cleanly", server.returncode == 0)
except Exception as e:
    check("server: runs", False, repr(e))
finally:
    if server.poll() is None:
        server.kill()
    print(server.stdout.read().decode(errors="replace")[-1500:])

# 5. The setup script parses, and the programs start
r = subprocess.run(["powershell", "-NoProfile", "-Command",
                    "$e=$null; [System.Management.Automation.Language.Parser]::ParseFile('%s',[ref]$null,[ref]$e) | Out-Null; $e.Count"
                    % (APP / "setup_win.ps1")], capture_output=True, text=True, timeout=60)
check("setup script: no PowerShell syntax errors", r.stdout.strip() == "0", r.stdout + r.stderr)
helper = next((p for p in (APP / "lester-sideload.exe", ROOT / "build/windows-bin/lester-sideload.exe") if p.exists()), None)
if helper:
    r = subprocess.run([str(helper)], input="not json\n", capture_output=True, text=True, timeout=60)
    events = [json.loads(x) for x in r.stdout.splitlines() if x.startswith("{")]
    check("install helper: starts and reports errors as JSON", any(e.get("event") == "error" for e in events), r.stdout[-300:])

print(f"\n{'All Windows checks passed' if not failures else str(len(failures)) + ' failed: ' + ', '.join(failures)}")
sys.exit(1 if failures else 0)
