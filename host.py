"""BotKit multi-client host: one bot process per client folder, supervised.

Layout (everything relative to this file):
    botkit/                 shared BotKit code (bot.py, cogs/, setup_wizard.py)
    clients/<name>/         config.json + .env (+ logs/bot.log*) for one client
    state/                  status.json, host.log, libretranslate.log, lock files

Commands:
    python host.py run                   supervise every client (Task Scheduler / systemd runs this)
    python host.py status                (alias: list) show clients, PIDs, restarts, supervisor health
    python host.py add NAME [wizard args] create clients/NAME and run setup_wizard.py into it
    python host.py start|stop|restart NAME
    python host.py logs NAME [-n 40]
    python host.py dashboard             local status page on 127.0.0.1:8765 (see HOSTING.md)
    python host.py lt [--port 5000]      run LibreTranslate on 127.0.0.1 under the same supervision

Control is file based, so the CLI and the dashboard work whether or not the supervisor is running:
    clients/NAME/.disabled   -> bot is stopped and stays stopped (survives reboots)
    clients/NAME/.restart    -> supervisor restarts the bot once, then deletes the file
Crashed bots are restarted with exponential backoff (5s, 10s, 20s ... max 300s; reset after
a bot has stayed up for 10 minutes). Logs rotate at 5 MB, keeping 5 old files.
Tokens are never read or printed by this script; each bot loads its own .env.
The dashboard log tail redacts token-shaped text and never opens a client's .env.
"""
import argparse
import html
import json
import logging
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
FORBIDDEN_PORTS = {53, 80, 3000, 5055}
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
IS_WIN = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000
LT_LANGS = "en,es,fr,de,pt,ja"
log = logging.getLogger("host")


# ------------------------------------------------------------------ paths / discovery
class Paths:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.code = self.root / "botkit"
        self.clients = self.root / "clients"
        self.state = self.root / "state"

    def client(self, name):
        return self.clients / name


def valid_name(name):
    return bool(NAME_RE.match(name or ""))


def client_ready(d):
    return (d / "config.json").is_file() and (d / ".env").is_file()


def discover(paths):
    """Return {name: dir} for every client folder that has config.json and .env (enabled or not)."""
    out = {}
    if paths.clients.is_dir():
        for d in sorted(paths.clients.iterdir()):
            if d.is_dir() and valid_name(d.name) and client_ready(d):
                out[d.name] = d
    return out


def child_python():
    """Console-less interpreter for children: python.exe next to pythonw.exe on Windows."""
    exe = Path(sys.executable)
    if IS_WIN and exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        return str(exe.with_name("python.exe"))
    return str(exe)


def backoff_delay(failures, base=5.0, cap=300.0):
    if failures <= 0:
        return 0.0
    return min(cap, base * (2 ** (failures - 1)))


# ------------------------------------------------------------------ logging helpers
class RotatingLog:
    """Size-rotated, thread-safe byte log (bot output is written verbatim)."""

    def __init__(self, path, max_bytes=5 * 1024 * 1024, backups=5):
        self.path, self.max_bytes, self.backups = Path(path), max_bytes, backups
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.f = open(self.path, "ab")

    def _rotate(self):
        self.f.close()
        try:
            for i in range(self.backups - 1, 0, -1):
                src, dst = self.path.with_name(f"{self.path.name}.{i}"), self.path.with_name(f"{self.path.name}.{i + 1}")
                if src.exists():
                    os.replace(src, dst)
            os.replace(self.path, self.path.with_name(self.path.name + ".1"))
        except OSError:
            pass  # file held open elsewhere (e.g. a viewer); keep appending and retry next time
        self.f = open(self.path, "ab")

    def write(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        with self.lock:
            if self.f.tell() + len(data) > self.max_bytes and self.f.tell() > 0:
                self._rotate()
            self.f.write(data)
            self.f.flush()

    def note(self, msg):
        self.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [host] {msg}\n")

    def close(self):
        with self.lock:
            self.f.close()


# ------------------------------------------------------------------ Windows job object
class KillOnCloseJob:
    """Children die with the supervisor (even if it is killed), so a restart never duplicates a bot."""

    def __init__(self):
        self.handle = None
        if not IS_WIN:
            return
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
                _fields_ = [(n, ctypes.c_ulonglong) for n in ("a", "b", "c", "d", "e", "f")]

            class EXT(ctypes.Structure):
                _fields_ = [("Basic", BASIC), ("Io", IOC), ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                            ("PeakJobMemoryUsed", ctypes.c_size_t)]

            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
            k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            h = k32.CreateJobObjectW(None, None)
            info = EXT()
            info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if h and k32.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)):
                self.handle, self._k32, self._wt = h, k32, wintypes
        except Exception as e:  # never fatal
            log.warning("job object unavailable: %s", e)

    def assign(self, proc):
        if self.handle:
            if not self._k32.AssignProcessToJobObject(self.handle, self._wt.HANDLE(int(proc._handle))):
                log.warning("could not assign pid %s to job", proc.pid)


# ------------------------------------------------------------------ one supervised process
class Managed:
    STABLE_AFTER = 600.0  # seconds up before the failure counter resets

    def __init__(self, name, cmd, cwd, env, log_path, job=None, base=5.0, cap=300.0, clock=time.time):
        self.name, self.cmd, self.cwd, self.env = name, cmd, cwd, env
        self.log = RotatingLog(log_path)
        self.job, self.base, self.cap, self.clock = job, base, cap, clock
        self.proc = None
        self.started = None
        self.failures = 0
        self.restarts = 0
        self.last_exit = None
        self.next_start = 0.0
        self.state = "stopped"

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        kw = dict(cwd=str(self.cwd), env=self.env, stdin=subprocess.DEVNULL,
                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if IS_WIN:
            kw["creationflags"] = CREATE_NO_WINDOW
        try:
            self.proc = subprocess.Popen(self.cmd, **kw)
        except OSError as e:
            self.log.note(f"failed to start: {e}")
            self._failed(None)
            return
        if self.job:
            self.job.assign(self.proc)
        self.started = self.clock()
        self.state = "running"
        self.log.note(f"started pid {self.proc.pid}")
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc):
        for line in iter(proc.stdout.readline, b""):
            self.log.write(line)
        proc.stdout.close()

    def _failed(self, code):
        now = self.clock()
        if self.started and now - self.started >= self.STABLE_AFTER:
            self.failures = 0
        self.failures += 1
        self.last_exit = code
        delay = backoff_delay(self.failures, self.base, self.cap)
        self.next_start = now + delay
        self.state = "backoff"
        self.proc, self.started = None, None
        self.log.note(f"exited with code {code}; restarting in {delay:.0f}s (failure #{self.failures})")

    def poll(self):
        """Detect a crash/exit and schedule the restart. Returns True if it just exited."""
        if self.proc is not None and self.proc.poll() is not None:
            self._failed(self.proc.returncode)
            return True
        return False

    def stop(self, reason="stop requested", timeout=10):
        if self.running:
            self.log.note(f"stopping pid {self.proc.pid} ({reason})")
            self.proc.terminate()
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
            self.last_exit = self.proc.returncode
        self.proc, self.started = None, None
        self.state = "stopped"

    def tick(self, enabled=True):
        """Drive the state machine once."""
        if not enabled:
            if self.running:
                self.stop("disabled")
            self.state, self.failures = "disabled", 0
            return
        self.poll()
        if self.proc is None and self.clock() >= self.next_start:
            if self.state == "backoff":
                self.restarts += 1
            self.start()

    def info(self):
        return {"state": self.state, "pid": self.proc.pid if self.running else None,
                "started": self.started, "restarts": self.restarts, "failures": self.failures,
                "last_exit": self.last_exit,
                "next_start": self.next_start if self.state == "backoff" else None}


# ------------------------------------------------------------------ the supervisor
def bot_env(client_dir, base_env=None):
    env = dict(os.environ if base_env is None else base_env)
    # never let a token from the supervisor's own environment leak into a client
    for k in ("DISCORD_TOKEN", "DEEPL_API_KEY", "LIBRETRANSLATE_API_KEY"):
        env.pop(k, None)
    env.update(BOTKIT_CONFIG=str(client_dir / "config.json"), BOTKIT_ENV=str(client_dir / ".env"),
               PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    return env


class Supervisor:
    def __init__(self, paths, python=None, base=5.0, cap=300.0, clock=time.time):
        self.paths, self.python = paths, python or child_python()
        self.base, self.cap, self.clock = base, cap, clock
        self.job = KillOnCloseJob()
        self.bots = {}
        self.stopping = False

    def bot_cmd(self, client_dir):
        return [self.python, str(self.paths.code / "bot.py")]

    def _managed(self, name, d):
        m = self.bots.get(name)
        if m is None:
            m = Managed(name, self.bot_cmd(d), self.paths.code, bot_env(d), d / "logs" / "bot.log",
                        job=self.job, base=self.base, cap=self.cap, clock=self.clock)
            self.bots[name] = m
        return m

    def tick(self):
        found = discover(self.paths)
        for name in list(self.bots):
            if name not in found:  # folder removed or incomplete
                self.bots[name].stop("client folder removed")
                self.bots.pop(name).log.close()
        for name, d in found.items():
            m = self._managed(name, d)
            restart = d / ".restart"
            if restart.exists():
                try:
                    restart.unlink()
                except OSError:
                    pass
                if m.running:
                    m.stop("restart requested")
                m.failures, m.next_start, m.state = 0, 0.0, "stopped"
            m.tick(enabled=not (d / ".disabled").exists())
        self.write_status()

    def write_status(self):
        self.paths.state.mkdir(parents=True, exist_ok=True)
        data = {"supervisor_pid": os.getpid(), "updated": self.clock(),
                "clients": {n: m.info() for n, m in self.bots.items()}}
        tmp = self.paths.state / "status.json.tmp"
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        for _ in range(5):
            try:
                os.replace(tmp, self.paths.state / "status.json")
                break
            except PermissionError:  # reader has it open on Windows
                time.sleep(0.05)

    def shutdown(self):
        self.stopping = True
        for m in self.bots.values():
            m.stop("supervisor shutting down")
        self.write_status()

    def run(self, interval=1.0):
        while not self.stopping:
            try:
                self.tick()
            except Exception:
                log.exception("tick failed")
            time.sleep(interval)


# ------------------------------------------------------------------ single-instance lock
class InstanceLock:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = None

    def acquire(self):
        f = open(self.path, "a+")
        try:
            if IS_WIN:
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()))
        f.flush()
        self.f = f
        return True


def setup_file_logging(path):
    h = RotatingFileHandler(path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(h)
    logging.getLogger().setLevel(logging.INFO)


def _install_signal_handlers(stop):
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda *_: stop())
        except (ValueError, OSError):
            pass


# ------------------------------------------------------------------ commands
def cmd_run(paths, args):
    paths.state.mkdir(parents=True, exist_ok=True)
    setup_file_logging(paths.state / "host.log")
    lock = InstanceLock(paths.state / "host.lock")
    if not lock.acquire():
        log.info("another supervisor is already running; exiting")
        print("Another supervisor is already running.", file=sys.stderr)
        return 3
    sup = Supervisor(paths)
    _install_signal_handlers(sup.shutdown)
    log.info("supervisor started pid %s, python %s", os.getpid(), sup.python)
    try:
        sup.run()
    finally:
        sup.shutdown()
        log.info("supervisor stopped")
    return 0


def read_status(paths):
    try:
        return json.loads((paths.state / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def fmt_age(seconds):
    seconds = int(max(0, seconds))
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= n:
            return f"{seconds // n}{unit}"
    return f"{seconds}s"


def supervisor_alive(st, now):
    return bool(st) and now - st.get("updated", 0) < 15


def supervisor_line(st, now, alive):
    if st:
        return (f"supervisor: {'running' if alive else 'NOT running (stale status)'} "
                f"(pid {st.get('supervisor_pid')}, updated {fmt_age(now - st.get('updated', 0))} ago)")
    return "supervisor: never ran (no state/status.json)"


# Friendly words for the dashboard. The raw value stays the supervisor's state word.
_STATE_LABEL = {"running": "running", "disabled": "stopped", "stopped": "stopped",
                "backoff": "crashed", "-": "unknown"}


def status_view(paths, now=None):
    """Same client list as `host.py status`: live fields only while the supervisor is fresh."""
    now = time.time() if now is None else now
    st = read_status(paths)
    alive = supervisor_alive(st, now)
    clients = st.get("clients", {}) if st else {}
    names = set(clients)
    if paths.clients.is_dir():
        names |= {d.name for d in paths.clients.iterdir() if d.is_dir()}
    rows = []
    for n in sorted(names):
        d = paths.client(n)
        c = clients.get(n, {}) if alive else {}
        notes = []
        if not (d / "config.json").is_file():
            notes.append("missing config.json")
        if not (d / ".env").is_file():
            notes.append("missing .env")
        if (d / ".disabled").exists():
            notes.append("stopped by user")
        if c.get("last_exit") is not None:
            notes.append(f"last exit {c['last_exit']}")
        if c.get("next_start"):
            notes.append(f"retry in {max(0, int(c['next_start'] - now))}s")
        state = c.get("state", "disabled" if (d / ".disabled").exists() else "-")
        rows.append({
            "name": n,
            "state": state,
            "label": _STATE_LABEL.get(state, state),
            "pid": c.get("pid"),
            "uptime": fmt_age(now - c["started"]) if c.get("started") else "",
            "restarts": c.get("restarts", ""),
            "last_exit": c.get("last_exit"),
            "notes": notes,
            "ready": client_ready(d),
            "controllable": valid_name(n) and d.is_dir(),
        })
    return {"status": st, "now": now, "alive": alive, "rows": rows}


def cmd_status(paths, args):
    view = status_view(paths)
    print(supervisor_line(view["status"], view["now"], view["alive"]))
    if not view["rows"]:
        print("no clients. Add one with: host.py add NAME")
        return 0
    print(f"{'client':<20} {'state':<10} {'pid':>7} {'uptime':>8} {'restarts':>8}  notes")
    for r in view["rows"]:
        print(f"{r['name']:<20} {r['state']:<10} {r['pid'] or '':>7} {r['uptime']:>8} "
              f"{r['restarts']:>8}  {', '.join(r['notes'])}")
    return 0


def _need_client(paths, name):
    if not valid_name(name):
        raise SystemExit(f"invalid client name {name!r} (letters, digits, - and _, max 40)")
    d = paths.client(name)
    if not d.is_dir():
        raise SystemExit(f"no such client: {name} (folders live in {paths.clients})")
    return d


def _supervisor_alive(paths):
    return supervisor_alive(read_status(paths), time.time())


def apply_control(paths, name, action):
    """Write the same control files as `host.py start|stop|restart`.

    The running supervisor applies them on its next tick. This does not spawn a bot itself.
    """
    d = _need_client(paths, name)
    if action == "start":
        if not client_ready(d):
            raise SystemExit(f"{name} needs config.json and .env first (host.py add {name})")
        (d / ".disabled").unlink(missing_ok=True)
    elif action == "stop":
        (d / ".disabled").write_text("stopped by host.py stop\n", encoding="utf-8")
    elif action == "restart":
        (d / ".disabled").unlink(missing_ok=True)
        (d / ".restart").write_text("1\n", encoding="utf-8")
    else:
        raise SystemExit(f"unknown action {action!r}")
    return d


def _wait_for(paths, name, pred, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        st = read_status(paths) or {}
        c = st.get("clients", {}).get(name)
        if c and pred(c):
            return c
        time.sleep(0.5)
    return None


def cmd_start(paths, args):
    apply_control(paths, args.name, "start")
    return _report(paths, args.name, "start", lambda c: c.get("state") == "running")


def cmd_stop(paths, args):
    apply_control(paths, args.name, "stop")
    return _report(paths, args.name, "stop", lambda c: c.get("state") == "disabled")


def cmd_restart(paths, args):
    old = ((read_status(paths) or {}).get("clients", {}).get(args.name) or {}).get("pid")
    apply_control(paths, args.name, "restart")
    return _report(paths, args.name, "restart",
                   lambda c: c.get("state") == "running" and c.get("pid") and c.get("pid") != old)


def _report(paths, name, verb, pred):
    if not _supervisor_alive(paths):
        print(f"{verb} recorded for {name}, but the supervisor is not running; it applies when it starts.")
        return 0
    c = _wait_for(paths, name, pred)
    if c:
        print(f"{name}: {c['state']}" + (f" (pid {c['pid']})" if c.get("pid") else ""))
        return 0
    print(f"{name}: {verb} requested; not confirmed yet, check: host.py status / host.py logs {name}")
    return 1


def cmd_add(paths, args, runner=subprocess.call):
    if not valid_name(args.name):
        raise SystemExit(f"invalid client name {args.name!r} (letters, digits, - and _, max 40)")
    d = paths.client(args.name)
    (d / "logs").mkdir(parents=True, exist_ok=True)
    if args.no_start:
        (d / ".disabled").write_text("created with --no-start\n", encoding="utf-8")
    wizard_args = list(args.wizard_args)
    if wizard_args and wizard_args[0] == "--":
        wizard_args = wizard_args[1:]
    cmd = [sys.executable, str(paths.code / "setup_wizard.py"),
           "--config-out", str(d / "config.json"), "--env-out", str(d / ".env")] + wizard_args
    rc = runner(cmd, cwd=str(paths.code))
    if rc != 0:
        print(f"setup wizard exited with {rc}; {d} left in place (fix and re-run add, or delete it)")
        return rc
    if client_ready(d):
        print(f"client {args.name} ready in {d}")
        print("It starts automatically within a few seconds if the supervisor is running"
              + (" (created stopped: run host.py start " + args.name + ")" if args.no_start else "") + ".")
    else:
        print(f"wizard finished but {d} still lacks config.json or .env (e.g. --no-env); add it before starting")
    return 0


def cmd_logs(paths, args):
    _need_client(paths, args.name)
    text = client_log_tail(paths, args.name, args.n)
    if text is None:
        print("(no log yet)")
        return 0
    print(text)
    return 0


def lt_command(python, host, port, langs, threads):
    boot = ("import sys; from libretranslate.main import main; "
            "sys.argv = ['libretranslate'] + sys.argv[1:]; main()")
    return [python, "-c", boot, "--host", host, "--port", str(port),
            "--load-only", langs, "--threads", str(threads)]


def cmd_lt(paths, args):
    if args.host not in LOOPBACK:
        raise SystemExit("refusing to bind LibreTranslate to a non-loopback address (LAN-only policy)")
    if args.port in FORBIDDEN_PORTS:
        raise SystemExit(f"port {args.port} is reserved on this machine")
    paths.state.mkdir(parents=True, exist_ok=True)
    setup_file_logging(paths.state / "lt-host.log")
    lock = InstanceLock(paths.state / "lt.lock")
    if not lock.acquire():
        log.info("LibreTranslate runner already running; exiting")
        return 3
    python = args.python or child_python()
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    m = Managed("libretranslate", lt_command(python, args.host, args.port, args.langs, args.threads),
                paths.root, env, paths.state / "libretranslate.log", job=KillOnCloseJob())
    stop = threading.Event()
    _install_signal_handlers(stop.set)
    log.info("LibreTranslate runner pid %s -> %s:%s", os.getpid(), args.host, args.port)
    try:
        while not stop.is_set():
            m.tick()
            (paths.state / "lt-status.json").write_text(json.dumps(dict(m.info(), updated=time.time(),
                                                        runner_pid=os.getpid())), encoding="utf-8")
            stop.wait(2)
    finally:
        m.stop("runner shutting down")
    return 0


# ------------------------------------------------------------------ log redaction / dashboard
# Secret assignments (DISCORD_TOKEN=, api_key:, "token": "..."). A hyphenated word
# such as "token-in-env:" is not a key, so ordinary bot log lines stay readable.
_REDACTED = "[redacted]"
_SECRET_ASSIGN = re.compile(
    r"(?i)([\"']?[A-Za-z0-9_]*(?:token|secret|password|api_key|apikey)[\"']?\s*[=:]\s*[\"']?)"
    r"([^\s\"',}&]+)"
)
_BEARER = re.compile(r"(?i)((?:authorization:\s*)?bearer\s+)([A-Za-z0-9._\-]{12,})")
_TOKEN_SHAPE = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"mfa\.[A-Za-z0-9_\-]{20,}"
    r"|sk-[A-Za-z0-9]{20,}"
    r"|xox[baprs]-[A-Za-z0-9\-]{10,}"
    r"|[A-Za-z0-9_\-]{24,}\.[A-Za-z0-9_\-]{6}\.[A-Za-z0-9_\-]{27,}"
    r")",
    re.IGNORECASE,
)


def redact_secrets(text):
    """Remove token-shaped strings and secret assignments. Ordinary log lines stay."""
    if not text:
        return text or ""
    text = _SECRET_ASSIGN.sub(lambda m: m.group(1) + _REDACTED, text)
    text = _BEARER.sub(lambda m: m.group(1) + _REDACTED, text)
    return _TOKEN_SHAPE.sub(_REDACTED, text)


def _read_tail(path, n):
    max_bytes = min(5 * 1024 * 1024, max(256 * 1024, max(int(n), 1) * 1024))
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        size = os.lseek(fd, 0, os.SEEK_END)
        os.lseek(fd, max(0, size - max_bytes), os.SEEK_SET)
        blob = b""
        while len(blob) < max_bytes:
            chunk = os.read(fd, max_bytes - len(blob))
            if not chunk:
                break
            blob += chunk
    finally:
        os.close(fd)
    text = blob.decode("utf-8", "replace")
    if size > max_bytes:
        cut = text.find("\n")
        if cut != -1:
            text = text[cut + 1:]
    if n <= 0:
        return ""
    return "\n".join(text.splitlines()[-n:])


def client_log_tail(paths, name, n=40):
    """Last n lines of clients/<name>/logs/bot.log, redacted.

    Returns None when there is no log to show. Never opens .env, and refuses to
    follow a symlink so the log path cannot be pointed at one.
    """
    if not valid_name(name):
        return None
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 40
    if n < 0:
        n = 0
    log_file = (paths.client(name) / "logs" / "bot.log")
    try:
        if log_file.is_symlink() or not log_file.is_file():
            return None
        resolved = log_file.resolve()
        logs_dir = (paths.client(name) / "logs").resolve()
        if resolved.parent != logs_dir or resolved.name != "bot.log":
            return None
        return redact_secrets(_read_tail(resolved, n))
    except OSError:
        return None


def _is_loopback(host):
    return (host or "").strip().lower() in LOOPBACK


def dashboard_exposure_warning(host):
    if _is_loopback(host):
        return None
    return (f"WARNING: the dashboard is binding to {host}, not 127.0.0.1. "
            "Anyone who can open that address can see client status and start, stop, or restart bots. "
            "Leave the default (127.0.0.1) unless you intend that. Do not port-forward this port.")


def check_dashboard_bind(host, port):
    if not host or not str(host).strip():
        raise SystemExit("dashboard host is required")
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        raise SystemExit(f"port {port} is not a valid TCP port")
    if port in FORBIDDEN_PORTS:
        raise SystemExit(f"port {port} is reserved on this machine")
    return dashboard_exposure_warning(host)


def _host_from_header(host_header):
    if not host_header:
        return ""
    h = str(host_header).strip()
    if h.startswith("["):
        end = h.find("]")
        return (h[1:end] if end != -1 else h).lower()
    if ":" in h:
        name, _, maybe_port = h.rpartition(":")
        if maybe_port.isdigit():
            return name.lower()
    return h.lower()


def dashboard_origin_allowed(origin, host_header, bind_host, port):
    """True when an Origin or Referer names this dashboard.

    Same port, and the same host the browser used (or a loopback alias of it).
    A missing header is not checked here; the caller treats that as "no claim".
    """
    if not origin:
        return False
    u = urllib.parse.urlparse(origin)
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    oport = u.port if u.port is not None else (443 if u.scheme == "https" else 80)
    try:
        if int(oport) != int(port):
            return False
    except (TypeError, ValueError):
        return False
    got = u.hostname.lower()
    allowed = set()
    if bind_host:
        allowed.add(str(bind_host).strip("[]").lower())
    header_host = _host_from_header(host_header)
    if header_host:
        allowed.add(header_host)
    if got in allowed:
        return True
    # Opening http://localhost/ against a 127.0.0.1 bind is still this machine.
    # The port was already required to match, so this is not another local site.
    if got in LOOPBACK and (allowed & LOOPBACK or "0.0.0.0" in allowed or "::" in allowed):
        return True
    return False


def _tokens_match(got, expected):
    if not isinstance(got, str) or not isinstance(expected, str):
        return False
    if not got or len(got) != len(expected):
        return False
    return secrets.compare_digest(got, expected)


def dashboard_request_allowed(headers, csrf_got, csrf_expected, bind_host, port):
    """Per-run form token, plus Origin/Referer when the browser sends one.

    The token is embedded in the page this process serves. Another origin cannot
    read it. Cookies are not used: on 127.0.0.1 they would be sent to every port,
    which would not stop a page hosted on a different local port.
    """
    if not _tokens_match(csrf_got, csrf_expected):
        return False
    if headers is None:
        return True
    origin = headers.get("Origin")
    if origin:
        return dashboard_origin_allowed(origin, headers.get("Host"), bind_host, port)
    referer = headers.get("Referer")
    if referer:
        return dashboard_origin_allowed(referer, headers.get("Host"), bind_host, port)
    return True


def _query_lines(query):
    raw = (urllib.parse.parse_qs(query).get("n") or ["40"])[0]
    try:
        n = int(raw)
    except ValueError:
        return 40
    return max(1, min(n, 400))


_PAGE_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; font: 15px/1.45 "Segoe UI", system-ui, sans-serif; background: #f3f4f7; color: #1b1e23; }
header.top { padding: 1.25rem 1.5rem 0.4rem; }
h1 { font-size: 1.35rem; font-weight: 600; margin: 0 0 0.2rem; }
.sub { margin: 0; color: #4b5160; }
.banner { margin: 0.75rem 1.5rem 0; padding: 0.7rem 0.9rem; background: #fff4e5; border: 1px solid #e6c48a; border-radius: 8px; }
main { padding: 0.85rem 1.5rem 1.5rem; }
table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 10px; overflow: hidden; }
th { text-align: left; font-size: 0.72rem; letter-spacing: 0.04em; text-transform: uppercase; color: #5c6573;
     padding: 0.65rem 0.75rem; background: #fafbfc; border-bottom: 1px solid #e6e8ee; }
td { padding: 0.65rem 0.75rem; border-bottom: 1px solid #eef0f4; vertical-align: middle; }
tr:last-child td { border-bottom: 0; }
.name { font-weight: 600; }
.state { font-weight: 600; }
.running { color: #0d7a32; }
.stopped { color: #5c6573; }
.crashed { color: #8a4b08; }
.unknown { color: #5c6573; }
.actions form { display: inline; }
.actions form + form { margin-left: 0.25rem; }
button { font: inherit; font-size: 0.85rem; padding: 0.22rem 0.55rem; border-radius: 6px;
         border: 1px solid #cfd3dc; background: #fff; cursor: pointer; }
button.start { background: #e8f6ec; border-color: #b7dfc3; }
button.stop { background: #fdeeee; border-color: #f0c4c4; }
button.restart { background: #f4f5f7; }
.notes { color: #4b5160; }
.client-log { background: #fff; border-radius: 10px; padding: 0.85rem 1rem; margin: 0 0 0.85rem; }
.client-log h2 { font-size: 1rem; margin: 0; }
pre { margin: 0.45rem 0 0; padding: 0.7rem 0.8rem; background: #16181d; color: #e8eaed; border-radius: 8px;
      overflow: auto; max-height: 16rem; white-space: pre-wrap; word-break: break-word;
      font: 12px/1.45 ui-monospace, SFMono-Regular, Consolas, monospace; }
footer { padding: 0 1.5rem 2rem; color: #5c6573; font-size: 0.85rem; max-width: 70rem; }
.empty { background: #fff; border-radius: 10px; padding: 1rem; }
"""


def _esc(value):
    return html.escape(str(value), quote=True)


def _control_forms(csrf, name):
    parts = []
    for action, label, cls in (("start", "Start", "start"), ("stop", "Stop", "stop"), ("restart", "Restart", "restart")):
        parts.append(
            '<form method="post" action="/control">'
            f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
            f'<input type="hidden" name="name" value="{_esc(name)}">'
            f'<input type="hidden" name="action" value="{action}">'
            f'<button type="submit" class="{cls}">{label}</button></form>'
        )
    return "".join(parts)


def _dash(value):
    return "—" if value is None or value == "" else _esc(value)


def render_dashboard(paths, csrf_token, lines=40, bind_host="127.0.0.1", bind_port=None, now=None):
    view = status_view(paths, now=now)
    warn = dashboard_exposure_warning(bind_host)
    where = _esc(bind_host)
    if bind_port:
        where += ":" + _esc(bind_port)
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="referrer" content="no-referrer">',
        '<meta http-equiv="refresh" content="5">',
        "<title>BotKit host</title>",
        f"<style>{_PAGE_CSS}</style></head><body>",
        '<header class="top"><h1>BotKit host</h1>',
        f'<p class="sub">{_esc(supervisor_line(view["status"], view["now"], view["alive"]))}</p></header>',
    ]
    if warn:
        parts.append(f'<p class="banner">{_esc(warn)}</p>')
    elif not view["alive"]:
        parts.append('<p class="banner">The supervisor is not running, so these states are not live. '
                     "Start, stop, and restart are saved and apply when it starts.</p>")
    parts.append("<main>")
    if not view["rows"]:
        parts.append('<p class="empty">No clients. Add one with <code>host.py add NAME</code>.</p>')
    else:
        parts.append("<table><thead><tr>"
                     "<th>Client</th><th>State</th><th>PID</th><th>Uptime</th>"
                     "<th>Last exit</th><th>Restarts</th><th>Notes</th><th></th>"
                     "</tr></thead><tbody>")
        for r in view["rows"]:
            actions = _control_forms(csrf_token, r["name"]) if r["controllable"] else "—"
            parts.append(
                f'<tr data-client="{_esc(r["name"])}" data-state="{_esc(r["state"])}">'
                f'<td class="name">{_esc(r["name"])}</td>'
                f'<td class="state {_esc(r["label"])}">{_esc(r["label"])}</td>'
                f'<td>{_dash(r["pid"])}</td>'
                f'<td>{_dash(r["uptime"])}</td>'
                f'<td>{_dash(r["last_exit"])}</td>'
                f'<td>{_dash(r["restarts"])}</td>'
                f'<td class="notes">{_esc(", ".join(r["notes"]))}</td>'
                f'<td class="actions">{actions}</td></tr>'
            )
        parts.append("</tbody></table>")
        parts.append('<section class="logs">')
        for r in view["rows"]:
            if not valid_name(r["name"]):
                continue
            tail = client_log_tail(paths, r["name"], lines)
            shown = "(no log yet)" if tail is None else tail
            parts.append(
                f'<article class="client-log" data-log="{_esc(r["name"])}">'
                f"<h2>{_esc(r['name'])}</h2><pre>{_esc(shown)}</pre></article>"
            )
        parts.append("</section>")
    parts.append("</main><footer>")
    parts.append(
        f"Listening on {where}. Buttons submit a token created when this dashboard process started, "
        "so another page on this machine cannot start or stop a client. "
        "The page never reads <code>.env</code>. Token-shaped text in the log tail is removed. "
        f"Showing the last {_esc(lines)} log lines. "
        "The supervisor is still <code>host.py run</code>."
    )
    parts.append("</footer></body></html>")
    return "\n".join(parts)


def _error_page(message):
    return ("<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>BotKit host</title></head>"
            f"<body><p>{_esc(message)}</p><p><a href=\"/\">Back</a></p></body></html>")


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return  # refreshes every few seconds; the request line is not useful here

    def do_GET(self):
        try:
            self._get()
        except Exception:
            log.exception("dashboard GET failed")
            self._plain(500, "internal error")

    def do_POST(self):
        try:
            self._post()
        except Exception:
            log.exception("dashboard POST failed")
            self._plain(500, "internal error")

    def _get(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/":
            self._plain(404, "not found")
            return
        port = self.server.server_address[1]
        body = render_dashboard(self.server.paths, self.server.csrf_token, _query_lines(parsed.query),
                                self.server.bind_host, port)
        self._html(200, body)

    def _post(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/control":
            self._plain(404, "not found")
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            self._plain(400, "bad request")
            return
        if length < 0 or length > 8192:
            self._plain(413, "too large")
            return
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        form = urllib.parse.parse_qs(raw, keep_blank_values=False)

        def one(key):
            return (form.get(key) or [""])[0]

        host, port = self.server.server_address[:2]
        if not dashboard_request_allowed(self.headers, one("csrf"), self.server.csrf_token, host, port):
            self._plain(403, "forbidden — reload the dashboard and try again")
            return
        action = one("action")
        if action not in ("start", "stop", "restart"):
            self._plain(400, "unknown action")
            return
        try:
            apply_control(self.server.paths, one("name"), action)
        except SystemExit as e:
            msg = e.code if isinstance(e.code, str) else "request failed"
            self._html(400, _error_page(msg))
            return
        self._redirect("/")

    def _redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self._extra_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _html(self, code, body):
        self._send(code, body, "text/html; charset=utf-8")

    def _plain(self, code, body):
        self._send(code, body, "text/plain; charset=utf-8")

    def _send(self, code, body, content_type):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self._extra_headers()
        self.end_headers()
        self.wfile.write(data)

    def _extra_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                         "base-uri 'none'; frame-ancestors 'none'")
        self.send_header("Cache-Control", "no-store")


class DashboardServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, paths, token):
        self.paths = paths
        self.csrf_token = token
        self.bind_host = server_address[0]
        # HTTPServer defaults to IPv4. A colon in the host means an IPv6 address such as ::1.
        if ":" in str(server_address[0]):
            self.address_family = socket.AF_INET6
        super().__init__(server_address, DashboardHandler)


def serve_dashboard(paths, host, port, token=None):
    token = token or secrets.token_urlsafe(32)
    return DashboardServer((host, port), paths, token)


def cmd_dashboard(paths, args):
    warn = check_dashboard_bind(args.host, args.port)
    if warn:
        print(warn, file=sys.stderr)
    try:
        httpd = serve_dashboard(paths, args.host, args.port)
    except OSError as e:
        raise SystemExit(f"dashboard could not bind {args.host}:{args.port}: {e}")
    shown_host, shown_port = httpd.server_address[:2]
    if ":" in str(shown_host):
        shown = f"[{shown_host}]:{shown_port}"
    else:
        shown = f"{shown_host}:{shown_port}"
    print(f"BotKit dashboard at http://{shown}/  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def build_parser():
    p = argparse.ArgumentParser(description="BotKit multi-client host")
    p.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the supervisor (foreground)")
    sub.add_parser("status", aliases=["list"], help="show clients")
    a = sub.add_parser("add", help="create a client and run the setup wizard into it")
    a.add_argument("name")
    a.add_argument("--no-start", action="store_true", help="create it stopped")
    a.add_argument("wizard_args", nargs=argparse.REMAINDER, help="extra setup_wizard.py args (e.g. --answers f.json)")
    for c in ("start", "stop", "restart"):
        sub.add_parser(c).add_argument("name")
    lg = sub.add_parser("logs")
    lg.add_argument("name")
    lg.add_argument("-n", type=int, default=40)
    dash = sub.add_parser("dashboard", help="local status page (127.0.0.1:8765 unless --host is set)")
    dash.add_argument("--host", default="127.0.0.1",
                      help="bind address (default 127.0.0.1; any other address is an explicit opt-in and prints a warning)")
    dash.add_argument("--port", type=int, default=8765)
    lt = sub.add_parser("lt", help="run LibreTranslate (127.0.0.1 only) under supervision")
    lt.add_argument("--host", default="127.0.0.1")
    lt.add_argument("--port", type=int, default=5000)
    lt.add_argument("--langs", default=LT_LANGS)
    lt.add_argument("--threads", type=int, default=2)
    lt.add_argument("--python", help="interpreter of the LibreTranslate venv")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    paths = Paths(args.root)
    handlers = {"run": cmd_run, "status": cmd_status, "list": cmd_status, "add": cmd_add,
                "start": cmd_start, "stop": cmd_stop, "restart": cmd_restart, "logs": cmd_logs,
                "dashboard": cmd_dashboard, "lt": cmd_lt}
    return handlers[args.cmd](paths, args) or 0


if __name__ == "__main__":
    sys.exit(main())
