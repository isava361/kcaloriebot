# Telegram Mini App

The Mini App provides a Russian mobile diary with day navigation, calorie goal
progress, partial/unknown macro indicators, food logging, deletion, and favorites.
New foods use nutrition per 100 g; existing serving-based favorites accept a
serving count. New entries are recorded at the current time on today's local day.
Editing entries, weight tracking, and week/month statistics remain in the bot.

The HTTP server and polling bot run as separate processes against the same
`DATABASE_PATH`. The frontend has no build step. The bot still works when the
optional web server is disabled.

## Local run

```bash
python -m pip install -e ".[dev,miniapp]"
# Set BOT_TOKEN and DATABASE_PATH in the process environment.
python -m kcaloriebot.web --host 127.0.0.1 --port 8080
```

Opening http://127.0.0.1:8080 in an ordinary browser displays instructions to
open the app from Telegram. Real data requires signed Telegram `initData`; there
is no public demo account or authentication bypass. HTTP tests generate their
own signatures with a fake token and use temporary databases:

```bash
python -m unittest tests.test_web -v
node --check kcaloriebot/static/app.js
```

## Ubuntu deployment

Use the account, paths, bot service, and environment file from the main README.
Take the documented database backup before updating the installation. Install
the additional web dependency:

```bash
sudo -u kcaloriebot -H /opt/kcaloriebot/.venv/bin/python -m pip install \
  -e "/opt/kcaloriebot/app[miniapp]"
```

Create `/etc/systemd/system/kcaloriebot-web.service`:

```ini
[Unit]
Description=KCalorieBot Mini App
After=network.target
PartOf=kcalculatorbot.service

[Service]
Type=simple
User=kcaloriebot
Group=kcaloriebot
WorkingDirectory=/opt/kcaloriebot/app
EnvironmentFile=/etc/kcaloriebot.env
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/opt/kcaloriebot/.venv/bin/python -m kcaloriebot.web --port 8080
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
UMask=0077
StateDirectory=kcaloriebot
StateDirectoryMode=0750
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/var/lib/kcaloriebot
```

Create `/etc/systemd/system/kcalculatorbot.service.d/miniapp.conf`
(create the parent directory first):

```ini
[Unit]
Wants=kcaloriebot-web.service
```

This ties the web process to bot stop/restart operations. Both writers stop for
the README's update/restore procedure; starting the bot also starts the web
server. The current update script checks only bot health, so also check the web
service after updates. Reinstall `.[miniapp]` when its dependencies change.

Point a domain such as `food.example.com` at the server and configure your HTTPS
reverse proxy to forward to `127.0.0.1:8080`. Serve the app at the domain root.
For example, with Caddy installed and ports 80/443 reachable, add this site to
`/etc/caddy/Caddyfile` and reload Caddy:

```caddyfile
food.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

Add the actual URL to `/etc/kcaloriebot.env`:

```dotenv
MINIAPP_URL=https://food.example.com/
```

Keep the existing `BOT_TOKEN` and `DATABASE_PATH`. Then:

```bash
sudo systemctl daemon-reload
sudo systemctl restart kcalculatorbot
sudo systemctl status kcaloriebot-web --no-pager
curl -I https://food.example.com/
```

Send `/app` in a private chat with the bot and tap **Открыть дневник**. Optionally
set the same URL as the bot's menu button through BotFather's `/setmenubutton`.

## Authorization and operation

The frontend sends `Telegram.WebApp.initData` in an Authorization header. The
server verifies Telegram's HMAC signature, rejects duplicate fields, requires a
valid user ID, and rejects credentials older than one hour (or more than 30
seconds in the future). After expiry, close and reopen the app. User IDs from
request bodies or query parameters do not select another user's diary.

The server limits request bodies to 16 KiB and exposes only three static files.
API responses disable caching. The bot token stays on the server; access logs
are disabled. Do not configure a reverse proxy to log Authorization headers.
The database keeps the same owner checks and nutrition validation as the bot.

Food submissions are not retried automatically after a network failure. If a
response is lost, reopen the diary to check whether it was saved before trying
again. Chat drafts remain independent of Mini App actions.

See the official [Telegram Mini Apps documentation](https://core.telegram.org/bots/webapps)
for launch buttons and the `initData` verification protocol.
