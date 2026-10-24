"""Měsíční aktualizace data/sazba_hypoteky.csv z ČBA Hypomonitoru.

Zdroj: https://www.cbamonitor.cz/statistika/prumerna-urokova-sazba-novych-hypotek
(oficiální statistika České bankovní asociace, aktualizuje se cca 1x měsíčně).
Stránka je vykreslená na serveru (na rozdíl od Sreality cenové mapy), takže
stačí obyčejný requests.get() bez JS enginu.

Hodnota "Aktuální hodnota pro nové hypotéky" je v HTML PŘED svým popiskem
(velké číslo -> % -> teprve pak text popisku), proto regex hledá číslo,
které popisku předchází, ne za ním následuje.

ŽÁDNÉ TICHÉ NÁHRADY: pokud se hodnotu nepodaří najít nebo je mimo rozumný
rozsah (1-15 %), skript skončí chybou a NIC nezapíše - nikdy si nevymýšlí
náhradní číslo. src/valuation.py pak sám použije záložní SAZBA_ZALOZNI.
"""
import csv
import re
import sys
from datetime import date
from pathlib import Path

import requests

URL = "https://www.cbamonitor.cz/statistika/prumerna-urokova-sazba-novych-hypotek"
ROOT = Path(__file__).resolve().parent.parent
OUT_CSV = ROOT / "data" / "sazba_hypoteky.csv"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
}

# Číslo (velké, tučné) přijde v HTML PŘED popiskem "Aktuální hodnota pro nové
# hypotéky" - proto capture group je před anchorem, ne za ním.
VZOR = re.compile(
    r'>\s*([\d]+,[\d]+)\s*</span>\s*<span[^>]*>\s*%\s*</span>\s*</div>\s*'
    r'<div class="text--xl text-base-100">\s*Aktuální hodnota pro nové hypotéky',
)

MIN_ROZUMNA_SAZBA = 1.0
MAX_ROZUMNA_SAZBA = 15.0


def stahni_sazbu():
    r = requests.get(URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    html = r.text

    shoda = VZOR.search(html)
    if not shoda:
        raise RuntimeError(
            "Na cbamonitor.cz se nepodařilo najít hodnotu 'Aktuální hodnota "
            "pro nové hypotéky' - struktura stránky se pravděpodobně změnila. "
            "ŽÁDNÁ TICHÁ NÁHRADA - je potřeba skript ručně opravit."
        )

    sazba_text = shoda.group(1).replace(",", ".")
    sazba = float(sazba_text)

    if not (MIN_ROZUMNA_SAZBA <= sazba <= MAX_ROZUMNA_SAZBA):
        raise RuntimeError(
            f"Stažená sazba {sazba} % je mimo rozumný rozsah "
            f"({MIN_ROZUMNA_SAZBA}-{MAX_ROZUMNA_SAZBA} %) - "
            "ŽÁDNÁ TICHÁ NÁHRADA, zastavuji se."
        )

    return sazba


def uloz(sazba):
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["sazba_pct", "datum_aktualizace", "zdroj"])
        w.writerow([sazba, date.today().isoformat(), URL])


def main():
    sazba = stahni_sazbu()
    uloz(sazba)
    print(f"Sazba hypoték aktualizována: {sazba} % -> {OUT_CSV}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"CHYBA: {e}", file=sys.stderr)
        sys.exit(1)
