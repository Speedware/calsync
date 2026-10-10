#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import time

from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, send_file
from werkzeug.middleware.proxy_fix import ProxyFix

import calsync

GROUP_RE = re.compile(r"[А-Яа-яЁё0-9 \-]{3,64}")
SLUG_RE = re.compile(r"[a-z0-9-]{1,64}")
CACHE_TTL = 600
LOCK_FILE = ".config.lock"

TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

_cache: dict[str, tuple[float, str | None, str | None]] = {}

app = Flask(__name__, static_folder="web", static_url_path="/static")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


class Upstream(Exception):
    pass


@contextlib.contextmanager
def _lock():
    f = open(LOCK_FILE, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _slugify(name: str) -> str:
    s = "".join(TRANSLIT.get(ch, ch) for ch in name.lower())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "group"


def _unique_slug(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def _canonical_group(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    if not soup.select_one("#sched_tabs"):
        return None
    for h in soup.find_all("h3"):
        text = h.get_text(" ", strip=True)
        if text.startswith("Расписание группы"):
            name = text[len("Расписание группы"):].strip()
            if name:
                return name
    return None


def _validate(group: str) -> tuple[str | None, str | None]:
    key = group.lower()
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1], hit[2]
    try:
        html = calsync.fetch_schedule_html(group, calsync.DEFAULT_TIMEOUT_SEC)
    except Exception:
        raise Upstream()
    canonical = _canonical_group(html)
    if canonical:
        _cache[key] = (time.time(), canonical, html)
    return canonical, (html if canonical else None)


def _link(slug: str) -> dict:
    base = request.host_url.rstrip("/")
    return {
        "slug": slug,
        "url": f"{base}/cal/{slug}.ics",
        "webcal": f"webcal://{request.host}/cal/{slug}.ics",
    }


def _stub_ics(name: str, slug: str) -> None:
    cal_dir = calsync.config.get("calendar_path", "cal")
    path = os.path.join(cal_dir, f"{slug}.ics")
    if os.path.exists(path):
        return
    os.makedirs(cal_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:calsync\r\n"
            f"X-WR-CALNAME:{name}\r\nEND:VCALENDAR\r\n"
        )


def _generate(name: str, slug: str, html: str | None) -> None:
    try:
        lessons = calsync.parse_schedule(html) if html else []
        if lessons:
            calsync.create_ics(lessons, name, slug)
        else:
            _stub_ics(name, slug)
    except Exception:
        _stub_ics(name, slug)


def _ensure_registered(name: str, html: str | None) -> str:
    with _lock():
        cfg = json.load(open(calsync.CONFIG_FILE, encoding="utf-8"))
        groups = cfg.setdefault("groups", [])
        for g in groups:
            if _norm(g.get("name", "")).lower() == name.lower():
                return g["slug"]
        slug = _unique_slug(_slugify(name), {g.get("slug") for g in groups})
        groups.append({"name": name, "slug": slug})
        tmp = calsync.CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=4)
        os.replace(tmp, calsync.CONFIG_FILE)
        calsync.config = cfg

    _generate(name, slug, html)
    return slug


@app.get("/")
def index():
    return send_file("web/index.html")


@app.get("/umte.png")
def background():
    return send_file("web/umte.png", max_age=86400)


@app.get("/cal/<slug>.ics")
def serve_ics(slug):
    # Caddy's file_server normally handles this path directly; this route is
    # the fallback for setups without a local reverse proxy in front of us.
    if not SLUG_RE.fullmatch(slug):
        return "not found", 404
    cal_dir = calsync.config.get("calendar_path", "cal")
    path = os.path.join(cal_dir, f"{slug}.ics")
    if not os.path.isfile(path):
        return "not found", 404
    resp = send_file(path, mimetype="text/calendar", max_age=1800)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.post("/api/subscribe")
def subscribe():
    group = _norm((request.json or {}).get("group"))
    if not GROUP_RE.fullmatch(group):
        return jsonify(ok=False, error="Проверьте название группы"), 400
    try:
        canonical, html = _validate(group)
    except Upstream:
        return jsonify(ok=False, error="Сервис расписания недоступен, попробуйте позже"), 502
    if not canonical:
        return jsonify(ok=False, error="Группа не найдена — проверьте название"), 404

    slug = _ensure_registered(canonical, html)
    return jsonify(ok=True, group=canonical, **_link(slug))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)
