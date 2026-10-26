"""Охват карты по регионам: где мало новостей или нет вовсе и где нет своего источника.
Запуск: python coverage.py  (в Actions пишет таблицу на страницу запуска). Ничего не публикует и не меняет."""
import os, re, sqlite3, time
import collector as C

DB = os.environ.get("DB_PATH", "incidents.db")
EXTRA = ["Москва", "Санкт-Петербург", "Республика Алтай", "Ненецкий АО", "Чукотский АО"]
SKIP = {"За рубежом", "Подписчик", ""}


def universe():
    regs = {r[1] for r in C.REGIONS} | {v[2] for v in C.GAZETTEER.values()} | set(EXTRA)
    return sorted(regs - SKIP)


def sourced():
    """Регионы, у которых есть канал или лента с местом по умолчанию -> список имён источников."""
    out = {}
    for line in open("channels.txt", encoding="utf-8"):
        p = [x.strip() for x in line.split("|")]
        if line.startswith("#") or len(p) < 3 or not p[2]:
            continue
        reg = C.default_region(p[2]) or (C.GAZETTEER.get(p[2].split(",")[0].strip()) or (0, 0, p[2]))[2]
        out.setdefault(reg, []).append(p[0])
    for line in open("feeds.txt", encoding="utf-8"):
        p = [x.strip() for x in line.split("|")]
        if line.startswith("#") or len(p) < 3 or not p[2]:
            continue
        reg = C.default_region(p[2]) or (C.GAZETTEER.get(p[2].split(",")[0].strip()) or (0, 0, p[2]))[2]
        out.setdefault(reg, []).append(p[0])
    return out


def main():
    regs, src = universe(), sourced()
    cnt, last = {}, {}
    if os.path.exists(DB):
        con = sqlite3.connect(DB)
        now = int(time.time())
        for reg, n5, n1, mx in con.execute(
                "SELECT region, COUNT(*), SUM(ts>=?), MAX(ts) FROM incidents WHERE ts>=? GROUP BY region",
                (now - 86400, now - 5 * 86400)):
            cnt[reg] = (n5, n1 or 0)
            last[reg] = mx
    rows = []
    for r in regs:
        n5, n1 = cnt.get(r, (0, 0))
        rows.append((n5, n1, r, len(src.get(r, []))))
    rows.sort()
    L = ["## Охват карты по регионам (5 дней)", ""]
    L.append("**Нет новостей вообще:** " + (", ".join(f"{r}" + ("" if s else " ⚠ нет источника") for n5, _, r, s in rows if n5 == 0) or "—"))
    L.append("")
    L.append("**Мало (1–4 за 5 дней):** " + (", ".join(f"{r} ({n5})" for n5, _, r, s in rows if 0 < n5 < 5) or "—"))
    L.append("")
    L.append("**Нет своего источника (канала или ленты):** " + ", ".join(r for _, _, r, s in rows if not s))
    L.append("")
    L.append("| Регион | 5 дней | 24 ч | Источников |")
    L.append("|---|---|---|---|")
    for n5, n1, r, s in rows:
        L.append(f"| {r} | {n5} | {n1} | {s} |")
    text = "\n".join(L)
    print(text)
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        open(p, "a", encoding="utf-8").write(text + "\n")


if __name__ == "__main__":
    main()
