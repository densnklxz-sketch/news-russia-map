"""Поиск повторов: одна и та же новость от разных изданий в разных формулировках."""
import re

DUP_HOURS = 72  # окно сравнения


def _norm(text):
    t = (text or "").lower().replace("ё", "е")
    return re.sub(r"\bтыс\b\.?|\bтысячи\b", "тысяч", t)  # «тыс.» и «тысяч» — одно слово


def sig(title, summary=""):
    """(слова заголовка, слова заголовка и начала анонса, числа заголовка и анонса).
    Слова: первые 4 буквы слов длиннее 3 знаков (так «перевёл» и «перевели» совпадут).
    Числа: от 2 цифр («770 тысяч» и «770 тыс.» дают одно число 770)."""
    t, s = _norm(title), _norm((summary or "")[:200])
    words = lambda x: {w[:4] for w in re.findall(r"[а-яa-z]{4,}", x)}
    nums = lambda x: set(re.findall(r"(?<![\d.,])\d{2,}(?![\d])", x))
    tw = words(t)
    return tw, tw | words(s), nums(t) | nums(s), nums(t)


def is_dup(a, b):
    """a, b — результаты sig()."""
    ti = len(a[0] & b[0])
    if ti >= 4 and ti / min(len(a[0]), len(b[0])) >= 0.6:
        return True
    fi = len(a[1] & b[1])
    if fi >= 6 and fi / min(len(a[1]), len(b[1])) >= 0.75:
        return True
    # одинаковое число в заголовках (сумма, возраст, количество) + несколько общих слов
    return bool(a[3] & b[3]) and ti >= 4
