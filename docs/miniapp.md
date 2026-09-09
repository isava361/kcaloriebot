# Telegram Mini App

Для уже настроенного сервера из нашего чата см. раздел
[Деплой изменений на food.ivansavelyev.ru](#deploy-food-server).

The Mini App provides a Russian mobile diary with day navigation, calorie goals,
partial/unknown macro indicators, and these workflows:

- Add food on the selected diary day, with an editable local date/time. Today
  defaults to the current time; past days default to noon. New or moved entries
  must be within the last 366 days. Existing older entries can still be edited
  without changing their time.
- Enter nutrition per 100 g or per serving, repeat recent food, or search
  favorites.
- Edit names, amounts, nutrition and dates. Concurrent changes in the bot or
  another window produce a conflict instead of overwriting newer data.
- Delete a food entry and undo it using the toast for 15 minutes. The undo
  button lasts until dismissed, expired, or the page is reloaded.
- View 7 days ending on the selected date, or its calendar month (through today
  for the current month), with daily calorie bars. Missing days remain missing;
  the calorie average uses only days with entries. Tap a bar to open that day.
- Record, correct and delete weight measurements. The journal shows measurements
  through the selected day, a 30-day chart of daily means, and means of all
  measurements in the last 7 days and preceding 7 days.

Entries are grouped into meals by their local time (breakfast, lunch, afternoon
snack, dinner, night snack) with per-meal totals. The summary shows the calories
left against the goal, the day's energy split by meal on one bar, and the share
of energy from each macronutrient. The strip under the date shows the seven days
ending on the selected one, scaled to the goal when one is set.

Telegram's theme takes precedence over the system theme and updates live.
MainButton submits the open form; BackButton closes dialogs with a dirty-form
confirmation. Older clients retain ordinary HTML controls. Supported clients
disable vertical swipe-to-minimize and adjust dialogs to the visible viewport.

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

Optional mobile browser checks use a fake Telegram SDK, signed test credentials,
temporary SQLite, and headless Chromium. They cover backdating, a committed but
lost response, draft recovery, editing, undo, theme changes, statistics and weight:

```bash
npm install --prefix data/ui-check --no-save playwright
data/ui-check/node_modules/.bin/playwright install chromium
RUN_MINIAPP_UI=1 python -m unittest tests.test_miniapp_ui -v
```

On PowerShell use `$env:RUN_MINIAPP_UI='1'` before the Python command. Optionally
set `CHROME_PATH` to an installed Chrome executable, or `PLAYWRIGHT_MODULE_PATH`
to another Playwright installation. These checks simulate Telegram integration;
also verify keyboard, back gestures and theme switching on a real phone.

<a id="deploy-food-server"></a>

## Деплой изменений на food.ivansavelyev.ru

Этот раздел фиксирует настройку из переписки 8–9 сентября 2026 года и порядок
обновления уже работающего приложения. Общая инструкция установки с нуля — ниже.

### Что известно о сервере

| Параметр | Значение из переписки |
| --- | --- |
| Каталог, из которого выполнялись команды | `/opt/kcaloriebot` |
| Путь загруженного Python-модуля в traceback | `/opt/kcaloriebot/app/kcaloriebot/web.py` |
| Python виртуального окружения | `/opt/kcaloriebot/.venv/bin/python` |
| Служба Mini App | `kcaloriebot-web.service` |
| Служба бота | `kcalculatorbot.service` — подтверждена списком systemd |
| Пользователь и рабочий каталог web | `kcaloriebot`, `/opt/kcaloriebot/app` — подтверждены systemd |
| Внутренний адрес Mini App | `http://127.0.0.1:18081/`; 8080 оказался занят |
| Nginx-сайт | `/etc/nginx/sites-available/kcaloriebot`, подключён через `sites-enabled` |
| Домен | `food.ivansavelyev.ru` |
| DNS A на момент диагностики | `212.227.127.234` |
| Порт 443 на момент диагностики | Занят процессом `mtg` |
| HTTPS | Nginx слушает 8443; адрес для проверки — `https://food.ivansavelyev.ru:8443/` |
| Каталог HTTP-проверок сертификатов | `/var/www/letsencrypt/.well-known/acme-challenge/` |

Путь рабочей БД в присланных логах пока не подтверждён. В общей инструкции
используется `/var/lib/kcaloriebot/kcaloriebot.db`; перед командами обновления
сверь его с реальной установкой. Каталог приглашения shell `/opt/kcaloriebot` сам по себе
не означает, что именно там находится Git checkout.

### 1. Подготовить изменения и сверить установку

Изменения агента сначала находятся на локальном компьютере. Их нужно проверить,
закоммитить и отправить в ту ветку репозитория, из которой обновляется сервер.
`git pull` на сервере получает только опубликованные коммиты. Само редактирование
файлов в Codex ничего на сервер не отправляет.

Следующие команды выполняются **на Ubuntu в Bash**, под root, как в переписке:

```bash
sudo systemctl list-unit-files 'kcal*'
sudo systemctl show kcaloriebot-web --no-pager -p User -p WorkingDirectory -p ExecStart -p EnvironmentFiles
sudo ss -ltnp '( sport = :80 or sport = :443 or sport = :8443 or sport = :18081 )'
```

Сверь unit бота и web, в том числе их `EnvironmentFile` и `DATABASE_PATH`.
Оба процесса должны использовать **один и тот же абсолютный путь БД**. Если
`DATABASE_PATH` не задан, приложение использует `data/kcaloriebot.db` внутри
каталога проекта; относительный путь считается от рабочего каталога процесса.
Не выбирай БД только по похожему имени файла.

Задай переменные **после `sudo -i`, в той же root-сессии**, в которой будешь
выполнять обновление. Этот шаг обязателен: диагностические команды выше
переменные не задают. Новая SSH-сессия или новый `sudo -i` их не сохраняют.
Путь `DB_PATH` ниже — пример, который требуется сверить с настройками.
Для подтверждённого web EnvironmentFile можно вывести только эту строку:

```bash
sudo grep -E '^[[:space:]]*DATABASE_PATH=' /etc/kcaloriebot.env
```

Если строки нет, проверь также `Environment=` и рабочие каталоги обоих unit-файлов:
пустой вывод не подтверждает пример пути БД. После проверки выполни:

```bash
APP_DIR=/opt/kcaloriebot/app
VENV_PY=/opt/kcaloriebot/.venv/bin/python
BOT_SERVICE=kcalculatorbot.service
WEB_SERVICE=kcaloriebot-web.service
DB_PATH=/var/lib/kcaloriebot/kcaloriebot.db
PUBLIC_URL=https://food.ivansavelyev.ru:8443

sudo systemctl show "$BOT_SERVICE" -p User -p WorkingDirectory -p ExecStart -p EnvironmentFiles
sudo systemctl cat "$BOT_SERVICE" "$WEB_SERVICE"
```

Если checkout находится в другом каталоге, исправь `APP_DIR`. Сохрани действующие
настройки окружения и порты: `ExecStart` web должен содержать `--port 18081`,
а `proxy_pass` в Nginx — `http://127.0.0.1:18081`.

### 2. Обновить код и БД

Ниже один блок обновления. Он запускается в дочерней оболочке и прекращается
при ошибке. Если ошибка случилась после остановки служб, сначала устрани её
или выполни откат из следующего раздела: блок не запускает старый код поверх
уже обновлённой БД автоматически.

Нужны `git`, `sqlite3`, `sudo` и существующее виртуальное окружение. Никакая
сборка frontend через npm на сервере не требуется.

Ошибка `APP_DIR: unbound variable` означает, что пропущен блок переменных шага 1
или он выполнен в другой оболочке. На этой строке службы ещё не остановлены,
код и БД не изменены. Задай переменные в текущей root-сессии и повтори блок.

```bash
(
set -euo pipefail
for deploy_var in APP_DIR VENV_PY BOT_SERVICE WEB_SERVICE DB_PATH; do
    if [ -z "${!deploy_var:-}" ]; then
        printf 'Не задана %s. Выполните блок переменных шага 1 в этой root-сессии.\n' "$deploy_var" >&2
        exit 1
    fi
done
command -v sqlite3 >/dev/null
test -f "$APP_DIR/pyproject.toml"
test -x "$VENV_PY"
test -f "$DB_PATH"
systemctl cat "$BOT_SERVICE" "$WEB_SERVICE" >/dev/null

CODE_USER=$(stat -c '%U' "$APP_DIR")
VENV_USER=$(stat -c '%U' /opt/kcaloriebot/.venv)
BOT_USER=$(systemctl show "$BOT_SERVICE" -p User --value)
BOT_USER=${BOT_USER:-root}
test "$(sudo -u "$CODE_USER" git -C "$APP_DIR" rev-parse --show-toplevel)" = "$APP_DIR"
test -z "$(sudo -u "$CODE_USER" git -C "$APP_DIR" status --porcelain)"
OLD_COMMIT=$(sudo -u "$CODE_USER" git -C "$APP_DIR" rev-parse HEAD)
BRANCH=$(sudo -u "$CODE_USER" git -C "$APP_DIR" branch --show-current)
test -n "$BRANCH"
sudo -u "$CODE_USER" git -C "$APP_DIR" fetch origin "$BRANCH"
NEW_COMMIT=$(sudo -u "$CODE_USER" git -C "$APP_DIR" rev-parse "origin/$BRANCH")
sudo -u "$CODE_USER" git -C "$APP_DIR" merge-base --is-ancestor "$OLD_COMMIT" "$NEW_COMMIT"
sudo -u "$CODE_USER" git -C "$APP_DIR" log --oneline "$OLD_COMMIT..$NEW_COMMIT"

# Останавливаем оба процесса, даже если PartOf/Wants ещё не настроены.
systemctl stop "$BOT_SERVICE" "$WEB_SERVICE"
test "$(systemctl show "$BOT_SERVICE" -p ActiveState --value)" = inactive
test "$(systemctl show "$WEB_SERVICE" -p ActiveState --value)" = inactive

BACKUP_DIR="/var/backups/kcaloriebot/deploy-$(date +%Y%m%d-%H%M%S)"
install -d -m 700 -o "$BOT_USER" "$BACKUP_DIR"
sudo -u "$BOT_USER" sqlite3 "$DB_PATH" ".backup '$BACKUP_DIR/before.db'"
test "$(sudo -u "$BOT_USER" sqlite3 -readonly "$BACKUP_DIR/before.db" 'PRAGMA integrity_check;')" = ok
chmod 600 "$BACKUP_DIR/before.db"
printf '%s\n' "$OLD_COMMIT" > "$BACKUP_DIR/old-commit.txt"
printf '%s\n' "$BRANCH" > "$BACKUP_DIR/branch.txt"
printf '%s\n' "$DB_PATH" > "$BACKUP_DIR/database-path.txt"
"$VENV_PY" -m pip freeze > "$BACKUP_DIR/pip-freeze.txt"
printf 'Backup: %s\nPrevious commit: %s\n' "$BACKUP_DIR" "$OLD_COMMIT"

# Устанавливаем именно проверенный выше коммит, без удаления локальных файлов.
sudo -u "$CODE_USER" git -C "$APP_DIR" merge --ff-only "$NEW_COMMIT"
sudo -u "$VENV_USER" -H "$VENV_PY" -m pip install -e "$APP_DIR[miniapp]"
sudo -u "$BOT_USER" -H "$VENV_PY" -m unittest discover -s "$APP_DIR/tests" -t "$APP_DIR"

# Миграция выполняется одним процессом до запуска обоих сервисов.
sudo -u "$BOT_USER" -H "$VENV_PY" -c \
  'import sys; from kcaloriebot.database import Database; Database(sys.argv[1]).initialize()' "$DB_PATH"
sudo -u "$BOT_USER" sqlite3 -readonly "$DB_PATH" 'PRAGMA user_version; PRAGMA integrity_check;'

systemctl start "$BOT_SERVICE" "$WEB_SERVICE"
curl -q --noproxy '*' --fail --retry 10 --retry-connrefused --retry-delay 1 \
  -I http://127.0.0.1:18081/
systemctl is-active "$BOT_SERVICE" "$WEB_SERVICE"
)
```

Для текущего релиза проверка БД должна вывести `6` и `ok`. Схема 6 добавляет
журнал повторных запросов и снимки удалённых записей; данные бота сохраняются.
Если проверка чистоты Git или возможности fast-forward не прошла, сначала
разбери локальные изменения/расхождение веток. Этот блок не делает `reset --hard`.

Используется SQLite `.backup`, чтобы получить согласованную копию базы, включая
данные из WAL. Не заменяй её копированием только файла `.db` работающего бота.
См. [команды SQLite CLI](https://www.sqlite.org/cli.html).

`scripts/update.sh` рассчитан на значения из общей инструкции: он ставит
`.[miniapp]`, запускает `kcaloriebot-web` явно после бота и проваливает
обновление, если web-юнит не удержался. Имя юнита в скрипте — `kcaloriebot-web`;
пока имена и пути не сверены с сервером, используй явное обновление обоих
процессов выше.

### 3. Проверить после обновления

```bash
sudo systemctl status "$BOT_SERVICE" "$WEB_SERVICE" --no-pager
sudo journalctl -u "$BOT_SERVICE" -u "$WEB_SERVICE" -n 80 --no-pager
curl -q --noproxy '*' -I "$PUBLIC_URL/"
curl -q --noproxy '*' -i "$PUBLIC_URL/api/diary"
```

Ожидается: главная страница — `200 OK`, API без Telegram-авторизации — `401`.
На HTML/CSS/JS теперь есть `Cache-Control: no-cache` и `ETag`, на API — `no-store`.
Полностью закрой Mini App в Telegram, получи кнопку через `/app` и открой заново.
Проверь добавление на вчерашнюю дату, редактирование, отмену удаления, недавнее,
поиск избранного, статистику и запись веса. По журналу убедись, что службы
не перезапускаются циклически.

Сразу после `systemctl start` процесс ещё может запускаться: в переписке
`status` показывал `active` спустя 79 мс, а первый `curl` получил отказ соединения.
Поэтому локальная проверка выше использует повторные попытки. Проверяй `/`,
а не `/~`: случайный `~` в URL давал ожидаемый `404` уже работающего приложения.

При обычном обновлении Python/HTML/CSS/JS достаточно перезапустить службы.
Web читает статику в память при запуске, поэтому перезапуск нужен даже после
изменения только CSS. `systemctl daemon-reload` нужен после изменения unit-файлов;
Nginx reload — после изменения его конфигурации. Повторно выпускать сертификаты
при каждом деплое не нужно.

### 4. Если снова мешают Nginx или сертификаты

В нашей диагностике `nginx -t` проходил, а reload не применял изменения: Nginx
не мог занять 443 из-за `mtg` и продолжал обслуживать старую конфигурацию.
Из-за этого ACME-запрос возвращал JSON `404` от Python, хотя в `nginx -T`
уже был правильный `location`. Проверяй не только синтаксис:

```bash
sudo nginx -t
sudo tail -n 60 /var/log/nginx/error.log
sudo ss -ltnp '( sport = :80 or sport = :443 or sport = :8443 )'
curl -q --noproxy '*' -i \
  http://food.ivansavelyev.ru/.well-known/acme-challenge/kcaloriebot-check
```

Последняя команда ожидает `200` и `acme-ok`, если тестовый файл из инструкции
webroot ниже сохранён. Если используется вариант с 8443, этот порт должен быть
в HTTPS `listen`, обычном HTTP-редиректе, `MINIAPP_URL` и URL кнопки в BotFather.
`mtg` при этом продолжает использовать 443. Сам факт успешного reload в systemd
ещё не подтверждает применение конфигурации: см.
[описание reload Nginx](https://nginx.org/en/docs/control.html#reconfiguration).

В переписке `panel.ivansavelyev.ru` и `vps.ivansavelyev.ru` использовали
`authenticator = standalone` и конфликтовали с Nginx на 80. Их исправление —
отдельная настройка: HTTP ACME-location для каждого домена должен отдавать
тот же webroot; затем для каждого сертификата выполняется `certbot reconfigure`
с `--authenticator webroot --webroot-path /var/www/letsencrypt`. Успешный результат
этих настроек в присланных логах не зафиксирован. Не добавляй дублирующий
`server_name`, если блок домена уже существует; используй процедуру webroot ниже.
Проверять сертификат Mini App отдельно можно так:

```bash
sudo certbot renew --cert-name food.ivansavelyev.ru --dry-run
```

Без `--cert-name` проверяются все сертификаты, включая `panel` и `vps`.
Официальная процедура:
[изменение настроек продления Certbot](https://eff-certbot.readthedocs.io/en/stable/using.html#modifying-the-renewal-configuration-of-existing-certificates).

### 5. Откат неудачного обновления

Откат БД возвращает состояние **до деплоя**: новые записи, сделанные после него,
в этой копии отсутствуют. Сначала останови оба сервиса и сохрани также копию
текущей БД. Для миграции 5 → 6 требуется вернуть и код, и БД; одного старого
коммита недостаточно.

В той же SSH-сессии сохрани переменные шага 1 и укажи каталог резервной копии,
напечатанный при обновлении. Следующий блок оставляет checkout на старом коммите
в detached HEAD, чтобы не переписывать историю рабочей ветки:

```bash
BACKUP_DIR=/var/backups/kcaloriebot/deploy-YYYYMMDD-HHMMSS
(
set -euo pipefail
test -f "$DB_PATH"
test -f "$BACKUP_DIR/before.db"
OLD_COMMIT=$(cat "$BACKUP_DIR/old-commit.txt")
test "$(cat "$BACKUP_DIR/database-path.txt")" = "$DB_PATH"
CODE_USER=$(stat -c '%U' "$APP_DIR")
VENV_USER=$(stat -c '%U' /opt/kcaloriebot/.venv)
BOT_USER=$(systemctl show "$BOT_SERVICE" -p User --value)
BOT_USER=${BOT_USER:-root}
test -z "$(sudo -u "$CODE_USER" git -C "$APP_DIR" status --porcelain)"
systemctl stop "$BOT_SERVICE" "$WEB_SERVICE"
test "$(systemctl show "$BOT_SERVICE" -p ActiveState --value)" = inactive
test "$(systemctl show "$WEB_SERVICE" -p ActiveState --value)" = inactive
sudo -u "$BOT_USER" sqlite3 "$DB_PATH" ".backup '$BACKUP_DIR/failed-$(date +%Y%m%d-%H%M%S).db'"
test "$(sudo -u "$BOT_USER" sqlite3 -readonly "$BACKUP_DIR/before.db" 'PRAGMA integrity_check;')" = ok
sudo -u "$CODE_USER" git -C "$APP_DIR" switch --detach "$OLD_COMMIT"
sudo -u "$VENV_USER" -H "$VENV_PY" -m pip install -e "$APP_DIR[miniapp]"
sudo -u "$BOT_USER" sqlite3 "$DB_PATH" ".restore '$BACKUP_DIR/before.db'"
test "$(sudo -u "$BOT_USER" sqlite3 -readonly "$DB_PATH" 'PRAGMA integrity_check;')" = ok
systemctl start "$BOT_SERVICE" "$WEB_SERVICE"
curl -q --noproxy '*' --fail --retry 10 --retry-connrefused --retry-delay 1 \
  -I http://127.0.0.1:18081/
)
```

После отката повтори проверки шага 3. Перед следующим обновлением выбери
исправленный коммит/ветку осознанно: сохранённая ветка из `branch.txt` может
по-прежнему указывать на релиз, который пришлось откатить.

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
server. `PartOf=` alone propagates stop and restart but never start, which is
why `scripts/update.sh` starts `kcaloriebot-web` explicitly: without that, an
update leaves the Mini App down and Nginx answering 502. The script also
installs `.[miniapp]` and fails the update if the web unit does not stay
running, printing its last journal lines.

This release upgrades SQLite schema 5 to 6, adding durable operation receipts
and temporary deleted-entry snapshots. Back up the database and stop **both**
processes before upgrading. Run the same code version for bot and web; rollback
requires the pre-upgrade database backup. Restart the web service after every
frontend deployment: it loads the three public assets into memory at startup.

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
curl -I http://127.0.0.1:8080/
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
        proxy_pass http://127.0.0.1:8080;
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

Food and weight mutations use a client-generated `Idempotency-Key`, with up to
three attempts after a network/server failure. The mutation and response receipt
commit in one SQLite transaction. The same user/key/payload replays its original
response even after a server restart; another payload with that key returns 409.
Receipts are retained without expiry, so this table grows with usage. Editing and
deletion additionally require the record's `version` to detect concurrent edits.

Unfinished food and weight forms and pending request keys are kept in local
browser storage, scoped to the server-verified user ID. Restore a draft using
**Продолжить**: its original date and time are displayed and preserved. Storage
is local to that browser/device; clearing storage removes drafts and pending
keys. No Telegram credentials are written there. Chat drafts remain independent.

Authenticated API responses and errors use `Cache-Control: no-store`. Public
HTML/CSS/JS use `no-cache` with content-hash ETags: unchanged files return 304,
and a restart after deployment provides new validators. `no-cache` permits
storage but requires validation. The CSP retains inline styles for Telegram
theme/viewport integration; inline scripts remain disallowed.

See the official [Telegram Mini Apps documentation](https://core.telegram.org/bots/webapps)
for launch buttons and the `initData` verification protocol.
