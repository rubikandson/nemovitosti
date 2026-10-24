#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Týdenní report podhodnocených bytů — deterministická náhrada Cowork úlohy
`tydenni-report-prilezitosti` pro svět bez Claude (běží v GitHub Actions).

Čte docs/data.json (výstup denní pipeline) a zapíše docs/weekly_report.md
s top 10 nových nabídek za posledních 7 dní, zvlášť pro Prahu a Střední Čechy.
POUZE reportuje nad už spočítanými poli — žádná oceňovací logika (ta zůstává
výhradně v src/valuation.py, viz CLAUDE.md hard rules).

Filtr (1:1 dle původní úlohy, schváleno uživatelem 2026-07-20):
  first_seen v posledních 7 dnech, sleva_pct != null, cena_czk != null,
  skryto != 1, vlastnictvi != "Družstevní" (null se ponechá a označí jako
  neověřené), patro != 0 (null se ponechá a označí jako neověřené).
  Řazení sestupně podle sleva_pct, top 10 v každém kraji.
"""
import json, os, sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "docs", "data.json")
OUT = os.path.join(ROOT, "docs", "weekly_report.md")

KRAJE = [("Praha", "Praha"), ("Středočeský", "Střední Čechy")]
WINDOW_DAYS = 7
TOP_N = 10


def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", ""))
    except ValueError:
        return None


def czk(v):
    try:
        return f"{int(round(float(v))):,}".replace(",", " ") + " Kč"
    except (TypeError, ValueError):
        return "?"


def num(v, suf=""):
    if v is None:
        return "?"
    try:
        f = float(v)
        return (f"{f:.0f}" if f == int(f) else f"{f:.1f}") + suf
    except (TypeError, ValueError):
        return str(v) + suf


def select(listings, kraj, now):
    hranice = now - timedelta(days=WINDOW_DAYS)
    out = []
    for r in listings:
        if r.get("kraj") != kraj:
            continue
        fs = parse_dt(r.get("first_seen"))
        if fs is None or fs < hranice:
            continue
        if r.get("sleva_pct") is None:
            continue
        if r.get("cena_czk") is None:
            continue
        if r.get("skryto") == 1:
            continue
        if r.get("vlastnictvi") == "Družstevní":
            continue
        if r.get("patro") == 0:
            continue
        out.append(r)
    out.sort(key=lambda r: r.get("sleva_pct"), reverse=True)
    return out[:TOP_N]


def line(r):
    parts = []
    if r.get("watchlist") == 1:
        parts.append("★")
    ctvrt = r.get("ctvrt") or r.get("lokalita") or r.get("nazev") or "?"
    parts.append(
        f"{ctvrt} — {r.get('dispozice') or '?'}, "
        f"{num(r.get('plocha_m2'), ' m²')}, {czk(r.get('cena_czk'))}, "
        f"sleva {num(r.get('sleva_pct'), ' %')} vs. tržní"
    )
    if r.get("url"):
        parts.append(f"— {r['url']}")
    tail = []
    if r.get("vlastnictvi") in (None, ""):
        tail.append("vlastnictví zatím neověřeno")
    if r.get("patro") is None:
        tail.append("patro zatím neověřeno")
    s = " ".join(parts)
    if tail:
        s += f" ({'; '.join(tail)})"
    return "- " + s


def main():
    if not os.path.exists(DATA):
        sys.exit(f"CHYBA: {DATA} neexistuje — nic negeneruji (žádná tichá náhrada).")
    with open(DATA, encoding="utf-8") as f:
        data = json.load(f)
    listings = data.get("listings")
    if not isinstance(listings, list):
        sys.exit("CHYBA: data.json nemá pole 'listings' — formát se změnil, končím.")
    now = datetime.now()
    generated = data.get("generated", "?")

    sekce, pocty = [], {}
    for kraj, label in KRAJE:
        vyber = select(listings, kraj, now)
        pocty[label] = len(vyber)
        blok = [f"## {label}"]
        blok.append("" if vyber else "žádné nové příležitosti tento týden")
        blok.extend(line(r) for r in vyber)
        sekce.append("\n".join(blok).rstrip())

    hlavicka = (
        f"# Týdenní report — podhodnocené byty\n\n"
        f"_Report generuje GitHub Action (`weekly_report.yml`) automaticky každé "
        f"pondělí z `docs/data.json`. Běží nezávisle na Claude i GPT._\n\n"
        f"Data vygenerována: **{generated}**. Nové nabídky za posledních "
        f"{WINDOW_DAYS} dní splňující filtr — Praha: **{pocty['Praha']}**, "
        f"Střední Čechy: **{pocty['Střední Čechy']}**. "
        f"Report vytvořen: {now.strftime('%Y-%m-%d %H:%M')} UTC.\n"
    )
    md = hlavicka + "\n" + "\n\n".join(sekce) + "\n"
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"Zapsáno: {OUT}")
    print(f"Praha: {pocty['Praha']}, Střední Čechy: {pocty['Střední Čechy']}")


if __name__ == "__main__":
    main()
