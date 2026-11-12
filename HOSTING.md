> Public copy of the BotKit multi-client hosting layout. Paths like `A:\botkit-host` are examples from the author’s Windows host — adjust to your install directory.
> **Never commit** `clients/*/`.env, tokens, or `state/`.

# BotKit multi-client hosting

One copy of the BotKit code runs many clients. Each client is a folder with its own
`config.json`, `.env` (its own bot token) and logs. A small supervisor (`host.py`) runs one bot
process per client, restarts crashed bots with backoff, and rotates logs. `host.py dashboard`
is a loopback page for that same status, log tail, and start/stop/restart.

```
A:\botkit-host\
  botkit\               shared code (bot.py, cogs\, setup_wizard.py, tests\)
  venv\                 Python 3.13 venv for the bots (requirements.txt installed)
  lt-venv\              Python 3.13 venv with LibreTranslate 1.9.6
  clients\<name>\       config.json, .env, logs\bot.log (+ .1 ... .5), optional .disabled
  state\                status.json, host.log, libretranslate.log, lt-status.json, lock files
  host.py               supervisor, CLI, and local dashboard
  tests\test_host.py    supervisor and dashboard tests
  install-tasks.ps1     (re)creates the two Task Scheduler tasks
```

Everything listens on **127.0.0.1 only**. The bots only make outbound connections to Discord.
LibreTranslate is bound to `127.0.0.1:5000` and `host.py lt` refuses non-loopback addresses and
ports 53/80/3000/5055. The dashboard (`host.py dashboard`) binds to `127.0.0.1:8765` and refuses
those same ports. Passing `--host` with any other address is an explicit opt-in and prints a
warning; don't do that, and don't port-forward it. No firewall rules or port forwards are needed.

## Daily operations (ToroPc, PowerShell)

```powershell
cd A:\botkit-host
$py = ".\venv\Scripts\python.exe"

& $py host.py status             # or: list. Supervisor health, PID, uptime, restarts per client
& $py host.py logs demo -n 50    # last 50 lines of clients\demo\logs\bot.log
& $py host.py restart demo       # e.g. after editing clients\demo\config.json
& $py host.py stop demo          # stays stopped (even across reboots) until start
& $py host.py start demo
& $py host.py dashboard          # http://127.0.0.1:8765  (loopback only; see below)
Get-Content clients\demo\logs\bot.log -Wait -Tail 20   # live tail
```

### Add a client
1. The client creates a bot in the Discord Developer Portal, turns on **Message Content** intent if
   they use translate, turns on **Server Members** if they use welcome messages or server logs, and
   invites it (the wizard prints the invite URL).
2. Run the wizard into a new client folder. It asks for the token with hidden input and writes it
   only to `clients\<name>\.env`:
   ```powershell
   & $py host.py add acme            # interactive wizard
   & $py host.py add acme --no-start # create it stopped; `host.py start acme` later
   & $py host.py add acme --answers A:\path\answers.json   # any setup_wizard.py options pass through
   ```
   For translate, give the wizard the URL `http://127.0.0.1:5000/translate`.
3. The supervisor notices the folder once both `config.json` and `.env` exist and starts the bot
   within a few seconds. Check with `host.py status` and `host.py logs acme` (look for
   `online as ...`).

Rules: client names are letters, digits, `-` and `_` (max 40). Every client needs its **own**
bot token. Never run the same token in two places at once. To remove a client, run `host.py stop <name>`,
then move its folder out of `clients\` (keep a backup of `.env`/`config.json`).

### Code updates
Replace files in `botkit\`, run the tests, then restart each client (`host.py restart <name>`), or
restart everything with `Stop-ScheduledTask 'BotKit Host'; Start-ScheduledTask 'BotKit Host'`.
```powershell
cd A:\botkit-host\botkit; ..\venv\Scripts\python.exe -m unittest discover -s tests   # 96 kit tests
cd A:\botkit-host;        .\venv\Scripts\python.exe -m unittest discover -s tests   # 23 host tests
```
Note: several restarts in a row can make Discord rate-limit the slash-command sync. The bot then
takes about a minute to log `online`. That's expected.

### How supervision works
* One `python.exe botkit\bot.py` per client, started with no console window. `BOTKIT_CONFIG` and
  `BOTKIT_ENV` point it at its client folder. Tokens from the supervisor's own environment are
  stripped, so each bot only ever sees its own `.env`.
* Crash/exit: restarted after 5 s, 10 s, 20 s, … up to 300 s. The counter resets after 10 minutes of uptime.
* stdout/stderr go to `clients\<name>\logs\bot.log`, rotated at 5 MB (5 old files kept).
  Supervisor events are in the same log (`[host]` lines) and in `state\host.log`.
* Control is file based. `.disabled` in a client folder means "keep stopped", and `.restart` means
  "restart once". So the CLI and the dashboard work even while the supervisor is down.
* The bots live in a Windows *job object* that is killed with the supervisor, so stopping or killing
  the task never leaves orphan bots behind (and a restart never duplicates a token).
* Only one supervisor can run at a time (`state\host.lock`).

### Local dashboard

`host.py dashboard` serves http://127.0.0.1:8765. It lists the same clients as `host.py status`:
state (running, stopped, crashed while backing off, or unknown when the supervisor is down), PID,
uptime, last exit, restarts, and the last 40 lines of `clients\<name>\logs\bot.log`. The page
reloads every few seconds. Pass `--port` to move it (`host.py dashboard --port 8766`).

Start, stop, and restart on the page write the same `.disabled` and `.restart` files as
`host.py start`, `stop`, and `restart`. The supervisor applies them on its next tick. The
dashboard does not replace `host.py run`, and it does not spawn bots itself.

It binds to **127.0.0.1** unless you pass `--host`. `localhost` and `::1` are still loopback.
Any other address, such as `0.0.0.0` or a LAN IP, is an explicit opt-in: the process prints a
warning, and anyone who can open that address can see status and start, stop, or restart bots.
Do not port-forward it. From another computer, tunnel the loopback port
(`ssh -L 8765:127.0.0.1:8765 user@host`) and open http://127.0.0.1:8765 on your machine.

Each dashboard process creates a token and puts it only in the Start, Stop, and Restart forms.
Those actions are POST requests. A request without that token is rejected, and so is a request
whose `Origin` or `Referer` is some other site or port. Another page on this computer cannot
read the token, so it cannot submit the forms. The token is not stored in a cookie (a cookie
set for 127.0.0.1 would be sent to every port on this machine). Reloading the page is a GET and
does not change a client.

The page never reads `clients\<name>\.env` or `config.json`. `host.py logs` and the log tail
replace Discord-shaped tokens, `mfa.` tokens, bearer tokens, and assignments such as
`DISCORD_TOKEN`, `DEEPL_API_KEY`, and `LIBRETRANSLATE_API_KEY` with `[redacted]`.

`install-tasks.ps1` does not register a dashboard task. `BotKit Host` stays
`pythonw.exe host.py run`. To leave the page up at logon, register a separate task whose
program is `A:\botkit-host\venv\Scripts\pythonw.exe`, arguments
`"A:\botkit-host\host.py" dashboard --host 127.0.0.1 --port 8765`, and start-in directory
`A:\botkit-host`. Stopping that task stops the page only. The bots keep running under `BotKit Host`.

### Autostart (Task Scheduler)
| Task | Runs | 
|---|---|
| `BotKit Host` | `venv\Scripts\pythonw.exe host.py run` |
| `BotKit LibreTranslate` | `lt-venv\Scripts\pythonw.exe host.py lt --host 127.0.0.1 --port 5000 --langs en,es,fr,de,pt,ja` |

Both are hidden (pythonw, no console), start **at logon** of your Windows user, and have a 5-minute
watchdog trigger that relaunches the task if it died (ignored while it's running). They also
restart on failure (every 1 min, 999 tries) and have no time limit.
`install-tasks.ps1` recreates them. To have them start **at boot, before anyone logs on**, run from an
**elevated** PowerShell: `powershell -ExecutionPolicy Bypass -File A:\botkit-host\install-tasks.ps1 -AtBoot`
(this switches them to "run whether user is logged on or not" (S4U) and adds an At startup trigger).

```powershell
Get-ScheduledTask 'BotKit*' | Get-ScheduledTaskInfo | select TaskName,LastRunTime,LastTaskResult
Stop-ScheduledTask 'BotKit Host'; Start-ScheduledTask 'BotKit Host'   # restart all bots
```

### LibreTranslate
* Log: `state\libretranslate.log`, status: `state\lt-status.json`. Models are in
  `%USERPROFILE%\.local\share\argos-translate`, downloaded on the first start.
* Check it:
  ```powershell
  Invoke-RestMethod -Method Post -Uri http://127.0.0.1:5000/translate -ContentType application/json `
    -Body '{"q":"Hola amigos","source":"auto","target":"en"}'
  ```
* Bot configs use `"libretranslate_url": "http://127.0.0.1:5000/translate"`.
* It uses about 1–1.5 GB of RAM for the 6 languages.

---

## Moving to a VPS (Ubuntu, ~$5/month)

A 1 GB VPS runs several bots. For LibreTranslate on the same box, use a **2 GB** plan (or skip
LibreTranslate and use DeepL). Ubuntu 22.04/24.04.

### 1. Server prep
```bash
ssh root@VPS_IP
adduser --disabled-password --gecos "" botkit
apt update && apt install -y python3 python3-venv python3-pip rsync
ufw default deny incoming && ufw allow OpenSSH && ufw enable   # nothing else is exposed
```

### 2. Copy the code and clients
From ToroPc (PowerShell; OpenSSH `scp` is built in to Windows 10/11). Stop the local bots **first** so that
the same token never runs twice:
```powershell
Stop-ScheduledTask 'BotKit Host'; Disable-ScheduledTask 'BotKit Host'
cd A:\botkit-host
tar -czf botkit-host.tgz host.py HOSTING.md tests botkit clients     # includes the .env files, so keep it private
scp botkit-host.tgz root@VPS_IP:/tmp/
Remove-Item botkit-host.tgz
```
On the VPS:
```bash
mkdir -p /opt/botkit-host && tar -xzf /tmp/botkit-host.tgz -C /opt/botkit-host && rm /tmp/botkit-host.tgz
cd /opt/botkit-host
rm -f clients/*/logs/* state/*.lock
python3 -m venv venv && venv/bin/pip install -r botkit/requirements.txt
chown -R botkit:botkit /opt/botkit-host
chmod 700 clients && chmod 600 clients/*/.env
sudo -u botkit venv/bin/python -m unittest discover -s tests
```

### 3. LibreTranslate (optional, loopback only)
```bash
sudo -u botkit python3 -m venv /opt/botkit-host/lt-venv
sudo -u botkit /opt/botkit-host/lt-venv/bin/pip install libretranslate==1.9.6
```
`/etc/systemd/system/botkit-lt.service`:
```ini
[Unit]
Description=LibreTranslate for BotKit (127.0.0.1:5000)
After=network-online.target
Wants=network-online.target

[Service]
User=botkit
WorkingDirectory=/opt/botkit-host
ExecStart=/opt/botkit-host/lt-venv/bin/python /opt/botkit-host/host.py lt --host 127.0.0.1 --port 5000 --langs en,es,fr,de,pt,ja
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### 4. The supervisor as a systemd service
`/etc/systemd/system/botkit-host.service`:
```ini
[Unit]
Description=BotKit multi-client supervisor
After=network-online.target botkit-lt.service
Wants=network-online.target

[Service]
User=botkit
WorkingDirectory=/opt/botkit-host
ExecStart=/opt/botkit-host/venv/bin/python /opt/botkit-host/host.py run
Restart=always
RestartSec=10
KillMode=control-group
# bots are children of this service; stopping it stops every bot

[Install]
WantedBy=multi-user.target
```
```bash
systemctl daemon-reload
systemctl enable --now botkit-lt botkit-host
cd /opt/botkit-host
sudo -u botkit venv/bin/python host.py status
sudo -u botkit venv/bin/python host.py logs demo
journalctl -u botkit-host -f            # supervisor itself
```
The same CLI works on Linux (`add`, `start`, `stop`, `restart`, `logs`, `dashboard`). Run it as the `botkit`
user so new files get the right owner.

Optional dashboard, still loopback only. The supervisor unit above stays `host.py run`.
`/etc/systemd/system/botkit-dashboard.service`:

```ini
[Unit]
Description=BotKit local dashboard (127.0.0.1:8765)
After=network-online.target botkit-host.service

[Service]
User=botkit
WorkingDirectory=/opt/botkit-host
ExecStart=/opt/botkit-host/venv/bin/python /opt/botkit-host/host.py dashboard --host 127.0.0.1 --port 8765
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now botkit-dashboard
curl -s http://127.0.0.1:8765/ | head
```

That page is not reachable from the network unless you change `--host` (the process warns if you do).
Prefer the SSH tunnel described above. `ufw` still only needs OpenSSH.

Alternative without the supervisor: one templated unit per client,
`/etc/systemd/system/botkit@.service` with
`Environment=BOTKIT_CONFIG=/opt/botkit-host/clients/%i/config.json BOTKIT_ENV=/opt/botkit-host/clients/%i/.env`,
`ExecStart=/opt/botkit-host/venv/bin/python /opt/botkit-host/botkit/bot.py`, `Restart=always`,
then `systemctl enable --now botkit@demo`. Use one approach or the other, not both.

### 5. Cut-over checklist
1. The local `BotKit Host` task is stopped and disabled (see step 2).
2. `host.py status` on the VPS shows every client `running`, and each log shows `online as ...`.
3. Translate check: `curl -s -X POST http://127.0.0.1:5000/translate -H 'Content-Type: application/json' -d '{"q":"Hola","source":"auto","target":"en"}'`.
4. Optional: stop/disable `BotKit LibreTranslate` on ToroPc too.
5. Backups: copy `/opt/botkit-host/clients` (config + .env) somewhere private regularly, for example with a nightly `tar` + `rsync`.
