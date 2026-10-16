"""Один раз загрузить в базу демо-записи из web/index-demo.html (чтобы карта не была пустой до первых сборов).

python seed_demo.py
"""
import json
import re
import time

import collector

s = open("web/index-demo.html", encoding="utf-8").read()
data = json.loads(re.search(r"var DATA = (\[.*?\]);\n", s, re.S).group(1))
con = collector.db()
n = 0
for i in data:
    cur = con.execute(
        "INSERT OR IGNORE INTO incidents(url,title,summary,type,lat,lon,region,place,precise,ts,source,verified,created_at,img) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
        (i["url"], i["title"], i.get("summary", ""), i["type"], i["lat"], i["lon"], i["region"], i["place"],
         i.get("precise", 0), i["ts"], i["source"], int(time.time()), i.get("img")))
    n += cur.rowcount
con.commit()
print("добавлено записей:", n)
