"""
PC Power - control your Windows PC from your phone.

Double-click (or run) once: it installs itself, starts with Windows, and sits in the
tray. Your phone gets a web page with:
  - live status (on/off, uptime, CPU / GPU / RAM, download speed, temps, open apps)
  - Shut down, Restart, Sleep, Lock
  - Timers (30 min / 1 h / 2 h) and "shut down when idle" (downloads / renders finished)
Shutdown is safe: Office files are saved first, apps are asked to close normally, and
only if something is still blocking after a while is it forced.
Optional push notifications to your phone via the free ntfy app.
"""
import http.server
import json
import os
import pathlib
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime

APP = "PC Power"
APP_VERSION = "1.1.0"   # GitHub builds replace this with the release tag
IS_WIN = os.name == "nt"
FROZEN = getattr(sys, "frozen", False)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

HERE = pathlib.Path(sys.executable if FROZEN else __file__).resolve().parent
if IS_WIN:
    DATA = pathlib.Path(os.environ.get("LOCALAPPDATA", pathlib.Path.home())) / "PCPower"
else:
    DATA = HERE / "pcpower-data"
DATA.mkdir(parents=True, exist_ok=True)

CONFIG_FILE = DATA / "config.json"
LOG_FILE = DATA / "pcpower.log"
LINK_FILE = DATA / "phone_link.txt"
INSTALLED_EXE = DATA / "PCPower.exe"
FW_RULE = "PC Power"
FW_RULE_TS = "PC Power (Tailscale)"

DEFAULTS = {
    "port": 8765,
    "countdown_seconds": 10,          # countdown for "Shut down now" / "Restart" (time to cancel)
    "auto_save_office": True,         # save open Word / Excel / PowerPoint files first
    "auto_force_after_seconds": 60,   # force-close apps still blocking after this long (0 = never)
    "idle_minutes": 5,                # "When idle": how long the PC must stay idle
    "idle_download_kbps": 300,        #   idle = download below this (KB/s) ...
    "idle_gpu_percent": 15,           #   ... and GPU below this ...
    "idle_cpu_percent": 25,           #   ... and CPU below this
    "idle_disk_mbps": 20,             #   ... and disk below this (MB/s), and no Steam/browser downloads
    "game_protect": True,             # timers / idle / bedtime wait while a full-screen game or app runs
    "bedtime": "",                    # e.g. "02:00": shut down every night if nobody is using the PC
    "bedtime_unused_minutes": 15,     #   "nobody using" = no mouse/keyboard for this long
    "push_notifications": True,       # ntfy push notifications (PC turned on, shutting down, ...)
    "ntfy_server": "https://ntfy.sh",
    "update_repo": "",                # "github-user/repo" to auto-update from its releases
    "auto_update": True,
}


# ---------------------------------------------------------------- basics
def log(msg):
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")
    except OSError:
        pass


def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    cfg.setdefault("key", "")
    if not cfg["key"]:
        cfg["key"] = secrets.token_urlsafe(16)
    if not cfg.get("ntfy_topic"):
        cfg["ntfy_topic"] = "pcpower-" + secrets.token_hex(8)
    save_config(cfg)
    return cfg


def save_config(cfg):
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError:
        pass


CFG = load_config()
PORT = int(CFG["port"])


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no packets sent; just picks the LAN interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def tailscale_ip():
    try:
        import psutil
        for addrs in psutil.net_if_addrs().values():
            for a in addrs:
                if a.family == socket.AF_INET:
                    p = [int(x) for x in a.address.split(".")]
                    if p[0] == 100 and 64 <= p[1] <= 127:  # 100.64.0.0/10
                        return a.address
    except Exception:
        pass
    return None


net = {"ip": lan_ip(), "ts": tailscale_ip()}


def link_for(ip):
    # key in the path (not ?key=) so scanners / previews / share sheets never drop it
    return f"http://{ip}:{PORT}/k/{CFG['key']}"


def phone_link():
    # Tailscale works at home (direct connection) AND away, so it's the best single link.
    return link_for(net["ts"] or net["ip"])


def all_hosts():
    """Every address the phone page may use; it switches to whichever answers."""
    return [f"http://{ip}:{PORT}" for ip in (net["ts"], net["ip"]) if ip]


def refresh_links():
    old = (net["ip"], net["ts"])
    net["ip"], net["ts"] = lan_ip(), tailscale_ip()
    try:
        LINK_FILE.write_text(phone_link() + "\n")
    except OSError:
        pass
    return old != (net["ip"], net["ts"])


def ps(command, timeout=10):
    r = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True,
                       text=True, timeout=timeout, creationflags=NO_WINDOW)
    return r.stdout


# ---------------------------------------------------------------- notifications
tray = {"icon": None}


def notify(msg):
    """Windows toast from the tray icon."""
    ic = tray["icon"]
    if ic is not None:
        try:
            ic.notify(msg, APP)
        except Exception:
            pass


def push(title, msg, tags="computer", priority="default", retries=0):
    """Push notification to the phone via ntfy (runs in background)."""
    if not CFG.get("push_notifications"):
        return

    def send():
        url = CFG["ntfy_server"].rstrip("/") + "/" + CFG["ntfy_topic"]
        for attempt in range(retries + 1):
            try:
                req = urllib.request.Request(url, data=msg.encode("utf-8"), method="POST", headers={
                    "Title": title.encode("ascii", "ignore").decode(), "Tags": tags, "Priority": priority})
                urllib.request.urlopen(req, timeout=10).read()
                return True
            except Exception as e:
                if attempt == retries:
                    log(f"Push failed: {e}")
                time.sleep(15)
        return False

    threading.Thread(target=send, daemon=True).start()


HOST = socket.gethostname()


# ---------------------------------------------------------------- live stats
stats = {"cpu": None, "ram": None, "gpu": None, "down": 0, "temps": {}, "apps": [],
         "boot": None, "viewed": 0.0}

SKIP_APPS = {"Application Frame Host", "ApplicationFrameHost", "TextInputHost",
             "Windows Input Experience", "Microsoft Text Input Application",
             "Settings", "SystemSettings", "pythonw", "Python", "PCPower", APP}


def open_apps():
    if not IS_WIN:
        try:
            return [n for n in ps("x").splitlines() if n.strip() and n.strip() not in SKIP_APPS]
        except Exception:
            return []
    try:
        out = ps("Get-Process | Where-Object { $_.MainWindowTitle } | "
                 "ForEach-Object { if ($_.Description) { $_.Description } else { $_.ProcessName } } | "
                 "Sort-Object -Unique", timeout=8)
        return [n.strip() for n in out.splitlines() if n.strip() and n.strip() not in SKIP_APPS]
    except Exception:
        return []


GPU_PS = (r"$s=(Get-Counter '\GPU Engine(*)\Utilization Percentage' -ErrorAction SilentlyContinue).CounterSamples;"
          r"$g=@{}; foreach($x in $s){ if($x.InstanceName -match 'engtype_(\w+)'){ $g[$matches[1]] += $x.CookedValue } };"
          r"if($g.Count){ [math]::Round((($g.Values | Measure-Object -Maximum).Maximum),0) }")


def gpu_percent():
    if not IS_WIN:
        return None
    try:
        out = ps(GPU_PS, timeout=8).strip()
        return min(100, int(float(out))) if out else None
    except Exception:
        return None


def lhm_temps():
    """Temperatures from LibreHardwareMonitor's web server (optional, port 8085)."""
    try:
        data = json.loads(urllib.request.urlopen("http://127.0.0.1:8085/data.json", timeout=1).read())
    except Exception:
        return {}
    found = {}

    def walk(node, kind):
        img = (node.get("ImageURL") or "").lower()
        if "cpu" in img:
            kind = "cpu"
        elif any(k in img for k in ("ati", "nvidia", "gpu")):  # LHM uses ati.png for AMD GPUs
            kind = "gpu"
        val = str(node.get("Value", ""))
        text = node.get("Text", "")
        if kind and "°C" in val and kind not in found:
            pref = ("Tctl", "Package", "Tdie") if kind == "cpu" else ("GPU Core", "Core")
            if any(p in text for p in pref):
                try:
                    found[kind] = round(float(val.replace("°C", "").replace(",", ".").strip()))
                except ValueError:
                    pass
        for c in node.get("Children", []) or []:
            walk(c, kind)

    walk(data, None)
    return found


def sampler():
    """Background loop: keeps stats fresh and tracks idleness for 'shut down when idle'."""
    try:
        import psutil
    except ImportError:
        log("psutil missing - stats and idle shutdown disabled")
        return
    stats["boot"] = psutil.boot_time()
    psutil.cpu_percent(None)
    last_net = psutil.net_io_counters().bytes_recv
    last_t = time.time()
    tick = 0
    while True:
        time.sleep(5)
        tick += 1
        now = time.time()
        recv = psutil.net_io_counters().bytes_recv
        stats["down"] = max(0, (recv - last_net) / max(0.1, now - last_t))
        last_net, last_t = recv, now
        stats["cpu"] = round(psutil.cpu_percent(None))
        stats["ram"] = round(psutil.virtual_memory().percent)

        watching = now - stats["viewed"] < 60
        idle_job = job.get("mode") == "idle"
        if (watching or idle_job) and tick % 2 == 0:
            stats["gpu"] = gpu_percent()
        if watching and tick % 2 == 0:
            stats["temps"] = lhm_temps()
        if watching and tick % 3 == 0:
            stats["apps"] = open_apps()
        if tick % 12 == 0 and refresh_links():
            log(f"Network changed: LAN {net['ip']}  Tailscale {net['ts']}")
            if tray["icon"]:
                tray["icon"].title = tooltip()

        try:
            d = psutil.disk_io_counters()
            dio = d.read_bytes + d.write_bytes
            stats["disk"] = max(0, (dio - stats.get("_dio", dio)) / 5)
            stats["_dio"] = dio
        except Exception:
            stats["disk"] = 0

        if watching or idle_job or tick % 12 == 0:
            stats["fullscreen"] = fullscreen_app() if CFG.get("game_protect") else None

        if idle_job:
            reasons = busy_reasons()
            stats["busy"] = reasons
            with lock:
                if job.get("mode") == "idle":
                    job["idle_secs"] = 0 if reasons else job["idle_secs"] + 5
        else:
            stats["busy"] = []


# ---------------------------------------------------------------- what is the PC doing?
def _downloads_folder():
    try:
        import ctypes
        from ctypes import wintypes
        guid = ctypes.c_char_p(b"\x90\xe2\x4d\x37\x3f\x12\x65\x45\x91\x64\x39\xc4\x92\x5e\x46\x7b")  # FOLDERID_Downloads
        path = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(guid, 0, None, ctypes.byref(path)) == 0:
            p = path.value
            ctypes.windll.ole32.CoTaskMemFree(path)
            return pathlib.Path(p)
    except Exception:
        pass
    return pathlib.Path.home() / "Downloads"


def browser_downloading():
    """True while Chrome / Edge (.crdownload) or Firefox (.part) are writing a download."""
    try:
        now = time.time()
        for f in _downloads_folder().iterdir():
            if f.suffix.lower() in (".crdownload", ".part", ".partial", ".download"):
                if now - f.stat().st_mtime < 120:
                    return True
    except Exception:
        pass
    return False


_steam_libs = {"t": 0, "libs": []}


def steam_libraries():
    if time.time() - _steam_libs["t"] < 600:
        return _steam_libs["libs"]
    libs = []
    try:
        import re
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            steam = pathlib.Path(winreg.QueryValueEx(k, "SteamPath")[0])
        libs.append(steam / "steamapps")
        vdf = (steam / "steamapps" / "libraryfolders.vdf").read_text(encoding="utf-8", errors="ignore")
        for p in re.findall(r'"path"\s+"([^"]+)"', vdf):
            sa = pathlib.Path(p.replace("\\\\", "\\")) / "steamapps"
            if sa not in libs:
                libs.append(sa)
    except Exception:
        pass
    _steam_libs.update(t=time.time(), libs=libs)
    return libs


def steam_downloading():
    """True while Steam is writing into a library's 'downloading' folder (paused downloads don't count)."""
    now = time.time()
    for lib in steam_libraries():
        dl = lib / "downloading"
        try:
            for root, _dirs, files in os.walk(dl):
                for name in files[:200]:
                    if now - os.path.getmtime(os.path.join(root, name)) < 120:
                        return True
        except Exception:
            pass
    return False


def user_idle_seconds():
    """Seconds since the last mouse / keyboard input."""
    if not IS_WIN:  # test hook (Linux): seconds read from a file
        try:
            return float(pathlib.Path(os.environ["PCPOWER_FAKE_IDLE_FILE"]).read_text())
        except Exception:
            return 0.0
    try:
        import ctypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(lii)
        ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
        return (ctypes.windll.kernel32.GetTickCount() - lii.dwTime) / 1000.0
    except Exception:
        return 0.0


def fullscreen_app():
    """Name of a full-screen app/game in front (games, full-screen video), else None."""
    if not IS_WIN:  # test hook (Linux): app name read from a file
        try:
            return pathlib.Path(os.environ["PCPOWER_FAKE_FS_FILE"]).read_text().strip() or None
        except Exception:
            return None
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return None
        cls = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(hwnd, cls, 256)
        if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd"):
            return None
        r = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(r))

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(mi)
        u.GetMonitorInfoW(u.MonitorFromWindow(hwnd, 2), ctypes.byref(mi))
        m = mi.rcMonitor
        if not (r.left <= m.left and r.top <= m.top and r.right >= m.right and r.bottom >= m.bottom):
            return None
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        import psutil
        name = psutil.Process(pid.value).name()
        name = name[:-4] if name.lower().endswith(".exe") else name
        return None if name.lower() in ("explorer", "pcpower", "pythonw", "python") else name
    except Exception:
        return None


def busy_reasons():
    """Why the PC is NOT idle right now (empty list = idle)."""
    r = []
    if steam_downloading():
        r.append("Steam is downloading")
    if browser_downloading():
        r.append("a browser download is running")
    if stats["down"] > CFG["idle_download_kbps"] * 1024:
        r.append(f"downloading {stats['down'] / 1048576:.1f} MB/s")
    if (stats.get("disk") or 0) > CFG["idle_disk_mbps"] * 1048576:
        r.append(f"disk busy {stats['disk'] / 1048576:.0f} MB/s")
    if (stats["gpu"] or 0) > CFG["idle_gpu_percent"]:
        r.append(f"GPU {stats['gpu']}%")
    if (stats["cpu"] or 0) > CFG["idle_cpu_percent"]:
        r.append(f"CPU {stats['cpu']}%")
    return r


# ---------------------------------------------------------------- power actions
#
# Safe shutdown: `shutdown /s /t N` with N > 0 silently implies /f (force-close, unsaved
# work lost). So the countdown happens here, Office files are saved, then
# `shutdown /s /t 0` WITHOUT /f lets apps close normally. If something still blocks
# (e.g. a "Save changes?" prompt) after auto_force_after_seconds, it is forced.

lock = threading.Lock()
job = {}            # pending timer / idle job: action, mode, deadline, total, idle_secs, warned
closing = {}        # after shutdown was issued: action, since, forcing, stop(Event)


def documents_folder():
    try:
        from win32com.shell import shell, shellcon
        return pathlib.Path(shell.SHGetFolderPath(0, shellcon.CSIDL_PERSONAL, None, 0))
    except Exception:
        return pathlib.Path.home() / "Documents"


def save_office_docs():
    """Save unsaved Word/Excel/PowerPoint files. Untitled ones go to
    Documents\\Auto-saved before shutdown\\ with a timestamp."""
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        if IS_WIN:
            log("Office auto-save skipped: pywin32 not installed")
        return []
    pythoncom.CoInitialize()
    saved = []
    stamp = datetime.now().strftime("%Y-%m-%d %H-%M")
    out_dir = documents_folder() / "Auto-saved before shutdown"

    def target(name, ext):
        out_dir.mkdir(parents=True, exist_ok=True)
        base = "".join(c for c in name if c not in '<>:"/\\|?*').strip() or "Untitled"
        p, n = out_dir / f"{base} ({stamp}){ext}", 2
        while p.exists():
            p, n = out_dir / f"{base} ({stamp}) {n}{ext}", n + 1
        return str(p)

    def get(progid):
        try:
            return win32com.client.GetActiveObject(progid)
        except Exception:
            return None

    apps = [
        ("Word.Application", "Documents", ".docx", lambda a: setattr(a, "DisplayAlerts", 0),
         lambda d, p: d.SaveAs2(p, 16)),
        ("Excel.Application", "Workbooks", ".xlsx", lambda a: setattr(a, "DisplayAlerts", False),
         lambda d, p: d.SaveAs(p, 51)),
        ("PowerPoint.Application", "Presentations", ".pptx", lambda a: setattr(a, "DisplayAlerts", 1),
         lambda d, p: d.SaveAs(p)),
    ]
    for progid, coll, ext, quiet, save_as in apps:
        app = get(progid)
        if not app:
            continue
        try:
            quiet(app)
            for doc in list(getattr(app, coll)):
                try:
                    if doc.Saved:
                        continue
                    if doc.Path:
                        doc.Save()
                        saved.append(doc.FullName)
                    else:
                        p = target(doc.Name, ext)
                        save_as(doc, p)
                        saved.append(p)
                except Exception as e:
                    log(f"{progid}: couldn't save {getattr(doc, 'Name', '?')}: {e}")
        except Exception as e:
            log(f"{progid} auto-save error: {e}")
    for p in saved:
        log(f"Auto-saved: {p}")
    return saved


def run_shutdown(action="shutdown", force=False, who="?"):
    cmd = ["shutdown", "/r" if action == "restart" else "/s", "/t", "0"]
    if force:
        cmd.append("/f")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, creationflags=NO_WINDOW)
    except OSError as e:
        log(f"shutdown failed: {e}")
        return False, str(e)
    log(f"{'FORCE ' if force else ''}{action} ({who}) -> exit {r.returncode} {r.stdout.strip()} {r.stderr.strip()}")
    if r.returncode == 0:
        return True, None
    return False, (r.stderr or r.stdout).strip() or f"shutdown exited with code {r.returncode}"


def _watchdog(action, stop, who):
    if stop.wait(CFG["auto_force_after_seconds"]):
        return
    with lock:
        closing["forcing"] = True
    blockers = ", ".join(open_apps()[:5]) or "an app"
    log(f"Still running after {CFG['auto_force_after_seconds']}s - forcing ({blockers})")
    push(f"{HOST}: forcing {action}", f"Force-closed because these were still open: {blockers}",
         tags="warning")
    for _ in range(10):
        if stop.is_set():
            return
        run_shutdown(action, force=True, who=f"{who} (auto)")
        if stop.wait(20):
            return


def execute(action, who):
    stop = threading.Event()
    with lock:
        closing.clear()
        closing.update(action=action, since=time.time(), forcing=False, stop=stop)
    if CFG["auto_save_office"]:
        try:
            saved = save_office_docs()
            if saved:
                notify(f"Saved {len(saved)} Office file(s) before {action}")
        except Exception as e:
            log(f"Office auto-save failed: {e}")
    ok, err = run_shutdown(action, who=who)
    if not ok:
        with lock:
            closing.clear()
        push(f"{HOST}: {action} failed", err or "unknown error", tags="x")
        return
    push(f"{HOST}: {'restarting' if action == 'restart' else 'shutting down'}",
         f"Requested from {who}.", tags="arrows_counterclockwise" if action == "restart" else "red_circle")
    if CFG["auto_force_after_seconds"]:
        threading.Thread(target=_watchdog, args=(action, stop, who), daemon=True).start()


def schedule(action, delay=None, mode="timer", who="?", protect=None):
    """protect = wait while a full-screen game/app runs. Default: on for timers and idle
    shutdowns (you may have started a game since), off for "shut down now"."""
    if action not in ("shutdown", "restart"):
        return False, "bad action"
    with lock:
        if closing:
            return False, "Already shutting down"
        job.clear()
        if mode == "idle":
            job.update(action=action, mode="idle", idle_secs=0, warned=set(), who=who,
                       protect=True if protect is None else protect)
        else:
            delay = CFG["countdown_seconds"] if delay is None else max(0, min(int(delay), 24 * 3600))
            job.update(action=action, mode="timer", deadline=time.time() + delay, total=delay,
                       warned=set(), who=who,
                       protect=(delay > CFG["countdown_seconds"]) if protect is None else protect)
    word = "Restart" if action == "restart" else "Shutdown"
    if mode == "idle":
        log(f"{word} when idle ({CFG['idle_minutes']} min) - from {who}")
        notify(f"Will {action} once the PC has been idle for {CFG['idle_minutes']} min")
    else:
        log(f"{word} in {delay}s - from {who}")
        notify(f"{word} in {fmt_secs(delay)} (from your phone). Tray icon → Cancel to stop.")
        if delay >= 300:
            push(f"{HOST}: {action} timer set", f"{word} in {fmt_secs(delay)}.", tags="timer")
    return True, None


def cancel(who="?"):
    with lock:
        if job:
            job.clear()
            msg = "Cancelled"
        elif closing:
            closing["stop"].set()
            closing.clear()
            msg = "Stopped. If Windows was already closing apps, it may still finish."
        else:
            return False, "Nothing to cancel"
    if IS_WIN:
        subprocess.run(["shutdown", "/a"], capture_output=True, creationflags=NO_WINDOW)
    log(f"Cancelled ({who})")
    notify("Shutdown cancelled")
    return True, msg


def force(who="?"):
    with lock:
        if job.get("mode") == "gamewait":
            # "Shut down anyway" while waiting for a game: normal safe shutdown, no waiting
            fire = (job["action"], who)
            job.clear()
        else:
            fire = None
    if fire:
        log(f"Not waiting for the game any more ({who})")
        threading.Thread(target=execute, args=fire, daemon=True).start()
        return True, None
    with lock:
        action = closing.get("action") or job.get("action") or "shutdown"
        job.clear()
        if closing:
            closing["forcing"] = True
    return run_shutdown(action, force=True, who=who)


def sleep_pc(who="?"):
    log(f"Sleep ({who})")

    def go():
        time.sleep(1.5)  # let the HTTP response reach the phone first
        if IS_WIN:
            import ctypes
            ctypes.windll.powrprof.SetSuspendState(False, False, False)

    threading.Thread(target=go, daemon=True).start()
    return True, None


def lock_pc(who="?"):
    log(f"Lock ({who})")
    if IS_WIN:
        import ctypes
        ctypes.windll.user32.LockWorkStation()
    return True, None


def fmt_secs(s):
    s = int(s)
    if s >= 3600:
        return f"{s // 3600} h {s % 3600 // 60:02d} min"
    if s >= 60:
        return f"{s // 60} min" + (f" {s % 60} s" if s % 60 else "")
    return f"{s} s"


bed = {"date": None, "noted": None}


def parse_hhmm(s):
    try:
        h, m = str(s).strip().split(":")
        h, m = int(h), int(m)
        return h * 60 + m if 0 <= h < 24 and 0 <= m < 60 else None
    except Exception:
        return None


def check_bedtime():
    """From the bedtime until 6 hours later: once nobody has touched the PC for
    bedtime_unused_minutes, start a 2-minute shutdown (once per night)."""
    bt = parse_hhmm(CFG.get("bedtime"))
    if bt is None:
        return
    now = datetime.now()
    since = (now.hour * 60 + now.minute - bt) % 1440
    if since >= 360:
        return
    night = (now.timestamp() - since * 60)
    key = datetime.fromtimestamp(night).strftime("%Y-%m-%d")
    if bed["date"] == key or job or closing:
        return
    unused = CFG["bedtime_unused_minutes"] * 60
    if user_idle_seconds() >= unused:
        bed["date"] = key
        schedule("shutdown", 120, who="bedtime", protect=True)
        log("Bedtime: PC unused - shutting down in 2 min")
        notify("Bedtime: shutting down in 2 minutes. Move the mouse or Cancel from the tray to stop.")
        push(f"{HOST}: bedtime", "Nobody is using the PC - shutting down in 2 minutes.", tags="crescent_moon")
    elif bed["noted"] != key:
        bed["noted"] = key
        log("Bedtime: PC is in use, waiting until it's unused")


def scheduler():
    tick = 0
    while True:
        time.sleep(1)
        tick += 1
        fire = None
        if tick % 30 == 0:
            check_bedtime()
        # bedtime countdown: someone touched the PC -> cancel
        if job.get("who") == "bedtime" and job.get("mode") == "timer" and user_idle_seconds() < 5:
            cancel("bedtime - PC is being used")
            push(f"{HOST}: bedtime cancelled", "Someone started using the PC.", tags="crescent_moon")
            continue
        game = fullscreen_app() if (job.get("protect") and CFG.get("game_protect") and tick % 5 == 0) else None
        with lock:
            if job.get("mode") == "idle" and job["idle_secs"] >= CFG["idle_minutes"] * 60:
                job.update(mode="timer", deadline=time.time() + 60, total=60)
                log("PC is idle - shutting down in 60s")
                notify(f"PC has been idle - {job['action']} in 60 s. Tray icon → Cancel to stop.")
                push(f"{HOST} is idle", f"{job['action'].capitalize()} in 60 seconds.", tags="zzz")
            if job.get("mode") == "gamewait" and tick % 5 == 0 and not game:
                job.update(mode="timer", deadline=time.time() + 60, total=60)
                log(f"{job['game']} closed - {job['action']} in 60s")
                notify(f"{job['game']} closed - {job['action']} in 60 s.")
                push(f"{HOST}: game closed", f"{job['action'].capitalize()} in 60 seconds.", tags="video_game")
            if job.get("mode") == "timer":
                left = job["deadline"] - time.time()
                for w in (300, 60):
                    if left <= w and w not in job["warned"] and job["total"] > w + 5:
                        job["warned"].add(w)
                        notify(f"{job['action'].capitalize()} in {fmt_secs(w)}. Tray icon → Cancel to stop.")
                if left <= 0:
                    g = fullscreen_app() if (job.get("protect") and CFG.get("game_protect")) else None
                    if g:
                        job.update(mode="gamewait", game=g)
                        log(f"{job['action']} postponed - {g} is running")
                        notify(f"{g} is running - {job['action']} will wait until you close it.")
                        push(f"{HOST}: waiting for {g}",
                             f"{job['action'].capitalize()} will happen once {g} is closed.", tags="video_game")
                    else:
                        fire = (job["action"], job["who"])
                        job.clear()
        if fire:
            threading.Thread(target=execute, args=fire, daemon=True).start()


def status():
    stats["viewed"] = time.time()
    with lock:
        if job:
            phase = {"idle": "idlewait", "gamewait": "gamewait"}.get(job["mode"], "timer")
            action = job["action"]
        elif closing:
            phase = "forcing" if closing.get("forcing") else "closing"
            action = closing["action"]
        else:
            phase, action = "idle", None
        s = {
            "host": HOST, "phase": phase, "action": action,
            "seconds_left": max(0, round(job["deadline"] - time.time())) if job.get("mode") == "timer" else None,
            "idle_secs": job.get("idle_secs") if job.get("mode") == "idle" else None,
            "idle_needed": CFG["idle_minutes"] * 60,
            "closing_for": round(time.time() - closing["since"]) if closing else None,
            "game": job.get("game"), "who": job.get("who"),
        }
    s.update(
        uptime=round(time.time() - stats["boot"]) if stats["boot"] else None,
        cpu=stats["cpu"], ram=stats["ram"], gpu=stats["gpu"], down=round(stats["down"]),
        temps=stats["temps"], apps=stats["apps"], busy=stats.get("busy", []),
        fullscreen=stats.get("fullscreen"),
        auto_force=CFG["auto_force_after_seconds"], auto_save=CFG["auto_save_office"],
        countdown=CFG["countdown_seconds"], tailscale=bool(net["ts"]),
        bedtime=CFG.get("bedtime") or "", bedtime_unused=CFG["bedtime_unused_minutes"],
        hosts=all_hosts(), version=APP_VERSION, update=update_state.get("available"),
    )
    return s


# ---------------------------------------------------------------- auto-update (GitHub releases)
update_state = {"available": None, "checked": 0}
UPDATE_FILE = DATA / "PCPower-update.exe"


def _ver(v):
    try:
        return tuple(int(x) for x in str(v).lstrip("vV").split("-")[0].split("."))
    except ValueError:
        return (0,)


def check_for_update(install=True, manual=False):
    """Look at the latest GitHub release of update_repo. If it's newer, download
    PCPower.exe and hand over to it (it replaces this copy and restarts)."""
    repo = (CFG.get("update_repo") or "").strip().strip("/")
    if not repo:
        if manual:
            notify("Updates aren't set up: add your GitHub repo as update_repo in Settings.")
        return
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/latest",
                                     headers={"User-Agent": "PCPower-updater",
                                              "Accept": "application/vnd.github+json"})
        rel = json.loads(urllib.request.urlopen(req, timeout=15).read())
    except Exception as e:
        log(f"Update check failed: {e}")
        if manual:
            notify("Couldn't check for updates (see log).")
        return
    update_state["checked"] = time.time()
    tag = rel.get("tag_name", "")
    asset = next((a for a in rel.get("assets", []) if a.get("name", "").lower() == "pcpower.exe"), None)
    if not asset or _ver(tag) <= _ver(APP_VERSION):
        update_state["available"] = None
        if manual:
            notify(f"PC Power is up to date (v{APP_VERSION}).")
        return
    update_state["available"] = tag
    log(f"Update available: {tag} (running v{APP_VERSION})")
    if not (install and FROZEN and IS_WIN):
        return
    if job or closing:
        log("Update postponed: a shutdown is pending")
        return
    try:
        tmp = UPDATE_FILE.with_suffix(".part")
        with urllib.request.urlopen(urllib.request.Request(
                asset["browser_download_url"], headers={"User-Agent": "PCPower-updater"}), timeout=120) as r, \
                open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        if tmp.stat().st_size < 1_000_000:
            raise ValueError("download looks too small")
        tmp.replace(UPDATE_FILE)
    except Exception as e:
        log(f"Update download failed: {e}")
        return
    log(f"Installing update {tag}")
    push(f"{HOST}: PC Power updated", f"Updated to {tag}.", tags="arrow_up")
    subprocess.Popen([str(UPDATE_FILE), "--update"], cwd=str(DATA))
    # the new copy stops this one through /quit, installs itself and starts


def updater():
    time.sleep(90)  # let the PC finish starting up
    while True:
        if CFG.get("auto_update", True):
            check_for_update()
        time.sleep(12 * 3600)


def set_settings(q):
    """Settings the phone page can change."""
    if "bedtime" in q:
        v = q["bedtime"].strip()
        if v and parse_hhmm(v) is None:
            return False, "Bedtime must look like 23:30"
        CFG["bedtime"] = v
        bed["date"] = None
        log(f"Bedtime set to {v or 'off'}")
    save_config(CFG)
    return True, None


# ---------------------------------------------------------------- phone page
PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="PC Power">
<meta name="theme-color" content="#0f1115">
<link rel="icon" href="/icon.svg">
<link rel="apple-touch-icon" href="/icon.png">
<title>PC Power</title>
<style>
  :root { --bg:#0f1115; --card:#1a1d24; --card2:#232731; --fg:#e8eaed; --mut:#9aa0a6;
          --red:#e5484d; --green:#30a46c; --amber:#f5a524; --blue:#3e8ef7; }
  * { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
  body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.4 system-ui, sans-serif;
         padding:max(16px, env(safe-area-inset-top)) 16px 24px; }
  .wrap { max-width:420px; margin:0 auto; display:flex; flex-direction:column; gap:12px; }
  .card { background:var(--card); border-radius:20px; padding:18px; }
  .head { display:flex; align-items:center; justify-content:space-between; gap:12px; }
  .head > div:first-child { min-width:0; flex:1; }
  h1 { margin:0; font-size:20px; white-space:nowrap; }
  .pc { font-size:13px; color:var(--mut); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .badge { display:inline-flex; align-items:center; gap:8px; padding:7px 13px; border-radius:999px;
           background:var(--card2); font-weight:600; font-size:14px; white-space:nowrap; }
  .dot { width:9px; height:9px; border-radius:50%; background:var(--mut); flex:none; }
  .on .dot, .dot.on { background:var(--green); box-shadow:0 0 8px var(--green); }
  .busy .dot { background:var(--amber); }
  .off .dot, .dot.off { background:var(--red); }
  #pcs { display:none; gap:8px; overflow-x:auto; padding-bottom:2px; }
  .pcchip { display:flex; align-items:center; gap:8px; border:0; border-radius:999px; padding:9px 14px;
            background:var(--card); color:var(--fg); font-size:14px; font-weight:600; white-space:nowrap; cursor:pointer; }
  .pcchip.sel { background:#2c3445; box-shadow:inset 0 0 0 2px var(--blue); }
  .stats { display:grid; grid-template-columns:repeat(3, 1fr); gap:8px; margin-top:16px; }
  .stat { background:var(--card2); border-radius:12px; padding:10px; min-width:0; }
  .stat b { display:block; font-size:17px; font-variant-numeric:tabular-nums; white-space:nowrap;
            overflow:hidden; text-overflow:ellipsis; }
  .stat span { font-size:12px; color:var(--mut); }
  .main { text-align:center; padding:26px 18px; }
  #power { width:132px; height:132px; border-radius:50%; border:0; background:var(--red); color:#fff;
           cursor:pointer; display:flex; align-items:center; justify-content:center; margin:0 auto;
           box-shadow:0 8px 30px rgba(229,72,77,.35); transition:transform .1s, opacity .2s; }
  #power:active { transform:scale(.96); }
  #power svg { width:56px; height:56px; }
  #hint { color:var(--mut); font-size:14px; margin-top:14px; min-height:20px; }
  .row { display:grid; grid-template-columns:repeat(3, 1fr); gap:8px; }
  .btn { border:0; border-radius:14px; padding:14px 6px; background:var(--card2); color:var(--fg);
         font-size:15px; font-weight:600; cursor:pointer; display:flex; flex-direction:column;
         align-items:center; gap:6px; }
  .btn svg { width:22px; height:22px; }
  .btn:active { transform:scale(.97); }
  button:disabled { opacity:.35; box-shadow:none; }
  .label { font-size:13px; color:var(--mut); margin:0 0 10px 2px; }
  .timers { display:grid; grid-template-columns:repeat(4, 1fr); gap:8px; }
  .chip { border:0; border-radius:12px; padding:12px 4px; background:var(--card2); color:var(--fg);
          font-size:14px; font-weight:600; cursor:pointer; }
  .chip.idle { grid-column:span 4; }
  #pending { display:none; background:#2b2416; border:1px solid #5a4510; }
  #pending .t { font-size:15px; font-weight:600; }
  #pending .s { font-size:13px; color:var(--mut); margin-top:2px; }
  .bar { height:6px; background:#3a3220; border-radius:3px; margin-top:10px; overflow:hidden; }
  .bar i { display:block; height:100%; background:var(--amber); width:0; transition:width .5s; }
  .wide { width:100%; margin-top:12px; padding:14px; border-radius:12px; font-size:16px; font-weight:600; cursor:pointer; }
  #cancel { border:0; background:#3a3f4b; color:#fff; }
  #force { display:none; border:2px solid var(--red); background:transparent; color:var(--red); }
  .bedrow { display:flex; gap:8px; align-items:center; }
  .bedrow input { flex:1; min-width:0; background:var(--card2); color:var(--fg); border:0; border-radius:12px;
                  padding:12px; font-size:16px; color-scheme:dark; }
  .bedrow button { border:0; border-radius:12px; padding:12px 14px; font-size:15px; font-weight:600;
                   background:var(--card2); color:var(--fg); cursor:pointer; }
  #bed-save { background:#2c3445; }
  .small { font-size:13px; color:var(--mut); margin-top:8px; }
  details summary { cursor:pointer; color:var(--mut); font-size:14px; }
  #apps { margin:8px 0 0; padding-left:18px; color:var(--fg); font-size:14px; }
  .links { display:flex; flex-direction:column; gap:8px; margin-top:10px; }
  .links button { border:0; border-radius:12px; padding:12px; background:var(--card2); color:var(--fg);
                  font-size:15px; font-weight:600; cursor:pointer; text-align:left; }
  #remove { color:var(--red); }
  #foot { text-align:center; font-size:12px; color:var(--mut); }
  #toast { position:fixed; left:50%; bottom:28px; transform:translateX(-50%); background:#2d323d;
           padding:10px 16px; border-radius:12px; font-size:14px; opacity:0; transition:opacity .3s;
           pointer-events:none; max-width:90vw; text-align:center; }
</style>
</head><body>
<div class="wrap">
  <div id="pcs"></div>

  <div class="card">
    <div class="head">
      <div><h1>PC Power</h1><div class="pc" id="host">__PCNAME__</div></div>
      <div class="badge" id="badge"><span class="dot"></span><span id="msg">Checking...</span></div>
    </div>
    <div class="stats">
      <div class="stat"><b id="s-cpu">–</b><span>CPU</span></div>
      <div class="stat"><b id="s-gpu">–</b><span>GPU</span></div>
      <div class="stat"><b id="s-ram">–</b><span>RAM</span></div>
      <div class="stat"><b id="s-down">–</b><span>Download</span></div>
      <div class="stat"><b id="s-up">–</b><span>Uptime</span></div>
      <div class="stat"><b id="s-temp">–</b><span id="s-temp-l">Temp</span></div>
    </div>
  </div>

  <div class="card" id="pending">
    <div class="t" id="p-title"></div>
    <div class="s" id="p-sub"></div>
    <div class="bar" id="p-bar"><i id="p-fill"></i></div>
    <button class="wide" id="cancel">Cancel</button>
    <button class="wide" id="force">Force now</button>
  </div>

  <div class="card main">
    <button id="power" aria-label="Shut down">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round">
        <path d="M12 3v8"/><path d="M6.4 6.6a8 8 0 1 0 11.2 0"/></svg>
    </button>
    <div id="hint">Tap to shut down</div>
  </div>

  <div class="row">
    <button class="btn" id="restart"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 12a8 8 0 1 1-2.34-5.66"/><path d="M20 4v5h-5"/></svg>Restart</button>
    <button class="btn" id="sleep"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/></svg>Sleep</button>
    <button class="btn" id="lock"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>Lock</button>
  </div>

  <div class="card">
    <p class="label">Shut down later</p>
    <div class="timers">
      <button class="chip" data-min="15">15 min</button>
      <button class="chip" data-min="30">30 min</button>
      <button class="chip" data-min="60">1 h</button>
      <button class="chip" data-min="120">2 h</button>
      <button class="chip idle" id="idle">When downloads &amp; renders finish</button>
    </div>
    <div class="small">Timers wait if a game is open full-screen.</div>
  </div>

  <div class="card">
    <p class="label">Bedtime shutdown</p>
    <div class="bedrow">
      <input type="time" id="bed-time" aria-label="Bedtime">
      <button id="bed-save">Save</button>
      <button id="bed-off">Off</button>
    </div>
    <div class="small" id="bed-info">Off</div>
  </div>

  <div class="card">
    <details><summary id="apps-sum">Open apps</summary><ul id="apps"></ul></details>
  </div>

  <div class="card">
    <details><summary>Your PCs</summary>
      <div class="links">
        <button id="add">+ Add another PC</button>
        <button id="copy">Copy this PC's link</button>
        <button id="remove">Remove this PC from the list</button>
      </div>
      <div class="small">To add a PC: on that PC, right-click the tray icon → <b>Copy phone link</b>, send it to
        your phone, then tap <b>Add another PC</b> and paste it.</div>
    </details>
  </div>
  <div id="foot"></div>
</div>
<div id="toast"></div>
<script>
const $ = id => document.getElementById(id);
const LS = { get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
             set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} } };

// ---- PCs this page knows about: [{key, name, hosts}] -------------------------------
const urlKey = (location.pathname.match(/^\/k\/([^/?#]+)/) || [])[1] ||
               new URLSearchParams(location.search).get("key") || "";
let pcs = LS.get("pcpower_pcs", []);
const oldKey = (() => { try { return localStorage.getItem("pcpower_key"); } catch (e) { return null; } })();
const ownKey = urlKey || oldKey || "";
const uniq = a => a.filter((v, i) => v && a.indexOf(v) === i);
if (ownKey) {
  let own = pcs.find(p => p.key === ownKey);
  if (!own) { own = { key: ownKey, name: "__PCNAME__", hosts: [] }; pcs.unshift(own); }
  own.hosts = uniq([location.origin, ...__HOSTS__, ...own.hosts]);
}
let curKey = urlKey || LS.get("pcpower_cur", null) || (pcs[0] && pcs[0].key);
if (!pcs.find(p => p.key === curKey) && pcs[0]) curKey = pcs[0].key;
const save = () => { LS.set("pcpower_pcs", pcs.map(p => ({ key: p.key, name: p.name, hosts: p.hosts }))); LS.set("pcpower_cur", curKey); };
save();
const cur = () => pcs.find(p => p.key === curKey);

let st = null, fails = 0, expect = null, sawOff = false, offText = null, buzzed = false;

function toast(t) { $("toast").textContent = t; $("toast").style.opacity = 1;
  clearTimeout(toast.h); toast.h = setTimeout(() => $("toast").style.opacity = 0, 2600); }
function badge(cls, text) { $("badge").className = "badge " + cls; $("msg").textContent = text; }
function fmt(s) { s = Math.max(0, Math.round(s)); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
  return h ? h + ":" + String(m).padStart(2, "0") + ":" + String(x).padStart(2, "0") : m + ":" + String(x).padStart(2, "0"); }
function upt(s) { if (s == null) return "–"; const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return d ? d + "d " + h + "h" : h ? h + "h " + m + "m" : m + "m"; }
function rate(b) { return b >= 1048576 ? (b / 1048576).toFixed(1) + " MB/s" : Math.round(b / 1024) + " KB/s"; }
const pct = v => v == null ? "–" : v + "%";
const isTs = b => /\/\/100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\./.test(b || "");

// ---- talking to a PC: try each of its addresses, keep the one that answers ----------
async function call(b, key, path, method, ms) {
  const ctl = new AbortController(); const t = setTimeout(() => ctl.abort(), ms || 3000);
  try {
    const sep = path.includes("?") ? "&" : "?";
    const r = await fetch(b + path + sep + "key=" + encodeURIComponent(key) + "&t=" + Date.now(),
                          { method: method || "GET", signal: ctl.signal, cache: "no-store" });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { const e = new Error(d.error || ("HTTP " + r.status)); e.http = true; throw e; }
    return d;
  } finally { clearTimeout(t); }
}
async function apiFor(pc, path, method, ms) {
  let last = new Error("no address");
  for (const b of uniq([pc._base, ...pc.hosts])) {
    try { const d = await call(b, pc.key, path, method, ms); pc._base = b; return d; }
    catch (e) { if (e.http) throw e; last = e; }
  }
  throw last;
}
const api = (path, method) => apiFor(cur(), path, method);

// ---- PC switcher -----------------------------------------------------------------
function renderPcs() {
  const box = $("pcs");
  box.style.display = pcs.length > 1 ? "flex" : "none";
  box.innerHTML = "";
  pcs.forEach(p => {
    const b = document.createElement("button");
    b.className = "pcchip" + (p.key === curKey ? " sel" : "");
    const dot = document.createElement("span");
    dot.className = "dot" + (p._up === true ? " on" : p._up === false ? " off" : "");
    b.appendChild(dot); b.appendChild(document.createTextNode(p.name || "PC"));
    b.onclick = () => switchTo(p.key);
    box.appendChild(b);
  });
  $("remove").style.display = pcs.length > 1 ? "block" : "none";
}
function switchTo(k) {
  if (k === curKey) return;
  curKey = k; save(); st = null; fails = 0; expect = null; sawOff = false; offText = null;
  $("host").textContent = cur().name || "PC"; badge("", "Checking..."); $("pending").style.display = "none";
  renderPcs(); tick();
}
async function pollOthers() {
  for (const p of pcs) {
    if (p.key === curKey) continue;
    try { await apiFor(p, "/ping", "GET", 2500); p._up = true; } catch (e) { p._up = false; }
  }
  renderPcs();
}

function setButtons(enabled) {
  ["power", "restart", "sleep", "lock", "idle"].forEach(id => $(id).disabled = !enabled);
  document.querySelectorAll(".chip").forEach(b => b.disabled = !enabled);
}

function render() {
  const s = st, pc = cur();
  pc._up = true;
  if (s.host && s.host !== pc.name) { pc.name = s.host; save(); }
  if (s.hosts) { const h = uniq([...pc.hosts, ...s.hosts]); if (h.length !== pc.hosts.length) { pc.hosts = h; save(); } }
  renderPcs();
  $("host").textContent = s.host + " · " + (isTs(pc._base) ? "via Tailscale" : "via Wi-Fi");
  $("s-cpu").textContent = pct(s.cpu); $("s-gpu").textContent = pct(s.gpu); $("s-ram").textContent = pct(s.ram);
  $("s-down").textContent = rate(s.down || 0); $("s-up").textContent = upt(s.uptime);
  const t = s.temps || {};
  if (t.cpu != null || t.gpu != null) {
    $("s-temp").textContent = [t.cpu, t.gpu].filter(v => v != null).map(v => v + "°").join(" / ");
    $("s-temp-l").textContent = t.cpu != null && t.gpu != null ? "CPU / GPU temp" : (t.cpu != null ? "CPU temp" : "GPU temp");
  } else { $("s-temp").textContent = "–"; $("s-temp-l").textContent = "Temp"; }
  const apps = s.apps || [];
  $("apps-sum").textContent = "Open apps (" + apps.length + ")";
  $("apps").innerHTML = ""; apps.forEach(a => { const li = document.createElement("li"); li.textContent = a; $("apps").appendChild(li); });
  if (document.activeElement !== $("bed-time")) $("bed-time").value = s.bedtime || "";
  $("bed-info").textContent = s.bedtime
    ? "Every night at " + s.bedtime + ", shuts down once nobody has used the PC for " + s.bedtime_unused +
      " min. Waits for games; moving the mouse cancels it."
    : "Off. Pick a time to shut the PC down every night when nobody is using it.";
  $("foot").textContent = "PC Power v" + s.version + (s.update ? " · updating to " + s.update + "..." : "");

  const word = s.action === "restart" ? "Restart" : "Shut down";
  const p = $("pending"); $("force").style.display = "none"; $("force").textContent = "Force now";
  $("hint").textContent = ""; $("cancel").style.display = "block"; $("p-bar").style.display = "block";
  if (s.phase === "idle") {
    p.style.display = "none"; badge("on", "PC is on"); setButtons(true);
    $("hint").textContent = s.fullscreen ? s.fullscreen + " is open full-screen" : "Tap to shut down";
  } else if (s.phase === "timer") {
    p.style.display = "block"; setButtons(false);
    badge("busy", word + " in " + fmt(s.seconds_left));
    $("p-title").textContent = (s.who === "bedtime" ? "Bedtime: " : "") + word + " in " + fmt(s.seconds_left);
    $("p-sub").textContent = s.who === "bedtime" ? "Moving the mouse on the PC also cancels it." : "You can still cancel.";
    $("p-bar").style.display = "none";
  } else if (s.phase === "gamewait") {
    p.style.display = "block"; setButtons(false); $("p-bar").style.display = "none";
    badge("busy", "Game open");
    $("p-title").textContent = "Waiting for " + s.game + " to close";
    $("p-sub").textContent = word + " happens 60 seconds after it closes.";
    $("force").textContent = word + " anyway"; $("force").style.display = "block";
  } else if (s.phase === "idlewait") {
    p.style.display = "block"; setButtons(false);
    badge("busy", "Waiting for idle");
    $("p-title").textContent = "Shut down when everything finishes";
    $("p-sub").textContent = s.busy && s.busy.length
      ? "Still busy: " + s.busy.join(", ") + "."
      : "Idle for " + fmt(s.idle_secs) + " of " + fmt(s.idle_needed) + ".";
    $("p-fill").style.width = Math.min(100, 100 * s.idle_secs / s.idle_needed) + "%";
  } else {
    p.style.display = "block"; setButtons(false); $("p-bar").style.display = "none";
    $("p-title").textContent = s.action === "restart" ? "Restarting..." : "Shutting down...";
    if (s.phase === "forcing") { badge("busy", "Forcing..."); $("p-sub").textContent = "Force-closing what's left."; $("cancel").style.display = "none"; }
    else if (s.closing_for > 20) {
      const left = s.auto_force ? Math.max(0, s.auto_force - s.closing_for) : null;
      badge("busy", "Waiting on an app");
      $("p-sub").textContent = "Something is blocking it (probably unsaved work)." +
        (left != null ? " Force-closing in ~" + left + "s." : " Save it on the PC or force it.");
      $("force").style.display = "block";
    } else { badge("busy", "Closing apps..."); $("p-sub").textContent = s.auto_save ? "Office files are saved first." : ""; }
  }
}

function renderOffline() {
  cur()._up = false; renderPcs();
  $("pending").style.display = "none"; setButtons(false);
  ["s-cpu", "s-gpu", "s-ram", "s-down", "s-up", "s-temp"].forEach(id => $(id).textContent = "–");
  if (expect === "off" || expect === "sleep") {
    offText = expect === "sleep" ? "PC is asleep" : "PC is off";
    $("hint").textContent = expect === "sleep" ? "Sleep confirmed ✓" : "Shutdown confirmed ✓";
    if (!buzzed && navigator.vibrate) navigator.vibrate([200, 100, 200]); buzzed = true;
    expect = null;
  } else if (expect === "restart") {
    sawOff = true; badge("busy", "Restarting..."); $("hint").textContent = "Waiting for the PC to come back"; return;
  }
  badge("off", offText || "PC is off / unreachable");
  if (!offText) $("hint").textContent = "Turn the PC on, or check you're on the same network (or Tailscale is on)";
}

let ticking = false;
async function tick() {
  if (ticking || !cur()) return; ticking = true;
  const k = curKey;
  try {
    const s = await api("/status");
    if (k !== curKey) return;
    st = s; fails = 0; offText = null; buzzed = false;
    if (expect === "restart" && sawOff) { expect = null; sawOff = false; toast("PC restarted ✓"); }
    render();
  } catch (e) {
    if (k !== curKey) return;
    if (/403|bad key/.test(String(e.message))) { badge("off", "Bad link"); $("hint").textContent = "Re-scan the QR code from the PC tray icon"; return; }
    if (++fails >= 2) renderOffline();
  } finally { ticking = false; }
}

function appsText() {
  const a = (st && st.apps) || [];
  return a.length ? "\n\nOpen apps:\n• " + a.slice(0, 10).join("\n• ") + (a.length > 10 ? "\n• +" + (a.length - 10) + " more" : "") : "";
}
function howText() {
  if (!st) return "";
  return (st.fullscreen ? "Heads up: " + st.fullscreen + " is open full-screen.\n\n" : "") +
    (st.auto_save ? "Unsaved Word, Excel and PowerPoint files are saved first. " : "") +
    "Apps are asked to close normally." +
    (st.auto_force ? " Anything still blocking after " + st.auto_force + "s is force-closed." : "");
}
const pcName = () => (cur() && cur().name) || "the PC";

async function act(params, confirmText, exp, okMsg) {
  if (confirmText && !confirm(confirmText)) return;
  try { await api("/action?" + params, "POST"); if (exp) expect = exp; if (okMsg) toast(okMsg); tick(); }
  catch (e) { toast("Failed: " + e.message); }
}

$("power").onclick = () => act("do=shutdown", "Shut down " + pcName() + "?\n\n" + howText() + appsText(), "off");
$("restart").onclick = () => act("do=restart", "Restart " + pcName() + "?\n\n" + howText() + appsText(), "restart");
$("sleep").onclick = () => act("do=sleep", "Put " + pcName() + " to sleep?", "sleep", "Going to sleep...");
$("lock").onclick = () => act("do=lock", null, null, "PC locked ✓");
document.querySelectorAll(".chip[data-min]").forEach(b => b.onclick = () =>
  act("do=shutdown&delay=" + (b.dataset.min * 60), "Shut down " + pcName() + " in " + b.textContent + "?", "off"));
$("idle").onclick = () => act("do=shutdown&mode=idle",
  "Shut down " + pcName() + " once downloads, renders and other heavy work are finished?\n\n" +
  "It watches Steam and browser downloads, network, disk, GPU and CPU, and shuts down after " +
  Math.round((st ? st.idle_needed : 300) / 60) + " quiet minutes in a row.", "off");
$("cancel").onclick = async () => { try { const d = await api("/cancel", "POST"); expect = null; toast(d.message || "Cancelled"); tick(); }
                                    catch (e) { toast(e.message); } };
$("force").onclick = async () => {
  const gw = st && st.phase === "gamewait";
  if (!confirm(gw ? "Don't wait for " + st.game + "?\n\nIt's closed like any other app (unsaved game progress may be lost)."
                  : "Force it now?\n\nApps are closed immediately — unsaved work in them is lost.")) return;
  try { await api("/force", "POST"); toast(gw ? "Shutting down..." : "Forcing..."); tick(); } catch (e) { toast(e.message); }
};
$("bed-save").onclick = async () => {
  const v = $("bed-time").value;
  if (!v) { toast("Pick a time first"); return; }
  try { await api("/settings?bedtime=" + encodeURIComponent(v), "POST"); toast("Bedtime set to " + v); tick(); }
  catch (e) { toast(e.message); }
};
$("bed-off").onclick = async () => {
  try { await api("/settings?bedtime=", "POST"); $("bed-time").value = ""; toast("Bedtime off"); tick(); }
  catch (e) { toast(e.message); }
};

// ---- your PCs ----------------------------------------------------------------------
function copyText(t) {
  const ta = document.createElement("textarea"); ta.value = t; ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta); ta.select(); let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) {}
  document.body.removeChild(ta);
  if (!ok) prompt("Copy this link:", t); else toast("Link copied ✓");
}
$("copy").onclick = () => { const p = cur(); const h = p.hosts.find(isTs) || p.hosts[0]; copyText(h + "/k/" + p.key); };
$("add").onclick = async () => {
  const raw = (prompt("Paste the other PC's link\n(on that PC: tray icon → Copy phone link)") || "").trim();
  if (!raw) return;
  let u, key;
  try { u = new URL(raw); key = (u.pathname.match(/^\/k\/([^/?#]+)/) || [])[1] || u.searchParams.get("key"); } catch (e) {}
  if (!u || !key) { toast("That doesn't look like a PC Power link"); return; }
  if (pcs.find(p => p.key === key)) { switchTo(key); toast("Already in your list"); return; }
  const pc = { key, name: "New PC", hosts: [u.origin] };
  try { const s = await apiFor(pc, "/status"); pc.name = s.host; pc.hosts = uniq([u.origin, ...(s.hosts || [])]); }
  catch (e) { toast("Added, but can't reach it right now"); }
  pcs.push(pc); save(); switchTo(key);
};
$("remove").onclick = () => {
  if (pcs.length < 2 || !confirm("Remove " + pcName() + " from this list?\n\n(The PC itself isn't changed.)")) return;
  pcs = pcs.filter(p => p.key !== curKey); curKey = pcs[0].key; save(); st = null; renderPcs(); tick();
};

if (!cur()) { badge("off", "Bad link"); $("hint").textContent = "Re-scan the QR code from the PC tray icon"; }
else { $("host").textContent = cur().name; renderPcs(); tick(); pollOthers(); }
setInterval(tick, 3000); setInterval(pollOthers, 15000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) { tick(); pollOthers(); } });
</script>
</body></html>
""".replace("__PCNAME__", HOST)

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="14" fill="#e5484d"/>
<g fill="none" stroke="#fff" stroke-width="5" stroke-linecap="round">
<path d="M32 14v18"/><path d="M20.7 21.5a16 16 0 1 0 22.6 0"/></g></svg>"""


def manifest():
    return json.dumps({"name": "PC Power", "short_name": "PC Power", "display": "standalone",
                       "background_color": "#0f1115", "theme_color": "#0f1115",
                       "start_url": f"/k/{CFG['key']}",
                       "icons": [{"src": "/icon.png", "sizes": "180x180", "type": "image/png"}]})


def qr_svg(text):
    try:
        import qrcode
        import qrcode.image.svg
        img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=12, border=2)
        return img.to_string(encoding="unicode")
    except Exception:
        return "<p>(QR code unavailable)</p>"


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def setup_page():
    ts = net["ts"]
    lan_link = link_for(net["ip"])
    if ts:
        ts_link = link_for(ts)
        qr_block = f"""<div class="qr">{qr_svg(ts_link)}</div>
<p class="mut">This one code works <b>at home and away</b>. It uses Tailscale, so keep the Tailscale app on your phone
switched on. Once the page is open it also tries your home Wi-Fi address and uses whichever one answers.</p>"""
        links = (f"<p class='mut'>Link: <code>{esc(ts_link)}</code><br>"
                 f"Wi-Fi only (if Tailscale is off): <code>{esc(lan_link)}</code></p>")
    else:
        qr_block = f"""<div class="qr">{qr_svg(lan_link)}</div>
<p class="mut">This link works on your home Wi-Fi. To use it from anywhere, install
<a href='https://tailscale.com/download'>Tailscale</a> on this PC and your phone, then reopen this page.</p>"""
        links = f"<p class='mut'>Link: <code>{esc(lan_link)}</code></p>"
    topic = CFG["ntfy_topic"]
    ntfy = (f"""<h2>Push notifications (optional)</h2>
<p>Get a notification when the PC turns on, shuts down, or has to force-close an app.</p>
<ol><li>Install the free <b>ntfy</b> app (<a href="https://play.google.com/store/apps/details?id=io.heckel.ntfy">Android</a> /
<a href="https://apps.apple.com/app/ntfy/id1625396347">iPhone</a>).</li>
<li>Tap <b>+</b> and subscribe to this topic: <code class="big">{esc(topic)}</code></li>
<li><button onclick="fetch('/testpush',{{method:'POST'}}).then(()=>this.textContent='Sent ✓')">Send a test notification</button></li></ol>
<p class="mut">Keep the topic name private: anyone who knows it can read these notifications.</p>"""
            if CFG.get("push_notifications") else "")
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>PC Power - phone setup</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;background:#0f1115;color:#e8eaed;display:flex;justify-content:center;padding:32px 16px}}
.c{{max-width:560px}} .qr{{background:#fff;display:inline-block;padding:12px;border-radius:14px;margin:12px 0}}
.qr svg{{width:240px;height:240px;display:block}} code{{background:#1a1d24;padding:6px 9px;border-radius:8px;word-break:break-all}}
code.big{{font-size:18px;user-select:all}} a{{color:#6ea8fe}} .mut{{color:#9aa0a6;font-size:14px}} h2{{margin-top:36px;font-size:20px}}
button{{background:#30a46c;color:#fff;border:0;border-radius:8px;padding:8px 14px;font-size:15px;cursor:pointer}}
ol{{padding-left:20px}} li{{margin:6px 0}} .two{{display:flex;gap:28px;flex-wrap:wrap}} .two>div{{flex:1;min-width:240px}}
h3{{margin:8px 0 0;font-size:17px}}</style></head><body><div class="c">
<h1>PC Power: set up your phone</h1>
<p>Scan with your phone's camera:</p>
{qr_block}
<ol><li>Open the link from the QR code.</li>
<li><b>iPhone (Safari):</b> Share → <i>Add to Home Screen</i>. <b>Android (Chrome):</b> ⋮ → <i>Add to Home screen</i>.</li>
<li>Done. Tap the red <b>PC Power</b> icon anytime.</li></ol>
{links}
<p class="mut">Page won't load on the phone? Tray icon → <i>Repair firewall</i>, and make sure this PC's Wi-Fi is set to <b>Private</b>
(Settings → Network &amp; internet → Wi-Fi → your network).</p>
{ntfy}
<h2>Good to know</h2>
<ul class="mut"><li>Reopen this page anytime: tray icon (by the clock) → <i>Phone setup</i>.</li>
<li>Settings: tray icon → <i>Settings</i> (opens config.json; restart PC Power after editing).</li>
<li>Lost your phone or shared the link by mistake? Tray icon → <i>Make new phone link</i>.</li></ul>
</div></body></html>"""


_png = {}


def icon_png():
    if "png" not in _png:
        import io
        buf = io.BytesIO()
        make_icon_image(180, square=True).save(buf, "PNG")
        _png["png"] = buf.getvalue()
    return _png["png"]


# ---------------------------------------------------------------- web server
class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        # lets the page loaded from one address talk to the other (every action still needs the key)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj))

    def _parse(self):
        u = urllib.parse.urlparse(self.path)
        return u.path, {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}

    def _authed(self, q):
        return secrets.compare_digest(q.get("key", ""), CFG["key"])

    def _local(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def do_GET(self):
        path, q = self._parse()
        if path == "/ping":
            return self._json(200, {"ok": True})
        if path == "/icon.svg":
            return self._send(200, ICON_SVG, "image/svg+xml")
        if path == "/icon.png":
            try:
                return self._send(200, icon_png(), "image/png")
            except Exception:
                return self._json(404, {"error": "no png"})
        if path == "/setup" and self._local():
            return self._send(200, setup_page(), "text/html; charset=utf-8")
        if path == "/" or path.startswith("/k/"):
            # The page holds no secrets; every action it takes is checked against the key.
            return self._send(200, PAGE.replace("__HOSTS__", json.dumps(all_hosts())), "text/html; charset=utf-8")
        if not self._authed(q):
            return self._json(403, {"error": "bad key"})
        if path == "/manifest.json":
            return self._send(200, manifest(), "application/manifest+json")
        if path == "/status":
            return self._json(200, status())
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path, q = self._parse()
        if self._local() and path == "/quit":
            self._json(200, {"ok": True})
            threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True).start()
            return
        if self._local() and path == "/testpush":
            push(f"{HOST}: test", "Notifications are working ✓", tags="white_check_mark")
            return self._json(200, {"ok": True})
        if not self._authed(q):
            log(f"Rejected {path} from {self.client_address[0]} (bad key)")
            return self._json(403, {"error": "bad key"})
        who = f"phone {self.client_address[0]}"
        if path == "/action":
            do = q.get("do")
            if do in ("shutdown", "restart"):
                delay = q.get("delay")
                try:
                    delay = int(delay) if delay is not None else None
                except ValueError:
                    return self._json(400, {"error": "bad delay"})
                ok, err = schedule(do, delay, mode=q.get("mode", "timer"), who=who)
            elif do == "sleep":
                ok, err = sleep_pc(who)
            elif do == "lock":
                ok, err = lock_pc(who)
            else:
                return self._json(400, {"error": "unknown action"})
            return self._json(200 if ok else 409, {"ok": True} if ok else {"error": err})
        if path == "/settings":
            ok, err = set_settings(q)
            return self._json(200 if ok else 400, {"ok": True} if ok else {"error": err})
        if path == "/cancel":
            ok, msg = cancel(who)
            return self._json(200 if ok else 409, {"ok": True, "message": msg} if ok else {"error": msg})
        if path == "/force":
            ok, err = force(who)
            return self._json(200 if ok else 500, {"ok": True} if ok else {"error": err})
        self._json(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        pass


# ---------------------------------------------------------------- install / uninstall (Windows)
def startup_shortcut():
    return pathlib.Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Startup\PC Power.lnk"


def launch_target():
    """(program, args) that starts PC Power in the background."""
    if FROZEN:
        return str(INSTALLED_EXE), ""
    pyw = pathlib.Path(sys.executable).with_name("pythonw.exe")
    return str(pyw if pyw.exists() else sys.executable), f'"{pathlib.Path(__file__).resolve()}"'


def make_shortcut():
    target, args = launch_target()
    psq = lambda s: s.replace("'", "''")
    ps(f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{psq(str(startup_shortcut()))}');"
       f"$s.TargetPath='{psq(target)}'; $s.Arguments='{psq(args)}';"
       f"$s.WorkingDirectory='{psq(str(DATA))}'; $s.Description='PC Power'; $s.Save()")


def run_elevated(lines):
    """Run cmd lines as admin with ONE UAC prompt. Returns True if it ran."""
    bat = pathlib.Path(tempfile.gettempdir()) / f"pcpower_{secrets.token_hex(4)}.cmd"
    bat.write_text("@echo off\r\n" + "\r\n".join(lines) + "\r\n", encoding="utf-8")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            f"try {{ Start-Process -FilePath '{bat}' -Verb RunAs -WindowStyle Hidden -Wait; exit 0 }} catch {{ exit 1 }}"],
                           creationflags=NO_WINDOW, timeout=180)
        return r.returncode == 0
    except Exception:
        return False
    finally:
        try:
            bat.unlink()
        except OSError:
            pass


def firewall_ok():
    r = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={FW_RULE}"],
                       capture_output=True, creationflags=NO_WINDOW)
    return r.returncode == 0


def setup_firewall():
    ok = run_elevated([
        # clean up the older "Remote Shutdown" version if present
        'netsh advfirewall firewall delete rule name="Remote Shutdown" >nul 2>&1',
        'schtasks /delete /f /tn "Remote Shutdown" >nul 2>&1',
        f'netsh advfirewall firewall delete rule name="{FW_RULE}" >nul 2>&1',
        f'netsh advfirewall firewall delete rule name="{FW_RULE_TS}" >nul 2>&1',
        # If someone clicked Cancel on Windows' "allow this app?" prompt, Windows silently
        # adds BLOCK rules for the program, and those beat any allow rule. Remove them.
        'powershell -NoProfile -Command "Get-NetFirewallApplicationFilter | Where-Object { $_.Program -match \'PCPower|python\' } | '
        'Get-NetFirewallRule | Where-Object { $_.Direction -eq \'Inbound\' -and $_.Action -eq \'Block\' } | Remove-NetFirewallRule"',
        f'netsh advfirewall firewall add rule name="{FW_RULE}" dir=in action=allow protocol=TCP localport={PORT} profile=private',
        f'netsh advfirewall firewall add rule name="{FW_RULE_TS}" dir=in action=allow protocol=TCP localport={PORT} profile=any remoteip=100.64.0.0/10',
    ])
    log("Firewall rules added" if ok else "Firewall setup was declined or failed")
    return ok


def stop_running_instance():
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/quit", data=b"", method="POST")
        urllib.request.urlopen(req, timeout=3).read()
        time.sleep(1.5)
    except Exception:
        pass
    # older script version (Remote Shutdown) has no /quit
    try:
        ps("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*remote_shutdown.py*' -and "
           f"$_.ProcessId -ne {os.getpid()} }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}")
    except Exception:
        pass


def install_if_needed():
    """First run / update: copy exe to %LOCALAPPDATA%\\PCPower, add to Startup, firewall.
    Returns True if this process should exit (a new installed copy was launched)."""
    if not IS_WIN:
        return False
    old_link = pathlib.Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Startup\Remote Shutdown.lnk"
    if old_link.exists():
        try:
            old_link.unlink()
        except OSError:
            pass

    if FROZEN and pathlib.Path(sys.executable).resolve() != INSTALLED_EXE.resolve():
        stop_running_instance()
        for _ in range(10):
            try:
                shutil.copy2(sys.executable, INSTALLED_EXE)
                break
            except OSError:
                time.sleep(1)
        else:
            log("Couldn't copy exe into place")
            return False
        make_shortcut()
        auto = "--update" in sys.argv
        if not auto or not firewall_ok():
            setup_firewall()  # manual install/update: also clears stale block rules (one UAC prompt)
        subprocess.Popen([str(INSTALLED_EXE)] + ([] if auto else ["--setup"]), cwd=str(DATA))
        log(f"{'Auto-updated' if auto else 'Installed'} to {INSTALLED_EXE} (v{APP_VERSION})")
        return True

    # running as the installed copy: remove a finished auto-update download
    for leftover in (UPDATE_FILE, UPDATE_FILE.with_suffix(".part")):
        try:
            leftover.unlink()
        except OSError:
            pass
    if not startup_shortcut().exists():
        make_shortcut()
    if not firewall_ok():
        setup_firewall()
    return False


def uninstall():
    import ctypes
    if ctypes.windll.user32.MessageBoxW(0, "Remove PC Power from this PC?\n\nThe phone shortcut will stop working.",
                                        APP, 0x4 | 0x20) != 6:  # Yes/No
        return
    try:
        startup_shortcut().unlink()
    except OSError:
        pass
    run_elevated([f'netsh advfirewall firewall delete rule name="{FW_RULE}" >nul 2>&1',
                  f'netsh advfirewall firewall delete rule name="{FW_RULE_TS}" >nul 2>&1'])
    log("Uninstalled")
    subprocess.Popen(f'cmd /c timeout /t 3 /nobreak >nul & rmdir /s /q "{DATA}"',
                     creationflags=NO_WINDOW, shell=True)
    os._exit(0)


# ---------------------------------------------------------------- tray icon
def make_icon_image(size=64, square=False):
    from PIL import Image, ImageDraw
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if square:
        d.rounded_rectangle((0, 0, s - 1, s - 1), radius=s // 5, fill=(229, 72, 77, 255))
    else:
        d.ellipse((0, 0, s - 1, s - 1), fill=(48, 164, 108, 255))  # green = running
    w = max(2, s // 10)
    pad = s * 0.24
    d.arc((pad, pad, s - pad, s - pad), start=-60, end=240, fill="white", width=w)
    d.line((s / 2, s * 0.16, s / 2, s * 0.5), fill="white", width=w)
    return img


def local_setup_url():
    return f"http://127.0.0.1:{PORT}/setup"


def tooltip():
    where = f"{net['ip']}" + (" + Tailscale" if net["ts"] else "")
    return f"PC Power v{APP_VERSION} - running ({where})"


def new_link():
    CFG["key"] = secrets.token_urlsafe(16)
    save_config(CFG)
    refresh_links()
    log("New phone link created - old links no longer work")
    notify("New phone link created. Re-add the shortcut on your phone.")
    webbrowser.open(local_setup_url())


def copy_link():
    subprocess.run(["clip"], input=phone_link(), text=True, creationflags=NO_WINDOW)
    notify("Phone link copied to clipboard")


def run_tray():
    import pystray
    M = pystray.MenuItem

    def busy(_):
        return bool(job or closing)

    def on_quit(icon, _):
        log("Stopped from tray")
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(
        M(lambda _: tooltip(), None, enabled=False),
        pystray.Menu.SEPARATOR,
        M("Phone setup (QR code)", lambda *_: webbrowser.open(local_setup_url()), default=True),
        M("Copy phone link", lambda *_: copy_link()),
        M("Cancel shutdown", lambda *_: cancel("tray"), visible=busy),
        pystray.Menu.SEPARATOR,
        M("Make new phone link", lambda *_: new_link()),
        M("Settings", lambda *_: subprocess.Popen(["notepad.exe", str(CONFIG_FILE)])),
        M("Open log", lambda *_: subprocess.Popen(["notepad.exe", str(LOG_FILE)])),
        M("Repair firewall", lambda *_: threading.Thread(target=setup_firewall, daemon=True).start()),
        M("Check for updates", lambda *_: threading.Thread(
            target=check_for_update, kwargs={"manual": True}, daemon=True).start()),
        pystray.Menu.SEPARATOR,
        M("Uninstall", lambda *_: uninstall()),
        M("Quit", on_quit),
    )
    icon = pystray.Icon("PCPower", make_icon_image(64), tooltip(), menu)
    tray["icon"] = icon
    icon.run()


# ---------------------------------------------------------------- main
def main():
    if "--make-icon" in sys.argv:
        img = make_icon_image(256, square=True)
        img.save(HERE / "pcpower.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        return

    if install_if_needed():
        return

    try:
        server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError:
        # already running - just show the setup page
        webbrowser.open(local_setup_url())
        return

    refresh_links()
    log(f"Started - LAN {net['ip']}  Tailscale {net['ts']}")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=sampler, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=updater, daemon=True).start()
    push(f"{HOST} is on", "PC Power is running.", tags="green_circle", retries=8)

    if "--setup" in sys.argv:
        webbrowser.open(local_setup_url())

    if os.environ.get("PCPOWER_NO_TRAY"):
        print(phone_link(), flush=True)
        threading.Event().wait()
    else:
        run_tray()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("CRASH:\n" + traceback.format_exc())
        raise
