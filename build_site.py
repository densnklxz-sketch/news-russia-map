"""Сборка страницы карты из базы: шаблон web/template.html + свежие записи -> готовый index.html.

Один раз:      python build_site.py
По кругу:      python build_site.py --loop 300     (раз в 5 минут)
Переменные:    DB_PATH, SITE_OUT (куда писать, по умолчанию /var/www/incident-map/index.html), SITE_DAYS (за сколько дней, 5)
"""
import json
import os
import re
import sqlite3
import sys
import time

DB_PATH = os.environ.get("DB_PATH", "incidents.db")
OUT = os.environ.get("SITE_OUT", "/var/www/incident-map/index.html")
DAYS = int(os.environ.get("SITE_DAYS", "5"))
TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "template.html")
FIELDS = "url, title, summary, type, lat, lon, region, place, precise, ts, source, img"


DRONE_RE = re.compile(r"бпла|беспилот|дрон|пво\b|сбит\w* (?:над|на подлет)", re.I)
DUP_HOURS = 48


def _stems(text):
    return {w[:4] for w in re.findall(r"[а-яёa-z0-9]{4,}", text.lower().replace("ё", "е"))}


def toks(r):
    """(основы слов заголовка, основы заголовка и начала анонса): одинаковые события в разных формулировках дают близкие наборы."""
    t = _stems(r.get("title") or "")
    return t, t | _stems((r.get("summary") or "")[:200])


def is_dup(a, b):
    ti = len(a[0] & b[0])
    if ti >= 4 and ti / min(len(a[0]), len(b[0])) >= 0.6:
        return True
    fi = len(a[1] & b[1])
    return fi >= 6 and fi / min(len(a[1]), len(b[1])) >= 0.75


def tidy(rows):
    """1) убираем повторы одной новости другими словами (оставляем самую раннюю);
    2) новости про беспилотники в Москве: на карте не больше двух за сутки (по одной на утро и на день/вечер), остальные только в ленте."""
    rows = sorted(rows, key=lambda r: r["ts"])
    kept, sigs = [], []
    for r in rows:
        t = toks(r)
        dup = False
        for k, kt in zip(reversed(kept), reversed(sigs)):
            if r["ts"] - k["ts"] > DUP_HOURS * 3600:
                break
            if (k.get("region") or "") == (r.get("region") or "") and is_dup(t, kt):
                dup = True
                break
        if not dup:
            kept.append(r)
            sigs.append(t)
    slots = set()
    for r in kept:
        if r.get("region") == "Москва" and DRONE_RE.search((r.get("title") or "") + " " + (r.get("summary") or "")[:200]):
            msk = time.gmtime(r["ts"] + 3 * 3600)
            slot = (msk.tm_year, msk.tm_yday, msk.tm_hour // 12)
            if slot in slots:
                r["nomap"] = 1
            else:
                slots.add(slot)
    return sorted(kept, key=lambda r: -r["ts"])


def build():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        f"SELECT {FIELDS} FROM incidents WHERE ts >= ? ORDER BY ts DESC LIMIT 3000",
        (int(time.time()) - DAYS * 86400,))]
    con.close()
    n0 = len(rows)
    rows = tidy(rows)
    print(f"повторов убрано: {n0 - len(rows)}; беспилотники в Москве скрыты с карты: {sum(1 for r in rows if r.get('nomap'))}")
    for r in rows:
        if not r.get("img"):
            r.pop("img", None)
    html = open(TEMPLATE, encoding="utf-8").read()
    # "</" внутри <script> ломает разметку, поэтому экранируем
    data = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")
    html = html.replace("__DATA__", data)
    key = os.environ.get("YMAPS_KEY", "").strip()
    html = html.replace("__BUILD__", time.strftime("%d.%m %H:%M UTC", time.gmtime()))
    html = html.replace("__YMAPS_KEY__", key)  # ключ Яндекс Карт (виден в коде страницы, защищается ограничением по домену)
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    tmp = OUT + ".tmp"
    open(tmp, "w", encoding="utf-8").write(html)
    os.replace(tmp, OUT)  # подмена файла целиком, посетитель не увидит половину страницы
    print(f"{time.strftime('%H:%M:%S')} записано {len(rows)} происшествий -> {OUT}")


if __name__ == "__main__":
    if "--loop" in sys.argv:
        pause = int(sys.argv[sys.argv.index("--loop") + 1])
        while True:
            try:
                build()
            except Exception as e:  # сбой одной сборки не должен останавливать службу
                print("ошибка сборки:", e, file=sys.stderr)
            time.sleep(pause)
    else:
        build()
