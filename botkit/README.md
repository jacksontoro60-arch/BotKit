# BotKit

A modular Python Discord bot (discord.py 2.x). One codebase, and each server gets only the features listed in `config.json`. Secrets stay in `.env` and are never hard-coded.

## Features

| Feature (`features` name) | What it does | Commands / UI |
|---|---|---|
| `scheduler` | Posts scheduled announcements on chosen weekdays at a set time, in a configured time zone | automatic |
| `mcstatus_alert` | Polls a Minecraft Java server and posts when it goes offline or comes back online (with player count) | `/mcstatus` |
| `modflag` | Members right-click a message to flag it, and an embed with a jump link goes to a private mod channel | Apps → **Flag for mods** |
| `purge_mine` | Members delete **their own** messages in a channel, including ones older than 14 days | `/purge_mine limit older_than_days` |
| `translate` | Auto-translates messages in chosen channels, plus a right-click Translate menu. Uses LibreTranslate (default) or DeepL | Apps → **Translate** |
| `verify` | A Verify button grants a role, with an optional math captcha. Attempts are logged to a mod channel, and the button keeps working after restarts | `/verify_setup` (admins) |
| `tickets` | Members open a private thread or channel for staff, from a button or `/ticket`. Staff can close it. The buttons keep working after restarts | `/ticket`, `/ticket_close`, `/ticket_setup` (admins) |
| `welcome` | Posts a configurable message when a member joins | automatic |
| `server_logs` | Posts joins, leaves, message deletes, message edits, and role changes to a log channel | automatic |
| `rolemenu` | Members add or remove allowed roles from a button row or select menu. The menu keeps working after restarts | `/rolemenu_setup` (admins) |

## Quick setup: the wizard (recommended)

```
python setup_wizard.py
```

The wizard builds `config.json` and `.env` for a new client:

1. Asks for the bot token with hidden input and writes it **only** to `.env` (other `.env` lines such as `DEEPL_API_KEY` are kept). The token is never printed or written to `config.json`.
2. Checks the token against Discord (`GET /users/@me`) and prints the invite URL (scopes `bot` + `applications.commands`, permissions `361045781648` = View Channels, Send Messages, Manage Messages, Embed Links, Read Message History, Manage Roles, Manage Channels, View Audit Log, Manage Threads, Create Private Threads, Send Messages in Threads).
3. Lists the servers the bot is in, then that server's text channels and roles, so you pick by number.
4. Asks which of the 10 features to enable and their settings: scheduler posts, Minecraft address, mod channel, verify role (pick one, or type `c` to create a `Verified` role through the API), ticket staff role and whether tickets are private threads or channels, welcome channel and message, log channel, role-menu roles, and the translate backend URL (it can test the URL for you).
5. Writes `config.json`. If one already exists, it is first copied to `config.json.bak`.

The bot has to be invited to the server before you run the wizard. If it's in no servers yet, the wizard prints the invite URL and stops.

Options: `--config-out PATH`, `--env-out PATH`, `--no-env` (don't write the token), `--no-create-role` (read-only use of the API), and `--answers answers.json` (or `--non-interactive --answers ...`) to read every answer from a JSON file. The answers format is documented at the top of `setup_wizard.py`. Channels and roles can be given as an ID, a name, or a list number, and the token is read from `DISCORD_TOKEN` (or `token_env`). Passing `--answers` still checks the token with Discord before it writes files. Example that writes a throwaway config and leaves `.env` alone:

```
python setup_wizard.py --answers answers.json --config-out /tmp/config.test.json --no-env --no-create-role
```

## Setup

1. Python 3.10+.
2. `python -m venv .venv`, then activate it (`.venv\Scripts\activate` on Windows, `source .venv/bin/activate` on Linux/macOS).
3. `pip install -r requirements.txt`
4. Copy `.env.example` to `.env` and set `DISCORD_TOKEN`. Add `DEEPL_API_KEY` / `LIBRETRANSLATE_API_KEY` only if you use them.
5. Run `python setup_wizard.py` (see above), **or** copy `config.example.json` to `config.json` and fill in the IDs by hand. (Enable Developer Mode in Discord, then right-click → Copy ID.)
6. `python bot.py`

### Discord Developer Portal
1. Create an application, add a Bot and copy its token into `.env`.
2. Under **Privileged Gateway Intents**:
   - Enable **Message Content** only if you use `translate` auto-translation. This must match `"message_content_intent": true`. `server_logs` does not need it. Deletes and edits are logged from gateway events, and message text is included only when the intent is already on and the message is cached.
   - Enable **Server Members** if you use `welcome` or `server_logs`. This must match `"members_intent": true` (the wizard sets that when either feature is on, and the bot also turns the intent on if those features are listed). Discord disconnects with "Disallowed intent" if the portal toggle is off while the bot requests the intent.
3. Invite the bot with scopes `bot` + `applications.commands`. The wizard URL asks for View Channels, Send Messages, Embed Links, Read Message History, Manage Messages (`purge_mine`), Manage Roles (`verify`, `rolemenu`), Manage Channels (`tickets` channel mode), View Audit Log (`server_logs`), and for thread tickets: Create Private Threads, Send Messages in Threads, and Manage Threads.
4. For `verify` and `rolemenu`: in Server Settings → Roles, drag the bot's role **above** every role it assigns. For thread tickets, give staff **Manage Threads** on the parent channel so they can see private threads. Channel-mode tickets grant the staff role access on each ticket channel instead.

## Configuration (`config.json`)

| Key | Meaning |
|---|---|
| `features` | Cogs to load (file names in `cogs/` without `.py`) |
| `guild_id` | Your server ID. Slash commands sync to it instantly. Remove it for global sync (can take up to an hour) |
| `message_content_intent` | `true` is needed for `translate` auto-translation. Not required for server logs |
| `members_intent` | `true` for `welcome` and `server_logs` (joins, leaves, role changes). Must match the Server Members intent in the Developer Portal |
| `scheduler.timezone` / `posts[]` | IANA time zone. Each post has `channel_id`, `days` (`mon`…`sun`), `time` (`HH:MM`, 24 h), `message` |
| `mcstatus.address` / `channel_id` / `interval_seconds` | Server address (`host` or `host:port`), alert channel, poll interval |
| `modflag.mod_channel_id` | Where flagged messages go |
| `translate.backend` | `libretranslate` (default) or `deepl` |
| `translate.libretranslate_url` | Any LibreTranslate-compatible `/translate` endpoint (public, paid, or self-hosted e.g. `http://127.0.0.1:5000/translate`) |
| `translate.channels` | `{"CHANNEL_ID": "en"}`: messages in that channel get translated into that language |
| `translate.default_target` / `max_chars` | Target for the right-click menu (default `en`), and the per-message length cap |
| `verify.role_id` / `log_channel_id` / `captcha` | Role to grant, mod log channel (optional), and `true` for the math-captcha modal |
| `verify.title` / `text` | Text of the verification embed |
| `tickets.staff_role_id` / `mode` | Staff who can close tickets. `mode` is `thread` (default) or `channel` |
| `tickets.channel_id` | Parent channel for private threads. Required when `mode` is `thread` |
| `tickets.category_id` | Category for private ticket channels. Required when `mode` is `channel` (`channel_id` is not used) |
| `tickets.title` / `text` | Text of the ticket panel |
| `welcome.channel_id` / `message` | Join message. Placeholders: `{member}` (mention), `{name}`, `{server}`, `{count}`. Bots are skipped. Leaves are not posted here |
| `server_logs.channel_id` / `events` | Log channel. `events` is any of `join`, `leave`, `delete`, `edit`, `roles` (default: all of them) |
| `rolemenu.style` | `buttons` (each click toggles one role) or `select` (the selection is the set of menu roles the member keeps) |
| `rolemenu.roles[]` | Up to 25 roles. Each has `role_id`, `label`, and an optional `emoji` |
| `rolemenu.title` / `text` | Text of the role-menu embed |

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

From this directory:

```
python -m unittest discover -s tests -v
```

That runs offline mock tests of translate routing, the verify role path, tickets, welcome, server logs, role menus, and the setup wizard (Discord API calls are mocked). It needs no token and no network. The supervisor suite is the same command from the repository root (`tests/`). GitHub Actions runs both.

## Troubleshooting
- **Slash commands missing:** check `guild_id`, then restart. Global sync can take up to an hour.
- **Translate does nothing:** Message Content intent must be on in the portal **and** in config, the channel ID must be in `translate.channels`, and check the logs for backend errors (bad URL or key).
- **Verify or the role menu can't change a role:** give the bot Manage Roles and move its role above the roles it assigns. After editing `rolemenu.roles` or `style`, run `/rolemenu_setup` again. A menu already posted keeps working across restarts while those roles stay in config.
- **No MC alerts:** the first check only records the state, so alerts fire on the next change. Test with `/mcstatus`.
- **Tickets don't open:** thread mode needs Create Private Threads, Send Messages in Threads, and Manage Threads. Channel mode needs Manage Channels and a category id. Staff see private threads only with Manage Threads on the parent channel.
- **Welcome or join/leave/role logs never fire:** turn on Server Members in the Developer Portal. `"members_intent"` must be true, or `welcome` / `server_logs` must be in `features` (the bot requests the intent either way).
- **Deleted or edited text says "content unavailable":** that is expected without Message Content. The log still records the event, and edits include a jump link. A "Deleted by" line needs View Audit Log; members who delete their own messages usually leave no audit entry.
