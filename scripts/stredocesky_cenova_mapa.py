# Cenová mapa pro velká města Středočeského kraje (nad 5000 obyvatel).
# Schváleno uživatelem 2026-08 jako první krok rozšíření appky mimo Prahu —
# uživatel výslovně chtěl JEN velká města (>5000 obyvatel), ne celý kraj:
# Středočeský kraj má přes 1000 obcí a naprostá většina má jen pár transakcí
# ročně (nespolehlivá cena/m²) a zabydlené s Prahou v jedné appce, kde model
# počítá jen s byty (ne rodinné domy — ty zůstávají mimo, schváleno zvlášť).
#
# Sreality cenová mapa je pro Prahu plochá (jedna URL → ~99 čtvrtí), ale pro
# kraje třístupňová: kraj → okres → obec (ověřeno 2026-08 přes Claude in
# Chrome). Tenhle skript proto dělá 1 request na kraj (získá 12 okresů) +
# 1 request na každý okres (získá obce s cenou/m² a počtem transakcí přímo,
# BEZ nutnosti dalšího requestu na úrovni obce).
#
# Filtrování na velká města: data/mesta_stredocechy.csv (42 měst nad 5000
# obyvatel, zdroj: Wikipedie "Seznam měst ve Středočeském kraji", stav
# ~2023 — mění se pomalu, přesnost dostatečná pro tenhle práh). Menší obce
# (typicky 1-5 transakcí/rok, extrémně nespolehlivá cena/m²) se ignorují
# úplně — schváleno uživatelem, ne odhad z naší strany.
#
# Sloupec kraj v price_map.csv: tenhle skript smí přepisovat JEN řádky
# s kraj="Středočeský" — Praha a případné další kraje zůstávají beze změny
# (viz scripts/sreality_cenova_mapa.py, stejný princip).
import csv
import json
import re
import sys

import requests

KRAJ_URL = "https://www.sreality.cz/cenova-mapa/hledani/byty/stredocesky-kraj-11"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}
CSV_PATH = "data/price_map.csv"
MESTA_CSV = "data/mesta_stredocechy.csv"
FIELDNAMES = ["klic", "ctvrt", "cena_za_m2_czk", "pocet_transakci", "najem_m2_mesic", "kraj"]
KRAJ_NAZEV = "Středočeský"


def _norm(s):
    return s.strip().lower().replace(" ", "_").replace("-", "_")


def _next_data(html: str) -> dict:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError("__NEXT_DATA__ nenalezen — Sreality patrně změnila strukturu stránky")
    return json.loads(m.group(1))


def nacti_velka_mesta() -> dict:
    """{normalizovaný název: okres} měst nad 5000 obyvatel — jen tahle se
    do cenové mapy zapíší, zbytek kraje se ignoruje (schváleno uživatelem).

    Okres se používá k rozlišení STEJNOJMENNÝCH obcí v jiném okrese
    (oprava bugu 2026-08-04, viz PREDAVACI.md): dřív se matchovalo jen
    podle jména, takže "Roztoky" (Praha-západ, velké město na seznamu)
    a malá vesnice "Roztoky" v okrese Rakovník sdílely stejný klíč a
    přepisovaly se navzájem podle toho, který okres se zpracoval
    poslední — bez ohledu na to, která je ta SPRÁVNÁ. Stejný problém
    má "Jesenice" (Praha-západ vs. malá obec v Rakovníku)."""
    with open(MESTA_CSV, encoding="utf-8-sig") as f:
        return {_norm(r["nazev"]): r["okres"].strip() for r in csv.DictReader(f)}


def nacti_stavajici_najmy():
    najmy = {}
    try:
        with open(CSV_PATH, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if row.get("najem_m2_mesic"):
                    najmy[row["klic"]] = row["najem_m2_mesic"]
    except FileNotFoundError:
        pass
    return najmy


def nacti_ostatni_kraje():
    """Řádky jiných krajů (ne Středočeský) ze stávajícího CSV — zachováme
    beze změny, tenhle skript se stará jen o Středočeský kraj."""
    ostatni = []
    try:
        with open(CSV_PATH, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if (row.get("kraj") or "Praha").strip() != KRAJ_NAZEV:
                    ostatni.append(row)
    except FileNotFoundError:
        pass
    return ostatni


def stahni_okresy() -> list:
    r = requests.get(KRAJ_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = _next_data(r.text)
    al = data["props"]["pageProps"].get("aggregatedLocalities") or []
    okresy = [a for a in al if a["locality"]["entityType"] == "district"]
    if len(okresy) < 10:
        raise RuntimeError(f"Neočekávaně málo okresů ({len(okresy)}) — zastavuji, nenahrazuji tichým odhadem")
    return okresy


def stahni_obce_okresu(okres: dict) -> list:
    seo = okres["locality"]["seoName"]
    entity_id = okres["locality"]["entityId"]
    url = f"{KRAJ_URL}/{seo}-{entity_id}"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = _next_data(r.text)
    al = data["props"]["pageProps"].get("aggregatedLocalities") or []
    return [a for a in al if a["locality"]["entityType"] == "municipality"]


def aktualizuj():
    velka_mesta = nacti_velka_mesta()
    najmy = nacti_stavajici_najmy()
    ostatni = nacti_ostatni_kraje()
    okresy = stahni_okresy()

    nalezeno = {}  # klic -> radek
    nespravny_okres = []
    for okres in okresy:
        okres_nazev = okres["locality"]["name"]
        obce = stahni_obce_okresu(okres)
        for a in obce:
            nazev = a["locality"]["name"]
            klic = _norm(nazev)
            if klic not in velka_mesta:
                continue
            # Stejnojmenná obec v JINÉM okrese, než je ta na seznamu velkých
            # měst (viz nacti_velka_mesta výše) — přeskočit, i když se jmenuje
            # stejně, NENÍ to město ze seznamu.
            if velka_mesta[klic] != okres_nazev:
                nespravny_okres.append(f"{nazev} (okres {okres_nazev}, na seznamu je okres {velka_mesta[klic]})")
                continue
            nalezeno[klic] = {
                "klic": klic,
                "ctvrt": nazev,
                "cena_za_m2_czk": int(a["avgPricePerSqm"]),
                "pocet_transakci": int(a["numTransactions"]),
                "najem_m2_mesic": najmy.get(klic, ""),
                "kraj": KRAJ_NAZEV,
            }
    if nespravny_okres:
        print(f"Přeskočeno {len(nespravny_okres)} stejnojmenných obcí v jiném okrese "
              f"(nejsou na seznamu velkých měst): {nespravny_okres}")

    chybi = set(velka_mesta) - set(nalezeno)
    if chybi:
        print(f"POZOR: {len(chybi)} velkých měst nemá cenu v Sreality cenové mapě "
              f"(pravděpodobně 0 transakcí za sledované období): {sorted(chybi)}")
    if len(nalezeno) < 20:
        raise RuntimeError(f"Neočekávaně málo měst nalezeno ({len(nalezeno)} z {len(velka_mesta)}) "
                            f"— zastavuji, nenahrazuji tichým odhadem")

    radky = sorted(nalezeno.values(), key=lambda r: -r["cena_za_m2_czk"])
    radky.extend(ostatni)
    with open(CSV_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        w.writerows(radky)
    print(f"Aktualizováno {len(nalezeno)} měst ({KRAJ_NAZEV}) v {CSV_PATH} ze Sreality, "
          f"zachováno {len(ostatni)} řádků jiných krajů.")


if __name__ == "__main__":
    try:
        aktualizuj()
    except Exception as e:
        print(f"CHYBA při aktualizaci cenové mapy Středočeského kraje: {e}", file=sys.stderr)
        sys.exit(1)
