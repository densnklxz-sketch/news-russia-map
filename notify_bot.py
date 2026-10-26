#!/usr/bin/env python3
"""Подписки на уведомления и рассылка новых происшествий (запускается в GitHub Actions после collector.py).

Как это работает:
  1. Мини-приложение открывает чат с ботом по ссылке t.me/<бот>?start=n<маска>_<хэш региона>.
  2. При каждом запуске скрипт забирает новые сообщения боту (getUpdates): /start подписывает, /stop отписывает.
  3. Каждому подписчику отправляются новые происшествия его региона и типов (не больше MAX_PER_USER за раз).

Подписчики хранятся в той же базе incidents.db (она лежит в кэше Actions).
Токен берётся из BOT_TOKEN и в логи не попадает.
"""
import html
import os
import re
import sqlite3
import sys
import time
import zlib

import requests

DB_PATH = os.environ.get("DB_PATH", "incidents.db")
TOKEN = os.environ.get("BOT_TOKEN", "").strip()
MAX_PER_USER = int(os.environ.get("MAX_PER_USER", "5"))
FRESH = int(os.environ.get("FRESH_HOURS", "6")) * 3600
MAP_LINK = "t.me/NewsRussiaMapBot/NewsRussiaMap"
WORKER_URL = os.environ.get("WORKER_URL", "").strip().rstrip("/")   # адрес Cloudflare Worker для мгновенных ответов (необязательно)
WORKER_KEY = os.environ.get("WORKER_KEY", "").strip()
SHARE_URL = "https://t.me/share/url?url=" + "https%3A%2F%2Ft.me%2FNewsRussiaMapBot%2FNewsRussiaMap&text=" + "%D0%9A%D0%B0%D1%80%D1%82%D0%B0%20%D0%BF%D1%80%D0%BE%D0%B8%D1%81%D1%88%D0%B5%D1%81%D1%82%D0%B2%D0%B8%D0%B9%20%D0%A0%D0%BE%D1%81%D1%81%D0%B8%D0%B8"
TYPE_KEYS = ["dtp", "fire", "wx", "crime", "tech", "mil", "law", "good"]  # порядок битов маски: как в мини-приложении
TYPES = {
    "dtp": ("🚗", "ДТП"), "fire": ("🔥", "Пожар"), "wx": ("⚡", "ЧС и погода"),
    "crime": ("⛔", "Правонарушение"), "tech": ("🆘", "Авария / ЧП"), "mil": ("🪖", "Атака / обстрел"),
    "law": ("🏛", "Решение / закон"), "good": ("🌿", "Хорошие новости"),
}
START_RE = re.compile(r"^/start(?:@\w+)?(?:\s+n(\d{1,3})_([0-9a-f]{1,8}|0))?\s*$", re.I)


def api(method, payload):
    try:
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=payload, timeout=20)
        data = r.json()
    except Exception as e:
        print(f"  Telegram: {method}: сбой запроса ({type(e).__name__})")
        return None, None
    if not data.get("ok"):
        return None, data.get("error_code")
    return data["result"], None


def rhash(name):
    return format(zlib.crc32(name.encode("utf-8")) & 0xFFFFFFFF, "x")


def say(chat_id, text):
    res, err = api("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": True})
    return res is not None, err


def describe(mask, region):
    names = [TYPES[k][0] + " " + TYPES[k][1] for n, k in enumerate(TYPE_KEYS) if mask >> n & 1]
    return f"Регион: {region or 'вся Россия'}\nТипы: {', '.join(names) or 'не выбраны'}"


def words(t):
    return set(re.findall(r"[а-яёa-z0-9]{4,}", t.lower()))


def similar(a, b):
    wa, wb = words(a), words(b)
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= 0.55


def main():
    if not TOKEN:
        print("BOT_TOKEN не задан: уведомления пропущены.")
        return 0
    if not os.path.exists(DB_PATH):
        print("Базы нет.")
        return 0
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    con.execute("""CREATE TABLE IF NOT EXISTS subs(
        chat_id INTEGER PRIMARY KEY, mask INTEGER, region TEXT, active INTEGER, last_ts INTEGER)""")
    now = int(time.time())
    regions = {rhash(r[0]): r[0] for r in con.execute("SELECT DISTINCT region FROM incidents WHERE region != ''")}

    # 0. подписки, принятые Cloudflare Worker (он отвечает пользователю сразу, а здесь мы только запоминаем)
    if WORKER_URL and WORKER_KEY:
        try:
            r = requests.get(WORKER_URL + "/subs", params={"key": WORKER_KEY}, timeout=20)
            for it in r.json().get("subs", []):
                cid = int(it["chat_id"])
                h = str(it.get("hash") or "0")
                region = "" if h == "0" else regions.get(h, "")
                con.execute("INSERT OR IGNORE INTO subs VALUES(?,?,?,?,?)", (cid, int(it.get("mask", 63)) & 255, region, 1, now))
                con.execute("UPDATE subs SET mask=?, region=?, active=? WHERE chat_id=?",
                            (int(it.get("mask", 63)) & 255, region, 1 if it.get("active", True) else 0, cid))
            print("Подписки из Worker синхронизированы.")
        except Exception as e:
            print(f"  Worker недоступен ({type(e).__name__}), используем только getUpdates.")

    # 1. входящие сообщения (на каждый чат за запуск отвечаем один раз, по последней команде)
    row = con.execute("SELECT v FROM meta WHERE k='tg_offset'").fetchone()
    offset = int(row[0]) if row else 0
    updates, err = api("getUpdates", {"offset": offset, "timeout": 0, "limit": 100, "allowed_updates": ["message"]})
    if updates is None:
        if not WORKER_URL:
            print("getUpdates недоступен (код %s). Если у бота включён webhook, сбросьте его." % err)
        updates = []
    final = {}
    for u in updates:
        offset = max(offset, u["update_id"] + 1)
        m = u.get("message") or {}
        chat = m.get("chat") or {}
        text = (m.get("text") or "").strip()
        if chat.get("type") != "private" or not text:
            continue
        cid = chat["id"]
        sm = START_RE.match(text)
        low = text.lower()
        if sm:
            mask = (int(sm.group(1)) if sm.group(1) else 63) & 255
            h = sm.group(2) or "0"
            region = "" if h == "0" else regions.get(h, "")
            note = ""
            if h != "0" and not region:
                note = "\n\nЭтот регион пока не найден в базе, поэтому подписка оформлена на всю Россию."
            con.execute("INSERT OR REPLACE INTO subs VALUES(?,?,?,1,?)", (cid, mask, region, now))
            final[cid] = ("sub", mask, region, note)
        elif low.startswith("/stop"):
            con.execute("UPDATE subs SET active=0 WHERE chat_id=?", (cid,))
            final[cid] = ("stop",)
        elif low.startswith("/share"):
            if final.get(cid, ("",))[0] != "sub":
                final[cid] = ("share",)
        elif low.startswith("/start") or low.startswith("/help"):
            if final.get(cid, ("",))[0] not in ("sub", "share"):
                final[cid] = ("help",)
    new_subs = 0
    for cid, act in final.items():
        if act[0] == "sub":
            _, mask, region, note = act
            say(cid, "✅ Подписка оформлена.\n" + describe(mask, region) + note +
                "\n\nПрисылаю до %d новых происшествий за раз. Изменить: откройте карту → Уведомления. "
                "Отписаться: /stop. Поделиться картой: /share\n\n🗺 %s" % (MAX_PER_USER, MAP_LINK))
            new_subs += 1
        elif act[0] == "stop":
            say(cid, "Вы отписались от уведомлений. Вернуться можно через карту: " + MAP_LINK)
        elif act[0] == "share":
            say(cid, "Отправьте карту друзьям: " + SHARE_URL + "\n\nИли просто перешлите ссылку: " + MAP_LINK)
        else:
            say(cid, "Откройте карту, выберите регион и типы событий во вкладке «Уведомления» и нажмите "
                     "«Подписаться в Telegram»: " + MAP_LINK + "\n\nКоманды: /stop — отписаться, /share — поделиться картой.")
    con.execute("INSERT OR REPLACE INTO meta VALUES('tg_offset', ?)", (str(offset),))
    con.commit()

    # 2. рассылка
    sent_total = 0
    for cid, mask, region, last_ts in con.execute(
            "SELECT chat_id, mask, region, last_ts FROM subs WHERE active=1").fetchall():
        since = max(last_ts or 0, now - FRESH)
        cands = [r for r in con.execute(
            "SELECT url, title, type, place, region, ts FROM incidents WHERE ts > ? AND title != '' AND place != '' "
            "ORDER BY ts DESC LIMIT 200", (since,))
            if (not region or r[4] == region) and mask >> TYPE_KEYS.index(r[2] if r[2] in TYPE_KEYS else "tech") & 1]
        picked = []
        for r in cands:
            if len(picked) >= MAX_PER_USER:
                break
            if any(similar(r[1], p[1]) for p in picked):
                continue
            picked.append(r)
        if not picked:
            continue
        lines = []
        for url, title, typ, place, reg, ts in picked:
            em, label = TYPES.get(typ, TYPES["tech"])
            where = place if place == reg or not reg else f"{place}, {reg}"
            lines.append(f'{em} <a href="{html.escape(url, quote=True)}">{html.escape(title)}</a>\n📍 {html.escape(where)}')
        extra = len(cands) - len(picked)
        text = "\n\n".join(lines) + (f"\n\nЕщё {extra} на карте." if extra > 0 else "") + f"\n\n🗺 {MAP_LINK}"
        ok, err = say(cid, text)
        if ok:
            sent_total += 1
            con.execute("UPDATE subs SET last_ts=? WHERE chat_id=?", (max(r[5] for r in cands), cid))
        elif err in (403, 400):  # бот заблокирован или чат недоступен
            con.execute("UPDATE subs SET active=0 WHERE chat_id=?", (cid,))
        time.sleep(0.05)
    con.commit()
    con.close()
    print(f"Новых подписок: {new_subs}; рассылок отправлено: {sent_total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
