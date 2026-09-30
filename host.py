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
    python host.py lt [--port 5000]      run LibreTranslate on 127.0.0.1 under the same supervision

Control is file based, so the CLI works whether or not the supervisor is running:
    clients/NAME/.disabled   -> bot is stopped and stays stopped (survives reboots)
    clients/NAME/.restart    -> supervisor restarts the bot once, then deletes the file
Crashed bots are restarted with exponential backoff (5s, 10s, 20s ... max 300s; reset after
a bot has stayed up for 10 minutes). Logs rotate at 5 MB, keeping 5 old files.
Tokens are never read or printed by this script; each bot loads its own .env.
"""
import argparse
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
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


def cmd_status(paths, args):
    st = read_status(paths)
    now = time.time()
    alive = bool(st) and now - st.get("updated", 0) < 15
    if st:
        print(f"supervisor: {'running' if alive else 'NOT running (stale status)'} "
              f"(pid {st.get('supervisor_pid')}, updated {fmt_age(now - st.get('updated', 0))} ago)")
    else:
        print("supervisor: never ran (no state/status.json)")
    clients = st.get("clients", {}) if st else {}
    names = sorted(set(clients) | {d.name for d in paths.clients.iterdir() if d.is_dir()} if paths.clients.is_dir() else set(clients))
    if not names:
        print("no clients. Add one with: host.py add NAME")
        return 0
    print(f"{'client':<20} {'state':<10} {'pid':>7} {'uptime':>8} {'restarts':>8}  notes")
    for n in names:
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
        up = fmt_age(now - c["started"]) if c.get("started") else ""
        print(f"{n:<20} {state:<10} {c.get('pid') or '':>7} {up:>8} {c.get('restarts', ''):>8}  {', '.join(notes)}")
    return 0


def _need_client(paths, name):
    if not valid_name(name):
        raise SystemExit(f"invalid client name {name!r} (letters, digits, - and _, max 40)")
    d = paths.client(name)
    if not d.is_dir():
        raise SystemExit(f"no such client: {name} (folders live in {paths.clients})")
    return d


def _supervisor_alive(paths):
    st = read_status(paths)
    return bool(st) and time.time() - st.get("updated", 0) < 15


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
    d = _need_client(paths, args.name)
    if not client_ready(d):
        raise SystemExit(f"{args.name} needs config.json and .env first (host.py add {args.name})")
    (d / ".disabled").unlink(missing_ok=True)
    return _report(paths, args.name, "start", lambda c: c.get("state") == "running")


def cmd_stop(paths, args):
    d = _need_client(paths, args.name)
    (d / ".disabled").write_text("stopped by host.py stop\n", encoding="utf-8")
    return _report(paths, args.name, "stop", lambda c: c.get("state") == "disabled")


def cmd_restart(paths, args):
    d = _need_client(paths, args.name)
    (d / ".disabled").unlink(missing_ok=True)
    old = ((read_status(paths) or {}).get("clients", {}).get(args.name) or {}).get("pid")
    (d / ".restart").write_text("1\n", encoding="utf-8")
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
    d = _need_client(paths, args.name)
    p = d / "logs" / "bot.log"
    if not p.exists():
        print("(no log yet)")
        return 0
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    print("\n".join(lines[-args.n:]))
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
                "start": cmd_start, "stop": cmd_stop, "restart": cmd_restart, "logs": cmd_logs, "lt": cmd_lt}
    return handlers[args.cmd](paths, args) or 0


if __name__ == "__main__":
    sys.exit(main())
