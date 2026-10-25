"""Сборщик происшествий: RSS -> отбор -> координаты -> SQLite.

Запуск один раз:      python collector.py
Запуск по кругу:      python collector.py --loop 600     (раз в 10 минут)
"""
import calendar
import html
import os
import re
import sqlite3
import sys
import time

import feedparser
import requests

DB_PATH = os.environ.get("DB_PATH", "incidents.db")
# Nominatim просит указывать контакт в User-Agent. Подставьте свой адрес.
USER_AGENT = os.environ.get("USER_AGENT", "incident-map-collector/0.1 (contact: you@example.com)")
YANDEX_KEY = os.environ.get("YANDEX_GEOCODER_KEY", "")  # если задан, используется вместо Nominatim

# Ленты. Первые пять проверены в октябре 2026 (отдают свежие записи без авторизации).
# Остальные нужно проверить на сервере: curl -I АДРЕС. Нерабочие просто пропускаются.
FEEDS = [
    ("РИА Новости", "https://ria.ru/export/rss2/archive/index.xml"),
    ("ТАСС", "https://tass.ru/rss/v2.xml"),
    ("Lenta.ru", "https://lenta.ru/rss/news"),
    ("МК", "https://www.mk.ru/rss/incident/index.xml"),
    ("Интерфакс", "https://www.interfax.ru/rss.asp"),
    ("Известия", "https://iz.ru/xml/rss/all.xml"),
    ("РБК", "https://rssexport.rbc.ru/rbcnews/news/30/full.rss"),
    ("Коммерсантъ", "https://www.kommersant.ru/RSS/news.xml"),
    ("МЧС", "https://mchs.gov.ru/deyatelnost/press-centr/operativnaya-informaciya/rss"),
]
# Свои ленты можно дописать без правки кода: файл feeds.txt, строки вида  Название|https://адрес
FEED_DEFAULT = {}  # место по умолчанию для региональной ленты: третье поле в feeds.txt
if os.path.exists("feeds.txt"):
    for line in open("feeds.txt", encoding="utf-8"):
        if "|" in line and not line.lstrip().startswith("#"):
            fp = [x.strip() for x in line.strip().split("|")]
            FEEDS.append((fp[0], fp[1]))
            if len(fp) > 2 and fp[2]:
                FEED_DEFAULT[fp[0]] = fp[2]

import socket
socket.setdefaulttimeout(20)  # зависшая лента не держит запуск дольше 20 секунд
# Публичные Telegram-каналы без API: файл channels.txt, строки вида  Название|username (без @)
CHANNELS = []
CH_OFFICIAL = set()  # официальные каналы (МЧС, МВД): их видео публикуем с указанием источника
CH_DEFAULT = dict(FEED_DEFAULT)  # место по умолчанию для канала: третье поле в channels.txt (и для лент из feeds.txt)
if os.path.exists("channels.txt"):
    for line in open("channels.txt", encoding="utf-8"):
        if "|" in line and not line.lstrip().startswith("#"):
            parts = [p.strip() for p in line.strip().split("|")]
            CHANNELS.append((parts[0], parts[1].lstrip("@")))
            if len(parts) > 3 and parts[3].lower() == "official":
                CH_OFFICIAL.add(parts[0] + " (Telegram)")
            if len(parts) > 2 and parts[2]:
                CH_DEFAULT[parts[0] + " (Telegram)"] = parts[2]

# Подвал чужого канала («Поддержи нас подпиской: Макс, ВК, ТГ», ссылки, @имена) в наши посты не берём
PROMO_RX = re.compile(r'''(?:[\U0001F300-\U0001FAFF☀-➿⬀-⯿️‍\s]*)(?:Поддерж\w*\s+нас|Здесь\s+мы\s+всегда\s+на\s+связи|Мы\s+всегда\s+на\s+связи|На\s+связи\s*\.{2,}|Подробнее\s+(?:на|в)\s+(?:канале|нашем|сайте|соцсетях)|Читайте\s+нас|Читай\s+нас|Подписывайся|Подписывайтесь|Подпишись|Подпишитесь|Подписаться\s+на|Присылай\w*|Прислать\s+новост\w+|Пишите\s+нам|Наш\s+канал|Мы\s+в\s+(?:соцсетях|Max|Макс|ВК|Telegram)|Реклама\s*:|Сотрудничество|\bt\.me/|\bvk\.(?:ru|com)/|\bmax\.ru/|https?://|@\w{4,})''', re.I)


def cut_promo(text):
    m = PROMO_RX.search(text)
    return text[:m.start()].rstrip() if m else text


TG_UA = "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Mobile Safari/537.36"


def tg_feed(user, official=False):
    """Читает публичную веб-версию канала t.me/s/<канал> и возвращает объект с полем entries."""
    r = requests.get(f"https://t.me/s/{user}", timeout=20, headers={"User-Agent": TG_UA})
    r.raise_for_status()
    out = []
    for chunk in r.text.split('data-post="')[1:]:
        post = chunk.split('"', 1)[0]
        m = re.search(r'tgme_widget_message_text[^>]*>(.*?)</div>', chunk, re.S)
        if not m or "/" not in post:
            continue
        body = re.sub(r"<br\s*/?>", "\n", m.group(1))
        body = cut_promo(html.unescape(re.sub(r"<[^>]+>", "", body)).strip())
        lines = [l.strip() for l in body.split("\n") if l.strip()]
        if not lines or len(body) < 25:
            continue
        title = lines[0]
        if len(title) < 20 and len(lines) > 1:
            title = title + " " + lines[1]
        title = re.sub(r"^\d{1,2}[:.]\d{2}\.?\s*", "", re.sub(r"^[^\w«\"(]+", "", title))
        title = re.sub(r"^[^\w«\"(]+", "", title)[:160]
        t = re.search(r'<time[^>]*datetime="([^"]+)"', chunk)
        ts = time.time()
        if t:
            try:
                from datetime import datetime
                ts = datetime.fromisoformat(t.group(1)).timestamp()
            except ValueError:
                pass
        vid = 1 if re.search(r"tgme_widget_message_(?:roundvideo|video)|message_video_wrap|js-message_video", chunk) else 0
        vurl = None
        if vid and official:  # сам файл берём только у официальных каналов; у остальных только ссылка на оригинал
            mv = re.search(r'<video[^>]+src="(https://[^"]+)"', chunk)
            vurl = html.unescape(mv.group(1)) if mv else None
        e = feedparser.FeedParserDict(link="https://t.me/" + post, title=title, summary=" ".join(lines)[:1200],
                                      published_parsed=time.gmtime(ts), vid=vid, vurl=vurl)
        # фото из чужих каналов не берём: они защищены авторским правом
        out.append(e)
    return type("TgFeed", (), {"entries": out})()


MAX_AGE_DAYS = int(os.environ.get("MAX_AGE_DAYS", "3"))  # старые записи из лент не берём

# Правила типов. Первое совпадение выигрывает, порядок важен.
RULES = [
    ("mil", r"\bБПЛА\b|беспилотн(?!\w*\s+(?:машин|автомоб|авто\b|такси|грузовик|транспорт|поезд|трамва|электрич|автобус|комбайн|трактор|техник))|дрон(ы|ов|ами)?\b|\bПВО\b|обстрел|ракетн\w+ (атак|удар|опасност)|атак\w+ ВСУ|удар\w* по (НПЗ|нефтебаз|подстанц)"),
    ("fire", r"пожар|возгоран|загорел|горит|горели|сгорел"),
    ("dtp", r"\bДТП\b|столкнул|опрокинул|наезд|сбил[аи]?\b|разбил(ся|ись)\s+на\s+(трассе|дороге)"),
    ("wx", r"наводнен|подтоплен|паводок|ураган|сильный ветер|снегопад|гололёд|гололед|лавин|землетряс|\bсель\b|шторм|ледяной дождь"),
    ("tech", r"отключени|без (света|воды|отопления|газа|электричества)|нет (света|воды|отопления|газа)|"
             r"порыв|прорыв\w* (труб|водопровод|теплотрасс)|взрыв газа|обрушени|авария на (водопровод|теплотрасс|ЛЭП|подстанц)"),
    ("crime", r"убийств|убит|ограбл|краж[аи]|похищен|нападени|драк[аиу]|мошенни|задержан|изнасил"),
]
# Рубрика "Решения и законы": региональные решения (запреты, ограничения, штрафы). Идёт в ленту и канал, без точки на карте.
# Нужны и действие, и региональный орган власти; политика отсекается списком STOP ниже.
LAW_ACT = re.compile(r"запрет\w*|законопроект|вступит в силу|вводят?\b|вводится|ограничени\w+ (на|продаж|движени)|штраф\w* за|ограничат|разрешат|запретят", re.I)
LAW_ANCHOR = re.compile(r"заксобрани|Законодательн\w+ собрани|Мосгордум|городск\w+ дум|губернатор|Госсовет|правительств\w+ (област|регион|Москв|Петербург|край|республик)|"
                        r"администраци\w+ (город|област|район)|мэр\w*|депутат\w+ (област|городск|регион)", re.I)
# Эти темы на карту не берём: политика, суды и приговоры (это не происшествия).
STOP = re.compile(r"\bСВО\b|Минобороны|перегово|санкци|выборы|Госдум|приговор|осужд|суд (признал|оставил|рассмотр)|арестован[аы]? на|заочно", re.I)
# Чувствительные темы (насилие над детьми, останки и т. п.) на карту не попадают.
SENSITIVE = re.compile(r"насил|расчлен|изнасил|педофил|суицид|самоубийств|покончил с собой|тел[оа]? ребён|части тел|останки|детск\w+ порн|избил\w* (девочк|мальчик|подрост)", re.I)

# Крупные города: быстрый путь без геокодера. (широта, долгота, регион)
GAZETTEER = {
    "Москва": (55.7558, 37.6173, "Москва"), "Санкт-Петербург": (59.9343, 30.3351, "Санкт-Петербург"),
    "Новосибирск": (55.0302, 82.9204, "Новосибирская область"), "Екатеринбург": (56.8389, 60.6057, "Свердловская область"),
    "Казань": (55.7961, 49.1064, "Татарстан"), "Нижний Новгород": (56.3269, 44.0059, "Нижегородская область"),
    "Краснодар": (45.0355, 38.9753, "Краснодарский край"), "Ростов-на-Дону": (47.2357, 39.7015, "Ростовская область"),
    "Самара": (53.1959, 50.1002, "Самарская область"), "Уфа": (54.7388, 55.9721, "Башкортостан"),
    "Красноярск": (56.0153, 92.8932, "Красноярский край"), "Иркутск": (52.2864, 104.2807, "Иркутская область"),
    "Владивосток": (43.1155, 131.8855, "Приморский край"), "Хабаровск": (48.4802, 135.0719, "Хабаровский край"),
    "Мурманск": (68.9585, 33.0827, "Мурманская область"), "Воронеж": (51.6605, 39.2003, "Воронежская область"),
    "Волгоград": (48.708, 44.5133, "Волгоградская область"), "Омск": (54.9885, 73.3242, "Омская область"),
    "Тюмень": (57.153, 65.5343, "Тюменская область"), "Челябинск": (55.1644, 61.4368, "Челябинская область"),
    "Пермь": (58.0105, 56.2502, "Пермский край"), "Сочи": (43.6028, 39.7342, "Краснодарский край"),
    "Оренбург": (51.7727, 55.0988, "Оренбургская область"), "Саратов": (51.5336, 46.0343, "Саратовская область"),
    "Калининград": (54.7104, 20.4522, "Калининградская область"), "Томск": (56.4977, 84.9744, "Томская область"),
    "Курган": (55.441, 65.3411, "Курганская область"), "Махачкала": (42.9849, 47.5047, "Дагестан"),
    "Рязань": (54.6269, 39.6916, "Рязанская область"), "Тула": (54.1961, 37.6182, "Тульская область"),
    "Калуга": (54.5293, 36.2754, "Калужская область"), "Брянск": (53.2434, 34.3641, "Брянская область"),
    "Курск": (51.7304, 36.1926, "Курская область"), "Белгород": (50.5997, 36.5983, "Белгородская область"),
    "Орёл": (52.9703, 36.0635, "Орловская область"), "Липецк": (52.6088, 39.5992, "Липецкая область"),
    "Тамбов": (52.7213, 41.4523, "Тамбовская область"), "Пенза": (53.1951, 45.0183, "Пензенская область"),
    "Ульяновск": (54.3142, 48.4031, "Ульяновская область"), "Тверь": (56.8587, 35.9176, "Тверская область"),
    "Ярославль": (57.6261, 39.8845, "Ярославская область"), "Владимир": (56.1290, 40.4066, "Владимирская область"),
    "Иваново": (57.0004, 40.9739, "Ивановская область"), "Кострома": (57.7679, 40.9269, "Костромская область"),
    "Смоленск": (54.7826, 32.0453, "Смоленская область"), "Псков": (57.8136, 28.3496, "Псковская область"),
    "Великий Новгород": (58.5213, 31.2757, "Новгородская область"), "Вологда": (59.2205, 39.8915, "Вологодская область"),
    "Архангельск": (64.5393, 40.5187, "Архангельская область"), "Сыктывкар": (61.6688, 50.8364, "Коми"),
    "Петрозаводск": (61.7849, 34.3469, "Карелия"), "Киров": (58.6036, 49.6680, "Кировская область"),
    "Ижевск": (56.8527, 53.2115, "Удмуртия"), "Чебоксары": (56.1439, 47.2489, "Чувашия"),
    "Йошкар-Ола": (56.6316, 47.8861, "Марий Эл"), "Саранск": (54.1874, 45.1839, "Мордовия"),
    "Астрахань": (46.3497, 48.0408, "Астраханская область"), "Элиста": (46.3078, 44.2558, "Калмыкия"),
    "Ставрополь": (45.0428, 41.9734, "Ставропольский край"), "Нальчик": (43.4981, 43.6189, "Кабардино-Балкария"),
    "Владикавказ": (43.0241, 44.6813, "Северная Осетия"), "Грозный": (43.3178, 45.6949, "Чечня"),
    "Магнитогорск": (53.4186, 59.0472, "Челябинская область"), "Сургут": (61.254, 73.3962, "ХМАО"),
    "Нижневартовск": (60.9344, 76.5531, "ХМАО"), "Ханты-Мансийск": (61.0, 69.0, "ХМАО"),
    "Новокузнецк": (53.7557, 87.1099, "Кемеровская область"), "Кемерово": (55.3333, 86.0833, "Кемеровская область"),
    "Барнаул": (53.3606, 83.7636, "Алтайский край"), "Абакан": (53.7212, 91.4292, "Хакасия"),
    "Улан-Удэ": (51.8335, 107.5841, "Бурятия"), "Чита": (52.0339, 113.5010, "Забайкальский край"),
    "Якутск": (62.0355, 129.6755, "Якутия"), "Южно-Сахалинск": (46.9591, 142.7380, "Сахалинская область"),
    "Петропавловск-Камчатский": (53.0452, 158.6483, "Камчатский край"), "Благовещенск": (50.2907, 127.5272, "Амурская область"),
    "Магадан": (59.5638, 150.8035, "Магаданская область"), "Севастополь": (44.6167, 33.5254, "Севастополь"),
    "Симферополь": (44.9521, 34.1024, "Крым"), "Ялта": (44.4952, 34.1663, "Крым"), "Керчь": (45.3562, 36.4674, "Крым"),
    "Новороссийск": (44.7239, 37.7708, "Краснодарский край"), "Анапа": (44.8940, 37.3162, "Краснодарский край"),
    "Таганрог": (47.2362, 38.8969, "Ростовская область"), "Шахты": (47.7085, 40.2142, "Ростовская область"),
    "Подольск": (55.4242, 37.5547, "Московская область"), "Балашиха": (55.7963, 37.9382, "Московская область"),
    "Люберцы": (55.6772, 37.8932, "Московская область"), "Мытищи": (55.9116, 37.7307, "Московская область"),
    "Красногорск": (55.8317, 37.3295, "Московская область"), "Химки": (55.8970, 37.4297, "Московская область"),
    "Одинцово": (55.6780, 37.2777, "Московская область"), "Королёв": (55.9142, 37.8545, "Московская область"),
    "Энгельс": (51.4854, 46.1267, "Саратовская область"), "Тольятти": (53.5078, 49.4204, "Самарская область"),
    "Набережные Челны": (55.7436, 52.3958, "Татарстан"), "Нижнекамск": (55.6366, 51.8209, "Татарстан"),
    "Стерлитамак": (53.6300, 55.9500, "Башкортостан"), "Новый Уренгой": (66.0833, 76.6333, "ЯНАО"),
    "Норильск": (69.3558, 88.1893, "Красноярский край"), "Тобольск": (58.1989, 68.2536, "Тюменская область"),
    "Видное": (55.552, 37.708, "Московская область"), "Домодедово": (55.4363, 37.7664, "Московская область"),
    "Жуковский": (55.5953, 38.1203, "Московская область"), "Раменское": (55.5669, 38.2301, "Московская область"),
    "Сергиев Посад": (56.3, 38.1333, "Московская область"), "Дубна": (56.7333, 37.1667, "Московская область"),
    "Электросталь": (55.7833, 38.45, "Московская область"), "Коломна": (55.0794, 38.7783, "Московская область"),
    "Серпухов": (54.9167, 37.4, "Московская область"), "Ступино": (54.8833, 38.0667, "Московская область"),
    "Дзержинск": (56.2389, 43.4631, "Нижегородская область"), "Кстово": (56.1519, 44.1966, "Нижегородская область"),
    "Волжский": (48.7858, 44.7794, "Волгоградская область"), "Балаково": (52.0278, 47.8007, "Саратовская область"),
    "Новочеркасск": (47.4212, 40.0914, "Ростовская область"), "Батайск": (47.1398, 39.7507, "Ростовская область"),
    "Аксай": (47.2677, 39.8693, "Ростовская область"), "Армавир": (45.0, 41.1167, "Краснодарский край"),
    "Туапсе": (44.1, 39.0833, "Краснодарский край"), "Ейск": (46.7111, 38.2736, "Краснодарский край"),
    "Находка": (42.8167, 132.8833, "Приморский край"), "Комсомольск-на-Амуре": (50.55, 137.0, "Хабаровский край"),
    "Орск": (51.2, 58.5667, "Оренбургская область"), "Ухта": (63.5667, 53.6833, "Коми"),
    "Ангарск": (52.5448, 103.8886, "Иркутская область"), "Братск": (56.1325, 101.614, "Иркутская область"),
    "Бийск": (52.5393, 85.2138, "Алтайский край"),
}

# Регионы: запасной вариант, когда в тексте нет города из списка. Центры примерные, метка будет пунктирной.
# (основа названия в тексте, регион, широта, долгота)
REGIONS = [
    ("Белгородск", "Белгородская область", 50.6, 36.6), ("Брянск", "Брянская область", 53.24, 34.36),
    ("Курск", "Курская область", 51.73, 36.19), ("Орловск", "Орловская область", 52.97, 36.07),
    ("Калужск", "Калужская область", 54.51, 36.26), ("Тульск", "Тульская область", 54.19, 37.62),
    ("Рязанск", "Рязанская область", 54.63, 39.69), ("Воронежск", "Воронежская область", 51.66, 39.2),
    ("Липецк", "Липецкая область", 52.61, 39.6), ("Тамбовск", "Тамбовская область", 52.72, 41.45),
    ("Пензенск", "Пензенская область", 53.2, 45.0), ("Саратовск", "Саратовская область", 51.53, 46.03),
    ("Волгоградск", "Волгоградская область", 48.71, 44.51), ("Ростовск", "Ростовская область", 47.24, 39.7),
    ("Краснодарск", "Краснодарский край", 45.04, 38.98), ("Ставропольск", "Ставропольский край", 45.04, 41.97),
    ("Астраханск", "Астраханская область", 46.35, 48.04), ("Самарск", "Самарская область", 53.2, 50.1),
    ("Ульяновск", "Ульяновская область", 54.31, 48.4), ("Оренбургск", "Оренбургская область", 51.77, 55.1),
    ("Челябинск", "Челябинская область", 55.16, 61.44), ("Свердловск", "Свердловская область", 56.84, 60.6),
    ("Тюменск", "Тюменская область", 57.15, 65.53), ("Курганск", "Курганская область", 55.44, 65.34),
    ("Пермск", "Пермский край", 58.01, 56.25), ("Кировск", "Кировская область", 58.6, 49.67),
    ("Нижегородск", "Нижегородская область", 56.33, 44.0), ("Владимирск", "Владимирская область", 56.13, 40.41),
    ("Ивановск", "Ивановская область", 57.0, 40.97), ("Костромск", "Костромская область", 57.77, 40.93),
    ("Ярославск", "Ярославская область", 57.63, 39.88), ("Тверск", "Тверская область", 56.86, 35.92),
    ("Смоленск", "Смоленская область", 54.78, 32.05), ("Московск", "Московская область", 55.75, 37.62), ("Подмосков", "Московская область", 55.75, 37.62),
    ("Ленинградск", "Ленинградская область", 59.9, 30.3), ("Ленобласт", "Ленинградская область", 59.9, 30.3), ("Псковск", "Псковская область", 57.81, 28.35),
    ("Новгородск", "Новгородская область", 58.52, 31.28), ("Вологодск", "Вологодская область", 59.22, 39.89),
    ("Архангельск", "Архангельская область", 64.54, 40.52), ("Мурманск", "Мурманская область", 68.96, 33.08),
    ("Калининградск", "Калининградская область", 54.71, 20.45), ("Новосибирск", "Новосибирская область", 55.03, 82.92),
    ("Омск", "Омская область", 54.99, 73.32), ("Томск", "Томская область", 56.5, 84.97),
    ("Кемеровск", "Кемеровская область", 55.35, 86.09), ("Иркутск", "Иркутская область", 52.29, 104.28),
    ("Красноярск", "Красноярский край", 56.01, 92.89), ("Алтайск", "Алтайский край", 53.35, 83.77),
    ("Забайкальск", "Забайкальский край", 52.03, 113.5), ("Хабаровск", "Хабаровский край", 48.48, 135.07),
    ("Приморск", "Приморский край", 43.12, 131.89), ("Амурск", "Амурская область", 50.29, 127.53),
    ("Сахалинск", "Сахалинская область", 46.96, 142.74), ("Камчат", "Камчатский край", 53.05, 158.65),
    ("Магаданск", "Магаданская область", 59.56, 150.8), ("Еврейск", "Еврейская автономная область", 48.8, 132.9),
    ("Татарстан", "Татарстан", 55.8, 49.1), ("Башкири", "Башкортостан", 54.74, 55.97), ("Башкортостан", "Башкортостан", 54.74, 55.97),
    ("Дагестан", "Дагестан", 42.98, 47.5), ("Чечн", "Чечня", 43.32, 45.69), ("Крым", "Крым", 44.95, 34.1),
    ("Бурят", "Бурятия", 51.83, 107.58), ("Якут", "Якутия", 62.04, 129.68), ("Саха", "Якутия", 62.04, 129.68),
    ("Коми", "Коми", 61.67, 50.84), ("Карели", "Карелия", 61.78, 34.35), ("Удмурт", "Удмуртия", 56.85, 53.21),
    ("Чуваш", "Чувашия", 56.14, 47.25), ("Мордови", "Мордовия", 54.19, 45.18), ("Марий", "Марий Эл", 56.63, 47.89),
    ("Калмык", "Калмыкия", 46.31, 44.26), ("Хакас", "Хакасия", 53.72, 91.43), ("Тыв", "Тыва", 51.72, 94.45),
    ("Кабардино", "Кабардино-Балкария", 43.5, 43.62), ("Осети", "Северная Осетия", 43.02, 44.68),
    ("Ингуше", "Ингушетия", 43.17, 44.81), ("Карачаево", "Карачаево-Черкесия", 43.9, 41.7),
    ("Адыге", "Адыгея", 44.61, 40.1), ("Ханты-Мансийск", "ХМАО", 61.0, 69.0), ("ХМАО", "ХМАО", 61.0, 69.0),
    ("Ямал", "ЯНАО", 66.5, 72.0), ("ЯНАО", "ЯНАО", 66.5, 72.0), ("Севастопол", "Севастополь", 44.62, 33.52),
]
REG_RE = [(rx, reg, lat, lon) for stem_, reg, lat, lon in REGIONS
          for rx in [re.compile(r"\b" + re.escape(stem_) + r"\w*\s*(?:област|кра[йяеюи]|округ|республик|АО\b|автоном)?", re.I)]]


def stem(name):
    """Основа слова, чтобы находить склонения: Оренбург -> Оренбург, Москва -> Моск."""
    return name[:-1] if name[-1] in "аяьйеыио" else name


CTX = r"центр\w*|окраин\w*|пригород\w*|район\w*|жител\w*|мэр\w*|власт\w*|город\w*|черт\w+|окрестност\w+|территори\w+|полици\w*|прокуратур\w*|следовател\w*|суд\w*|СК|МЧС|ГИБДД|ГУ МВД|УМВД"


def _wstem(w):
    """Основа слова для поиска склонений (Нижний -> Нижн, Ростов -> Ростов, Москва -> Моск)."""
    if w.islower():
        return re.escape(w)
    if re.search(r"(ий|ый|ой|ая|яя|ое|ее|ые|ие)$", w) and len(w) > 5:
        return re.escape(w[:-2])
    return re.escape(stem(w))


def gaz_rx(n):
    body = r"[\s-]+".join(_wstem(w) + (r"\w*" if not w.islower() else "") for w in re.split(r"[\s-]+", n))
    return re.compile(r"\b(?:в|во|на|под|около|у|из|к|ко|до|над|возле|близ|вблизи|по|от|через|" + CTX + r")\s+(?:города\s+|г\.\s*)?" + body, re.I)


GAZ_RE = [(n, gaz_rx(n)) for n in GAZETTEER]


_PREP = r"\b(?:в|во|на|под|около|у|из|к|ко|до|над|возле|близ|вблизи|по|от|через)\s+"
GAZ_ALIAS = [("Санкт-Петербург", re.compile(r"\b(?:в|во|на|под|около|у|из|к|ко|до|над|возле|близ|вблизи|по|от|через|" + CTX + r")\s+(?:города\s+)?(?:Петербург|Питер)\w*", re.I))]

GAZ_RE += GAZ_ALIAS

# Другие страны: такие новости идут в ленту и канал, но без точки на карте (регион «За рубежом», координаты не используются).
import foreign as _foreign
FOREIGN_REGION = "За рубежом"


def foreign_place(title, text):
    """Название зарубежного места (именительный падеж), если новость про другую страну и в заголовке нет российского места."""
    name = _foreign.find(title, text)
    if not name:
        return None
    if any(rx.search(title) for _, rx in GAZ_RE) or any(
            re.search(r"\b(?:в|во|на|по|над|из)\s+" + rx.pattern, title, re.I) for rx, *_ in REG_RE):
        return None  # в заголовке есть российский город или регион: считаем российской новостью
    return name


STREET_RE = re.compile(r"(?:ул\.|улиц[аеуы]|проспект\w*|пр-т|шоссе|переул\w+|бульвар\w*)\s+[А-ЯЁ][\w-]+(?:\s+\d+\w?)?")
CAND_RE = re.compile(r"\b(?:в|во|под)\s+([А-ЯЁ][а-яё-]{3,}(?:\s[А-ЯЁ][а-яё-]+)?)")

_last_geocode = 0.0


def db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS incidents(
        url TEXT PRIMARY KEY, title TEXT, summary TEXT, type TEXT, lat REAL, lon REAL,
        region TEXT, place TEXT, precise INTEGER, ts INTEGER, source TEXT,
        verified INTEGER DEFAULT 0, created_at INTEGER)""")
    for col in ("img TEXT", "posted_at INTEGER", "vid INTEGER", "vurl TEXT"):
        try:
            con.execute("ALTER TABLE incidents ADD COLUMN " + col)
        except sqlite3.OperationalError:
            pass  # столбец уже есть
    con.execute("CREATE TABLE IF NOT EXISTS geocache(q TEXT PRIMARY KEY, lat REAL, lon REAL, region TEXT)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_ts ON incidents(ts)")
    return con


def geocode(con, q):
    """Вернуть (lat, lon, region) или None. Результаты кэшируются."""
    global _last_geocode
    row = con.execute("SELECT lat, lon, region FROM geocache WHERE q=?", (q,)).fetchone()
    if row:
        return row if row[0] is not None else None
    result = None
    try:
        if YANDEX_KEY:
            r = requests.get("https://geocode-maps.yandex.ru/1.x/", timeout=15, params={
                "apikey": YANDEX_KEY, "geocode": q, "format": "json", "results": 1, "lang": "ru_RU"})
            items = r.json()["response"]["GeoObjectCollection"]["featureMember"]
            if items:
                obj = items[0]["GeoObject"]
                lon, lat = map(float, obj["Point"]["pos"].split())
                meta = obj["metaDataProperty"]["GeocoderMetaData"]
                if meta.get("AddressDetails", {}).get("Country", {}).get("CountryNameCode") == "RU":
                    region = meta["AddressDetails"]["Country"].get("AdministrativeArea", {}).get("AdministrativeAreaName", "")
                    result = (lat, lon, region)
        else:
            wait = 1.1 - (time.time() - _last_geocode)  # правило Nominatim: не чаще 1 запроса в секунду
            if wait > 0:
                time.sleep(wait)
            _last_geocode = time.time()
            r = requests.get("https://nominatim.openstreetmap.org/search", timeout=15,
                             headers={"User-Agent": USER_AGENT},
                             params={"q": q, "format": "jsonv2", "countrycodes": "ru", "limit": 1,
                                     "accept-language": "ru", "addressdetails": 1})
            data = r.json()
            if data:
                a = data[0].get("address", {})
                result = (float(data[0]["lat"]), float(data[0]["lon"]), a.get("state", ""))
    except Exception as e:  # сеть и разбор ответа не должны ронять весь сбор
        print("  геокодер:", e, file=sys.stderr)
        return None
    con.execute("INSERT OR REPLACE INTO geocache VALUES(?,?,?,?)",
                (q, *(result if result else (None, None, None))))
    con.commit()
    return result


def classify(text):
    if STOP.search(text) or SENSITIVE.search(text):
        return None
    for t, pattern in RULES:
        if re.search(pattern, text, re.I):
            return t
    if LAW_ACT.search(text) and LAW_ANCHOR.search(text):
        return "law"
    return None


# ---- малые города, посёлки и сёла: поиск через Nominatim с учётом склонений и области из текста ----
GEO_BUDGET = int(os.environ.get("GEO_BUDGET", "70"))  # новых запросов к геокодеру за один запуск (результаты кэшируются)
_geo_used = 0
SETTLE_TYPES = {"city", "town", "village", "hamlet", "suburb", "locality", "isolated_dwelling", "neighbourhood", "quarter"}
SETTLE_RE = re.compile(
    r"(?:(?i:\b(?:в|во|на|под|у|около|возле|близ|вблизи|рядом с|недалеко от|в районе|центр\w*|окраин\w*|пригород\w*|район\w*|жител\w*|мэр\w*|власт\w*|черт\w+|окрестност\w+|территори\w+)\s+"
    r"(?:(?:селе|села|селу|посёлке|поселке|посёлка|поселка|деревне|деревни|городе|города|станице|станицы|хуторе|хутора|ауле|пгт|микрорайоне)\s+)?)"
    r"|(?i:\b(?:с|д|г|п|ст|х|пос)\.\s*)|(?i:\b(?:село|села|селе|селу|посёлок|посёлка|посёлке|поселок|поселка|поселке|деревня|деревни|деревне|город|города|городе|станица|станицы|станице|хутор|хутора|хуторе|аул|аула|ауле|пгт)\s+))"
    r"([А-ЯЁ][а-яё]+(?:-[А-ЯЁа-яё][а-яё]+){0,2}(?:\s[А-ЯЁ][а-яё]+)?)")
ADJ_REGION = re.compile(r"(ск|цк)(ой|ая|ий|ую|их|им|ом|ое)$")
AREA_WORD = re.compile(r"\s*(област|кра[йяеюи]|округ|республик|район|р-н)", re.I)
STOPCAP = {"России", "Россия", "Российской", "Украине", "Украины", "Белоруссии", "Европе", "Азии", "Сибири", "Подмосковье", "Подмосковья",
           "Донбасса", "Донбасс", "Киева", "Харькова", "Одессы", "Телеграм", "Минобороны", "Москвы", "Москве", "Петербурге", "Петербурга", "Интернете", "Крыму", "Крыма"}
_HINT_REPUBLICS = ("Татарстан", "Башкортостан", "Башкири", "Дагестан", "Чечн", "Крым", "Бурят", "Якут", "Калмык", "Хакас", "Ингуш",
                   "Адыге", "Мордови", "Удмурт", "Чуваш", "Марий", "Карели", "Осети", "Кабардино", "Карачаево")


def region_hint(text):
    """Область/край/республика, названные в тексте, или None."""
    for rx, reg, lat, lon in REG_RE:
        m = rx.search(text)
        if not m:
            continue
        g = m.group(0)
        if re.search(r"област|кра[йяеюи]|округ|республик", g, re.I) or g.startswith(_HINT_REPUBLICS):
            return reg
    if re.search(r"Подмосков", text):
        return "Московская область"
    return None


def name_variants(c):
    """Возможные именительные формы названия из текста (в Махачкале -> Махачкала и т.п.), без внешних библиотек."""
    if "-" in c and " " not in c:
        first, rest = c.split("-", 1)
        return [f + "-" + rest for f in name_variants(first)][:6]
    ws = c.split()
    def tails(w, last):
        out = [w]
        rules = (("ах", "и"), ("ах", "ы"), ("ях", "и"), ("ом", ""), ("ом", "о"), ("ом", "ое"), ("ом", "ый"), ("ом", "ий"), ("ем", ""),
                 ("ой", "а"), ("ой", "ая"), ("ей", "яя"), ("ью", "ь"), ("е", ""), ("е", "а"), ("е", "о"), ("и", "ь"), ("и", "я"), ("ы", "а"),
                 ("а", ""), ("а", "о"), ("у", "а"), ("у", ""), ("я", "ь"), ("я", "й"))
        for old, add in rules:
            if w.endswith(old) and len(w) - len(old) >= 3:
                f = w[:-len(old)] + add
                if f not in out:
                    out.append(f)
        return out if last else out[:6]
    def head_forms(w):
        out = []
        for old, adds in (("ой", ("ая",)), ("ей", ("яя",)), ("ом", ("ый", "ое")), ("ем", ("ий",)), ("ого", ("ий", "ый")), ("ую", ("ая",))):
            if w.endswith(old):
                out += [w[:-len(old)] + a for a in adds]
        return out + [w]
    last_forms = tails(ws[-1], True)
    res = []
    for lf in last_forms:
        if len(ws) == 1:
            res.append(lf)
        else:
            for hf in head_forms(ws[0])[:3]:
                res.append(hf + " " + lf)
    uniq = []
    for r in res:
        if r not in uniq:
            uniq.append(r)
    return uniq[:7]


def geocode_place(con, name, hint):
    """Населённый пункт name (возможно с областью hint) -> (lat, lon, region) или None. Только населённые пункты."""
    global _geo_used, _last_geocode
    key = "pl:" + name + "|" + (hint or "")
    row = con.execute("SELECT lat, lon, region FROM geocache WHERE q=?", (key,)).fetchone()
    if row:
        return row if row[0] is not None else None
    if _geo_used >= GEO_BUDGET:
        return None
    _geo_used += 1
    result = None
    try:
        wait = 1.1 - (time.time() - _last_geocode)
        if wait > 0:
            time.sleep(wait)
        _last_geocode = time.time()
        r = requests.get("https://nominatim.openstreetmap.org/search", timeout=15, headers={"User-Agent": USER_AGENT},
                         params={"q": name + (", " + hint if hint else ""), "format": "jsonv2", "countrycodes": "ru", "limit": 3,
                                 "accept-language": "ru", "addressdetails": 1})
        found = [d for d in r.json() if d.get("type") in SETTLE_TYPES or d.get("addresstype") in SETTLE_TYPES]
        if not hint and len({(d.get("address") or {}).get("state", "") for d in found}) > 1:
            found = []  # одноимённые населённые пункты в разных областях (Заря, Новый...), без подсказки региона не угадать
        for d in found:
            state = (d.get("address") or {}).get("state", "")
            if hint and max(hint.split(), key=len)[:6].lower() not in state.lower():
                continue
            canon = next((reg for rx, reg, _la, _lo in REG_RE if rx.search(state)), state)
            result = (float(d["lat"]), float(d["lon"]), hint or canon)
            break
    except Exception as e:
        print("  геокодер (нас. пункт):", e, file=sys.stderr)
        return None
    con.execute("INSERT OR REPLACE INTO geocache VALUES(?,?,?,?)", (key, *(result if result else (None, None, None))))
    con.commit()
    return result


def find_small_town(con, text, prefer=None):
    hint = region_hint(text)
    hints = [h for h in dict.fromkeys([prefer, hint]) if h] or [None]  # регион канала важнее региона из текста: «из Ишима Тюменской области» не место аварии
    seen = set()
    n = 0
    for m in SETTLE_RE.finditer(text):
        words = m.group(1).split()
        cands = [" ".join(words)] if len(words) == 2 and not ADJ_REGION.search(words[1]) else []
        cands.append(words[0])
        for c in cands:
            first = c.split()[0]
            if c in seen or first in STOPCAP or len(first) < 4:
                continue
            seen.add(c)
            if re.match(r"\s*(?:проспект|ул\.|улиц|шоссе|переул|бульвар|набережн|площад|мост|парк|район|област)", text[m.start(1) + len(c):m.start(1) + len(c) + 16], re.I):
                continue  # это улица, район или область, а не населённый пункт
            if ADJ_REGION.search(first) and AREA_WORD.match(text[m.end():m.end() + 14]):
                continue  # это название области или района, а не города
            n += 1
            if n > 3:
                return None
            for v in name_variants(c):
                for h in hints:
                    hit = geocode_place(con, v, h)
                    if hit:
                        return hit[0], hit[1], hit[2], v, 0
    return None



STREET2_RE = re.compile(r"[А-ЯЁ][а-яё]+(?:ом|ой|ем|ей|им)\s+(?:проспекте|шоссе|бульваре|переулке|набережной|площади)")


def default_place(con, text, default):
    """Место по умолчанию для канала («Фонтанка» всегда про Петербург): город, область или «посёлок, область»."""
    if not default:
        return None
    name, _, hint = [x.strip() for x in default.partition(",")]
    street = STREET_RE.search(text) or STREET2_RE.search(text)
    if name in GAZETTEER:
        lat, lon, reg = GAZETTEER[name]
        if street:
            hit = geocode(con, f"{street.group(0)}, {name}")
            if hit:
                return (*hit[:2], hit[2] or reg, f"{name}, {street.group(0)}", 1)
        return lat, lon, reg, name, 0
    for _rx, reg, lat, lon in REG_RE:
        if reg == name:
            return lat, lon, reg, reg, 0
    hit = geocode_place(con, name, hint or None)
    return (hit[0], hit[1], hit[2], name, 0) if hit else None


def default_region(default):
    """Область места по умолчанию канала: «Екатеринбург» -> «Свердловская область», «Кстово, Нижегородская область» -> «Нижегородская область»."""
    if not default:
        return None
    name, _, hint = [x.strip() for x in default.partition(",")]
    if hint:
        return hint
    if name in GAZETTEER:
        reg = GAZETTEER[name][2]
        return reg if re.search(r"област|край|республик|округ", reg, re.I) or reg in ("Татарстан", "Башкортостан", "Дагестан") else None
    return name if re.search(r"област|край|республик", name, re.I) else None


def locate(con, text, default=None):
    """Вернуть (lat, lon, region, place, precise) или None. default - место канала по умолчанию."""
    street = STREET_RE.search(text) or STREET2_RE.search(text)
    city = next((n for n, rx in GAZ_RE if rx.search(text)), None)
    if city and street:
        hit = geocode(con, f"{street.group(0)}, {city}")
        if hit:
            return (*hit[:2], hit[2] or GAZETTEER[city][2], f"{city}, {street.group(0)}", 1)
    if city:
        lat, lon, region = GAZETTEER[city]
        return lat, lon, region, city, 0
    small = find_small_town(con, text, default_region(default))
    if small:
        return small
    for rx, reg, lat, lon in REG_RE:
        # регион засчитываем, только если рядом слово "область/край/республика" или предлог места
        if re.search(r"\b(?:в|во|на|по|над|из|юге|севере|западе|востоке)\s+" + rx.pattern, text, re.I):
            return lat, lon, reg, reg, 0
    for cand in ([] if default else CAND_RE.findall(text)[:2]):
        if cand.split()[0] in STOPCAP or re.match(r"Росси|Российск|Украин|Белорус", cand):
            continue
        hit = geocode(con, cand)
        if hit:
            return (*hit[:2], hit[2], cand, 0)
    h = region_hint(text)  # "Губернатор Самарской области ..." - регион назван без предлога
    if h:
        for _rx, reg, lat, lon in REG_RE:
            if reg == h:
                return lat, lon, reg, reg, 0
    return default_place(con, text, default)


def clean(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


WORKER_URL = os.environ.get("WORKER_URL", "").strip().rstrip("/")
WORKER_KEY = os.environ.get("WORKER_KEY", "").strip()
CHANNEL_USER = os.environ.get("CHANNEL", "@nrmpnews").strip().lstrip("@")


def pull_submissions(con):
    """Новости подписчиков, которые администратор уже опубликовал в канале, добавляем в ленту и на карту (повторно в канал не идут)."""
    if not (WORKER_URL and WORKER_KEY):
        return 0
    try:
        items = requests.get(WORKER_URL + "/approved", params={"key": WORKER_KEY}, timeout=20).json().get("items", [])
    except Exception as e:
        print("предложка: не удалось получить список:", e, file=sys.stderr)
        return 0
    n = 0
    for it in items:
        text = re.sub(r"[ \t]+", " ", (it.get("text") or "")).strip()
        mid = it.get("mid")
        url = f"https://t.me/{CHANNEL_USER}/{mid}"
        if not mid or len(text) < 20 or con.execute("SELECT 1 FROM incidents WHERE url=?", (url,)).fetchone():
            continue
        where = locate(con, text)
        if not where:
            print("  предложка: нет места:", text[:70])
            continue
        lat, lon, region, place, precise = where
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        title = lines[0]
        if len(title) < 20 and len(lines) > 1:
            title += " " + lines[1]
        ts = int(it.get("ts") or time.time())
        con.execute("INSERT INTO incidents(url,title,summary,type,lat,lon,region,place,precise,ts,source,verified,created_at,posted_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
                    (url, title[:160], " ".join(lines)[:1200], classify(text) or "tech", lat, lon, region, place, precise, ts,
                     "Подписчик", int(time.time()), int(time.time())))
        n += 1
        print("  предложка:", title[:70], "|", place)
    con.commit()
    return n


def _norm_reg(r):
    return (r or "").split("—")[0].split("(")[0].strip().lower()


def audit_places(con):
    """Проверка мест за 14 дней: область метки не совпадает с областью из текста -> пересчитать.
    Города из заголовка надёжнее подсказки региона, а новости про несколько областей не трогаем."""
    con.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    since = int(time.time()) - 14 * 86400
    rows = con.execute("SELECT url,title,summary,region,place,source FROM incidents WHERE lat IS NOT NULL AND ts>? AND source!='Подписчик' "
                       "AND type!='mil' AND region IS NOT NULL AND region!='За рубежом'", (since,)).fetchall()
    repair = not con.execute("SELECT 1 FROM meta WHERE k='audit_repair_1'").fetchone()
    bad = fixed = back = 0
    for url, title, summary, region, place, source in rows:
        title = title or ""
        text = title + ". " + (summary or "")
        tcity = next((n for n, rx in GAZ_RE if rx.search(title)), None)
        res = None
        if repair and place and _norm_reg(place) == _norm_reg(region) and tcity and GAZETTEER[tcity][2] != region:
            # разовый откат: прошлая версия проверки могла увести метку с города из заголовка на область
            res = locate(con, text, CH_DEFAULT.get(source))
            if res and res[2] != region:
                back += 1
                con.execute("UPDATE incidents SET lat=?,lon=?,region=?,place=?,precise=? WHERE url=?", (res[0], res[1], res[2], res[3], res[4], url))
                print("  возвращено место:", title[:60], "|", region, "->", res[3])
            continue
        hint = region_hint(text)
        if not hint or _norm_reg(hint) == _norm_reg(region) or tcity:
            continue
        own = next((rx for rx, reg, _la, _lo in REG_RE if reg == region), None)
        if own is not None and own.search(text):
            continue  # область метки тоже названа в тексте: новость про несколько областей
        if any(GAZETTEER[n][2] == region and rx.search(text) for n, rx in GAZ_RE):
            continue  # в тексте назван город области метки
        bad += 1
        res = locate(con, text, CH_DEFAULT.get(source))
        if not res or _norm_reg(res[2]) != _norm_reg(hint):
            la, lo = next(((la, lo) for _rx, reg, la, lo in REG_RE if reg == hint), (None, None))
            if la is None:
                continue
            res = (la, lo, hint, hint, 0)
        con.execute("UPDATE incidents SET lat=?,lon=?,region=?,place=?,precise=? WHERE url=?", (res[0], res[1], res[2], res[3], res[4], url))
        fixed += 1
        print("  исправлено место:", title[:60], "|", region, "->", res[3])
    if repair:
        con.execute("INSERT OR REPLACE INTO meta VALUES('audit_repair_1','1')")
    con.commit()
    print(f"Проверка мест: проверено {len(rows)}, не сошлось {bad}, исправлено {fixed}, возвращено {back}")


def run_once():
    con = db()
    pull_submissions(con)
    audit_places(con)
    added = 0
    for source, feed_url in FEEDS + [(n + " (Telegram)", "tg:" + u) for n, u in CHANNELS]:
        try:
            feed = tg_feed(feed_url[3:], source in CH_OFFICIAL) if feed_url.startswith("tg:") else feedparser.parse(feed_url, agent=USER_AGENT)
        except Exception as ex:  # одна нерабочая лента не должна ронять весь запуск
            print(f"{source}: ОШИБКА {type(ex).__name__}, пропускаю")
            continue
        print(f"{source}: {len(feed.entries)} записей")
        for e in feed.entries:
            url = e.get("link")
            if not url or con.execute("SELECT 1 FROM incidents WHERE url=?", (url,)).fetchone():
                continue
            published = calendar.timegm(e.published_parsed) if e.get("published_parsed") else int(time.time())
            if published < time.time() - MAX_AGE_DAYS * 86400:
                continue
            title, summary = clean(e.get("title")), clean(e.get("summary"))
            text = f"{title}. {summary}"
            kind = classify(text)
            if not kind:
                continue
            where = locate(con, text, CH_DEFAULT.get(source))
            fp = foreign_place(title, text)
            if fp:
                print("  за рубежом:", fp, "|", title[:70])
                where = (0.0, 0.0, FOREIGN_REGION, fp, 0)  # другая страна: только лента и канал, без точки на карте
            if not where:
                print("  нет места:", title[:70])
                continue
            lat, lon, region, place, precise = where
            ts = calendar.timegm(e.published_parsed) if e.get("published_parsed") else int(time.time())
            # Храним заголовок, ссылку и короткую выдержку. Полный текст статей не копируем.
            img = None
            for m in (e.get("media_content") or []) + (e.get("media_thumbnail") or []) + (e.get("enclosures") or []):
                u = m.get("url") or m.get("href") or ""
                if u.startswith("https://"):
                    img = u
                    break
            con.execute("INSERT INTO incidents(url,title,summary,type,lat,lon,region,place,precise,ts,source,verified,created_at,img,vid,vurl) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?)",
                        (url, title, summary[:1200], kind, lat, lon, region, place, precise, ts, source, int(time.time()), img,
                         e.get("vid") or 0, e.get("vurl")))
            added += 1
        con.commit()
    print(f"Добавлено: {added}")


if __name__ == "__main__":
    if "--loop" in sys.argv:
        pause = int(sys.argv[sys.argv.index("--loop") + 1])
        while True:
            run_once()
            time.sleep(pause)
    else:
        run_once()
