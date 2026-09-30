"""Offline tests for host.py (the multi-client supervisor). No token, no network.
Run from the host folder:  python -m unittest discover -s tests -v
Fake bots are tiny python scripts, so nothing touches Discord.
"""
import io, json, os, subprocess, sys, tempfile, time, unittest
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


if __name__ == "__main__":
    unittest.main()
