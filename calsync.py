#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import re
import sys
import time
import warnings
from datetime import datetime, timedelta

import pytz
import requests
from bs4 import BeautifulSoup
from colorama import Fore, init
from ics import Calendar, Event
from ics.alarm import DisplayAlarm
from ics.grammar.parse import ContentLine

warnings.filterwarnings("ignore", category=FutureWarning, module="ics.component")
init(autoreset=True)

os.chdir(os.path.dirname(os.path.abspath(__file__)))

CONFIG_FILE = "config.json"
LOG_FILE = "calsync.log"
LOG_BACKUP_DAYS = 31
BASE = "https://umeos.ru"
SCHEDULE_ENDPOINT = BASE + "/blocks/umerasp/json_rasper.php"
MIN_INTERVAL_MIN = 60
DEFAULT_INTERVAL_MIN = 60
DEFAULT_TIMEOUT_SEC = 45
CONNECT_TIMEOUT_SEC = 10
HTTP_RETRIES = 2
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
TZ = pytz.timezone("Europe/Moscow")
DAY_RE = re.compile(r"^\s*(\S+)\s+(\d{2}\.\d{2}\.\d{4})\s*$")
WEBINAR_TAG = "вебинар"
VOLATILE = ("DTSTAMP", "LAST-MODIFIED", "CREATED")

_file_handler = logging.handlers.TimedRotatingFileHandler(
    LOG_FILE, when="midnight", backupCount=LOG_BACKUP_DAYS, encoding="utf-8"
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _console_handler])

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Referer": BASE + "/"})
_retry = requests.adapters.Retry(
    total=HTTP_RETRIES, connect=HTTP_RETRIES, read=HTTP_RETRIES,
    backoff_factor=3, status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset({"GET", "POST"}),
)
SESSION.mount("https://", requests.adapters.HTTPAdapter(max_retries=_retry))

config: dict = {}


def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        raise FileNotFoundError(f"config file {CONFIG_FILE} not found")
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def fetch_schedule_html(group_name: str, timeout: int) -> str:
    resp = SESSION.post(
        SCHEDULE_ENDPOINT,
        data={"type": "group", "groupname": group_name},
        timeout=(CONNECT_TIMEOUT_SEC, timeout),
    )
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp.text


def _info_cell(cell):
    texts = [t.strip() for t in cell.find_all(string=True, recursive=False) if t.strip()]
    tags = [b.get_text(strip=True) for b in cell.select("span.badge")]
    return (
        texts[0] if texts else "Не указано",
        texts[1] if len(texts) > 1 else "",
        tags,
    )


def parse_schedule(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    lessons: list[dict] = []
    seen: set[tuple] = set()

    for pane in soup.select("div.tabs__content") or [soup]:
        table = pane.find("table", class_="generaltable")
        if not table:
            continue

        current_date = None
        for row in table.select("tbody tr") or table.find_all("tr"):
            header = row.find("th")
            if header is not None:
                m = DAY_RE.match(header.get_text(" ", strip=True))
                current_date = m.group(2) if m else None
                continue

            tds = row.find_all("td")
            if len(tds) < 4 or current_date is None:
                continue

            start, _, end = tds[1].get_text(strip=True).partition("-")
            number = tds[0].get_text(strip=True)
            subject, teacher, tags = _info_cell(tds[3])

            key = (current_date, number, subject, start.strip())
            if key in seen:
                continue
            seen.add(key)

            lessons.append({
                "Date": current_date,
                "Number": number,
                "Start Time": start.strip(),
                "End Time": end.strip(),
                "Room": tds[2].get_text(strip=True),
                "Subject": subject,
                "Teacher": teacher,
                "Tags": tags,
                "Type": WEBINAR_TAG if WEBINAR_TAG in tags else (tags[0] if tags else ""),
            })

    return lessons


def parse_time(value: str):
    return datetime.strptime(value.replace(".", ":"), "%H:%M").time()


def _stable(ics_text: str) -> str:
    return "\n".join(ln for ln in ics_text.splitlines() if not ln.startswith(VOLATILE)).strip()


def _uid(slug: str, lesson: dict) -> str:
    raw = f"{slug}-{lesson['Date']}-{lesson['Number']}-{lesson['Subject']}"
    return re.sub(r"\s+", "_", raw) + "@umeos.ru"


class _KeepOld(Exception):
    pass


def _event_end_date(event):
    end = getattr(event, "end", None)
    dt = getattr(end, "datetime", end)
    return dt.date()


def _carry_over(old_content: str, feed_start, cal: Calendar, group_name: str) -> int:
    if not old_content:
        return 0
    try:
        events = list(Calendar(old_content).events)
    except Exception as exc:
        logging.error(Fore.RED + f"{group_name}: existing .ics unreadable, keeping it as-is: {exc}")
        raise _KeepOld()

    kept = 0
    for event in events:
        try:
            past = _event_end_date(event) < feed_start
        except Exception:
            past = True
        if past:
            cal.events.add(event)
            kept += 1
    return kept


def create_ics(lessons: list[dict], group_name: str, slug: str) -> None:
    path_prefix = config.get("calendar_path", "cal")
    os.makedirs(path_prefix, exist_ok=True)
    path = os.path.join(path_prefix, f"{slug}.ics")

    parsed = []
    for lesson in lessons:
        try:
            day = datetime.strptime(lesson["Date"], "%d.%m.%Y").date()
            start_dt = TZ.localize(datetime.combine(day, parse_time(lesson["Start Time"])))
            end_dt = TZ.localize(datetime.combine(day, parse_time(lesson["End Time"])))
        except ValueError as exc:
            logging.warning(
                Fore.YELLOW + f"{group_name}: skipped lesson "
                f"{lesson['Date']} {lesson['Start Time']}-{lesson['End Time']}: {exc}"
            )
            continue
        parsed.append((day, start_dt, end_dt, lesson))

    if not parsed:
        logging.warning(Fore.YELLOW + f"{group_name}: no valid lessons, {slug}.ics left untouched")
        return

    feed_start = min(day for day, *_ in parsed)

    old_content = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            old_content = f.read()

    cal = Calendar()
    cal.extra.append(ContentLine(name="X-WR-CALNAME", value=group_name))
    cal.extra.append(ContentLine(name="NAME", value=group_name))

    try:
        archived = _carry_over(old_content, feed_start, cal, group_name)
    except _KeepOld:
        return

    subject_replacements = config.get("subject_replacements", {})
    teacher_replacements = config.get("teacher_replacements", {})

    for day, start_dt, end_dt, lesson in parsed:
        is_webinar = lesson["Type"] == WEBINAR_TAG
        subject = subject_replacements.get(lesson["Subject"], lesson["Subject"])
        teacher = teacher_replacements.get(lesson["Teacher"], lesson["Teacher"])
        tag_line = ", ".join(lesson["Tags"]) or lesson["Type"]

        event = Event()
        event.uid = _uid(slug, lesson)
        event.name = f"🌐 {subject}" if is_webinar else subject
        event.location = lesson["Room"]
        event.description = (
            f"Пара №{lesson['Number']}\n"
            f"Дисциплина: {lesson['Subject']}\n"
            f"Преподаватель: {teacher}\n"
            f"Вид занятия: {tag_line}"
        )
        event.begin = start_dt
        event.end = end_dt
        if is_webinar:
            event.alarms.append(DisplayAlarm(trigger=timedelta(minutes=-30)))

        cal.events.add(event)

    new_content = cal.serialize()
    if _stable(new_content) == _stable(old_content):
        logging.info(Fore.CYAN + f"No changes: {slug}.ics")
        return

    with open(path, "w", encoding="utf-8") as f:
        f.write(new_content)
    logging.info(Fore.GREEN + f"Updated {slug}.ics: {len(parsed)} from feed + {archived} archived")


def process_group(group: dict, timeout: int) -> bool:
    name = group.get("name")
    if not name:
        logging.error(Fore.RED + "group without 'name' field - skipped")
        return False

    slug = group.get("slug") or name
    try:
        html = fetch_schedule_html(name, timeout)
    except requests.RequestException as exc:
        logging.error(Fore.RED + f"{name}: request failed: {exc}")
        return False

    lessons = parse_schedule(html)
    if not lessons:
        logging.warning(Fore.YELLOW + f"{name}: empty schedule or parse failed")
        return False

    try:
        create_ics(lessons, name, slug)
    except OSError as exc:
        logging.error(Fore.RED + f"{name}: cannot write {slug}.ics: {exc}")
        return False
    return True


def run_once() -> None:
    groups = config.get("groups", [])
    if not groups:
        logging.warning(Fore.YELLOW + "no groups in config")
        return

    slugs = [g.get("slug") or g.get("name") for g in groups]
    dupes = {s for s in slugs if slugs.count(s) > 1}
    if dupes:
        logging.error(Fore.RED + f"duplicate slug/name, .ics files would overwrite each other: {dupes}")

    timeout = config.get("request_timeout_sec", DEFAULT_TIMEOUT_SEC)
    started = time.monotonic()
    ok = sum(process_group(g, timeout) for g in groups)
    logging.info(Fore.BLUE + f"Done in {time.monotonic() - started:.1f}s. OK: {ok}/{len(groups)}")


def main() -> int:
    global config

    ap = argparse.ArgumentParser(description="umeos.ru schedule -> .ics")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    try:
        config = load_config()
    except FileNotFoundError as exc:
        logging.critical(Fore.RED + str(exc))
        return 1
    except json.JSONDecodeError as exc:
        logging.critical(Fore.RED + f"invalid JSON in {CONFIG_FILE}: {exc}")
        return 1

    interval_min = config.get("launch_interval_min", DEFAULT_INTERVAL_MIN)
    if interval_min < MIN_INTERVAL_MIN:
        logging.warning(Fore.YELLOW + f"launch_interval_min={interval_min} raised to {MIN_INTERVAL_MIN}")
        interval_min = MIN_INTERVAL_MIN

    logging.info(
        Fore.BLUE + f"Dir: {config.get('calendar_path')} | "
        f"Groups: {[g.get('name') for g in config.get('groups', [])]}"
    )

    if args.once:
        run_once()
        return 0

    logging.info(Fore.BLUE + f"Run interval: {interval_min} min")
    while True:
        try:
            run_once()
        except Exception as exc:
            logging.error(Fore.RED + f"processing loop error: {exc}")
        try:
            time.sleep(interval_min * 60)
        except KeyboardInterrupt:
            logging.info(Fore.BLUE + "stopped by user")
            return 0


if __name__ == "__main__":
    sys.exit(main())
