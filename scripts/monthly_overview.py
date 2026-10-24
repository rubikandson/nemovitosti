#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Měsíční přehled — deterministická náhrada Cowork úlohy `oceneni-prehled-refresh`
pro svět bez Claude. Původní úloha obnovovala Cowork artefakt; artefakt je funkce
Claude/Coworku, tady místo něj generujeme statický `docs/monthly_overview.md`
(stejný OBSAH: sazba, cenová mapa, aktivní nabídky, příležitosti, watchlist,
top 8). Běží v GitHub Actions, bez AI.

Čte docs/data.json a data/sazba_hypoteky.csv. Žádná oceňovací logika (ta je jen
v src/valuation.py, CLAUDE.md hard rules) — jen agregace hotových polí.
"""
import csv, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "docs", "data.json")
SAZBA = os.path.join(ROOT, "data", "sazba_hypoteky.csv")
OUT = os.path.join(ROOT, "docs", "monthly_overview.md")
TOP_N = 8


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


def read_sazba():
    if not os.path.exists(SAZBA):
        return None
    with open(SAZBA, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows[-1] if rows else None


def main():
    if not os.path.exists(DATA):
        sys.exit(f"CHYBA: {DATA} neexistuje — nic negeneruji (žádná tichá náhrada).")
    with open(DATA, encoding="utf-8") as f:
        data = json.load(f)
    listings = data.get("listings")
    price_map = data.get("price_map")
    if not isinstance(listings, list) or not isinstance(price_map, list):
        sys.exit("CHYBA: data.json nemá očekávaná pole listings/price_map — končím.")

    generated = data.get("generated", "?")
    pocet_ctvrti = len(price_map)
    pm_updated = max((p.get("updated_at") or "" for p in price_map), default="?")

    total = len(listings)
    active = [
        r for r in listings
        if r.get("active") == 1
        and r.get("cena_czk") is not None
        and r.get("trzni_hodnota") is not None
    ]
    vis = [r for r in active if r.get("skryto") != 1]
    opp = sorted(
        (r for r in vis if r.get("sleva_pct") is not None),
        key=lambda r: r["sleva_pct"], reverse=True,
    )
    nad10 = sum(1 for r in vis if (r.get("sleva_pct") or -999) >= 10)
    watch = sum(1 for r in vis if r.get("watchlist") == 1)
    top = opp[:TOP_N]

    sazba = read_sazba()
    sazba_line = (
        f"**{num(sazba['sazba_pct'], ' %')}** (k {sazba.get('datum_aktualizace','?')}, "
        f"zdroj: {sazba.get('zdroj','?')})"
        if sazba else "_sazba_hypoteky.csv chybí — hodnota neuvedena_"
    )

    md = []
    md.append("# Ocenění nemovitostí — měsíční přehled\n")
    md.append(
        "_Generuje GitHub Action (`monthly_overview.yml`) automaticky 2. den v "
        "měsíci z `docs/data.json`. Nahrazuje původní Cowork artefakt "
        "`oceneni-prehled`. Běží nezávisle na Claude i GPT._\n"
    )
    md.append(f"Data vygenerována: **{generated}**\n")
    md.append("## Souhrn")
    md.append(f"- **Sazba hypotéky:** {sazba_line}")
    md.append(f"- **Cenová mapa:** {pocet_ctvrti} čtvrtí (aktualizováno {pm_updated})")
    md.append(f"- **Aktivní oceněné nabídky:** {len(active)} z {total} celkem")
    md.append(f"- **Příležitosti (sleva ≥ 10 %):** {nad10}")
    md.append(f"- **Watchlist:** {watch}\n")
    md.append("## Top 8 příležitostí")
    md.append("| # | Čtvrť/obec | Dispozice | Cena | Sleva | Odkaz |")
    md.append("|---|---|---|---|---|---|")
    for i, r in enumerate(top, 1):
        ctvrt = r.get("ctvrt") or r.get("lokalita") or r.get("nazev") or "?"
        md.append(
            f"| {i} | {ctvrt} | {r.get('dispozice') or '?'} | {czk(r.get('cena_czk'))} "
            f"| {num(r.get('sleva_pct'), ' %')} | {r.get('url','')} |"
        )
    if any((r.get("ctvrt") or "") == "Dolní Měcholupy" for r in top):
        md.append(
            "\n> Pozn.: Dolní Měcholupy jsou známý edge-case (domy prodávané jako "
            "byt, viz PREDAVACI.md bod 8.3) — model je počítá bytovým vzorcem."
        )
    md.append(
        "\n---\nŽivá appka: https://rubikandson.github.io/nemovitosti/ · "
        "Repo: https://github.com/rubikandson/nemovitosti · "
        "Actions: https://github.com/rubikandson/nemovitosti/actions\n"
    )
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    print(f"Zapsáno: {OUT}")
    print(f"Sazba: {sazba['sazba_pct'] if sazba else '?'} %, příležitosti >=10%: {nad10}, watchlist: {watch}")


if __name__ == "__main__":
    main()
