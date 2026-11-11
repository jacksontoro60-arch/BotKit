"""Offline tests for host.py (the multi-client supervisor). No token, no network.
Run from the host folder:  python -m unittest discover -s tests -v
Fake bots are tiny python scripts, so nothing touches Discord.
"""
import http.client, io, json, os, socket, subprocess, sys, tempfile, threading, time, unittest, urllib.parse
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import host  # noqa: E402

FAKE_BOT = r'''
import os, sys, time, json
cfg = json.load(open(os.environ["BOTKIT_CONFIG"], encoding="utf-8"))
print("fake bot", cfg.get("mode"), "token-in-env:", "DISCORD_TOKEN" in os.environ, flush=True)
mode = cfg.get("mode")
if mode == "crash":
    sys.exit(7)
while True:
    time.sleep(0.1)
'''


def wait_until(pred, timeout=10.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


class HostTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.paths = host.Paths(self.root)
        (self.paths.code).mkdir(parents=True)
        (self.paths.code / "bot.py").write_text(FAKE_BOT, encoding="utf-8")
        self.paths.clients.mkdir()
        self.sup = None

    def tearDown(self):
        if self.sup:
            self.sup.shutdown()
            for m in self.sup.bots.values():
                m.log.close()
        self.tmp.cleanup()

    def client(self, name, mode="ok", env=True):
        d = self.paths.client(name)
        (d / "logs").mkdir(parents=True, exist_ok=True)
        (d / "config.json").write_text(json.dumps({"mode": mode}), encoding="utf-8")
        if env:
            (d / ".env").write_text("DISCORD_TOKEN=fake\n", encoding="utf-8")
        return d

    def supervisor(self, **kw):
        self.sup = host.Supervisor(self.paths, python=sys.executable, base=kw.get("base", 0.05), cap=kw.get("cap", 0.2))
        return self.sup

    def pump(self, until, timeout=10.0):
        return wait_until(lambda: (self.sup.tick(), until())[1], timeout)


class Discovery(HostTestCase):
    def test_only_complete_valid_folders(self):
        self.client("good")
        self.client("noenv", env=False)
        self.client("bad name")
        (self.paths.clients / "empty").mkdir()
        self.assertEqual(list(host.discover(self.paths)), ["good"])

    def test_valid_name(self):
        for ok in ("demo", "acme-1", "A_b"):
            self.assertTrue(host.valid_name(ok))
        for bad in ("", "-x", "a b", "../x", "x" * 41, "a/b"):
            self.assertFalse(host.valid_name(bad))

    def test_backoff(self):
        self.assertEqual(host.backoff_delay(0), 0)
        self.assertEqual([host.backoff_delay(n) for n in (1, 2, 3, 4)], [5, 10, 20, 40])
        self.assertEqual(host.backoff_delay(20), 300)

    def test_bot_env_isolates_secrets(self):
        d = self.paths.client("x")
        env = host.bot_env(d, {"DISCORD_TOKEN": "leak", "DEEPL_API_KEY": "k", "PATH": "p"})
        self.assertNotIn("DISCORD_TOKEN", env)
        self.assertNotIn("DEEPL_API_KEY", env)
        self.assertEqual(env["BOTKIT_CONFIG"], str(d / "config.json"))
        self.assertEqual(env["BOTKIT_ENV"], str(d / ".env"))
        self.assertEqual(env["PATH"], "p")


class Supervision(HostTestCase):
    def test_runs_one_process_per_client_with_own_log(self):
        self.client("a")
        self.client("b")
        sup = self.supervisor()
        sup.tick()
        self.assertEqual(sorted(sup.bots), ["a", "b"])
        pids = {m.proc.pid for m in sup.bots.values()}
        self.assertEqual(len(pids), 2)
        log_a = self.paths.client("a") / "logs" / "bot.log"
        self.assertTrue(wait_until(lambda: "fake bot ok" in log_a.read_text(encoding="utf-8")))
        self.assertIn("token-in-env: False", log_a.read_text(encoding="utf-8"))  # supervisor never injects it
        st = json.loads((self.paths.state / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(st["clients"]["a"]["state"], "running")

    def test_crash_restarts_with_backoff(self):
        self.client("c", mode="crash")
        sup = self.supervisor()
        self.assertTrue(self.pump(lambda: sup.bots["c"].restarts >= 2))
        m = sup.bots["c"]
        self.assertEqual(m.last_exit, 7)
        self.assertGreaterEqual(m.failures, 2)
        text = (self.paths.client("c") / "logs" / "bot.log").read_text(encoding="utf-8")
        self.assertIn("exited with code 7", text)

    def test_backoff_delays_grow(self):
        clock = [1000.0]
        m = host.Managed("x", [sys.executable, "-c", "import sys; sys.exit(1)"], self.root, dict(os.environ),
                         self.root / "x.log", base=5, cap=300, clock=lambda: clock[0])
        try:
            m.tick()
            m.proc.wait(10)
            m.poll()
            self.assertEqual(m.next_start, 1005)
            m.tick()                     # too early: no restart
            self.assertIsNone(m.proc)
            clock[0] = 1005
            m.tick()
            m.proc.wait(10)
            m.poll()
            self.assertEqual(m.next_start, 1015)
            self.assertEqual(m.restarts, 1)
        finally:
            m.stop()
            m.log.close()

    def test_stop_start_restart_markers(self):
        d = self.client("s")
        sup = self.supervisor()
        sup.tick()
        first = sup.bots["s"].proc
        (d / ".disabled").write_text("x")
        sup.tick()
        self.assertIsNotNone(first.poll())
        self.assertEqual(sup.bots["s"].state, "disabled")
        sup.tick()
        self.assertIsNone(sup.bots["s"].proc)  # stays stopped
        (d / ".disabled").unlink()
        sup.tick()
        second = sup.bots["s"].proc
        self.assertIsNone(second.poll())
        (d / ".restart").write_text("1")
        sup.tick()
        self.assertFalse((d / ".restart").exists())
        self.assertIsNotNone(second.poll())
        self.assertNotEqual(sup.bots["s"].proc.pid, second.pid)
        self.assertEqual(sup.bots["s"].state, "running")

    def test_removed_client_is_stopped(self):
        d = self.client("gone")
        sup = self.supervisor()
        sup.tick()
        p = sup.bots["gone"].proc
        (d / ".env").unlink()
        sup.tick()
        self.assertIsNotNone(p.poll())
        self.assertNotIn("gone", sup.bots)

    def test_shutdown_stops_all(self):
        self.client("a")
        self.client("b")
        sup = self.supervisor()
        sup.tick()
        procs = [m.proc for m in sup.bots.values()]
        sup.shutdown()
        self.assertTrue(all(p.poll() is not None for p in procs))


class RotatingLogTests(HostTestCase):
    def test_rotates_and_keeps_backups(self):
        p = self.root / "l" / "bot.log"
        lg = host.RotatingLog(p, max_bytes=100, backups=2)
        for i in range(30):
            lg.write(f"line {i:03d} ..........\n")
        lg.close()
        self.assertTrue(p.exists())
        self.assertTrue(p.with_name("bot.log.1").exists())
        self.assertTrue(p.with_name("bot.log.2").exists())
        self.assertFalse(p.with_name("bot.log.3").exists())
        self.assertLessEqual(p.stat().st_size, 100)
        self.assertIn("line 029", p.read_text())


class Cli(HostTestCase):
    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = host.main(["--root", str(self.root), *argv])
        return rc, buf.getvalue()

    def test_add_runs_wizard_into_client_folder(self):
        calls = []

        def fake_runner(cmd, cwd):
            calls.append((cmd, cwd))
            d = self.paths.client("acme")
            (d / "config.json").write_text("{}")
            (d / ".env").write_text("DISCORD_TOKEN=x\n")
            return 0

        args = host.build_parser().parse_args(["--root", str(self.root), "add", "acme", "--answers", "a.json"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = host.cmd_add(self.paths, args, runner=fake_runner)
        self.assertEqual(rc, 0)
        cmd, cwd = calls[0]
        d = self.paths.client("acme")
        self.assertTrue(cmd[1].endswith("setup_wizard.py"))
        self.assertEqual(cmd[cmd.index("--config-out") + 1], str(d / "config.json"))
        self.assertEqual(cmd[cmd.index("--env-out") + 1], str(d / ".env"))
        self.assertEqual(cmd[-2:], ["--answers", "a.json"])
        self.assertTrue((d / "logs").is_dir())
        self.assertIn("ready", buf.getvalue())

    def test_add_no_start_and_bad_name(self):
        args = host.build_parser().parse_args(["--root", str(self.root), "add", "--no-start", "x1"])
        with redirect_stdout(io.StringIO()):
            host.cmd_add(self.paths, args, runner=lambda cmd, cwd: 1)
        self.assertTrue((self.paths.client("x1") / ".disabled").exists())
        with self.assertRaises(SystemExit):
            self.run_cli("add", "../evil")

    def test_stop_start_without_supervisor(self):
        d = self.client("demo")
        rc, out = self.run_cli("stop", "demo")
        self.assertTrue((d / ".disabled").exists())
        self.assertIn("not running", out)
        rc, out = self.run_cli("start", "demo")
        self.assertFalse((d / ".disabled").exists())
        rc, out = self.run_cli("restart", "demo")
        self.assertTrue((d / ".restart").exists())
        with self.assertRaises(SystemExit):
            self.run_cli("start", "nope")

    def test_status_lists_clients(self):
        self.client("a")
        self.client("b", env=False)
        sup = self.supervisor()
        sup.tick()
        rc, out = self.run_cli("status")
        self.assertIn("supervisor: running", out)
        self.assertRegex(out, r"a\s+running\s+\d+")
        self.assertIn("missing .env", out)
        rc, out2 = self.run_cli("list")
        self.assertIn("b", out2)

    def test_lt_refuses_lan_and_reserved_ports(self):
        with self.assertRaises(SystemExit):
            self.run_cli("lt", "--host", "0.0.0.0")
        for port in ("53", "80", "3000", "5055"):
            with self.assertRaises(SystemExit):
                self.run_cli("lt", "--port", port)
        cmd = host.lt_command("py", "127.0.0.1", 5000, "en,es", 2)
        self.assertEqual(cmd[cmd.index("--host") + 1], "127.0.0.1")
        self.assertEqual(cmd[cmd.index("--load-only") + 1], "en,es")

    def test_instance_lock_is_exclusive(self):
        a = host.InstanceLock(self.root / "state" / "t.lock")
        self.assertTrue(a.acquire())
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import host; "
                "sys.exit(0 if host.InstanceLock(sys.argv[2]).acquire() else 3)")
        r = subprocess.run([sys.executable, "-c", code, str(HERE.parent), str(self.root / "state" / "t.lock")])
        self.assertEqual(r.returncode, 3)
        a.f.close()


class BotPy(unittest.TestCase):
    """bot.py must honour BOTKIT_CONFIG / BOTKIT_ENV so one code dir serves many clients."""

    def test_bot_reads_client_paths(self):
        bot_dir = HERE.parent / "botkit"
        if not (bot_dir / "bot.py").exists():
            self.skipTest("botkit code dir not present")
        try:
            import discord, dotenv  # noqa: F401
        except ImportError:
            self.skipTest("discord.py not installed")
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            (t / "config.json").write_text(json.dumps({"features": [], "message_content_intent": True,
                                                       "note": "\U0001F4C5 utf-8"}), encoding="utf-8")
            (t / ".env").write_text("DISCORD_TOKEN=not-a-real-token\n", encoding="utf-8")
            env = dict(os.environ, BOTKIT_CONFIG=str(t / "config.json"), BOTKIT_ENV=str(t / ".env"))
            env.pop("DISCORD_TOKEN", None)
            env.pop("PYTHONUTF8", None)
            code = ("import sys, os; sys.path.insert(0, sys.argv[1]); import bot; "
                    "print(bot.CONFIG['note'].encode('unicode_escape').decode(), bot.intents.message_content, "
                    "os.environ.get('DISCORD_TOKEN') == 'not-a-real-token')")
            r = subprocess.run([sys.executable, "-c", code, str(bot_dir)], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("utf-8 True True", r.stdout)


# Built at runtime so the source file does not contain a contiguous token.
# GitHub secret scanning treats a realistic Discord token as a live secret.
TOKEN = ("q" * 24) + "." + ("r" * 6) + "." + ("s" * 27)
MFA_TOKEN = "mfa." + ("m" * 26)
BEARER_TOKEN = "sk-" + ("k" * 20)
ENV_SENTINEL = "ENV_SENTINEL_do_not_leak"
DEEPL_SENTINEL = "deepl-secret-value"


class Dashboard(HostTestCase):
    def setUp(self):
        super().setUp()
        self.httpd = None
        self.thread = None
        self.token = "csrf-test-token-value"

    def tearDown(self):
        if self.httpd is not None:
            self.httpd.shutdown()
            if self.thread is not None:
                self.thread.join(5)
            self.httpd.server_close()
        super().tearDown()

    def start_dash(self):
        self.httpd = host.serve_dashboard(self.paths, "127.0.0.1", 0, token=self.token)
        self.port = self.httpd.server_address[1]
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                status, _, _ = self.request("GET", "/")
                if status == 200:
                    return
            except OSError:
                time.sleep(0.02)
        self.fail("dashboard did not accept a connection")

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, raw.decode("utf-8", "replace")
        finally:
            conn.close()

    def post_control(self, name, action, csrf=None, extra_headers=None):
        fields = {"name": name, "action": action}
        if csrf is not None:
            fields["csrf"] = csrf
        data = urllib.parse.urlencode(fields)
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if extra_headers:
            headers.update(extra_headers)
        return self.request("POST", "/control", body=data, headers=headers)

    def test_redact_secrets_and_cli_logs(self):
        text = "\n".join([
            "online as DemoBot",
            "token-in-env: False",
            "[host] started pid 1234",
            f"saw {TOKEN}",
            "DISCORD_TOKEN=fake",
            f"DEEPL_API_KEY={DEEPL_SENTINEL}",
            "LIBRETRANSLATE_API_KEY=lt-secret",
            MFA_TOKEN,
            "Authorization: Bearer " + BEARER_TOKEN,
            'loaded "discord_token": "abc123"',
        ])
        out = host.redact_secrets(text)
        self.assertIn("online as DemoBot", out)
        self.assertIn("token-in-env: False", out)
        self.assertIn("[host] started pid 1234", out)
        for secret in (TOKEN, "fake", DEEPL_SENTINEL, "lt-secret", "abc123", MFA_TOKEN, BEARER_TOKEN):
            self.assertNotIn(secret, out)
        self.assertIn("[redacted]", out)

        d = self.client("demo")
        (d / ".env").write_text(
            f"DISCORD_TOKEN={ENV_SENTINEL}\nPLAIN_MARKER_do_not_show came from dotenv\n",
            encoding="utf-8")
        (d / "logs" / "bot.log").write_text(text + "\n", encoding="utf-8")
        tail = host.client_log_tail(self.paths, "demo", 40)
        self.assertIn("online as DemoBot", tail)
        self.assertNotIn(TOKEN, tail)
        self.assertNotIn(ENV_SENTINEL, tail)
        self.assertNotIn("PLAIN_MARKER_do_not_show", tail)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = host.main(["--root", str(self.root), "logs", "demo", "-n", "40"])
        logged = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("online as DemoBot", logged)
        self.assertNotIn(TOKEN, logged)
        self.assertNotIn(DEEPL_SENTINEL, logged)
        logf = d / "logs" / "bot.log"
        logf.unlink()
        logf.symlink_to(d / ".env")
        self.assertIsNone(host.client_log_tail(self.paths, "demo", 40))
        page = host.render_dashboard(self.paths, self.token)
        self.assertNotIn(ENV_SENTINEL, page)
        self.assertNotIn("PLAIN_MARKER_do_not_show", page)

    def test_page_shows_live_state_without_secrets(self):
        now = 1_700_000_000.0
        d = self.client("demo")
        self.client("boom")
        self.client("partial", env=False)
        (self.paths.clients / "<img onerror=x>").mkdir()
        (d / ".env").write_text(
            f"DISCORD_TOKEN={ENV_SENTINEL}\nDEEPL_API_KEY={DEEPL_SENTINEL}\n"
            "PLAIN_MARKER_do_not_show came from dotenv\n", encoding="utf-8")
        (d / "config.json").write_text(
            json.dumps({"mode": "ok", "note": "CONFIG_MARKER_do_not_show"}), encoding="utf-8")
        (d / "logs" / "bot.log").write_text(f"online as DemoBot\nsaw {TOKEN}\n", encoding="utf-8")
        self.paths.state.mkdir()
        (self.paths.state / "status.json").write_text(json.dumps({
            "supervisor_pid": 42,
            "updated": now,
            "clients": {
                "demo": {"state": "running", "pid": 4242, "started": now - 125, "restarts": 1,
                         "failures": 0, "last_exit": None},
                "boom": {"state": "backoff", "pid": None, "started": None, "restarts": 4,
                         "failures": 4, "last_exit": 7, "next_start": now + 9},
            },
        }), encoding="utf-8")
        view = host.status_view(self.paths, now=now)
        by = {r["name"]: r for r in view["rows"]}
        self.assertEqual(by["demo"]["label"], "running")
        self.assertEqual(by["demo"]["uptime"], "2m")
        self.assertEqual(by["demo"]["pid"], 4242)
        self.assertEqual(by["boom"]["state"], "backoff")
        self.assertEqual(by["boom"]["label"], "crashed")
        self.assertEqual(by["boom"]["last_exit"], 7)
        self.assertIn("retry in 9s", by["boom"]["notes"])
        self.assertIn("missing .env", by["partial"]["notes"])
        page = host.render_dashboard(self.paths, self.token, lines=40, bind_host="127.0.0.1",
                                     bind_port=8765, now=now)
        self.assertIn('data-client="demo" data-state="running"', page)
        self.assertIn('data-client="boom" data-state="backoff"', page)
        self.assertIn(">crashed<", page)
        self.assertIn(">2m<", page)
        self.assertIn(">4242<", page)
        self.assertIn(">7<", page)
        self.assertIn("retry in 9s", page)
        self.assertIn("online as DemoBot", page)
        self.assertNotIn(TOKEN, page)
        self.assertNotIn(ENV_SENTINEL, page)
        self.assertNotIn(DEEPL_SENTINEL, page)
        self.assertNotIn("PLAIN_MARKER_do_not_show", page)
        self.assertNotIn("CONFIG_MARKER_do_not_show", page)
        self.assertIn("[redacted]", page)
        self.assertNotIn("<img", page)
        self.assertIn("&lt;img", page)
        self.assertIn("value=\"csrf-test-token-value\"", page)
        stale = host.status_view(self.paths, now=now + 30)
        self.assertFalse(stale["alive"])
        self.assertEqual({r["name"]: r["state"] for r in stale["rows"]}["demo"], "-")

    def test_apply_control_drives_the_supervisor(self):
        d = self.client("demo")
        sup = self.supervisor()
        sup.tick()
        self.assertEqual(sup.bots["demo"].state, "running")
        host.apply_control(self.paths, "demo", "stop")
        self.assertEqual((d / ".disabled").read_text(encoding="utf-8"), "stopped by host.py stop\n")
        sup.tick()
        self.assertEqual(sup.bots["demo"].state, "disabled")
        self.assertIsNone(sup.bots["demo"].proc)
        host.apply_control(self.paths, "demo", "start")
        self.assertFalse((d / ".disabled").exists())
        sup.tick()
        self.assertEqual(sup.bots["demo"].state, "running")
        old = sup.bots["demo"].proc.pid
        host.apply_control(self.paths, "demo", "restart")
        self.assertTrue((d / ".restart").exists())
        sup.tick()
        self.assertFalse((d / ".restart").exists())
        self.assertIsNotNone(sup.bots["demo"].proc)
        self.assertNotEqual(sup.bots["demo"].proc.pid, old)
        self.assertEqual(sup.bots["demo"].state, "running")
        with self.assertRaises(SystemExit):
            host.apply_control(self.paths, "../evil", "stop")
        self.assertFalse((self.root / ".disabled").exists())

    def test_dashboard_bind_defaults_to_loopback(self):
        args = host.build_parser().parse_args(["dashboard"])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8765)
        self.assertIsNone(host.check_dashboard_bind("127.0.0.1", 8765))
        self.assertIsNone(host.check_dashboard_bind("localhost", 8765))
        self.assertIsNone(host.check_dashboard_bind("::1", 8765))
        for host_name in ("0.0.0.0", "192.168.1.50"):
            warn = host.check_dashboard_bind(host_name, 8765)
            self.assertIn("WARNING", warn)
            self.assertIn("127.0.0.1", warn)
        for port in (53, 80, 3000, 5055, 0, 70000):
            with self.assertRaises(SystemExit):
                host.check_dashboard_bind("127.0.0.1", port)
        self.assertFalse(host.dashboard_origin_allowed(
            "http://127.0.0.1:9/", "127.0.0.1:8765", "127.0.0.1", 8765))
        self.assertFalse(host.dashboard_origin_allowed(
            "http://evil.example/", "127.0.0.1:8765", "127.0.0.1", 8765))
        self.assertTrue(host.dashboard_origin_allowed(
            "http://localhost:8765/", "localhost:8765", "127.0.0.1", 8765))
        self.assertFalse(host.dashboard_request_allowed(
            {"Origin": "http://127.0.0.1:9/", "Host": "127.0.0.1:8765"}, self.token, self.token,
            "127.0.0.1", 8765))
        self.assertFalse(host.dashboard_request_allowed({}, "nope", self.token, "127.0.0.1", 8765))
        try:
            ipv6 = host.serve_dashboard(self.paths, "::1", 0, token="t" * 16)
        except OSError:
            ipv6 = None
        if ipv6 is not None:
            try:
                self.assertEqual(ipv6.address_family, socket.AF_INET6)
                self.assertIn("::1", ipv6.server_address[0])
            finally:
                ipv6.server_close()

    def test_http_actions_require_token_and_same_origin(self):
        d = self.client("demo")
        (d / ".env").write_text(
            f"DISCORD_TOKEN={ENV_SENTINEL}\nPLAIN_MARKER_do_not_show came from dotenv\n",
            encoding="utf-8")
        (d / "logs" / "bot.log").write_text("\n".join([
            "LINE_ONE_UNIQUE",
            "online as DemoBot",
            "token-in-env: False",
            f"saw {TOKEN}",
            f"DEEPL_API_KEY={DEEPL_SENTINEL}",
            "<script>alert(1)</script>",
        ]) + "\n", encoding="utf-8")
        self.client("partial", env=False)
        self.start_dash()
        status, headers, body = self.request("GET", "/?n=4")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["content-type"])
        self.assertIn("default-src 'none'", headers["content-security-policy"])
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertIn("token-in-env: False", body)
        self.assertIn("[redacted]", body)
        self.assertNotIn("LINE_ONE_UNIQUE", body)
        self.assertNotIn(TOKEN, body)
        self.assertNotIn(DEEPL_SENTINEL, body)
        self.assertNotIn(ENV_SENTINEL, body)
        self.assertNotIn("PLAIN_MARKER_do_not_show", body)
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;", body)
        self.assertIn("not running", body.lower())
        self.assertIn(self.token, body)

        status, _, _ = self.post_control("demo", "stop")
        self.assertEqual(status, 403)
        self.assertFalse((d / ".disabled").exists())
        status, _, _ = self.post_control("demo", "stop", csrf="wrong-token")
        self.assertEqual(status, 403)
        self.assertFalse((d / ".disabled").exists())
        status, _, _ = self.post_control(
            "demo", "stop", csrf=self.token, extra_headers={"Origin": "http://127.0.0.1:9/"})
        self.assertEqual(status, 403)
        self.assertFalse((d / ".disabled").exists())
        status, _, _ = self.post_control(
            "demo", "stop", csrf=self.token,
            extra_headers={"Referer": "http://evil.example/steal"})
        self.assertEqual(status, 403)
        self.assertFalse((d / ".disabled").exists())

        status, headers, _ = self.post_control(
            "demo", "stop", csrf=self.token,
            extra_headers={"Origin": f"http://127.0.0.1:{self.port}/"})
        self.assertEqual(status, 303)
        self.assertEqual(headers.get("location"), "/")
        self.assertEqual((d / ".disabled").read_text(encoding="utf-8"), "stopped by host.py stop\n")
        _, _, body = self.request("GET", "/")
        self.assertIn('data-client="demo" data-state="disabled"', body)
        self.assertIn(">stopped<", body)
        self.assertNotIn(ENV_SENTINEL, body)
        self.assertNotIn("PLAIN_MARKER_do_not_show", body)

        status, _, _ = self.post_control("demo", "start", csrf=self.token)
        self.assertEqual(status, 303)
        self.assertFalse((d / ".disabled").exists())
        status, _, _ = self.post_control("demo", "restart", csrf=self.token)
        self.assertEqual(status, 303)
        self.assertTrue((d / ".restart").read_text(encoding="utf-8").startswith("1"))
        self.assertFalse((d / ".disabled").exists())

        status, _, body = self.post_control("partial", "start", csrf=self.token)
        self.assertEqual(status, 400)
        self.assertIn("config.json", body)
        self.assertNotIn(ENV_SENTINEL, body)
        status, _, body = self.post_control("../evil", "stop", csrf=self.token)
        self.assertEqual(status, 400)
        self.assertFalse((self.root / ".disabled").exists())
        status, _, body = self.request(
            "GET", f"/control?name=demo&action=stop&csrf={self.token}")
        self.assertEqual(status, 404)
        self.assertFalse((d / ".disabled").exists())
        status, _, body = self.request("GET", "/clients/demo/.env")
        self.assertEqual(status, 404)
        self.assertNotIn(ENV_SENTINEL, body)
        self.assertNotIn("PLAIN_MARKER_do_not_show", body)


if __name__ == "__main__":
    unittest.main()
