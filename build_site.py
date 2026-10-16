"""Сборка страницы карты из базы: шаблон web/template.html + свежие записи -> готовый index.html.

Один раз:      python build_site.py
По кругу:      python build_site.py --loop 300     (раз в 5 минут)
Переменные:    DB_PATH, SITE_OUT (куда писать, по умолчанию /var/www/incident-map/index.html), SITE_DAYS (за сколько дней, 14)
"""
import json
import os
import sqlite3
import sys
import time

DB_PATH = os.environ.get("DB_PATH", "incidents.db")
OUT = os.environ.get("SITE_OUT", "/var/www/incident-map/index.html")
DAYS = int(os.environ.get("SITE_DAYS", "14"))
TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "template.html")
FIELDS = "url, title, summary, type, lat, lon, region, place, precise, ts, source, img"


def build():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        f"SELECT {FIELDS} FROM incidents WHERE ts >= ? ORDER BY ts DESC LIMIT 3000",
        (int(time.time()) - DAYS * 86400,))]
    con.close()
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
