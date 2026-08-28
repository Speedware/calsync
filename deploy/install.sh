#!/usr/bin/env bash
set -euo pipefail

DEFAULT_REPO="https://github.com/Speedware/calsync.git"

REPO="${1:-${CALSYNC_REPO:-$DEFAULT_REPO}}"
DIR="${CALSYNC_DIR:-/opt/calsync}"
USER_NAME="${CALSYNC_USER:-calsync}"

[[ $EUID -eq 0 ]] || { echo "run as root:  sudo bash install.sh [git-url]" >&2; exit 1; }
command -v apt-get >/dev/null || { echo "Debian/Ubuntu required (apt-get not found)" >&2; exit 1; }

read -rp "Domain (A record must already point here): " DOMAIN </dev/tty
[[ -n "$DOMAIN" ]] || { echo "domain required" >&2; exit 1; }

read -rp "Refresh interval in minutes [360]: " INTERVAL </dev/tty
INTERVAL="${INTERVAL:-360}"
[[ "$INTERVAL" =~ ^[0-9]+$ ]] || { echo "interval must be a number" >&2; exit 1; }

GROUPS=()
while true; do
	read -rp "Enter the group (format ОУИТб-ПИ01-23-3) or leave the field blank to skip input: " GN </dev/tty || true
	[[ -n "$GN" ]] || break
	read -rp "Enter slug (.ics file name): " GS </dev/tty || true
	[[ -n "$GS" ]] || { echo "slug required, group skipped" >&2; continue; }
	GROUPS+=("$GN|$GS")
done

echo
echo "domain=$DOMAIN  interval=${INTERVAL}m  groups=${#GROUPS[@]}"
echo

echo "==> system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3 python3-venv python3-pip curl ca-certificates \
	debian-keyring debian-archive-keyring apt-transport-https gnupg

echo "==> caddy"
if ! command -v caddy >/dev/null; then
	curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
		| gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
	curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
		> /etc/apt/sources.list.d/caddy-stable.list
	apt-get update -qq
	apt-get install -y -qq caddy
fi

echo "==> user $USER_NAME"
id -u "$USER_NAME" >/dev/null 2>&1 || \
	useradd --system --home-dir "$DIR" --shell /usr/sbin/nologin "$USER_NAME"

echo "==> code in $DIR"
if [[ -d "$DIR/.git" ]]; then
	git config --system --add safe.directory "$DIR" 2>/dev/null || true
	git -C "$DIR" pull --ff-only
elif [[ -e "$DIR" && -n "$(ls -A "$DIR" 2>/dev/null)" ]]; then
	echo "$DIR exists and is not a git repo - remove it and retry" >&2
	exit 1
else
	git clone "$REPO" "$DIR"
	git config --system --add safe.directory "$DIR" 2>/dev/null || true
fi

echo "==> dependencies"
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"

echo "==> config"
[[ -f "$DIR/config.json" ]] || cp "$DIR/config.example.json" "$DIR/config.json"
"$DIR/.venv/bin/python" - "$DIR/config.json" "$INTERVAL" ${GROUPS[@]+"${GROUPS[@]}"} <<'PY'
import json, sys
cfg_path, interval, pairs = sys.argv[1], int(sys.argv[2]), sys.argv[3:]
cfg = json.load(open(cfg_path, encoding="utf-8"))
cfg["launch_interval_min"] = interval
groups, seen = [], set()
for item in pairs:
    name, _, slug = item.partition("|")
    name, slug = name.strip(), slug.strip()
    if not name or not slug or slug in seen:
        continue
    seen.add(slug)
    groups.append({"name": name, "slug": slug})
if groups:
    cfg["groups"] = groups
json.dump(cfg, open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False, indent=4)
PY

mkdir -p "$DIR/cal"
chown -R "$USER_NAME:$USER_NAME" "$DIR"
chmod 755 "$DIR" "$DIR/cal"

echo "==> systemd"
sed "s#@DIR@#$DIR#g; s#@USER@#$USER_NAME#g" "$DIR/deploy/calsync.service" \
	> /etc/systemd/system/calsync.service
systemctl daemon-reload
systemctl enable --now calsync

echo "==> caddy config"
sed "s#@DOMAIN@#$DOMAIN#g; s#@DIR@#$DIR#g" "$DIR/deploy/Caddyfile.tmpl" \
	> /etc/caddy/Caddyfile
systemctl reload caddy 2>/dev/null || systemctl restart caddy

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
	ufw allow 80,443/tcp >/dev/null || true
fi

cat > /etc/calsync.env <<EOF
CALSYNC_DIR=$DIR
CALSYNC_DOMAIN=$DOMAIN
CALSYNC_USER=$USER_NAME
EOF
ln -sf "$DIR/deploy/calsyncctl" /usr/local/bin/calsync
chmod +x "$DIR/deploy/calsyncctl"

echo
echo "================================================================"
echo " Done. Calendars:"
for g in ${GROUPS[@]+"${GROUPS[@]}"}; do echo "   https://$DOMAIN/cal/${g#*|}.ics"; done
echo " Control:  calsync list | add <group> <slug> | update | interval <min>"
echo "================================================================"
