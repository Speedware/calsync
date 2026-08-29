# calsync — umeos.ru schedule → .ics, served over HTTPS by Caddy

Schedule is fetched with a public POST to
`https://umeos.ru/blocks/umerasp/json_rasper.php`.
Past lessons in the .ics are kept, future ones are refreshed.

Two services:

- **calsync** — background loop, regenerates the `.ics` for every group in `config.json`.
- **calsync-web** — page at `/` where a student enters a group; it is validated
  against umeos.ru and a subscription link `…/cal/<slug>.ics` is issued (slug:
  the group's saved one, or auto-transliterated for a new group). The group is
  added to `config.json` so the loop keeps it fresh.

Caddy serves `/cal/*.ics` as files and proxies everything else to calsync-web.
Page assets live in `web/`.

## Deploy (Debian/Ubuntu, root, domain A record already pointing here)

```bash
curl -fsSL https://raw.githubusercontent.com/Speedware/calsync/main/deploy/install.sh | sudo bash
```

Prompts for domain, refresh interval (minutes, default 60), and optional
group + slug pairs. Installs git/python/caddy, both systemd services, and
Caddy with automatic SSL.

Subscription URL: `https://<domain>/cal/<slug>.ics`

## Control

    calsync list                 groups and links
    calsync add <group> <slug>   add a group manually
    calsync rm <slug>            remove a group
    calsync update               refresh now
    calsync interval <minutes>   change refresh period (min 60)
    calsync status               both services + group list
    calsync logs [web]           service logs
    calsync upgrade              git pull + re-render units/Caddyfile + restart

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.json config.json
.venv/bin/python calsync.py --once      # one refresh
.venv/bin/python calsync.py             # loop
.venv/bin/python web.py                 # page on http://127.0.0.1:8000
```
