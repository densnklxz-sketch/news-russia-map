"""Поиск повторов: одна и та же новость от разных изданий в разных формулировках.
Сравниваем смысл (слова с учётом синонимов), цифры (сколько погибших, сумма, возраст) и место."""
import math
import re

DUP_HOURS = 72  # окно сравнения

# синонимы сводим к одному слову: «погибли», «скончались», «унесло жизни» - одно и то же
_SYN = [
    (r"погиб\w*|скончал\w*|умер\w*|гибел\w*|смерт\w*|унесл\w* жизн\w*|унес\w* жизн\w*", "погиб"),
    (r"пострада\w*|ранен\w*|ранил\w*|травмир\w*|госпитализ\w*", "пострад"),
    (r"дтп|авари\w*|столкнул\w*|столкновен\w*|врезал\w*|влетел\w*|наезд\w*|сбил\w*|опрокинул\w*", "авария"),
    (r"пожар\w*|возгоран\w*|загорел\w*|горит|горел\w*|огонь|огня", "пожар"),
    (r"задержа\w*|арестова\w*|схватил\w*|поймал\w*", "задерж"),
    (r"бпла|беспилотник\w*|дрон\w*", "бпла"),
    (r"отключ\w*|обесточ\w*|без\s+света|без\s+электричества", "свет"),
    (r"эвакуир\w*|эвакуац\w*", "эвакуац"),
    (r"мужчин\w*|парень|парня|водител\w*", "мужчина"),
    (r"женщин\w*|девушк\w*", "женщина"),
    (r"ребен\w*|ребён\w*|дет[еиям]\w*|школьник\w*|подросток\w*", "ребенок"),
]
_SYN = [(re.compile(p), r) for p, r in _SYN]
_UNIT = r"(?:человек\w*|погиб\w*|пострад\w*|ранен\w*|дет\w*|лет|года|кв|кг|тонн\w*|млн|млрд|тыс\w*|руб\w*|домов|квартир|автомоб\w*|машин\w*)"


_NUMW = {"один": "1", "одна": "1", "одного": "1", "два": "2", "две": "2", "двое": "2", "двух": "2", "три": "3", "трое": "3", "трех": "3",
         "четыре": "4", "четверо": "4", "четырех": "4", "пять": "5", "пятеро": "5", "пяти": "5", "шесть": "6", "шести": "6",
         "семь": "7", "семи": "7", "восемь": "8", "восьми": "8", "девять": "9", "девяти": "9", "десять": "10", "десяти": "10"}


def _norm(text):
    t = (text or "").lower().replace("ё", "е")
    t = re.sub(r"\b(" + "|".join(_NUMW) + r")\b", lambda m: _NUMW[m.group(1)], t)  # «три человека» = «3 человека»
    return re.sub(r"\bтыс\b\.?|\bтысячи\b", "тысяч", t)  # «тыс.» и «тысяч» - одно слово


def _syn(t):
    for rx, rep in _SYN:
        t = rx.sub(rep, t)
    return t


def _nums(t):
    big = set(re.findall(r"(?<![\d.,])\d{2,}(?![\d])", t))                       # 770, 35, 2026
    small = {m + "!" for m in re.findall(r"(?<![\d.,])(\d{1,2})\s+" + _UNIT, t)}       # «3 человека»: одна цифра с единицей
    return big | small


def sig(title, summary=""):
    """(слова заголовка, слова заголовка и начала анонса, числа заголовка и анонса, числа заголовка).
    Слова: первые 4 буквы слов длиннее 3 знаков, синонимы сведены к одному слову."""
    t, s = _norm(title), _norm((summary or "")[:400])
    words = lambda x: {w[:4] for w in re.findall(r"[а-яa-z]{4,}", _syn(x))}
    tw = words(t)
    return tw, tw | words(s), _nums(t) | _nums(s), _nums(t)


def is_dup(a, b):
    """a, b - результаты sig()."""
    ti = len(a[0] & b[0])
    ratio_t = ti / min(len(a[0]), len(b[0])) if a[0] and b[0] else 0
    conflict = bool(a[3] and b[3] and not (a[3] & b[3]))   # в заголовках разные числа: скорее разные события
    if ti >= 4 and ratio_t >= (0.85 if conflict else 0.6):
        return True
    fi = len(a[1] & b[1])
    ratio_f = fi / min(len(a[1]), len(b[1])) if a[1] and b[1] else 0
    if fi >= 6 and ratio_f >= (0.85 if conflict else 0.7):
        return True
    shared = a[2] & b[2]
    # одинаковые цифры (сумма, возраст, число пострадавших) + заметное совпадение слов
    if shared and ((ti >= 3 and ratio_t >= 0.45) or (fi >= 5 and ratio_f >= 0.5)):
        return True
    return False


_MSK = {"Москва", "Московская область"}


def regions_compatible(a, b):
    a, b = a or "", b or ""
    return a == b or not a or not b or (a in _MSK and b in _MSK)


def _km(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    x = math.sin((lat2 - lat1) * p / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(x))


def same_place(r, k):
    """Записи про одно место: одинаковый регион (с Москвой и областью вместе) либо точки ближе 120 км."""
    if regions_compatible(r.get("region"), k.get("region")):
        return True
    try:
        return _km(float(r["lat"]), float(r["lon"]), float(k["lat"]), float(k["lon"])) < 120
    except (KeyError, TypeError, ValueError):
        return False


_OFFICIAL = re.compile(r"тасс|риа|интерфакс|мчс|мвд|рбк|известия|коммерсант|лента|мк\b|ura|ngs|нгс|e1", re.I)


def quality(r):
    """Какая из записей-двойников лучше: с фото или видео, с более полным текстом, из крупного источника."""
    q = 0
    if r.get("img"):
        q += 3
    if r.get("vid"):
        q += 1
    q += min(len(r.get("summary") or ""), 800) / 400
    if _OFFICIAL.search(r.get("source") or ""):
        q += 1
    if r.get("precise"):
        q += 1
    return q
