# Aplikuje ruční úpravu nabídky z GitHub Issue (titulek "uprava:<id>", tělo = JSON).
# Povolená pole odpovídají ručním vstupům modelu; hodnoty mají přednost před automatikou.
import json
import os
import sqlite3
import sys

POVOLENA = {"stav", "rok_vystavby", "balkon", "parkovani", "dalsi_koef_pct",
            "lokalita", "najem_m2_mesic", "najem_priplatky_rocni", "zakladni_cena_m2",
            "watchlist", "skryto"}
CISELNA = {"rok_vystavby", "dalsi_koef_pct", "najem_m2_mesic",
           "najem_priplatky_rocni", "zakladni_cena_m2"}
# watchlist/skryto jsou čisté přepínače (0/1) — na rozdíl od ostatních polí
# prázdná hodnota neznamená "vrátit na automatiku", ale rovnou 0 (vypnuto).
BOOLEANOVA = {"watchlist", "skryto"}

event = json.load(open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8"))
issue = event["issue"]
titulek = issue["title"].strip()
if not titulek.startswith("uprava:"):
    print("není úprava, přeskakuji"); sys.exit(0)
lid = int(titulek.split(":", 1)[1])

telo = issue["body"] or "{}"
if "```" in telo:  # JSON může být v code bloku
    telo = telo.split("```")[1].removeprefix("json").strip()
data = json.loads(telo)

zmeny = {}
for k, v in data.items():
    if k not in POVOLENA:
        print(f"pole {k} není povolené, přeskakuji"); continue
    if k in BOOLEANOVA:
        zmeny[k] = 1 if str(v).strip().lower() in ("1", "true", "ano") else 0
    elif v in ("", None):
        zmeny[k] = None                       # prázdné = vrátit na automatiku
    elif k in CISELNA:
        zmeny[k] = float(v)
    else:
        zmeny[k] = str(v)
if not zmeny:
    print("žádné změny"); sys.exit(0)

con = sqlite3.connect("data/nemovitosti.db")
sada = ", ".join(f"{k}=?" for k in zmeny)
n = con.execute(f"UPDATE listings SET {sada} WHERE id=?", (*zmeny.values(), lid)).rowcount
con.commit()
print(f"nabídka {lid}: upraveno polí {len(zmeny)} ({n} řádek): {zmeny}")
