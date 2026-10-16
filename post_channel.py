#!/usr/bin/env python3
"""Публикация свежих происшествий в Telegram-канал через бота.

Запускается после collector.py (в GitHub Actions). За один запуск публикует не больше MAX_POSTS записей,
между постами выдерживает MIN_GAP_MIN минут и пишет только в дневное время по Москве.
Формат поста: заголовок-ссылка на источник, короткая выдержка из анонса (до ~380 знаков), строка «тип · место», ссылка на карту.
Полный текст статей не копируется.

Переменные окружения:
  BOT_TOKEN      токен бота (секрет, в логи не выводится)
  CHANNEL        @имя_канала или числовой id (по умолчанию @nrmpnews)
  DB_PATH        путь к базе (по умолчанию incidents.db)
  MAX_POSTS      сколько постов за запуск (по умолчанию 1)
  MIN_GAP_MIN    минимальный интервал между постами, минут (по умолчанию 25)
  FRESH_HOURS    публиковать только записи не старше N часов (по умолчанию 6)
  POST_HOURS     окно публикации по Москве, например 8-23 (по умолчанию 8-23)
  CHECK_ONLY=1   только проверить токен и права бота, ничего не публиковать
"""
import html
import os
import re
import sqlite3
import sys
import time

import requests

DB_PATH = os.environ.get("DB_PATH", "incidents.db")
TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHANNEL = os.environ.get("CHANNEL", "@nrmpnews").strip() or "@nrmpnews"
MAX_POSTS = int(os.environ.get("MAX_POSTS", "1"))
MIN_GAP = int(os.environ.get("MIN_GAP_MIN", "25")) * 60
FRESH = int(os.environ.get("FRESH_HOURS", "6")) * 3600
HOURS = os.environ.get("POST_HOURS", "8-23")
CHECK_ONLY = os.environ.get("CHECK_ONLY", "") not in ("", "0", "false")
MAP_LINE = "🗺 Все происшествия на карте: t.me/NewsRussiaMapBot/NewsRussiaMap"

TYPES = {
    "dtp": ("🚗", "ДТП"), "fire": ("🔥", "Пожар"), "wx": ("⚡", "ЧС и погода"),
    "crime": ("⛔", "Правонарушение"), "tech": ("🆘", "Авария / ЧП"), "mil": ("🪖", "Атака / обстрел"),
}


def api(method, payload):
    """Вызов Bot API. Токен в сообщения об ошибках не попадает."""
    try:
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=payload, timeout=20)
        data = r.json()
    except Exception as e:
        print(f"  Telegram: {method}: сбой запроса ({type(e).__name__})")
        return None
    if not data.get("ok"):
        print(f"  Telegram: {method}: {data.get('error_code')} {data.get('description')}")
        return None
    return data["result"]


def check():
    me = api("getMe", {})
    if not me:
        print("Токен не принят: проверьте секрет BOT_TOKEN.")
        return False
    print("Бот:", "@" + me.get("username", "?"))
    member = api("getChatMember", {"chat_id": CHANNEL, "user_id": me["id"]})
    if not member:
        print("Канал недоступен: проверьте CHANNEL и что бот добавлен в канал.")
        return False
    status = member.get("status")
    can = member.get("can_post_messages")
    print(f"Роль бота в канале: {status}; право публиковать: {can}")
    if status != "administrator" or not can:
        print("Нужно сделать бота администратором канала с правом «Публикация сообщений».")
        return False
    return True


def words(t):
    return set(w for w in re.findall(r"[а-яёa-z0-9]{4,}", t.lower()))


def similar(a, b):
    wa, wb = words(a), words(b)
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= 0.55


def excerpt(summary, title, limit=380):
    """Короткая выдержка из анонса ленты: 1–3 предложения, не длиннее limit. Пусто, если анонс повторяет заголовок."""
    t = re.sub(r"\s+", " ", summary or "").strip()
    # убираем «МОСКВА, 16 окт — РИА Новости.» и «МОСКВА, 16 октября. /ТАСС/.» в начале
    t = re.sub(r"^[А-ЯЁ][А-ЯЁ\- ]{2,30},\s*\d{1,2}\s+[а-яё]+\.?\s*(?:[—–-]\s*[^.]{1,40}\.|/[^/]{1,20}/\.)?\s*", "", t)
    if len(t) < 40 or similar(t, title) and len(words(t) - words(title)) < 4:
        return ""
    if len(t) <= limit:
        return t
    cut = t[:limit]
    ends = [m.end() for m in re.finditer(r"[.!?»)](?=\s|$)", cut)]
    if ends and ends[-1] > 120:
        return cut[:ends[-1]].strip()
    return cut.rsplit(" ", 1)[0].rstrip(",;:—– ") + "…"


def in_window():
    try:
        lo, hi = (int(x) for x in HOURS.split("-"))
    except ValueError:
        return True
    h = time.gmtime(time.time() + 3 * 3600).tm_hour  # Москва = UTC+3
    return lo <= h <= hi


def main():
    if not TOKEN:
        print("BOT_TOKEN не задан: публикация в канал пропущена.")
        return 0
    if not check():
        return 0
    if CHECK_ONLY:
        print("Проверка пройдена, ничего не опубликовано.")
        return 0
    if not in_window():
        print(f"Вне окна публикации ({HOURS} по Москве).")
        return 0
    if not os.path.exists(DB_PATH):
        print("Базы нет, публиковать нечего.")
        return 0

    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("ALTER TABLE incidents ADD COLUMN posted_at INTEGER")
    except sqlite3.OperationalError:
        pass
    con.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    now = int(time.time())
    row = con.execute("SELECT v FROM meta WHERE k='last_post'").fetchone()
    last = int(row[0]) if row else 0
    if now - last < MIN_GAP:
        print(f"Предыдущий пост был {(now - last) // 60} мин назад, ждём.")
        return 0

    recent = [r[0] for r in con.execute(
        "SELECT title FROM incidents WHERE posted_at > ?", (now - 86400,))]
    cands = con.execute(
        "SELECT url, title, type, place, region, summary FROM incidents "
        "WHERE posted_at IS NULL AND ts >= ? AND title != '' AND place != '' ORDER BY ts DESC LIMIT 60",
        (now - FRESH,)).fetchall()
    sent = 0
    for url, title, typ, place, region, summary in cands:
        if sent >= MAX_POSTS:
            break
        if any(similar(title, t) for t in recent):
            con.execute("UPDATE incidents SET posted_at=0 WHERE url=?", (url,))  # дубль чужой новости: не публикуем
            continue
        em, label = TYPES.get(typ, TYPES["tech"])
        where = place if place == region or not region else f"{place}, {region}"
        body = excerpt(summary, title)
        text = (f'<a href="{html.escape(url, quote=True)}">{html.escape(title)}</a>\n\n'
                + (f"{html.escape(body)}\n\n" if body else "")
                + f"{em} {label} · 📍 {html.escape(where)}\n\n{MAP_LINE}")
        res = api("sendMessage", {"chat_id": CHANNEL, "text": text, "parse_mode": "HTML"})
        if res is None:
            break  # не повторяем в этом запуске, чтобы не засыпать лог
        con.execute("UPDATE incidents SET posted_at=? WHERE url=?", (now, url))
        con.execute("INSERT OR REPLACE INTO meta VALUES('last_post', ?)", (str(now),))
        recent.append(title)
        sent += 1
        print("Опубликовано:", title[:80])
    con.commit()
    con.close()
    if not sent:
        print("Подходящих свежих записей нет.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
