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
Use the Nginx instructions below if it is already installed on your server.

### Nginx on an existing Ubuntu server

Nginx accepts HTTPS requests on port 443 and forwards them to the Mini App on
`127.0.0.1:8080`. These instructions assume Nginx and the Python service run
directly on the same server. If Nginx runs in Docker, `127.0.0.1` inside its
container refers to that container; the upstream address needs to match your
container network instead.

#### 1. Prepare the domain and start the web service

Replace `food.example.com` everywhere below with your own subdomain, for example
`food.your-domain.ru`. Create its DNS `A` record pointing to the server's public
IPv4 address. If it has an `AAAA` record, IPv6 must also reach this server.
Allow inbound TCP ports 80 and 443 in the server/provider firewall. Port 8080
stays local to the server.

Use a dedicated subdomain: the frontend requests `/static/...` and `/api/...`
from the domain root, so mounting it under `/miniapp/` needs code changes.
Other sites can keep using the same Nginx instance through their own
`server_name` blocks.

After creating the systemd unit and drop-in above, start the web service and
check it locally:

```bash
sudo systemctl daemon-reload
sudo systemctl start kcaloriebot-web
sudo systemctl status kcaloriebot-web --no-pager
curl -I http://127.0.0.1:18081/
```

The last command should return `200 OK`. If it fails, inspect
`sudo journalctl -u kcaloriebot-web -n 100 --no-pager` before configuring Nginx.

#### 2. Add a site to Nginx

For Ubuntu's standard `sites-available` / `sites-enabled` layout, create:

```bash
sudoedit /etc/nginx/sites-available/kcaloriebot
```

Paste this initial HTTP configuration:

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name food.example.com;

    client_max_body_size 16k;

    location / {
        proxy_pass http://127.0.0.1:18081;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Authorization $http_authorization;
        proxy_cache off;
        proxy_intercept_errors off;
    }
}
```

If IPv6 is disabled on the server, omit `listen [::]:80;`. Enable this new site
once, validate the whole Nginx configuration, and reload only if validation
succeeds:

```bash
sudo ln -s /etc/nginx/sites-available/kcaloriebot /etc/nginx/sites-enabled/kcaloriebot
sudo nginx -t && sudo systemctl reload nginx
curl -I http://food.example.com/
```

Skip `ln -s` if that link already exists. If your Nginx installation uses
`conf.d/*.conf` instead, save the server block as
`/etc/nginx/conf.d/kcaloriebot.conf` and omit the symlink. Use only one layout for
this site. If the chosen hostname already has a server block, edit that block
instead of adding a duplicate.

The `location /` block proxies both the frontend and API. It preserves Telegram's
Authorization header and disables inherited proxy caching for this app. See
the [Nginx proxy module reference](https://nginx.org/en/docs/http/ngx_http_proxy_module.html).

#### 3. Enable HTTPS

If this hostname already has a working HTTPS server block and a certificate
covering it, put the same `location /` block and `client_max_body_size` directive
in that HTTPS block, then run `sudo nginx -t && sudo systemctl reload nginx`.
Keep its existing certificate and renewal configuration.

Otherwise, use Certbot's Nginx plugin to obtain and install a certificate. If
Certbot is already installed, use that installation with the Nginx plugin.
For a server without Certbot, Ubuntu provides these packages:

```bash
sudo apt update
sudo apt install -y certbot python3-certbot-nginx
```

Once the public DNS record resolves correctly and HTTP is reachable, run:

```bash
sudo certbot --nginx -d food.example.com --redirect
sudo nginx -t && sudo systemctl reload nginx
curl -I https://food.example.com/
sudo certbot renew --cert-name food.example.com --dry-run
```

Certbot adds the HTTPS listener and certificate settings to the matching Nginx
site and enables HTTP-to-HTTPS redirection. Follow its prompts for an email
address and the certificate authority's terms. HTTPS should return `200 OK`,
and the renewal dry run should succeed. Check that automated renewal is scheduled
with `systemctl list-timers --all`; for the Ubuntu apt installation the timer
is normally `certbot.timer`. Keep port 80 reachable for HTTP validation on renewal.
See the [Certbot Nginx guide](https://eff-certbot.readthedocs.io/en/stable/using.html#nginx)
and the [Ubuntu Nginx plugin package](https://packages.ubuntu.com/noble/python3-certbot-nginx).

Use the actual certificate name from `sudo certbot certificates` if it differs
from the hostname. Without `--cert-name`, the dry run checks every certificate
on the server, including certificates for other applications.

If the renewal check reports a `404` for `/.well-known/acme-challenge/...`,
inspect the domain's DNS and its active port-80 Nginx server block. A working
Mini App homepage does not prove that challenge requests reach the temporary
location installed by Certbot. Check the effective configuration with
`sudo nginx -T` and the port owner with `sudo ss -ltnp 'sport = :80'` before
changing the routing. If another certificate reports that port 80 is occupied,
check its saved authenticator: a `standalone` renewal needs to bind that port,
whereas the Nginx plugin uses the running Nginx server. Those certificates need
their own renewal configuration review. A failed dry run does not replace the
currently installed certificate.

##### Use a permanent webroot for certificate validation

If the Nginx authenticator's temporary challenge route fails, a permanent
webroot makes the HTTP validation route independently testable. This procedure
uses `certbot reconfigure`, available in Certbot 2.3.0 and newer; check with
`certbot --version` first.

Create the challenge directory and a public test file:

```bash
sudo install -d -m 755 /var/www/letsencrypt/.well-known/acme-challenge
printf 'acme-ok\n' | sudo tee /var/www/letsencrypt/.well-known/acme-challenge/kcaloriebot-check
```

In the Mini App's enabled Nginx site, replace its **port-80 server block** with
the following, substituting your hostname. Retain the existing HTTPS block.
The redirect belongs inside `location /`, so it does not intercept challenges:

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name food.example.com;

    location ^~ /.well-known/acme-challenge/ {
        root /var/www/letsencrypt;
        default_type text/plain;
        try_files $uri =404;
    }

    location / {
        return 301 https://food.example.com$request_uri;
    }
}
```

Apply and test the public HTTP route, also from another machine if possible:

```bash
sudo nginx -t && sudo systemctl reload nginx
curl -i http://food.example.com/.well-known/acme-challenge/kcaloriebot-check
```

Continue only if the response is `200 OK` with body `acme-ok`. A `404` still
requires checking DNS (including any AAAA records), enabled server blocks,
and any proxy in front of this Nginx instance.

Switch this certificate's authenticator to webroot:

```bash
sudo certbot reconfigure --cert-name food.example.com \
  --authenticator webroot --webroot-path /var/www/letsencrypt
sudo certbot renew --cert-name food.example.com --dry-run
```

`reconfigure` tests the new settings and saves them only on success. For the
certificate originally installed with `--nginx`, its existing Nginx installer
is retained. Keep the challenge directory and Nginx location for future
renewals. This changes only the named certificate; certificates belonging to
other applications need their own domain routing and renewal setup reviewed.
See [Certbot renewal configuration](https://eff-certbot.readthedocs.io/en/stable/using.html#modifying-the-renewal-configuration-of-existing-certificates).

#### 4. Check the API and connect Telegram

```bash
curl -i https://food.example.com/api/diary
```

This unauthenticated request should return `401 Unauthorized` with a JSON error:
the API requires signed Telegram credentials. A `502 Bad Gateway` points to an
unavailable Python service or an incorrect upstream address; check its journal
and the local `curl` command from step 1. An Nginx welcome page or another site's
HTML points to the DNS record, `server_name`, or site activation.

Continue with **Connect the Mini App to the bot** below to set `MINIAPP_URL` and
restart the bot.

#### If port 8080 is already in use

`OSError: [Errno 98] ... ('127.0.0.1', 8080): address already in use` means
another process is already listening on the requested address. Stop the failed
service's restart loop and inspect the occupied port and a possible replacement:

```bash
sudo systemctl stop kcaloriebot-web
sudo ss -ltnp 'sport = :8080'
sudo ss -ltnp 'sport = :18081'
```

If the second port check shows no listening sockets, use `18081` for the Mini
App. If it is also occupied, choose another free port and substitute it in all
commands below. Edit the web service:

```bash
sudoedit /etc/systemd/system/kcaloriebot-web.service
```

Replace its `ExecStart` line with:

```ini
ExecStart=/opt/kcaloriebot/.venv/bin/python -m kcaloriebot.web --host 127.0.0.1 --port 18081
```

In the Mini App's Nginx site (`/etc/nginx/sites-available/kcaloriebot` in the
example above), update the upstream in its active proxy location, including
the HTTPS server block if the certificate has already been configured:

```nginx
proxy_pass http://127.0.0.1:18081;
```

Apply and check both services:

```bash
sudo systemctl daemon-reload
sudo systemctl restart kcaloriebot-web
sudo systemctl status kcaloriebot-web --no-pager
curl -I http://127.0.0.1:18081/
sudo nginx -t && sudo systemctl reload nginx
curl -I https://food.example.com/
```

Both `curl` checks should return `200 OK` once HTTPS is configured. The public
`MINIAPP_URL` remains `https://food.example.com/`: Nginx still accepts HTTPS on
port 443, and only its internal upstream port changes.

#### If another service owns HTTPS port 443

Nginx can pass `nginx -t` and accept a reload signal but still fail to apply
the configuration when binding a new listening port. It then keeps serving
the previous configuration. This can explain why an ACME request reaches the
Python app even though `nginx -T` shows a correct static-file location. Inspect:

```bash
sudo tail -n 60 /var/log/nginx/error.log
sudo ss -ltnp '( sport = :80 or sport = :443 )'
```

If another service (for example `mtg`) owns port 443, one option is to put the
Mini App's HTTPS listener on a free port such as 8443. Check it first:

```bash
sudo ss -ltnp 'sport = :8443'
```

If no listening sockets are listed, edit the Mini App's Nginx site. Replace
both HTTPS listen directives with:

```nginx
listen 8443 ssl;
listen [::]:8443 ssl ipv6only=on;
```

Retain the certificate paths and the proxy to the Python service (for example
`127.0.0.1:18081` if you changed its port). In the port-80 server block, keep
the permanent ACME location from the webroot section and update the ordinary
redirect inside `location /`:

```nginx
return 301 https://food.example.com:8443$request_uri;
```

Allow inbound TCP 8443 in the server and provider firewalls. If UFW is already
active, its rule is `sudo ufw allow 8443/tcp`. Apply and verify the new listener
and the HTTP challenge independently:

```bash
sudo nginx -t && sudo systemctl reload nginx
sudo ss -ltnp 'sport = :8443'
curl -q --noproxy '*' -i http://food.example.com/.well-known/acme-challenge/kcaloriebot-check
curl -q --noproxy '*' -I https://food.example.com:8443/
```

The HTTP challenge must return `200 OK` with `acme-ok`, and the HTTPS homepage
must return `200 OK`. Complete the webroot `certbot reconfigure` procedure
above after the challenge succeeds. HTTP certificate validation still uses
port 80.

Set the public URL including the HTTPS port:

```dotenv
MINIAPP_URL=https://food.example.com:8443/
```

Restart `kcalculatorbot` and request a fresh `/app` button. If a menu button or
Main Mini App URL was configured in BotFather, update it to this same URL.
Use this URL for subsequent HTTPS checks instead of the port-443 examples in
this guide. Verify access from the phone's network as well as from the server.

See [Nginx reload behavior](https://nginx.org/en/docs/control.html#reconfiguration).

### Alternative: Caddy

For a server using Caddy as its HTTPS proxy, add this site to
`/etc/caddy/Caddyfile` and reload Caddy with ports 80/443 reachable:

```caddyfile
food.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

### Connect the Mini App to the bot

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
