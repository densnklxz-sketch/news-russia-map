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
import json
import os
import re
import sqlite3
import sys
import time
import zlib

import requests

DB_PATH = os.environ.get("DB_PATH", "incidents.db")
TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHANNEL = os.environ.get("CHANNEL", "@nrmpnews").strip() or "@nrmpnews"
MAX_POSTS = int(os.environ.get("MAX_POSTS", "1"))
MIN_GAP = int(os.environ.get("MIN_GAP_MIN", "25")) * 60
FRESH = int(os.environ.get("FRESH_HOURS", "6")) * 3600
HOURS = os.environ.get("POST_HOURS", "8-23")
CHECK_ONLY = os.environ.get("CHECK_ONLY", "") not in ("", "0", "false")
SITE_URL = os.environ.get("SITE_URL", "https://news-russia-map.ru/").strip() or "https://news-russia-map.ru/"
MAP_URL = os.environ.get("MAP_URL", "https://t.me/NewsRussiaMapBot/NewsRussiaMap").strip()  # ссылка на мини-приложение (открывается внутри Telegram)
MAP_BUTTON = {"inline_keyboard": [[{"text": "🗺 Открыть карту", "url": MAP_URL + ("&" if "?" in MAP_URL else "?") + "mode=fullscreen"}]]}
MAP_LINE = ('🗺 Все происшествия на <a href="' + MAP_URL + ("&" if "?" in MAP_URL else "?") + 'mode=fullscreen">карте</a>\n'
            '👉 Новости России|NRMP <a href="https://t.me/nrmpnews">Подписаться</a>')


def map_for(url):
    """Ссылка на карту, открывающая именно эту новость (?p=<crc32 ссылки>). Возвращает (строка_для_текста, кнопка)."""
    code = format(zlib.crc32(url.encode("utf-8")) & 0xFFFFFFFF, "x")
    link = f"{MAP_URL}{'&' if '?' in MAP_URL else '?'}startapp={code}&mode=fullscreen"
    line = ('🗺 Все происшествия на <a href="' + html.escape(link, quote=True) + '">карте</a>\n'
            '👉 Новости России|NRMP <a href="https://t.me/nrmpnews">Подписаться</a>')
    return line, {"inline_keyboard": [[{"text": "🗺 Открыть карту", "url": link}]]}


TYPES = {
    "dtp": ("🚗", "ДТП"), "fire": ("🔥", "Пожар"), "wx": ("⚡", "ЧС и погода"),
    "crime": ("⛔", "Правонарушение"), "tech": ("🆘", "Авария / ЧП"), "mil": ("🪖", "Атака / обстрел"),
    "law": ("🏛", "Решение / закон"), "good": ("🌿", "Хорошие новости"),
}


def api(method, payload):
    """Вызов Bot API. Токен в сообщения об ошибках не попадает."""
    payload = {k: v for k, v in payload.items() if v is not None}  # пустые поля (например, нет кнопки) не отправляем
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


PROMO_RX = re.compile(r'''(?:[\U0001F300-\U0001FAFF☀-➿⬀-⯿️‍\s]*)(?:Плохо\s+грузит\s+Telegram|Не\s+грузит\s+Telegram|(?:ЧП|Новости|Мы|Читайте|Подписывайтесь|Подписаться|Присоединяйтесь|Наш\s+канал|Канал)\s+(?:\w+\s+){0,2}в\s+(?:MAX|Max|Макс)\b|\bв\s+MAX\b|Мы\s+в\s+MAX|Поддерж\w*\s+нас|ВКонтакте\s*[\U0001F300-\U0001FAFF\u2600-\u27BF]|ВКонтакте\s+(?:ТГ|Телеграм|Telegram|MAX|Max|Макс)|ТГ[\s-]*канал|Telegram[\s-]*канал|Здесь\s+мы\s+всегда\s+на\s+связи|Мы\s+всегда\s+на\s+связи|На\s+связи\s*\.{2,}|Подробнее\s+(?:на|в)\s+(?:канале|нашем|сайте|соцсетях)|Читайте\s+нас|Читай\s+нас|Подписывайся|Подписывайтесь|Подпишись|Подпишитесь|Подписаться\s+на|Присылай\w*|Прислать\s+новост\w+|Пишите\s+нам|Наш\s+канал|Мы\s+в\s+(?:соцсетях|Max|Макс|ВК|Telegram)|Реклама\s*:|Сотрудничество|\bt\.me/|\bvk\.(?:ru|com)/|\bmax\.ru/|https?://|@\w{4,})''', re.I)


EMOJI_RX = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2300-\u23FF\uFE0F\u200d]+")


def excerpt(summary, title, limit=700):
    """Короткая выдержка из анонса ленты: 1–3 предложения, не длиннее limit. Пусто, если анонс повторяет заголовок."""
    t = re.sub(r"\s+", " ", EMOJI_RX.sub("", summary or "")).strip()
    t = re.sub(r"(?:^|\s)#[\w\d_]+", " ", t).strip()  # хештеги из чужих каналов («#поиск»)
    t = re.sub(r"\s+", " ", t)
    m = PROMO_RX.search(t)  # подвал чужого канала: «Поддержи нас подпиской…», ссылки, @имена
    if m:
        t = t[:m.start()].rstrip()
    first = re.match(r"[^.!?]*[.!?]+\s*", t)  # первое предложение повторяет заголовок: не дублируем его в тексте
    if first and similar(first.group(0), title) and len(t) > len(first.group(0)) + 40:
        t = t[len(first.group(0)):].lstrip()
    # режем всё, что похоже на код и рекламную «подвал» страницы
    t = re.split(r"-->|<!--|\{|\}|=>|\bfunction\b|\blocation\.|Установи|Промокод", t)[0].strip()
    sents = re.findall(r"[^.!?]+[.!?»)]*\s*", t)
    t = "".join(x for x in sents if not re.search(r"https?:|www\.|\.ru\b|\.com\b|©|читайте также", x, re.I)).strip()
    # убираем «МОСКВА, 16 окт — РИА Новости.» и «МОСКВА, 16 октября. /ТАСС/.» в начале
    t = re.sub(r"^[А-ЯЁ][А-ЯЁ\- ]{2,30},\s*\d{1,2}\s+[а-яё]+\.?\s*(?:[—–-]\s*[^.]{1,40}\.|/[^/]{1,20}/\.)?\s*", "", t)
    # источник сам обрезал текст («…на ул….», «...»): последнее предложение неполное, убираем его
    if re.search(r"(?:…|\.{3})\.?\s*$", t) or "…" in t[-60:]:
        full = re.findall(r"[^.!?…]+[.!?»)]+(?=\s|$)", t.replace("…", "\u0001"))
        full = [x for x in full if "\u0001" not in x]
        t = " ".join(x.strip() for x in full).strip()
    # повторы одного и того же предложения
    seen, uniq = set(), []
    for x in re.findall(r"[^.!?]+[.!?»)]*\s*", t):
        k = re.sub(r"\W+", " ", x.lower()).strip()
        if k and k not in seen:
            seen.add(k)
            uniq.append(x)
    t = "".join(uniq).strip()
    if len(t) < 40 or similar(t, title) and len(words(t) - words(title)) < 4:
        return ""
    # как в коротких новостных каналах: не больше четырёх законченных предложений
    sents = re.findall(r"[^.!?]+[.!?»)]*\s*", t)
    out, total = [], 0
    for x in sents[:4]:
        if out and total + len(x) > limit:
            break
        out.append(x)
        total += len(x)
    t = "".join(out).strip()
    if len(t) > limit * 1.4:  # одно очень длинное предложение: режем по запятой
        cut = t[:limit]
        t = cut.rsplit(",", 1)[0].rstrip(",;:—– ") + "." if "," in cut[60:] else cut.rsplit(" ", 1)[0].rstrip(",;:—– ") + "."
    return t


RUBRIC_RX = re.compile(r"^\W*(?:Уральский\s+меридиан|Главное|Срочно|Молния|Важно|Эксклюзив|Подробности|Горячая\s+линия|Происшествия|Криминал)(?:\s*[:\-—|/]+\s*|\s+(?=[А-ЯЁA-Z«\"]))")
FACT_RX = re.compile(
    r"\b\d{1,3}-летн\w+|\b\d{1,3}\s(?:лет|года|год)\b"
    r"|\b\d[\d\s,.]*\s?(?:млн|млрд|тыс\.?)?\s?(?:руб\w*|₽|долл\w*|евро)"
    r"|\b\d[\d,.]*\s?(?:кг|граммов|грамма|грамм|км|литров|тонн\w*|гектар\w*|кв\. ?м)(?![а-яё])"
    r"|\b\d+\s+(?:человек\w*|погибш\w*|пострадавш\w*|раненых)"
    r"|\bпогиб\w*|\bпострада\w*", re.I)


def clean_title(t):
    """Служебная рубрика источника в начале заголовка («Уральский меридиан В Екатеринбурге…»)."""
    return RUBRIC_RX.sub("", t or "", count=1).strip() or t


def fmt_body(body):
    """Экранирует текст и выделяет жирным до двух ключевых фактов (возраст, сумма, число пострадавших)."""
    t = html.escape(body)
    n = [0]

    def b(m):
        if n[0] >= 2:
            return m.group(0)
        n[0] += 1
        return "<b>" + m.group(0) + "</b>"
    return FACT_RX.sub(b, t)


MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def old_date(title):
    """Заголовок про событие с датой, которая не сегодня и не вчера («атака 9 сентября»): считаем устаревшим."""
    msk = time.gmtime(time.time() + 3 * 3600)
    ok = {(msk.tm_mday, msk.tm_mon)}
    y = time.gmtime(time.time() + 3 * 3600 - 86400)
    ok.add((y.tm_mday, y.tm_mon))
    for d, m in re.findall(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")\b", title.lower()):
        if (int(d), MONTHS.index(m) + 1) not in ok:
            return True
    return False


def where_text(place, region):
    if region == "За рубежом":
        return nominative(place)
    """«Петербург, Санкт-Петербург» -> «Санкт-Петербург»: не повторяем место, если оно уже есть в названии региона."""
    if not region or place == region:
        return place
    if re.search(r"област|край|республик|округ", region.lower()):
        return f"{place}, {region}"
    a, b = place.lower().replace("ё", "е"), region.lower().replace("ё", "е")
    stem = a[:max(4, len(a) - 2)]
    if stem in b or b[:max(4, len(b) - 3)] in a:
        return region
    return f"{place}, {region}"


def clean_text(t):
    """Отсекает мусор со страниц: код, меню, реклама, промокоды, ссылки."""
    if re.search(r"[{}<>=;]|-->|=>|function|var |https?:|www\.|\.ru\b|\.com\b|cookie|подпиш|реклам|промокод|©|читайте также|фото:|\bчитать\b", t, re.I):
        return False
    if not re.search(r"[.!?»)]$", t):
        return False
    cyr = len(re.findall(r"[А-Яа-яЁё]", t))
    return cyr / max(len(t), 1) > 0.6


def fetch_lead(url):
    """Если анонс в ленте короткий, берём описание и первые абзацы со страницы новости (несколько предложений, не вся статья)."""
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0 (compatible; NRMPBot/1.0)"})
        r.encoding = r.apparent_encoding if not r.encoding or r.encoding.lower() == "iso-8859-1" else r.encoding
        raw = r.text[:600000]
        page = raw
        page = re.sub(r"(?is)<(script|style|noscript|nav|footer|aside|form|iframe|svg|header)\b.*?</\1>", " ", page)
        page = re.sub(r"(?s)<!--.*?-->", " ", page)
    except Exception:
        return ""
    parts = []
    # многие сайты кладут текст статьи в разметку JSON-LD (articleBody): берём его в первую очередь
    try:
        m = re.search(r'"articleBody"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
        if m:
            body = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", json.loads('"' + m.group(1) + '"')))).strip()
            if len(body) >= 120 and clean_text(body):
                parts.append(body[:2200])
    except Exception:
        pass
    for pat in (r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:description["\']',
                r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)'):
        m = re.search(pat, page, re.I)
        if m:
            d = html.unescape(m.group(1)).strip()
            if clean_text(d):
                parts.append(d)
            break
    n = 0
    for m in re.finditer(r"<p[^>]*>(.*?)</p>", page, re.S | re.I):
        t = html.unescape(re.sub(r"<[^>]+>", "", m.group(1)))
        t = re.sub(r"\s+", " ", t).strip()
        if len(t) >= 60 and clean_text(t):
            parts.append(t)
            n += 1
        if n >= 8 or len(" ".join(parts)) > 2200:
            break
    out = []
    for t in parts:
        if not any(similar(t, o) or t in o or o in t for o in out):
            out.append(t)
    return " ".join(out)


DIGEST_HOUR = int(os.environ.get("DIGEST_HOUR", "21"))  # час по Москве, 0-23; -1 отключает сводку


def digest(con, now):
    """Вечерняя сводка дня: сколько событий, по типам, топ-3 региона с главным заголовком каждого. Раз в сутки."""
    msk = time.gmtime(now + 3 * 3600)
    if DIGEST_HOUR < 0 or msk.tm_hour != DIGEST_HOUR:
        return
    day = time.strftime("%Y-%m-%d", msk)
    row = con.execute("SELECT v FROM meta WHERE k='last_digest'").fetchone()
    if row and row[0] == day:
        return
    rows = con.execute("SELECT title, type, region, place FROM incidents WHERE ts >= ? AND region != '' AND region != 'За рубежом' AND type != 'law' ORDER BY ts DESC",
                       (now - 86400,)).fetchall()
    if len(rows) < 10:
        return
    by_type, by_reg = {}, {}
    for title, typ, region, place in rows:
        by_type[typ] = by_type.get(typ, 0) + 1
        by_reg.setdefault(region, []).append(title)
    lines = [f"📊 <b>Итоги дня · {msk.tm_mday:02d}.{msk.tm_mon:02d}</b>", f"За сутки на карте {len(rows)} происшествий.", ""]
    for k, n in sorted(by_type.items(), key=lambda x: -x[1]):
        em, label = TYPES.get(k, TYPES["tech"])
        lines.append(f"{em} {label}: {n}")
    lines += ["", "<b>Где больше всего:</b>"]
    for region, titles in sorted(by_reg.items(), key=lambda x: -len(x[1]))[:3]:
        lines.append(f"📍 <b>{html.escape(region)}</b> · {len(titles)}\n   {html.escape(titles[0])}")
    lines += ["", MAP_LINE]
    if api("sendMessage", {"chat_id": CHANNEL, "text": "\n".join(lines), "parse_mode": "HTML",
                           "disable_web_page_preview": True, "reply_markup": MAP_BUTTON}) is not None:
        con.execute("INSERT OR REPLACE INTO meta VALUES('last_digest', ?)", (day,))
        con.commit()
        print("Опубликована сводка дня.")


# ---- Дополнительные регулярные публикации (каждая не чаще раза в сутки/неделю, всё в окне POST_HOURS) ----
WEEKLY_HOUR = int(os.environ.get("WEEKLY_HOUR", "20"))        # воскресенье, час по Москве; -1 отключает
POLL_HOUR = int(os.environ.get("POLL_HOUR", "19"))            # опрос раз в сутки; -1 отключает
REGION_DIGESTS = [x.strip() for x in os.environ.get("REGION_DIGESTS", "").split(",") if x.strip()]  # например: Москва,Санкт-Петербург
REGION_DIGEST_HOUR = int(os.environ.get("REGION_DIGEST_HOUR", "20"))
PIN_INTRO = os.environ.get("PIN_INTRO", "1") not in ("", "0", "false")
POLLS = [
    ("Какие события вам важнее всего видеть в ленте?", ["ДТП", "Пожары", "ЧС и погода", "Атаки и обстрелы"]),
    ("Как часто вы заглядываете на карту?", ["Несколько раз в день", "Раз в день", "Раз в несколько дней", "Впервые"]),
    ("Что добавить на карту в первую очередь?", ["Больше регионов", "Фото с места", "Уведомления по району", "Видео"]),
    ("Чем вы пользуетесь для новостей о происшествиях?", ["Карта и канал", "Только канал", "Телеграм-чаты", "Сайты СМИ"]),
]


def _meta_get(con, k):
    row = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row[0] if row else None


def _meta_set(con, k, v):
    con.execute("INSERT OR REPLACE INTO meta VALUES(?, ?)", (k, v))
    con.commit()


def weekly(con, now):
    """Итоги недели по воскресеньям."""
    msk = time.gmtime(now + 3 * 3600)
    if WEEKLY_HOUR < 0 or msk.tm_wday != 6 or msk.tm_hour != WEEKLY_HOUR:
        return
    day = time.strftime("%Y-%m-%d", msk)
    if _meta_get(con, "last_weekly") == day:
        return
    rows = con.execute("SELECT type, region, ts FROM incidents WHERE ts >= ? AND region != '' AND region != 'За рубежом' AND type != 'law'",
                       (now - 7 * 86400,)).fetchall()
    if len(rows) < 30:
        return
    by_type, by_reg, by_day = {}, {}, {}
    for typ, region, ts in rows:
        by_type[typ] = by_type.get(typ, 0) + 1
        by_reg[region] = by_reg.get(region, 0) + 1
        d = time.strftime("%d.%m", time.gmtime(ts + 3 * 3600))
        by_day[d] = by_day.get(d, 0) + 1
    lines = ["📈 <b>Итоги недели</b>", f"За 7 дней на карте {len(rows)} происшествий.", ""]
    for k, n in sorted(by_type.items(), key=lambda x: -x[1]):
        em, label = TYPES.get(k, TYPES["tech"])
        lines.append(f"{em} {label}: {n}")
    top = sorted(by_reg.items(), key=lambda x: -x[1])[:5]
    lines += ["", "<b>Больше всего событий:</b>"] + [f"📍 {html.escape(r)}: {n}" for r, n in top]
    d, n = max(by_day.items(), key=lambda x: x[1])
    lines += ["", f"Самый насыщенный день: {d} ({n}).", "", MAP_LINE]
    if api("sendMessage", {"chat_id": CHANNEL, "text": "\n".join(lines), "parse_mode": "HTML",
                           "disable_web_page_preview": True, "reply_markup": MAP_BUTTON}) is not None:
        _meta_set(con, "last_weekly", day)
        print("Опубликованы итоги недели.")


def region_digests(con, now):
    """Сводка «<регион> за сутки» для регионов из REGION_DIGESTS."""
    msk = time.gmtime(now + 3 * 3600)
    if not REGION_DIGESTS or REGION_DIGEST_HOUR < 0 or msk.tm_hour != REGION_DIGEST_HOUR:
        return
    day = time.strftime("%Y-%m-%d", msk)
    for region in REGION_DIGESTS:
        key = "rd_" + region
        if _meta_get(con, key) == day:
            continue
        rows = con.execute("SELECT title, type, place FROM incidents WHERE ts >= ? AND region = ? ORDER BY ts DESC",
                           (now - 86400, region)).fetchall()
        if len(rows) < 3:
            continue
        picked = []
        for title, typ, place in rows:
            if any(similar(title, t) for _, t, _ in picked):
                continue
            picked.append((typ, title, place))
            if len(picked) >= 6:
                break
        lines = [f"📍 <b>{html.escape(region)} за сутки</b>", f"Событий на карте: {len(rows)}.", ""]
        for typ, title, place in picked:
            em, _ = TYPES.get(typ, TYPES["tech"])
            lines.append(f"{em} {html.escape(title)}")
        lines += ["", MAP_LINE]
        if api("sendMessage", {"chat_id": CHANNEL, "text": "\n".join(lines), "parse_mode": "HTML",
                               "disable_web_page_preview": True, "reply_markup": MAP_BUTTON}) is not None:
            _meta_set(con, key, day)
            print(f"Опубликована сводка: {region}.")


def daily_poll(con, now):
    """Опрос раз в сутки (вопросы по кругу)."""
    msk = time.gmtime(now + 3 * 3600)
    if POLL_HOUR < 0 or msk.tm_hour != POLL_HOUR:
        return
    day = time.strftime("%Y-%m-%d", msk)
    if _meta_get(con, "last_poll") == day:
        return
    q, opts = POLLS[msk.tm_yday % len(POLLS)]
    if api("sendPoll", {"chat_id": CHANNEL, "question": q, "options": opts, "is_anonymous": True}) is not None:
        _meta_set(con, "last_poll", day)
        print("Опубликован опрос.")


INTRO = ("🗺 <b>Карта происшествий России</b>\n\n"
         "Новости о ДТП, пожарах, ЧС и авариях по всей стране: коротко, со ссылкой на источник и точкой на карте.\n\n"
         "• Откройте карту и смотрите, что происходит рядом\n"
         "• Подпишитесь на уведомления по своему региону\n"
         "• Итоги дня и недели в канале\n\n"
         + MAP_LINE)


def pin_intro(con):
    """Один раз публикует и закрепляет приветственный пост."""
    if not PIN_INTRO or _meta_get(con, "intro_pinned"):
        return
    res = api("sendMessage", {"chat_id": CHANNEL, "text": INTRO, "parse_mode": "HTML",
                              "disable_web_page_preview": True, "reply_markup": MAP_BUTTON})
    if res is None:
        return
    _meta_set(con, "intro_pinned", "1")
    if api("pinChatMessage", {"chat_id": CHANNEL, "message_id": res["message_id"], "disable_notification": True}) is None:
        print("Приветственный пост опубликован, но закрепить не удалось: дайте боту право «Закреплять сообщения».")
    else:
        print("Приветственный пост закреплён.")


def extras(con, now):
    for fn in (weekly, region_digests, daily_poll):
        try:
            fn(con, now)
        except Exception as e:
            print(f"  {fn.__name__}: сбой ({type(e).__name__})")
    try:
        pin_intro(con)
    except Exception as e:
        print(f"  pin_intro: сбой ({type(e).__name__})")


from foreign import nominative
from dedupe import DUP_HOURS, sig as _dsig, is_dup as _dis_dup

USE_PHOTO = os.environ.get("USE_PHOTO", "1") not in ("0", "false", "")
BAD_IMG = re.compile(r"logo|favicon|placeholder|default|noimage|no-image|avatar|sprite|\.svg|\.gif|\.ico", re.I)


def good_photo(img):
    """Фото берём, если ссылка похожа на настоящую картинку: не логотип, не крошечная, не больше 8 МБ."""
    if not img or not str(img).startswith("http") or BAD_IMG.search(img):
        return ""
    try:
        r = requests.head(img, timeout=8, allow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code >= 400:
            return ""
        ctype = r.headers.get("Content-Type", "")
        size = int(r.headers.get("Content-Length") or 0)
    except Exception:
        return ""
    if ctype and not ctype.lower().startswith("image/"):
        return ""
    if 0 < size < 15000 or size > 8_000_000:
        return ""
    return img


def page_image(url):
    """Запасная картинка: og:image со страницы новости, если в ленте фото не было."""
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0 (compatible; NRMPBot/1.0)"})
        head = r.text[:150000]
    except Exception:
        return ""
    for pat in (r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']'):
        m = re.search(pat, head, re.I)
        if m:
            u = html.unescape(m.group(1)).strip()
            if u.startswith("//"):
                u = "https:" + u
            return u if u.startswith("http") else ""
    return ""


SOURCE_LINK = os.environ.get("SOURCE_LINK", "0") not in ("", "0", "false")  # 1 = ссылка на самом заголовке


def src_tail(url, source):
    """« · ТАСС» со ссылкой на новость, если название источника короче 6 знаков, иначе « · СМИ»."""
    if SOURCE_LINK:
        return ""
    name = (source or "").strip()
    word = name if 0 < len(name) < 6 else "СМИ"
    return f' · <a href="{html.escape(url, quote=True)}">{html.escape(word)}</a>'


STYLE = os.environ.get("POST_STYLE", "short")  # short: эмодзи типа в заголовке, одна строка места, без нижней подписи; old: прежний вид


def head_html(url, title, em=""):
    """Заголовок поста: жирным без ссылки (SOURCE_LINK=1 вернёт ссылку на источник). Эмодзи типа стоит перед заголовком."""
    pre = f"{em} " if em and STYLE == "short" else ""
    if SOURCE_LINK:
        return f'{pre}<a href="{html.escape(url, quote=True)}">{html.escape(title)}</a>\n\n'
    return f"{pre}<b>{html.escape(title)}</b>\n\n"


def meta_line(em, label, where, url, source, extra=""):
    """Строка под текстом: «📍 Запорожье · ТАСС» (в старом стиле ещё и тип новости)."""
    lead = "📍 " if STYLE == "short" else f"{em} {label} · 📍 "
    return f"{lead}{html.escape(where)}{src_tail(url, source)}{extra}"


SUB_LINE = '👉 Новости России|NRMP <a href="https://t.me/nrmpnews">Подписаться</a>'


def foot(post_line):
    return f"\n\n{SUB_LINE}" if STYLE == "short" else f"\n\n{post_line}"


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
    digest(con, now)
    extras(con, now)
    row = con.execute("SELECT v FROM meta WHERE k='last_post'").fetchone()
    last = int(row[0]) if row else 0
    if now - last < MIN_GAP:
        print(f"Предыдущий пост был {(now - last) // 60} мин назад, ждём.")
        return 0

    recent = [r[0] for r in con.execute(
        "SELECT title FROM incidents WHERE posted_at > ?", (now - 86400,))]
    # уже опубликованные за последние DUP_HOURS часов: та же новость от другого издания второй раз не выходит
    posted_sigs = [(r[2] or "", _dsig(r[0] or "", r[1] or "")) for r in con.execute(
        "SELECT title, summary, region FROM incidents WHERE posted_at > ?", (now - DUP_HOURS * 3600,))]
    cands = con.execute(
        "SELECT url, title, type, place, region, summary, img, source, vid, vurl FROM incidents "
        "WHERE posted_at IS NULL AND ts >= ? AND title != '' AND place != '' ORDER BY ts DESC LIMIT 60",
        (now - FRESH,)).fetchall()
    sent = 0
    for url, title, typ, place, region, summary, img, source, vid, vurl in cands:
        if sent >= MAX_POSTS:
            break
        if old_date(title):
            con.execute("UPDATE incidents SET posted_at=0 WHERE url=?", (url,))  # старая новость в свежей ленте
            continue
        cur_sig = _dsig(title, summary or "")
        if any(similar(title, t) for t in recent) or any(rg == (region or "") and _dis_dup(cur_sig, sg) for rg, sg in posted_sigs):
            con.execute("UPDATE incidents SET posted_at=0 WHERE url=?", (url,))  # дубль чужой новости: не публикуем
            continue
        em, label = TYPES.get(typ, TYPES["tech"])
        post_line, post_button = map_for(url)
        if region == "За рубежом" or typ == "law":  # другая страна или рубрика решений: на карте точки нет, даём только подписку без ссылки на карту
            post_line, post_button = '👉 Новости России|NRMP <a href="https://t.me/nrmpnews">Подписаться</a>', None
        where = where_text(place, region)
        title = clean_title(title)
        body = excerpt(summary, title)
        if len(body) < 900:
            more = excerpt(fetch_lead(url), title)
            if len(more) > len(body):
                body = more
        vextra = f'\n🎥 <a href="{html.escape(url, quote=True)}">Видео в источнике</a>' if (vid and not vurl) else ""
        text = (head_html(url, title, em)
                + (f"{fmt_body(body)}\n\n" if body else "")
                + meta_line(em, label, where, url, source, vextra) + foot(post_line))
        res = None
        if vurl:  # видео официального канала (МЧС, МВД): публикуем с указанием источника
            vcap = (head_html(url, title, em)
                    + meta_line(em, label, where, url, source, f"\n🎥 Видео: {html.escape(source.replace(' (Telegram)', ''))}") + foot(post_line))
            res = api("sendVideo", {"chat_id": CHANNEL, "video": vurl, "caption": vcap[:1024], "parse_mode": "HTML",
                                    "supports_streaming": True, "reply_markup": post_button})
        photo = good_photo(img) if (USE_PHOTO and res is None) else ""
        if USE_PHOTO and res is None and not photo:
            photo = good_photo(page_image(url))  # в ленте фото нет: пробуем картинку со страницы новости
        if photo:
            cap = text
            if len(cap) > 1000:  # лимит подписи к фото 1024: укорачиваем выдержку
                body_short = excerpt(body, title, limit=max(120, 920 - (len(text) - len(html.escape(body)))))
                cap = (head_html(url, title, em)
                       + (f"{fmt_body(body_short)}\n\n" if body_short else "")
                       + meta_line(em, label, where, url, source, vextra) + foot(post_line))
            res = api("sendPhoto", {"chat_id": CHANNEL, "photo": photo, "caption": cap[:1024],
                                    "parse_mode": "HTML", "reply_markup": post_button})
        if res is None:  # нет фото или Telegram его не принял: обычный пост
            res = api("sendMessage", {"chat_id": CHANNEL, "text": text, "parse_mode": "HTML", "reply_markup": post_button,
                                      "disable_web_page_preview": not SOURCE_LINK})
        if res is None:
            break  # не повторяем в этом запуске, чтобы не засыпать лог
        con.execute("UPDATE incidents SET posted_at=? WHERE url=?", (now, url))
        con.execute("INSERT OR REPLACE INTO meta VALUES('last_post', ?)", (str(now),))
        recent.append(title)
        posted_sigs.append((region or "", cur_sig))
        sent += 1
        print("Опубликовано:", title[:80])
    con.commit()
    con.close()
    if not sent:
        print("Подходящих свежих записей нет.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
