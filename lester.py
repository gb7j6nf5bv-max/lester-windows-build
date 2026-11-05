#!/usr/bin/env python3
"""Lester - a local map UI that sets your iPhone's location over USB or Wi-Fi.

Uses pymobiledevice3 (Apple's developer "simulate location" service).
Started by Lester.app on a Mac, or Lester.exe on Windows (see app/lester_win.py), which also
start the iOS 17+ tunnel. Only uses the Python standard library; pymobiledevice3 is called as a CLI.
"""

import bisect
import hmac
import json
import math
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

IS_WINDOWS = os.name == "nt" or os.environ.get("LESTER_OS") == "windows"   # LESTER_OS: for tests
COMPUTER = "PC" if IS_WINDOWS else "Mac"   # how messages name this computer
EXE = ".exe" if IS_WINDOWS else ""
PORT = int(os.environ.get("IPHONE_GPS_PORT", "8765"))
REMOTE_PORT = PORT + 1   # phones on the same Wi-Fi connect here, only when turned on


def _default_state_dir():
    if IS_WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData/Local")) / "Lester"
    return Path.home() / "Library/Lester"


STATE_DIR = Path(os.environ.get("LESTER_HOME") or _default_state_dir())
HERE = Path(__file__).resolve().parent

if os.name == "nt":
    # Lester runs without a console window, so every iPhone-tools command would otherwise
    # flash one up. subprocess.run() goes through Popen, so this covers both.
    class _NoWindowPopen(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("creationflags", 0x08000000)   # CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)
    subprocess.Popen = _NoWindowPopen
PAGE = HERE / "index.html"
ICON = next((p for p in (HERE / "icon.png", HERE / "assets/lester-icon.png") if p.exists()), HERE / "icon.png")


def find_pmd3():
    env = os.environ.get("LESTER_PMD3")
    if env and Path(env).exists():
        return env
    found = shutil.which("pymobiledevice3")
    if found:
        return found
    for p in (Path.home() / ".local/bin/pymobiledevice3", Path("/opt/homebrew/bin/pymobiledevice3")):
        if p.exists():
            return str(p)
    return None


PMD3 = find_pmd3()

# The "set" command keeps a session open; the location holds while it runs.
_sim_proc = None
_sim_lock = threading.Lock()
_current = None


TIMED_OUT = 124


def run(args, timeout=20):
    """Runs an iPhone-tools command. A phone that doesn't answer in time counts as a failure
    (exit code TIMED_OUT) instead of an error that would break the request."""
    try:
        r = subprocess.run([PMD3, *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return TIMED_OUT, "", "timed out: the iPhone didn't answer in time"
    return r.returncode, r.stdout, r.stderr


TUNNELD = "http://127.0.0.1:49151/"
KNOWN_FILE = STATE_DIR / "devices.json"


def _load_known():
    try:
        return json.loads(KNOWN_FILE.read_text())
    except (OSError, ValueError):
        return {}


_known = _load_known()   # udid -> {"name", "ios"} from when the phone was last seen over the cable


def _remember(udid, name, ios):
    if _known.get(udid) != {"name": name, "ios": ios}:
        _known[udid] = {"name": name, "ios": ios}
        try:
            KNOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
            KNOWN_FILE.write_text(json.dumps(_known))
        except OSError:
            pass


def tunneld_devices():
    """Phones the iOS 17+ tunnel can reach right now: {udid: "USB" | "Network"}."""
    try:
        with urllib.request.urlopen(TUNNELD, timeout=2) as r:
            data = json.load(r)
    except (OSError, ValueError):
        return {}
    out = {}
    for udid, tunnels in (data or {}).items():
        if not tunnels:
            continue
        # Cable tunnels are named "usbmux-<udid>-USB" (or start with "usb"); Wi-Fi ones are
        # named after the phone's network address, "wifi-…", "mobdev2-…" or "…-Network".
        ifaces = [str(t.get("interface", "")).lower() for t in tunnels]
        cable = any(i.startswith("usb") and "network" not in i for i in ifaces)
        out[udid] = "USB" if cable else "Network"
    return out


def list_devices():
    """Phones Lester can talk to. macOS's own list (usbmux) can miss a phone on Wi-Fi,
    so phones the iOS 17+ tunnel reaches are added too."""
    devices, err = [], None
    try:
        code, out, errtxt = run(["usbmux", "list"])
        if code != 0:
            raise RuntimeError(errtxt.strip() or "usbmux list failed")
        devices = json.loads(out or "[]")
    except Exception as e:
        err = e
    # Prefer USB over Wi-Fi entries for the same phone
    seen, result = set(), []
    for d in sorted(devices, key=lambda d: d.get("ConnectionType") != "USB"):
        udid = d.get("UniqueDeviceID") or d.get("Identifier")
        if udid in seen:
            continue
        seen.add(udid)
        name, ios = d.get("DeviceName", "iPhone"), d.get("ProductVersion", "?")
        if ios != "?":
            _remember(udid, name, ios)
        result.append({"udid": udid, "name": name, "ios": ios, "connection": d.get("ConnectionType", "?")})
    for udid, conn in tunneld_devices().items():
        if udid in seen:
            continue
        info = _known.get(udid, {})
        result.append({"udid": udid, "name": info.get("name", "iPhone"),
                       "ios": info.get("ios", "17"), "connection": conn})
    if not result and err:
        raise err
    return result


def ios_major(version):
    try:
        return int(str(version).split(".")[0])
    except ValueError:
        return 0


def tunnel_args(device):
    # iOS 17+ needs the tunnel daemon (started by the launcher with admin rights)
    return ["--tunnel", device["udid"]] if ios_major(device["ios"]) >= 17 else ["--udid", device["udid"]]


def stop_sim():
    global _sim_proc
    if _sim_proc and _sim_proc.poll() is None:
        _sim_proc.terminate()
        try:
            _sim_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _sim_proc.kill()
    _sim_proc = None


_link = "idle"  # idle | holding | reconnecting
_trip = None    # current or last trip, see start_travel()


def _start_proc(dev, action, *args):
    """Start a long-running simulate-location session ('set' or 'play').
    Must hold _sim_lock. Returns the launch time. Raises on failure."""
    global _sim_proc
    stop_sim()
    cmd = [PMD3, "developer", "dvt", "simulate-location", action, *tunnel_args(dev), *args]
    # stdout goes to /dev/null and stderr to a temp file: a pipe nobody reads can fill up
    # and freeze the session
    errf = tempfile.TemporaryFile(mode="w+")
    launched = time.time()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errf, text=True)
    # iOS 17+ drops the fake location when this session ends, so it must keep running.
    # Still running after a few seconds without an error counts as success.
    deadline = time.time() + 5
    while time.time() < deadline and proc.poll() is None:
        time.sleep(0.25)
    if proc.poll() is not None:
        errf.seek(0)
        err = errf.read()
        errf.close()
        raise RuntimeError(explain_error(err, dev) if proc.returncode else "Session closed right away")
    _sim_proc = proc
    return launched


def _start_sim(dev, lat, lon):
    return _start_proc(dev, "set", "--", str(lat), str(lon))


# ---------- Travel along a route ----------
EARTH_R = 6371008.8
MAX_DENSE = 20000      # cap on playback points for very long trips
CONNECT_DELAY = 1.5    # rough time for a new session to reach the phone


def haversine(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(h)))


def densify(points, speed):
    """Fill in a route so playback moves smoothly: about one point per second of travel.
    Returns ([(lat, lon, seconds_from_start)], meters, seconds)."""
    seg = [haversine(points[i], points[i + 1]) for i in range(len(points) - 1)]
    total = sum(seg)
    duration = total / speed
    step = max(1.0, duration / MAX_DENSE)
    out = [(points[0][0], points[0][1], 0.0)]
    t = 0.0
    for i, d in enumerate(seg):
        if d == 0:
            continue
        a, b = points[i], points[i + 1]
        n = max(1, math.ceil(d / speed / step))
        for k in range(1, n + 1):
            f = k / n
            out.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, t + d / speed * f))
        t += d / speed
    return out, total, duration


def trip_elapsed(trip):
    if trip.get("stopped_at") is not None:
        return trip["stopped_at"]
    return min(max(time.time() - trip["started"], 0.0), trip["duration"])


def trip_position(trip, el=None):
    el = trip_elapsed(trip) if el is None else el
    dense, times = trip["dense"], trip["times"]
    if el <= 0:
        return dense[0][:2]
    if el >= trip["duration"]:
        return dense[-1][:2]
    i = bisect.bisect_right(times, el)
    a, b = dense[i - 1], dense[i]
    f = (el - a[2]) / (b[2] - a[2]) if b[2] > a[2] else 1.0
    return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)


def _play_from(dev, trip, el):
    """Play the trip from `el` seconds in. Must hold _sim_lock. Updates trip['started']."""
    lat, lon = trip_position(trip, el)
    pts = [(lat, lon, el)] + [p for p in trip["dense"] if p[2] > el]
    base = datetime.now(timezone.utc)
    fd, path = tempfile.mkstemp(suffix=".gpx", prefix="lester-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<gpx version="1.1" creator="Lester" xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>\n')
            for plat, plon, t in pts:
                ts = (base + timedelta(seconds=t - el)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                f.write(f'<trkpt lat="{plat:.7f}" lon="{plon:.7f}"><time>{ts}</time></trkpt>\n')
            f.write("</trkseg></trk></gpx>\n")
        launched = _start_proc(dev, "play", path)
    finally:
        # play reads the whole file up front, so it can go once the session is running
        try:
            os.unlink(path)
        except OSError:
            pass
    trip["started"] = launched + CONNECT_DELAY - el


def start_travel(points, speed):
    global _current, _link, _trip
    dense, meters, seconds = densify(points, speed)
    if seconds < 2:
        raise ValueError("That trip is too short. Pick a destination further away.")
    devices = list_devices()
    if not devices:
        raise RuntimeError("No iPhone found. Plug it in with a cable, unlock it, and tap Trust.")
    dev = devices[0]
    prepare(dev)
    trip = {"id": int(time.time() * 1000), "route": points, "dense": dense, "times": [p[2] for p in dense],
            "distance": meters, "duration": seconds, "speed": speed, "started": time.time(), "stopped_at": None}
    with _sim_lock:
        _play_from(dev, trip, 0.0)
        _trip = trip
        _current = {"lat": dense[-1][0], "lon": dense[-1][1]}
        _link = "holding"
    return dev, trip


def stop_travel():
    """Stop moving and stay where the phone is now."""
    global _current
    trip = _trip
    if not trip or trip["stopped_at"] is not None or trip_elapsed(trip) >= trip["duration"]:
        return
    devices = list_devices()
    with _sim_lock:
        el = trip_elapsed(trip)
        lat, lon = trip_position(trip, el)
        trip["stopped_at"] = el
        _current = {"lat": lat, "lon": lon}
        if devices:
            _start_sim(devices[0], lat, lon)
        else:
            stop_sim()


def trip_summary(trip, full=False):
    if not trip:
        return None
    el = trip_elapsed(trip)
    lat, lon = trip_position(trip, el)
    s = {"id": trip["id"], "elapsed": round(el, 2), "duration": round(trip["duration"], 2),
         "distance": round(trip["distance"]), "speed": trip["speed"],
         "done": el >= trip["duration"], "paused": bool(trip.get("paused")),
         "stopped": trip["stopped_at"] is not None and not trip.get("paused"),
         "position": {"lat": lat, "lon": lon}}
    if full:
        s["route"] = trip["route"]
    return s


def display_location():
    """Where the phone is right now (moves during a trip)."""
    trip = _trip
    if _current and trip and trip["stopped_at"] is None and trip_elapsed(trip) < trip["duration"]:
        lat, lon = trip_position(trip)
        return {"lat": lat, "lon": lon}
    return _current


_prepared = set()


def prepare(dev):
    """Once per phone: mount Apple's developer image and allow Wi-Fi connections."""
    if dev["udid"] in _prepared:
        return
    for args, timeout in ((["mounter", "auto-mount"], 180), (["lockdown", "wifi-connections", "on"], 20)):
        try:
            run([*args, *tunnel_args(dev)], timeout=timeout)
        except subprocess.TimeoutExpired:
            pass
    _prepared.add(dev["udid"])


def set_location(lat, lon):
    global _current, _link, _trip
    devices = list_devices()
    if not devices:
        raise RuntimeError("No iPhone found. Plug it in with a cable, unlock it, and tap Trust.")
    dev = devices[0]
    prepare(dev)
    with _sim_lock:
        _start_sim(dev, lat, lon)
        _current = {"lat": lat, "lon": lon}
        _trip = None
        _link = "holding"
    return dev


def watchdog():
    """When the cable is pulled the session dies; pick up again over Wi-Fi.
    A trip resumes from where it was; otherwise the location is re-applied."""
    global _link
    fails = 0
    while True:
        time.sleep(min(2 + fails * 2, 15))
        with _sim_lock:
            cur, trip = _current, _trip
            alive = _sim_proc is not None and _sim_proc.poll() is None
            if trip and trip["stopped_at"] is None and not alive:
                # freeze the trip clock while disconnected
                trip["stopped_at"] = trip_elapsed(trip)
                trip["paused"] = True
        if not cur or alive:
            continue
        _link = "reconnecting"
        try:
            devices = list_devices()
            if devices:
                with _sim_lock:
                    if _current == cur and _trip is trip:  # user didn't change anything meanwhile
                        if trip and trip.get("paused") and trip["stopped_at"] < trip["duration"]:
                            el = trip["stopped_at"]
                            trip["stopped_at"] = None
                            trip.pop("paused", None)
                            try:
                                _play_from(devices[0], trip, el)
                            except Exception:
                                trip["stopped_at"], trip["paused"] = el, True
                                raise
                        else:
                            if trip:
                                trip.pop("paused", None)
                            here = display_location() or cur
                            _start_sim(devices[0], here["lat"], here["lon"])
                        _link = "holding"
                        fails = 0
                        print(f"Picked up again over {devices[0]['connection']}")
            else:
                fails += 1
        except Exception as e:
            fails += 1
            print(f"Reconnect failed, retrying: {e}")


def clear_location():
    global _current, _link, _trip
    devices = list_devices()
    with _sim_lock:
        _current = None
        _trip = None
        _link = "idle"
        stop_sim()
        if devices:
            dev = devices[0]
            code, out, err = run(["developer", "dvt", "simulate-location", "clear", *tunnel_args(dev)], timeout=30)
            if code != 0:
                raise RuntimeError(explain_error(out + err, dev))
        _current = None


def explain_error(text, dev):
    t = text.lower()
    tail = text.strip().splitlines()[-1] if text.strip() else "unknown error"
    if "developer mode" in t or "developermode" in t:
        return "Developer Mode is off. On the iPhone: Settings > Privacy & Security > Developer Mode > On, then restart it."
    if "tunneld" in t or ("tunnel" in t and ios_major(dev["ios"]) >= 17):
        if IS_WINDOWS:
            return "The iOS 17+ tunnel isn't running. Quit Lester and open it again, then click Yes when Windows asks."
        return "The iOS 17+ tunnel isn't running. Quit Lester and open it again, then enter your Mac password."
    if "image" in t and ("mount" in t or "dvt" in t):
        return "Developer disk image isn't mounted. Unplug/replug the iPhone and restart the app (it mounts automatically)."
    if "pair" in t or "trust" in t:
        return f"The iPhone doesn't trust this {COMPUTER} yet. Unlock it and tap Trust."
    return f"pymobiledevice3 error: {tail}"


ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ALLOWED_ORIGINS = {f"http://{h}" for h in ALLOWED_HOSTS}
MAX_BODY = 2_000_000  # routes can have thousands of points


# ---------- Joystick: fast moves over one open session ----------
# `simulate-location set` takes seconds to connect, too slow for steering. lester_stream.py runs
# with pymobiledevice3's own Python, keeps a session open and applies each move in ~30 ms.
STREAM_HELPER = next((p for p in (HERE / "lester_stream.py", HERE / "app/lester_stream.py") if p.exists()), None)


def _pmd3_python():
    """The Python that pymobiledevice3 is installed in (it sits next to the command)."""
    env = os.environ.get("LESTER_PYTHON")
    if env:
        return env
    try:
        return str(Path(os.path.realpath(PMD3)).parent / ("python.exe" if IS_WINDOWS else "python"))
    except (TypeError, OSError):
        return None


def _readline(proc, timeout):
    """One line from the helper, or None if it takes too long."""
    box = []
    t = threading.Thread(target=lambda: box.append(proc.stdout.readline()), daemon=True)
    t.start()
    t.join(timeout)
    return box[0].strip() if box else None


def _start_stream(dev):
    """Must hold _sim_lock."""
    global _sim_proc
    stop_sim()
    py = _pmd3_python()
    if not (STREAM_HELPER and py and os.path.exists(py)):
        raise RuntimeError("The joystick helper is missing from this copy of Lester.")
    proc = subprocess.Popen([py, "-I", str(STREAM_HELPER), dev["udid"]], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
    line = _readline(proc, 20)
    if line != "READY":
        proc.kill()
        raise RuntimeError("Couldn't reach the iPhone for the joystick. Check it's connected, then try again.")
    proc.is_stream = True
    _sim_proc = proc


def move_location(lat, lon):
    """A joystick step. Returns False if a previous move is still being applied."""
    global _current, _link, _trip
    if not _sim_lock.acquire(timeout=0.3):
        return False
    try:
        proc = _sim_proc
        if not (proc and getattr(proc, "is_stream", False) and proc.poll() is None):
            devices = list_devices()
            if not devices:
                raise RuntimeError("No iPhone found. Plug it in with a cable, unlock it, and tap Trust.")
            dev = devices[0]
            if ios_major(dev["ios"]) < 17:
                raise RuntimeError("The joystick needs iOS 17 or newer.")
            prepare(dev)
            _start_stream(dev)
            proc = _sim_proc
        proc.stdin.write(f"set {lat} {lon}\n")
        proc.stdin.flush()
        line = _readline(proc, 8)
        if line != "OK":
            stop_sim()
            raise RuntimeError("The iPhone stopped answering. Try again.")
        _trip = None
        _current = {"lat": lat, "lon": lon}
        _link = "holding"
        return True
    finally:
        _sim_lock.release()


# ---------- Itinerary: timed moves, run by the Mac ----------
# e.g. 4:00 PM walk to B, 5:00 PM walk back to A, optionally every day. Steps run up to 15 minutes
# late (if the Mac was asleep), once per day each. Road routes come from OpenStreetMap.
ITIN_FILE = STATE_DIR / "itinerary.json"
ACTIONS = {"teleport": None, "walk": ("foot", 5), "bike": ("bike", 16), "drive": ("car", 50)}
_itin = {"enabled": False, "repeatDaily": True, "steps": []}
_itin_lock = threading.Lock()
_itin_note = None


def _day_key(t=None):
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _load_itinerary():
    global _itin
    try:
        _itin = validate_itinerary(json.loads(ITIN_FILE.read_text()))
    except (OSError, ValueError):
        pass


def _save_itinerary():
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        ITIN_FILE.write_text(json.dumps(_itin))
    except OSError as e:
        print(f"Couldn't save the itinerary: {e}")


def validate_itinerary(data):
    if not isinstance(data, dict):
        raise ValueError("Bad itinerary")
    steps = data.get("steps") or []
    if not isinstance(steps, list) or len(steps) > 50:
        raise ValueError("An itinerary can have up to 50 steps")
    clean = []
    for st in steps:
        try:
            hour, minute = int(st["hour"]), int(st["minute"])
            lat, lon = float(st["lat"]), float(st["lon"])
            action = str(st["action"])
            name = str(st.get("name") or "Pin")[:100]
            sid = str(st.get("id") or secrets.token_hex(6))[:40]
        except (KeyError, TypeError, ValueError):
            raise ValueError("A step is missing its time or place")
        if not (0 <= hour <= 23 and 0 <= minute <= 59 and action in ACTIONS and
                math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("A step has a bad time, action or place")
        last = st.get("lastRunDay")
        clean.append({"id": sid, "hour": hour, "minute": minute, "action": action, "name": name,
                      "lat": lat, "lon": lon, "lastRunDay": last if isinstance(last, str) else None})
    clean.sort(key=lambda x: (x["hour"], x["minute"]))
    return {"enabled": bool(data.get("enabled")) and bool(clean), "repeatDaily": bool(data.get("repeatDaily", True)),
            "steps": clean}


def set_itinerary(data):
    global _itin
    new = validate_itinerary(data)
    with _itin_lock:
        # A step added for a time already past today waits until tomorrow
        old_ids = {st["id"] for st in _itin["steps"]}
        now = time.localtime()
        for st in new["steps"]:
            if st["id"] not in old_ids and (st["hour"], st["minute"]) < (now.tm_hour, now.tm_min):
                st["lastRunDay"] = _day_key()
        _itin = new
        _save_itinerary()
    return itinerary_info()


def _step_label(st):
    t = time.strftime("%I:%M %p", (2000, 1, 1, st["hour"], st["minute"], 0, 0, 1, -1)).lstrip("0")
    what = "Teleport to" if st["action"] == "teleport" else f"{st['action'].capitalize()} to"
    return f"{t}: {what} {st['name']}"


def itinerary_info():
    today, now = _day_key(), time.localtime()
    upcoming = [st for st in _itin["steps"] if st.get("lastRunDay") != today and
                (st["hour"], st["minute"]) >= (now.tm_hour, now.tm_min - 15)]
    nxt = upcoming[0] if upcoming else (_itin["steps"][0] if _itin["repeatDaily"] and _itin["steps"] else None)
    return {**_itin, "next": _step_label(nxt) if (nxt and _itin["enabled"]) else None, "note": _itin_note,
            "labels": {st["id"]: _step_label(st) for st in _itin["steps"]}}


def road_route(start, end, profile):
    """Points along roads from OpenStreetMap's router, or a straight line if that fails."""
    url = (f"https://routing.openstreetmap.de/routed-{profile}/route/v1/driving/"
           f"{start[1]},{start[0]};{end[1]},{end[0]}?overview=full&geometries=geojson")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Lester (location app)"})
        with urllib.request.urlopen(req, timeout=20) as r:
            j = json.load(r)
        pts = [(c[1], c[0]) for c in j["routes"][0]["geometry"]["coordinates"]]
        return [tuple(start)] + pts + [tuple(end)]
    except Exception:
        return [tuple(start), tuple(end)]


def _run_step(st):
    global _itin_note
    dest = (st["lat"], st["lon"])
    try:
        here = display_location()
        if st["action"] == "teleport" or not here:
            set_location(*dest)
        else:
            profile, kmh = ACTIONS[st["action"]]
            start_travel(road_route((here["lat"], here["lon"]), dest, profile), kmh / 3.6)
        _itin_note = f"Ran {_step_label(st)}"
    except Exception as e:
        _itin_note = f"Couldn't run {_step_label(st)}: {e}"
    print(_itin_note)


def itinerary_loop():
    while True:
        time.sleep(15)
        due = None
        with _itin_lock:
            if not _itin["enabled"]:
                continue
            today, now = _day_key(), time.time()
            for st in _itin["steps"]:
                if st.get("lastRunDay") == today:
                    continue
                lt = time.localtime(now)
                due_t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, st["hour"], st["minute"], 0, 0, 0, -1))
                if 0 <= now - due_t < 15 * 60:
                    st["lastRunDay"] = today
                    due = dict(st)
                    break
            if due:
                if not _itin["repeatDaily"] and all(s.get("lastRunDay") == today for s in _itin["steps"]):
                    _itin["enabled"] = False
                _save_itinerary()
        if due:
            _run_step(due)


# ---------- Control from a phone on the same Wi-Fi ----------
# Off by default. When on, a second listener on all network interfaces (REMOTE_PORT) serves
# the same page and API, but every request must carry the secret access key (from the QR
# code). Phones can't quit Lester or change these settings; only the Mac can.
REMOTE_FILE = STATE_DIR / "remote.json"
_remote = {"enabled": False, "key": None}
_remote_server = None
_remote_lock = threading.Lock()


def _load_remote():
    try:
        data = json.loads(REMOTE_FILE.read_text())
        _remote["enabled"] = bool(data.get("enabled"))
        key = data.get("key")
        _remote["key"] = key if isinstance(key, str) and len(key) >= 20 else None
    except (OSError, ValueError):
        pass


def _save_remote():
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(REMOTE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(_remote, f)
    except OSError as e:
        print(f"Couldn't save phone settings: {e}")


def lan_ip():
    """This Mac's address on the local network (no traffic is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def remote_info():
    ip = lan_ip()
    url = f"http://{ip}:{REMOTE_PORT}/?k={_remote['key']}" if ip and _remote["key"] else None
    return {"enabled": _remote["enabled"], "running": _remote_server is not None,
            "url": url if _remote["enabled"] else None, "ip": ip, "port": REMOTE_PORT}


def _start_remote_server():
    global _remote_server
    if _remote_server:
        return
    srv = ThreadingHTTPServer(("0.0.0.0", REMOTE_PORT), Handler)
    srv.is_remote = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _remote_server = srv
    print(f"Phone access on port {REMOTE_PORT}")


def _stop_remote_server():
    global _remote_server
    srv, _remote_server = _remote_server, None
    if srv:
        threading.Thread(target=lambda: (srv.shutdown(), srv.server_close()), daemon=True).start()


def set_remote(enabled):
    with _remote_lock:
        if enabled and not _remote["key"]:
            _remote["key"] = secrets.token_urlsafe(24)
        _remote["enabled"] = bool(enabled)
        _save_remote()
        if enabled:
            try:
                _start_remote_server()
            except OSError:
                _remote["enabled"] = False
                _save_remote()
                raise RuntimeError(f"Port {REMOTE_PORT} is in use by another app, so phones can't connect.")
        else:
            _stop_remote_server()
    return remote_info()


def reset_remote_key():
    """New access key: phones set up with the old QR code stop working."""
    with _remote_lock:
        _remote["key"] = secrets.token_urlsafe(24)
        _save_remote()
    return remote_info()


# ---------- Pair Lester for iPhone ----------
# Lester for iPhone talks to its own phone through LocalDevVPN using RemotePairing. Recent iOS
# only allows a *new* pairing over the USB cable, so the Mac makes the key (lester-pair, built on
# the idevice library) and puts it straight into the iPhone app's private storage. The Mac's copy
# is deleted right away: the key works like a password for the phone.
# On Windows the same key is made by lester_pair.py, with pymobiledevice3's own Python.
if IS_WINDOWS:
    PAIR_HELPER = next((p for p in (HERE / "lester_pair.py", HERE / "app/lester_pair.py") if p.exists()), None)
else:
    PAIR_HELPER = next((p for p in (HERE / "lester-pair", HERE / "build/lester-pair") if p.exists()), None)


def _pair_command(udid, key_path):
    if IS_WINDOWS:
        return [_pmd3_python() or "python", "-I", str(PAIR_HELPER), udid, PAIR_HOSTNAME, key_path]
    return [str(PAIR_HELPER), udid, PAIR_HOSTNAME, key_path]
IOS_APP_ID = "app.lester.phone"
PAIR_HOSTNAME = "Lester"   # must match LocationEngine.hostName in the iPhone app
_pair_lock = threading.Lock()


def usb_phones():
    return [d for d in _devices_or_none() if d["connection"] == "USB"]


def dev_args(dev):
    """Cable: talk over usbmux directly. Wi-Fi (iOS 17+): through Lester's tunnel."""
    return ["--udid", dev["udid"]] if dev["connection"] == "USB" else tunnel_args(dev)


def list_user_apps(dev):
    """The phone's own (non-Apple) apps, by app ID."""
    code, out, err = run(["apps", "list", "--type", "User", *dev_args(dev)], timeout=90)
    if code != 0:
        raise RuntimeError("Couldn't list the apps on the iPhone. Unlock it and try again.")
    return json.loads(out or "{}")


def find_ios_app(dev, apps=None):
    """Lester for iPhone's app ID on this phone (sideloading apps sometimes change it)."""
    apps = apps if apps is not None else list_user_apps(dev)
    for app_id, info in apps.items():
        if app_id == IOS_APP_ID or app_id.startswith(IOS_APP_ID + "."):
            return app_id
    for app_id, info in apps.items():
        if info.get("CFBundleExecutable") == "Lester" and info.get("CFBundleDisplayName", info.get("CFBundleName")) == "Lester":
            return app_id
    return None


def pair_phone(udid):
    if not PAIR_HELPER:
        raise RuntimeError("The pairing tool is missing from this copy of Lester.")
    if not _pair_lock.acquire(blocking=False):
        raise RuntimeError("Already pairing a phone. Wait for it to finish.")
    try:
        dev = next((d for d in usb_phones() if d["udid"] == udid), None)
        if not dev:
            raise RuntimeError(f"Plug the iPhone into this {COMPUTER} with a cable, unlock it, and try again.")
        if ios_major(dev["ios"]) < 17:
            raise RuntimeError("Lester for iPhone needs iOS 17.4 or newer.")
        app_id = find_ios_app(dev)
        if not app_id:
            raise RuntimeError("Install Lester for iPhone on this phone first, then pair it.")
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        fd, key_path = tempfile.mkstemp(prefix="pair-", suffix=".plist", dir=STATE_DIR)
        os.close(fd)
        try:
            try:
                r = subprocess.run(_pair_command(udid, key_path), capture_output=True, text=True, timeout=120)
            except subprocess.TimeoutExpired:
                raise RuntimeError("The iPhone didn't answer in time. Unlock it, keep it plugged in, and try again.")
            except OSError:
                if IS_WINDOWS:
                    raise RuntimeError("The pairing tool couldn't start. Quit Lester and open it again.")
                raise RuntimeError("The pairing tool needs macOS 26.5 or newer.")
            result = (r.stdout or r.stderr).strip().splitlines()[-1:] or ["ERROR unknown"]
            if r.returncode != 0 or not result[0].startswith("OK"):
                detail = result[0].removeprefix("ERROR ").strip()
                if not IS_WINDOWS and ("dyld" in (r.stderr or "") or r.returncode < 0):
                    raise RuntimeError("The pairing tool needs macOS 26.5 or newer.")
                if "PairingRejected" in detail or "authenticate" in detail:
                    raise RuntimeError("The iPhone wasn't approved in time. Click Pair again, then on the iPhone "
                                       "tap Trust and enter your passcode when it asks.")
                raise RuntimeError(f"The iPhone didn't accept the pairing. Unlock it and try again. ({detail})")
            code, out, err = run(["apps", "push", *tunnel_args(dev), app_id, key_path,
                                  "Documents/remote-pairing.plist"], timeout=60)
            if code != 0:
                raise RuntimeError("Paired, but couldn't put the key into Lester for iPhone. "
                                   "Open the app once, then try again.")
        finally:
            try:
                os.unlink(key_path)
            except OSError:
                pass
        _mark_paired(udid)
        return {"ok": True, "device": dev["name"], "app": app_id}
    finally:
        _pair_lock.release()


# ---------- iPhone setup guide ----------
# Live checks for each step of setting up Lester for iPhone, for the Mac's guide.
IPA_FILE = next((p for p in (HERE / "Lester-iPhone.ipa", HERE / "build/Lester-iPhone.ipa") if p.exists()), None)
LOCALDEVVPN_URL = "https://apps.apple.com/us/app/localdevvpn/id6755608044"
PAIRED_FILE = STATE_DIR / "paired.json"


def _paired_set():
    try:
        return set(json.loads(PAIRED_FILE.read_text()))
    except (OSError, ValueError, TypeError):
        return set()


def _mark_paired(udid):
    try:
        PAIRED_FILE.write_text(json.dumps(sorted(_paired_set() | {udid})))
    except OSError:
        pass


def _ios_ok(version):
    try:
        parts = [int(x) for x in str(version).split(".")[:2]] + [0]
        return (parts[0], parts[1]) >= (17, 4)
    except ValueError:
        return False


def _devices_or_none():
    """The phone list, or none at all if the computer can't look for iPhones right now
    (on Windows: Apple's iPhone driver isn't installed)."""
    try:
        return list_devices()
    except Exception:
        return []


def setup_status(udid):
    dev = next((d for d in _devices_or_none() if d["udid"] == udid), None)
    st = {"connected": dev is not None, "usb": bool(dev and dev["connection"] == "USB"),
          "name": dev["name"] if dev else None, "ios": dev["ios"] if dev else None,
          "iosOk": _ios_ok(dev["ios"]) if dev else None, "trusted": False, "devmode": None, "app": None,
          "vpn": False, "paired": udid in _paired_set(), "download": IPA_FILE is not None, "vpnUrl": LOCALDEVVPN_URL,
          "devmodeJob": _devmode_jobs.get(udid), "revealCommand": _reveal_command(),
          "canInstall": SIDELOAD_HELPER is not None and IPA_FILE is not None,
          "installJob": _install_jobs.get(udid), "appleId": _remembered_apple_id(), "os": "windows" if IS_WINDOWS else "mac"}
    if not dev:
        return st
    code, out, err = run(["lockdown", "info", *dev_args(dev)], timeout=30)
    st["trusted"] = code == 0
    st["unreachable"] = code == TIMED_OUT
    if not st["trusted"]:
        return st
    code, out, err = run(["amfi", "developer-mode-status", *dev_args(dev)], timeout=30)
    st["devmode"] = "true" in (out or "").lower() if code == 0 else None
    try:
        apps = list_user_apps(dev)
        st["app"] = find_ios_app(dev, apps)
        st["vpn"] = any("localdevvpn" in (str(i.get("CFBundleDisplayName", "")) + str(i.get("CFBundleName", ""))).lower()
                        for i in apps.values())
    except RuntimeError:
        pass
    return st


# Turning Developer Mode on from the Mac. Apple only allows this when the iPhone has no passcode;
# with a passcode the switch must be flipped on the iPhone (Lester reveals it and explains).
# It restarts the phone, so it runs in the background and the guide polls its progress.
_devmode_jobs = {}


def _pmd3_for_terminal():
    """The iPhone tools' path as someone would type it in Terminal (PowerShell on Windows)."""
    path = PMD3 or "pymobiledevice3"
    if IS_WINDOWS:
        return f'& "{path}"'
    home = str(Path.home())
    if path.startswith(home + "/") and " " not in path:
        return "~" + path[len(home):]
    return f'"{path}"' if " " in path else path


def _devmode_command():
    return f"{_pmd3_for_terminal()} amfi enable-developer-mode"


def _reveal_command():
    """Makes the Developer Mode switch appear in the iPhone's Settings."""
    return f"{_pmd3_for_terminal()} amfi reveal-developer-mode"


def _devmode_job(udid):
    _devmode_jobs[udid] = {"state": "running", "message": "Turning on Developer Mode. Your iPhone will restart: keep it plugged in."}
    try:
        code, out, err = run(["amfi", "enable-developer-mode", "--udid", udid], timeout=360)
        text = (out or "") + (err or "")
        if code == TIMED_OUT:
            raise subprocess.TimeoutExpired("amfi", 360)
        if code == 0:
            _devmode_jobs[udid] = {"state": "done", "message": "Developer Mode is on."}
        elif "passcode" in text.lower():
            try:
                run(["amfi", "reveal-developer-mode", "--udid", udid], timeout=30)
            except subprocess.TimeoutExpired:
                pass
            _devmode_jobs[udid] = {"state": "passcode", "message":
                "This iPhone has a passcode, so Apple only lets you turn Developer Mode on from the iPhone itself. "
                "The switch is now showing: Settings → Privacy & Security → Developer Mode."}
        else:
            tail = text.strip().splitlines()[-1] if text.strip() else "unknown error"
            _devmode_jobs[udid] = {"state": "error", "message": f"Couldn't turn it on from the {COMPUTER} ({tail[:160]}). Use the steps on the iPhone instead."}
    except subprocess.TimeoutExpired:
        _devmode_jobs[udid] = {"state": "error", "message": "The iPhone took too long to come back after restarting. Unlock it and check again."}


def enable_devmode(udid):
    dev = next((d for d in usb_phones() if d["udid"] == udid), None)
    if not dev:
        raise RuntimeError(f"Plug the iPhone into this {COMPUTER} with a cable for this step.")
    if (_devmode_jobs.get(udid) or {}).get("state") == "running":
        return _devmode_jobs[udid]
    threading.Thread(target=_devmode_job, args=(udid,), daemon=True).start()
    _devmode_jobs[udid] = {"state": "running", "message": "Turning on Developer Mode. Your iPhone will restart: keep it plugged in."}
    return _devmode_jobs[udid]


def reveal_devmode(udid):
    dev = next((d for d in _devices_or_none() if d["udid"] == udid), None)
    if not dev:
        raise RuntimeError(f"Plug the iPhone into this {COMPUTER} and unlock it.")
    code, out, err = run(["amfi", "reveal-developer-mode", *dev_args(dev)], timeout=30)
    if code != 0:
        raise RuntimeError("Couldn't show the switch. Unlock the iPhone and try again.")
    return {"ok": True}


# ---------- Install Lester for iPhone straight from the Mac ----------
# lester-sideload (built on isideload, MIT) signs the app with the person's own free Apple ID and
# installs it over the cable. The password is handed to it once over a pipe, goes only to Apple's
# sign-in, and is never saved or logged. Two-factor codes and certificate choices come back here.
SIDELOAD_HELPER = next((p for p in (HERE / f"lester-sideload{EXE}", HERE / f"build/lester-sideload{EXE}") if p.exists()), None)
APPLE_ID_FILE = STATE_DIR / "apple-id.txt"   # just the email, to fill in next time
_install_jobs = {}    # udid -> public status shown in the guide
_install_procs = {}   # udid -> helper process


def _remembered_apple_id():
    try:
        return APPLE_ID_FILE.read_text().strip()[:200] or None
    except OSError:
        return None


def _friendly_install_error(text):
    t = text.lower()
    if "password" in t and ("incorrect" in t or "invalid" in t or "-20101" in t):
        return "Apple ID or password is wrong. Check them and try again."
    if "2fa" in t or "two-factor" in t or "verification" in t or "abort" in t:
        return "The two-factor code wasn't accepted. Try again and use the newest code."
    if "maximum" in t and "app id" in t:
        return "This Apple ID has made too many app IDs this week (Apple allows 10). Try again in a few days, or use another Apple ID."
    if "isn't connected" in t or "usbmux" in t:
        return "The iPhone isn't connected. Plug it in with a cable and unlock it."
    if "developer mode" in t:
        return "Developer Mode is off on the iPhone. Do step 2 first."
    return text[:300]


def _install_reader(udid, proc):
    job = _install_jobs[udid]
    for line in proc.stdout:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        kind = ev.get("event")
        if kind == "status":
            job.update(state="running", text=str(ev.get("text", ""))[:200])
        elif kind == "progress":
            try:
                job["progress"] = max(0.0, min(1.0, float(ev.get("value", 0))))
            except (TypeError, ValueError):
                pass
        elif kind == "2fa":
            numbers = [{"id": int(n.get("id", 0)), "number": str(n.get("number", ""))[:40]}
                       for n in (ev.get("numbers") or [])[:10] if isinstance(n, dict)]
            job.update(state="2fa", sms=bool(ev.get("sms")), numbers=numbers,
                       text="Enter the code Apple sent " + ("by text." if ev.get("sms") else "to your devices."),
                       lastError=str(ev.get("lastError") or "")[:200] or None)
        elif kind == "certs":
            certs = [{"serial": str(c.get("serial") or ""), "name": str(c.get("name") or "Certificate")[:80],
                      "machine": str(c.get("machine") or "")[:80]} for c in (ev.get("certs") or [])[:10] if isinstance(c, dict)]
            job.update(state="certs", certs=certs, text="Your Apple ID already has the most certificates Apple allows. Pick one to replace.")
        elif kind == "done":
            job.update(state="done", text="Lester for iPhone is installed.", progress=1.0, installedAt=time.time())
        elif kind == "error":
            job.update(state="error", text=_friendly_install_error(str(ev.get("text", "Install failed"))))
    proc.wait()
    if job.get("state") not in ("done", "error"):
        job.update(state="error", text="The installer stopped unexpectedly. Try again.")
    _install_procs.pop(udid, None)


def start_install(udid, apple_id, password, remember):
    if not SIDELOAD_HELPER:
        raise RuntimeError("This copy of Lester can't install the iPhone app itself. Download the .ipa instead.")
    if not IPA_FILE:
        raise RuntimeError("This copy of Lester doesn't include the iPhone app.")
    if not (isinstance(apple_id, str) and "@" in apple_id and 3 < len(apple_id) <= 200):
        raise ValueError("Enter your Apple ID email")
    if not (isinstance(password, str) and 0 < len(password) <= 200):
        raise ValueError("Enter your Apple ID password")
    if not any(d["udid"] == udid for d in usb_phones()):
        raise RuntimeError(f"Plug the iPhone into this {COMPUTER} with a cable for this step.")
    old = _install_procs.get(udid)
    if old and old.poll() is None:
        raise RuntimeError("Already installing on this iPhone.")
    if remember:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            APPLE_ID_FILE.write_text(apple_id.strip())
        except OSError:
            pass
    proc = subprocess.Popen([str(SIDELOAD_HELPER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, bufsize=1)
    _install_procs[udid] = proc
    _install_jobs[udid] = {"state": "running", "text": "Starting…", "progress": 0.0}
    # The only place the password goes: one line into the helper's private pipe
    proc.stdin.write(json.dumps({"appleId": apple_id.strip(), "password": password, "udid": udid,
                                 "ipa": str(IPA_FILE)}) + "\n")
    proc.stdin.flush()
    threading.Thread(target=_install_reader, args=(udid, proc), daemon=True).start()
    return _install_jobs[udid]


def answer_install(udid, data):
    """A two-factor code or action, or the certificates to replace."""
    proc = _install_procs.get(udid)
    if not proc or proc.poll() is not None:
        raise RuntimeError("The install isn't waiting for anything. Start it again.")
    if "code" in data:
        code = str(data["code"]).strip()
        if not (code.isdigit() and 4 <= len(code) <= 8):
            raise ValueError("Enter the code Apple sent (just the numbers)")
        msg = {"code": code}
    elif data.get("action") in ("resend", "devices", "abort"):
        msg = {"action": data["action"]}
    elif data.get("action") == "sms":
        msg = {"action": "sms", "id": int(data.get("id", 0))}
    elif isinstance(data.get("revoke"), list):
        serials = [str(x)[:80] for x in data["revoke"][:5]]
        msg = {"revoke": serials}
    else:
        raise ValueError("Bad answer")
    _install_jobs[udid].update(state="running", text="Checking…")
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()
    return _install_jobs[udid]


def cancel_install(udid):
    proc = _install_procs.pop(udid, None)
    if proc and proc.poll() is None:
        proc.kill()
    _install_jobs.pop(udid, None)
    return {"ok": True}


def download_ipa():
    """Copies Lester for iPhone into Downloads and shows it in Finder (File Explorer on Windows)."""
    if not IPA_FILE:
        raise RuntimeError("This copy of Lester doesn't include the iPhone app.")
    downloads = Path.home() / "Downloads"
    dest = downloads / "Lester-iPhone.ipa"
    n = 2
    while dest.exists():
        dest = downloads / f"Lester-iPhone {n}.ipa"
        n += 1
    shutil.copyfile(IPA_FILE, dest)
    reveal_file(dest)
    return {"ok": True, "file": dest.name}


def reveal_file(path):
    if IS_WINDOWS:
        subprocess.run(["explorer", f"/select,{path}"])
    else:
        subprocess.run(["open", "-R", str(path)])


def alert(title, msg):
    """A plain error box, for when Lester can't show its window."""
    try:
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, title, 0x10)
        else:
            subprocess.run(["osascript", "-e", f'display alert "{title}" message "{msg}" as critical'])
    except Exception:
        pass


# On Windows the page's Mac wording is swapped as it's served, so one page works for both
WINDOWS_TEXT = [
    ("No prompt? Open <b>Finder</b>, click the iPhone in the sidebar, then click <b>Trust</b>.",
     "No prompt? Open the <b>Apple Devices</b> app, click the iPhone, then click <b>Trust</b>."),
    ("open <b>Terminal</b> (press <b>⌘ Space</b>, type <b>Terminal</b>, press <b>Return</b>), paste it and press <b>Return</b>.",
     "open <b>PowerShell</b> (press the <b>Windows key</b>, type <b>PowerShell</b>, press <b>Enter</b>), "
     "paste it and press <b>Enter</b>."),
    ("Paste it into Terminal and press Return.", "Paste it into PowerShell and press Enter."),
    ("this Mac", "this PC"), ("the Mac", "the PC"), ("your Mac", "your PC"),
    ("Mac’s", "PC’s"), ("Mac's", "PC's"),
]


def page_bytes():
    page = PAGE.read_text(encoding="utf-8")
    if IS_WINDOWS:
        for old, new in WINDOWS_TEXT:
            page = page.replace(old, new)
        page = page.replace("<head>", '<head><script>window.LESTER_OS = "windows";</script>', 1)
    return page.encode("utf-8")


LOCKED_PAGE = b"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lester</title><style>html,body{margin:0;height:100%;background:#000;color:#a3a3a3;font:16px -apple-system,system-ui,sans-serif;
display:grid;place-items:center;text-align:center;padding:24px;box-sizing:border-box}b{display:block;color:#fff;font-size:22px;margin:16px 0 8px}</style>
<div><svg width="48" height="48" viewBox="0 0 24 24"><path d="M6.5 3h4.6v12.4h6.4V20H6.5z" fill="#ff2a3c"/></svg>
<b>This link needs a new code</b>On your computer, open Lester, click the phone button, and scan the QR code again.</div>"""


def parse_route(data):
    """Validate a route from the page: 2-10000 [lat, lon] points and a speed in m/s."""
    pts = data.get("points")
    if not isinstance(pts, list) or not 2 <= len(pts) <= 10000:
        raise ValueError("The route is empty or too long")
    out = []
    for p in pts:
        try:
            lat, lon = float(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError, KeyError):
            raise ValueError("The route has a bad point")
        if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("The route has a bad point")
        out.append((lat, lon))
    try:
        speed = float(data.get("speed"))
    except (TypeError, ValueError):
        raise ValueError("Pick a travel speed")
    if not (math.isfinite(speed) and 0.3 <= speed <= 70):
        raise ValueError("Speed must be between 1 and 250 km/h")
    return out, speed


class Handler(BaseHTTPRequestHandler):
    server_version = "Lester"
    sys_version = ""

    def log_message(self, *a):
        pass

    @property
    def from_phone(self):
        return getattr(self.server, "is_remote", False)

    def _route(self):
        u = urllib.parse.urlsplit(self.path)
        return u.path, urllib.parse.parse_qs(u.query)

    def _allowed(self, query, page=False):
        """Mac: the Host must be Lester's own address (blocks DNS rebinding).
        Phone: phone access must be on and the request must carry the secret key."""
        if not self.from_phone:
            if self.headers.get("Host", "") in ALLOWED_HOSTS:
                return True
            self._json(403, {"error": "forbidden"})
            return False
        key = self.headers.get("X-Lester-Key") or (query.get("k") or [""])[0]
        if _remote["enabled"] and _remote["key"] and hmac.compare_digest(key.encode(), _remote["key"].encode()):
            return True
        if page:
            self._send(401, "text/html; charset=utf-8", LOCKED_PAGE)
        else:
            self._json(401, {"error": f"This phone needs a new code. Scan the QR code in Lester on your {COMPUTER}."})
        return False

    def _send(self, code, ctype, body, extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Map requests say only "http://127.0.0.1:8765/" (OpenStreetMap blocks Edge's map requests
        # without it), never the page's full address, so a phone's access key isn't sent anywhere
        self.send_header("Referrer-Policy", "strict-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, "application/json", json.dumps(obj).encode())

    def do_GET(self):
        path, query = self._route()
        # The Home Screen icon isn't secret, and iPhone fetches it without the key
        if path == "/icon.png":
            if ICON.exists():
                return self._send(200, "image/png", ICON.read_bytes(), [("Cache-Control", "max-age=86400")])
            return self.send_error(404)
        if not self._allowed(query, page=path in ("/", "/index.html")):
            return
        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", page_bytes())
        elif path == "/api/status":
            viewer = "phone" if self.from_phone else "mac"
            try:
                devices = list_devices()
                self._json(200, {"devices": devices, "current": display_location(),
                                 "link": _link, "trip": trip_summary(_trip), "viewer": viewer,
                                 "appleDevicesMissing": not devices and apple_devices_missing()})
            except Exception as e:
                self._json(200, {"devices": [], "current": display_location(), "link": _link,
                                 "trip": trip_summary(_trip), "viewer": viewer, "error": str(e),
                                 "appleDevicesMissing": apple_devices_missing()})
        elif path == "/api/ping":
            self._json(200, {"ok": True})
        elif path == "/api/trip":
            self._json(200, {"trip": trip_summary(_trip, full=True)})
        elif path == "/api/itinerary":
            self._json(200, itinerary_info())
        elif path == "/api/remote" and not self.from_phone:
            self._json(200, remote_info())
        elif path == "/api/iphone-setup" and not self.from_phone:
            udid = (query.get("udid") or [""])[0]
            if not (20 <= len(udid) <= 64 and all(c.isalnum() or c == "-" for c in udid)):
                return self._json(400, {"error": "Pick an iPhone first"})
            self._json(200, setup_status(udid))
        elif path == "/api/usb-phones" and not self.from_phone:
            try:
                phones = list_devices()
            except Exception:
                phones = []
            self._json(200, {"phones": phones, "pairing_available": PAIR_HELPER is not None,
                             "appleDevicesMissing": not phones and apple_devices_missing()})
        else:
            self.send_error(404)

    def do_POST(self):
        path, query = self._route()
        if not self._allowed(query):
            return
        # Only our own page may change things. Requiring JSON also forces browsers to
        # run a CORS preflight for any other site, which this server never approves.
        origin = self.headers.get("Origin")
        own = ALLOWED_ORIGINS if not self.from_phone else {f"http://{self.headers.get('Host', '')}"}
        if origin is not None and origin not in own:
            return self._json(403, {"error": "forbidden"})
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self._json(415, {"error": "JSON required"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            self.close_connection = True
            return self._json(413, {"error": "Request too large"})
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            return self._json(400, {"error": "Bad request"})
        try:
            if path == "/api/set":
                try:
                    lat, lon = float(data["lat"]), float(data["lon"])
                except (KeyError, TypeError, ValueError):
                    raise ValueError("Pick a place first")
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError("Coordinates out of range")
                dev = set_location(lat, lon)
                self._json(200, {"ok": True, "device": dev["name"], "current": _current})
            elif path == "/api/travel":
                points, speed = parse_route(data)
                dev, trip = start_travel(points, speed)
                self._json(200, {"ok": True, "device": dev["name"], "trip": trip_summary(trip)})
            elif path == "/api/move":
                try:
                    lat, lon = float(data["lat"]), float(data["lon"])
                except (KeyError, TypeError, ValueError):
                    raise ValueError("Bad position")
                if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError("Coordinates out of range")
                moved = move_location(lat, lon)
                self._json(200, {"ok": True, "moved": moved, "current": display_location()})
            elif path == "/api/itinerary":
                self._json(200, set_itinerary(data))
            elif path == "/api/stop":
                stop_travel()
                self._json(200, {"ok": True, "current": display_location(), "trip": trip_summary(_trip)})
            elif path == "/api/clear":
                clear_location()
                self._json(200, {"ok": True})
            elif path == "/api/remote" and not self.from_phone:
                self._json(200, set_remote(bool(data.get("enabled"))))
            elif path == "/api/remote/reset" and not self.from_phone:
                self._json(200, reset_remote_key())
            elif path == "/api/iphone-setup/reveal" and not self.from_phone:
                udid = str(data.get("udid") or "")
                if not (20 <= len(udid) <= 64 and all(c.isalnum() or c == "-" for c in udid)):
                    raise ValueError("Pick an iPhone first")
                self._json(200, reveal_devmode(udid))
            elif path == "/api/iphone-setup/devmode" and not self.from_phone:
                udid = str(data.get("udid") or "")
                if not (20 <= len(udid) <= 64 and all(c.isalnum() or c == "-" for c in udid)):
                    raise ValueError("Pick an iPhone first")
                self._json(200, enable_devmode(udid))
            elif path in ("/api/iphone-setup/install", "/api/iphone-setup/install/answer",
                          "/api/iphone-setup/install/cancel") and not self.from_phone:
                udid = str(data.get("udid") or "")
                if not (20 <= len(udid) <= 64 and all(c.isalnum() or c == "-" for c in udid)):
                    raise ValueError("Pick an iPhone first")
                if path.endswith("/install"):
                    job = start_install(udid, data.get("appleId"), data.get("password"), bool(data.get("remember")))
                elif path.endswith("/answer"):
                    job = answer_install(udid, data)
                else:
                    job = cancel_install(udid)
                self._json(200, job)
            elif path == "/api/open-apple-devices" and not self.from_phone:
                webbrowser.open(APPLE_DEVICES_URL)
                self._json(200, {"ok": True})
            elif path == "/api/iphone-setup/download" and not self.from_phone:
                self._json(200, download_ipa())
            elif path == "/api/pair-phone" and not self.from_phone:
                udid = data.get("udid")
                if not isinstance(udid, str) or not 20 <= len(udid) <= 64 or not all(c.isalnum() or c == "-" for c in udid):
                    raise ValueError("Pick an iPhone first")
                self._json(200, pair_phone(udid))
            elif path == "/api/quit" and not self.from_phone:
                self._json(200, {"ok": True})
                # shutdown() blocks until serve_forever stops, so run it on another thread
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.send_error(404)
        except Exception as e:
            self._json(500, {"error": str(e)})


# Windows talks to iPhones through Apple's driver service (from the Apple Devices app or
# iTunes), which listens on this port. Without it Lester can't see any iPhone.
APPLE_DEVICES_URL = "https://apps.microsoft.com/detail/9np83lwlpz9k"


def apple_devices_missing():
    if not IS_WINDOWS:
        return False
    try:
        socket.create_connection(("127.0.0.1", int(os.environ.get("LESTER_USBMUX_PORT", "27015"))), timeout=1).close()
        return False
    except OSError:
        return True


def main():
    if not PMD3:
        sys.exit("pymobiledevice3 not found. Open Lester again to finish setting it up.")
    threading.Thread(target=watchdog, daemon=True).start()
    _load_itinerary()
    threading.Thread(target=itinerary_loop, daemon=True).start()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        msg = f"Port {PORT} is already in use. Lester may already be running, or another app is using it."
        print(msg)
        alert("Lester couldn't start", msg)
        sys.exit(1)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"Lester is running at {url}  (Ctrl+C to quit)")
    _load_remote()
    if _remote["enabled"]:
        try:
            set_remote(True)
        except RuntimeError as e:
            print(e)
    if not os.environ.get("NO_BROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("\nStopping Lester. Your iPhone goes back to its real location.")
        stop_sim()
        _stop_remote_server()
        server.server_close()
        try:
            (STATE_DIR / "server.pid").unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()
