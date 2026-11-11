# BotKit

[![Tests](https://github.com/jacksontoro60-arch/BotKit/actions/workflows/tests.yml/badge.svg)](https://github.com/jacksontoro60-arch/BotKit/actions/workflows/tests.yml)

A modular Python Discord bot (discord.py 2.x). One codebase, and each server gets only the features listed in `config.json`. Secrets stay in `.env` and are never hard-coded.

## Repository layout

- `botkit/` — Discord bot product (run `bot.py` / `setup_wizard.py` from here)
- `host.py`, `HOSTING.md`, `install-tasks.ps1` — optional multi-client supervisor
- `tests/` — supervisor tests; kit tests live in `botkit/tests/`


## Features

| Feature (`features` name) | What it does | Commands / UI |
|---|---|---|
| `scheduler` | Posts scheduled announcements on chosen weekdays at a set time, in a configured time zone | automatic |
| `mcstatus_alert` | Polls a Minecraft Java server and posts when it goes offline or comes back online (with player count) | `/mcstatus` |
| `modflag` | Members right-click a message to flag it, and an embed with a jump link goes to a private mod channel | Apps → **Flag for mods** |
| `purge_mine` | Members delete **their own** messages in a channel, including ones older than 14 days | `/purge_mine limit older_than_days` |
| `translate` | Auto-translates messages in chosen channels, plus a right-click Translate menu. Uses LibreTranslate (default) or DeepL | Apps → **Translate** |
| `verify` | A Verify button grants a role, with an optional math captcha. Attempts are logged to a mod channel, and the button keeps working after restarts | `/verify_setup` (admins) |

## Quick setup: the wizard (recommended)

From the `botkit/` directory:

```
python setup_wizard.py
```

The wizard builds `config.json` and `.env` for a new client:

1. Asks for the bot token with hidden input and writes it **only** to `.env` (other `.env` lines such as `DEEPL_API_KEY` are kept). The token is never printed or written to `config.json`.
2. Checks the token against Discord (`GET /users/@me`) and prints the invite URL (scopes `bot` + `applications.commands`, permissions `268528640` = View Channels, Send Messages, Manage Messages, Embed Links, Read Message History, Manage Roles).
3. Lists the servers the bot is in, then that server's text channels and roles, so you pick by number.
4. Asks which of the 6 features to enable and their settings: scheduler posts, Minecraft address, mod channel, verify role (pick one, or type `c` to create a `Verified` role through the API), and the translate backend URL (it can test the URL for you).
5. Writes `config.json`. If one already exists, it is first copied to `config.json.bak`.

The bot has to be invited to the server before you run the wizard. If it's in no servers yet, the wizard prints the invite URL and stops.

Options: `--config-out PATH`, `--env-out PATH`, `--no-env` (don't write the token), `--no-create-role` (read-only use of the API), and `--answers answers.json` (or `--non-interactive --answers ...`) to read every answer from a JSON file. The answers format is documented at the top of `setup_wizard.py`. Channels and roles can be given as an ID, a name, or a list number, and the token is read from `DISCORD_TOKEN` (or `token_env`). Passing `--answers` still checks the token with Discord before it writes files. Example that writes a throwaway config and leaves `.env` alone:

```
python setup_wizard.py --answers answers.json --config-out /tmp/config.test.json --no-env --no-create-role
```

## Setup

Run these from the `botkit/` directory (`requirements.txt`, `bot.py`, and `setup_wizard.py` live there).

1. Python 3.10+.
2. `python -m venv .venv`, then activate it (`.venv\Scripts\activate` on Windows, `source .venv/bin/activate` on Linux/macOS).
3. `pip install -r requirements.txt`
4. Copy `.env.example` to `.env` and set `DISCORD_TOKEN`. Add `DEEPL_API_KEY` / `LIBRETRANSLATE_API_KEY` only if you use them.
5. Run `python setup_wizard.py` (see above), **or** copy `config.example.json` to `config.json` and fill in the IDs by hand. (Enable Developer Mode in Discord, then right-click → Copy ID.)
6. `python bot.py`

### Discord Developer Portal
1. Create an application, add a Bot and copy its token into `.env`.
2. Under **Privileged Gateway Intents**, enable **Message Content** if you use `translate` auto-translation. This must match `"message_content_intent": true` in config.
3. Invite the bot with scopes `bot` + `applications.commands`. Permissions: Send Messages, Embed Links, Read Message History, plus Manage Messages (`purge_mine`) and Manage Roles (`verify`).
4. For `verify`: in Server Settings → Roles, drag the bot's role **above** the verified role.

## Configuration (`config.json`)

| Key | Meaning |
|---|---|
| `features` | Cogs to load (file names in `cogs/` without `.py`) |
| `guild_id` | Your server ID. Slash commands sync to it instantly. Remove it for global sync (can take up to an hour) |
| `message_content_intent` | `true` is needed for `translate` auto-translation |
| `scheduler.timezone` / `posts[]` | IANA time zone. Each post has `channel_id`, `days` (`mon`…`sun`), `time` (`HH:MM`, 24 h), `message` |
| `mcstatus.address` / `channel_id` / `interval_seconds` | Server address (`host` or `host:port`), alert channel, poll interval |
| `modflag.mod_channel_id` | Where flagged messages go |
| `translate.backend` | `libretranslate` (default) or `deepl` |
| `translate.libretranslate_url` | Any LibreTranslate-compatible `/translate` endpoint (public, paid, or self-hosted e.g. `http://127.0.0.1:5000/translate`) |
| `translate.channels` | `{"CHANNEL_ID": "en"}`: messages in that channel get translated into that language |
| `translate.default_target` / `max_chars` | Target for the right-click menu (default `en`), and the per-message length cap |
| `verify.role_id` / `log_channel_id` / `captcha` | Role to grant, mod log channel (optional), and `true` for the math-captcha modal |
| `verify.title` / `text` | Text of the verification embed |

Translation keys come only from the environment: `DEEPL_API_KEY` (Free keys ending in `:fx` automatically use the free endpoint) and `LIBRETRANSLATE_API_KEY`. The public libretranslate.com instance needs a paid API key. A self-hosted LibreTranslate server is free.

## Free translation: self-hosted LibreTranslate

A self-hosted [LibreTranslate](https://github.com/LibreTranslate/LibreTranslate) server is free and needs no API key. Load only the languages you need to keep it small: about 1 GB of RAM and roughly 1 GB of models for `en,es,fr,de,pt,ja`. The first start downloads the models, which takes a few minutes. Keep it **LAN-only**: bind to `127.0.0.1` when it runs on the same machine as the bot, and never port-forward it to the internet.

**Linux / VPS (same machine as the bot):**
```bash
python3 -m venv ~/lt-venv
~/lt-venv/bin/pip install libretranslate
nohup ~/lt-venv/bin/libretranslate --host 127.0.0.1 --port 5000 \
      --load-only en,es,fr,de,pt,ja --threads 2 > libretranslate.log 2>&1 &
curl -s -X POST http://127.0.0.1:5000/translate -H "Content-Type: application/json" \
     -d '{"q":"Hola","source":"auto","target":"en"}'
```
For 24/7 use, make it a systemd service (`ExecStart=/home/USER/lt-venv/bin/libretranslate --host 127.0.0.1 --port 5000 --load-only en,es,fr,de,pt,ja`, `Restart=always`). With Docker instead: `docker run -d --restart unless-stopped -p 127.0.0.1:5000:5000 -e LT_LOAD_ONLY=en,es,fr,de,pt,ja libretranslate/libretranslate`.

**Windows, in PowerShell:**
```powershell
cd C:\botkit
py -m venv lt-venv
.\lt-venv\Scripts\pip install libretranslate
.\lt-venv\Scripts\libretranslate --host 127.0.0.1 --port 5000 --load-only en,es,fr,de,pt,ja --threads 2
# test from a second window:
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:5000/translate -ContentType application/json -Body '{"q":"Hola","source":"auto","target":"en"}'
```
- If the bot runs on the **same** Windows PC, use `http://127.0.0.1:5000/translate`.
- If the bot runs on **another machine on your LAN**, start LibreTranslate with `--host <the PC's LAN IP>` (for example `192.168.1.50`) and allow the port only from the local subnet: `New-NetFirewallRule -DisplayName "LibreTranslate LAN" -Direction Inbound -Protocol TCP -LocalPort 5000 -RemoteAddress LocalSubnet -Action Allow`. Then set the URL to `http://192.168.1.50:5000/translate`. Don't add a router port-forward.
- To start it automatically, create a Task Scheduler task "At log on" that runs `C:\botkit\lt-venv\Scripts\libretranslate.exe` with the same arguments (or wrap it in NSSM to run it as a service).
- Optional: to require a key, run with `--api-keys` and put `LIBRETRANSLATE_API_KEY=...` in the bot's `.env`.

Then set it in `config.json`: add `"translate"` to `features`, set `"libretranslate_url": "http://127.0.0.1:5000/translate"`, map channels in `translate.channels`, and restart the bot.

## Hosting (24/7)

- **Self-host:** any small Linux VPS (512 MB–1 GB is plenty). Run the bot as a systemd service with `Restart=always`:
  ```ini
  [Service]
  WorkingDirectory=/opt/botkit
  ExecStart=/opt/botkit/.venv/bin/python bot.py
  Restart=always
  ```
- **Managed:** Hosting plan **$10/month** (24/7 hosting, auto-restart, uptime monitoring, updates, daily backups), or **Hosting + Care $25/month** (adds fixes when Discord/discord.py changes, 1 small change per month, 48 h response). You always keep the code and can move off the hosting at any time.

## Adding a feature
Drop `cogs/myfeature.py` with an `async def setup(bot)` and add `"myfeature"` to `features`. Settings live under your own config key and are read from `bot.config`.

## Tests

Both suites are offline. Discord calls are mocked, and they need no token and no network.

Supervisor (`host.py`), from the repository root:

```
python -m unittest discover -s tests -v
```

Kit (translate routing, the verify role path, and the setup wizard), from `botkit/`:

```
python -m unittest discover -s tests -v
```

GitHub Actions runs both on every push and pull request to `main` (Python 3.10 and 3.12).

## Troubleshooting
- **Slash commands missing:** check `guild_id`, then restart. Global sync can take up to an hour.
- **Translate does nothing:** Message Content intent must be on in the portal **and** in config, the channel ID must be in `translate.channels`, and check the logs for backend errors (bad URL or key).
- **Verify says it can't give the role:** give the bot Manage Roles and move its role above the verified role.
- **No MC alerts:** the first check only records the state, so alerts fire on the next change. Test with `/mcstatus`.

## License

MIT (see `LICENSE`). Multi-client hosting docs are in [`HOSTING.md`](HOSTING.md).

