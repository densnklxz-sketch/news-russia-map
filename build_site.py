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
from foreign import nominative

FIELDS = "url, title, summary, type, lat, lon, region, place, precise, ts, source, img, vid"


DRONE_RE = re.compile(r"бпла|беспилот|дрон|пво\b|сбит\w* (?:над|на подлет)", re.I)
from dedupe import DUP_HOURS, sig as _sig, is_dup  # повторы одной новости от разных изданий


def toks(r):
    return _sig(r.get("title") or "", r.get("summary") or "")


MIL_MAP_HOURS = int(os.environ.get("MIL_MAP_HOURS", "24"))  # сколько часов метки атак и обстрелов живут на карте


# Атаки и обстрелы на карте: только значимые (пострадавшие, ущерб, инфраструктура), не больше N на регион. В ленте остаются все.
MIL_MIN_SCORE = int(os.environ.get("MIL_MIN_SCORE", "3"))
MIL_PER_REGION = int(os.environ.get("MIL_PER_REGION", "2"))
_MIL_W = [
    (3, re.compile(r"погиб|убит|ранен|ранил|пострада|жертв|тяжел\w+ состоя", re.I)),
    (2, re.compile(r"пожар|возгоран|загорел|повре[жд]|разруш|обруш|попадан|прилёт|прилет|взрыв|упал\w*|обломк|выбит\w+ стекл", re.I)),
    (2, re.compile(r"НПЗ|нефтебаз|подстанц|ТЭЦ|электроснабж|без (света|воды|тепла|отопления)|аэропорт|завод|порт\b|мост|склад|терминал|энергетик|логистическ", re.I)),
    (1, re.compile(r"эвакуир|эвакуац|многоквартир|жил\w+ дом|школ|больниц|детск\w+ сад", re.I)),
]
_MIL_ROUTINE = re.compile(r"(сбит\w*|сбили|уничтожен\w*|уничтожили|перехвачен\w*|отражен\w*)\s", re.I)


def mil_score(r):
    t = (r.get("title") or "") + " " + (r.get("summary") or "")[:300]
    sc = sum(w for w, rx in _MIL_W if rx.search(t))
    if sc == 0 and _MIL_ROUTINE.search(t):
        sc = -1  # сводка «сбито N беспилотников» без последствий
    return sc


# Остальные категории на карте: только самое важное. Порог значимости и лимит «на регион в сутки»; в ленте и канале остаётся всё.
MAP_RULES = {  # тип: (минимальный балл, не больше на регион за сутки)
    "dtp": (3, 2), "fire": (3, 2), "crime": (3, 2), "tech": (3, 2), "wx": (2, 3), "good": (-99, 3),
}
_IMP_W = [
    (5, re.compile(r"убит\w*|убийств\w*|застрелен\w*|погиб\w*|гибел|смерт|скончал|унёс\w* жизн|унес\w* жизн|найден\w* мертв|тела\b", re.I)),
    (3, re.compile(r"пострада\w*|ранен\w*|ранил|госпитализ\w*|травм|жертв|в реанимац|тяжел\w+ состоя", re.I)),
    (3, re.compile(r"массов\w+|крупн\w+|мощн\w+|цепн\w+|серьёзн\w+|серьезн\w+|чрезвычайн\w+|режим ЧС|режим повышенной|стихийн\w+|без света\s+остал|остал\w+ без", re.I)),
    (2, re.compile(r"эвакуир\w*|эвакуац\w*|обруш\w*|взрыв\w*|горят|горит|площад\w+\s+(?:более\s+)?\d{3,}|\d{3,}\s*(?:кв\.?\s*м|квадрат)|тысяч\w*\s+(?:человек|жител|абонент|домов)|тыс\.\s*(?:человек|жител|абонент)", re.I)),
    (1, re.compile(r"многоквартир|школ\w*|больниц\w*|детск\w+ сад|автобус|поезд|трасс\w+ М-?\d|федеральн\w+ трасс|перекрыт\w*|заблокирован\w*|ТЦ|торгов\w+ центр|завод|склад", re.I)),
]
_NO_HARM = re.compile(r"без пострадавших|никто не пострадал|обошлось без|пострадавших нет|жертв нет", re.I)
_MINOR = re.compile(r"мошенни|телефонн\w+ мошенн|кража|краж[аи]\b|украл|хищени|взятк|штраф|оштрафова|задержан\w+ за|нетрезв|пьян\w+ за рулём|пьян\w+ за рулем|поймал\w* на|выявил\w* нарушени", re.I)


def imp_score(r):
    t = (r.get("title") or "") + " " + (r.get("summary") or "")[:400]
    sc = 0
    no_harm = bool(_NO_HARM.search(t))
    for w, rx in _IMP_W:
        if rx.search(t):
            if no_harm and w >= 3:
                continue
            sc += w
    if _MINOR.search(t):
        sc -= 2
    if r.get("img"):
        sc += 1
    return sc


def trim_map(kept):
    """Помечает nomap у менее значимых событий: порог баллов и лимит на регион в сутки."""
    now = time.time()
    groups = {}
    for r in kept:
        rule = MAP_RULES.get(r.get("type"))
        if not rule or r.get("nomap") or r.get("source") == "Подписчик":
            continue
        if imp_score(r) < rule[0]:
            r["nomap"] = 1
            continue
        key = (r.get("type"), r.get("region") or "", int((now - r["ts"]) // 86400))
        groups.setdefault(key, []).append(r)
    for (typ, _reg, _d), lst in groups.items():
        lst.sort(key=lambda r: (-imp_score(r), -r["ts"]))
        for r in lst[MAP_RULES[typ][1]:]:
            r["nomap"] = 1


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
    for r in kept:
        if r.get("type") == "mil" and time.time() - r["ts"] > MIL_MAP_HOURS * 3600:
            r["nomap"] = 1  # атаки и обстрелы держим на карте сутки, дальше только в ленте
        if r.get("type") == "law":
            r["nomap"] = 1  # рубрика «Решения и законы»: только лента и канал
        if r.get("region") == "За рубежом":
            r["place"] = nominative(r.get("place") or "")
            r["nomap"] = 1  # другие страны: только в ленте, без точки на карте
    mil = [r for r in kept if r.get("type") == "mil" and not r.get("nomap")]
    by_reg = {}
    for r in mil:
        if mil_score(r) < MIL_MIN_SCORE:
            r["nomap"] = 1  # мелкие и сводные атаки: только в ленте
        else:
            by_reg.setdefault(r.get("region") or "", []).append(r)
    for lst in by_reg.values():
        lst.sort(key=lambda r: (-mil_score(r), -r["ts"]))
        for r in lst[MIL_PER_REGION:]:
            r["nomap"] = 1  # в регионе на карте только самые значимые
    trim_map(kept)
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
    print(f"повторов убрано: {n0 - len(rows)}; скрыто с карты (БПЛА в Москве и другие страны): {sum(1 for r in rows if r.get('nomap'))}")
    for r in rows:
        if not r.get("img"):
            r.pop("img", None)
        if not r.get("vid"):
            r.pop("vid", None)
    html = open(TEMPLATE, encoding="utf-8").read()
    # "</" внутри <script> ломает разметку, поэтому экранируем
    data = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")
    html = html.replace("__DATA__", data)
    # Текстовая версия для поисковиков и тех, у кого не работает JavaScript: последние заголовки
    import html as _h
    site = os.environ.get("SITE_URL", "https://news-russia-map.ru/").strip() or "https://news-russia-map.ru/"
    items = "".join('<li><a href="%s">%s</a> — %s</li>' % (_h.escape(r["url"], quote=True), _h.escape(r["title"]), _h.escape(r.get("place") or ""))
                    for r in rows[:40])
    seo = '<noscript><h1>Карта происшествий России</h1><p>ДТП, пожары, ЧС и новости регионов. Последние события:</p><ul>%s</ul></noscript>' % items
    html = html.replace("</body>", seo + "\n</body>", 1)
    outdir = os.path.dirname(OUT) or "."
    os.makedirs(outdir, exist_ok=True)
    open(os.path.join(outdir, "robots.txt"), "w", encoding="utf-8").write("User-agent: *\nAllow: /\nSitemap: %ssitemap.xml\n" % site)
    open(os.path.join(outdir, "sitemap.xml"), "w", encoding="utf-8").write(
        '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        '<url><loc>%s</loc><lastmod>%s</lastmod><changefreq>always</changefreq></url></urlset>\n' % (site, time.strftime("%Y-%m-%d", time.gmtime())))
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
