# Projekt: Ocenění nemovitostí

Interaktivní aplikace nad databází nemovitostí: výpočet tržní hodnoty dle
uživatelova modelu, hledání podhodnocených příležitostí, peer group srovnání.
Vše běží lokálně (SQLite + Python + Flask), bez závislosti na Google Sheets.

## Startovní protokol
1. Přečti tento soubor.
2. Zkontroluj, že existuje `data/nemovitosti.db` — pokud ne, spusť `python -m src.main init`.
3. Ptej se uživatele česky, po jedné věci, a vysvětluj co děláš.

## Struktura
```
CLAUDE.md            – tento soubor
README.md            – návod k použití
requirements.txt     – závislosti (pandas, openpyxl, requests, flask)
data/
  nemovitosti.db     – SQLite databáze (negeneruje se do gitu)
  price_map.csv      – cenová mapa: ~100 pražských čtvrtí, aktualizuje se
                       automaticky 1× měsíčně ze Sreality (viz níže)
  sazba_hypoteky.csv – průměrná sazba hypoték, automaticky 1× měsíčně
                       z ČBA Hypomonitoru (viz níže)
  imports/           – sem se ukládají ruční CSV/XLSX k importu
examples/
  vzorove_nabidky.csv – ukázkový import
src/
  db.py              – schéma a připojení k databázi
  importers.py       – import z CSV/XLSX
  sreality.py        – import ze Sreality (URL vyhledávání → API)
  valuation.py       – oceňovací logika (CORNERSTONE — viz níže)
  report.py          – seznam příležitostí, export CSV/XLSX
  app.py             – webová aplikace (Flask API)
  static/index.html  – UI aplikace (filtry, tabulka, detail, peer group)
  main.py            – CLI vstupní bod
scripts/
  mfcr_najemne.py           – stažení nájemního benchmarku z MFČR
  sreality_cenova_mapa.py   – měsíční aktualizace data/price_map.csv ze Sreality
  cba_hypomonitor.py        – měsíční aktualizace data/sazba_hypoteky.csv z ČBA
.github/workflows/
  update.yml           – denní import + ocenění + build (4:00 UTC)
  cenova_mapa.yml       – měsíční refresh cenové mapy + sazby hypotéky
                          (1. den v měsíci, 5:00 UTC)
```

## Hard rules
- ŽÁDNÉ TICHÉ NÁHRADY: když něco nejde udělat přesně tak, jak uživatel zadal
  (zdroj dat nedostupný, API nefunguje…), ZASTAV SE a zeptej se. Nikdy nenahrazovat
  zadání vlastním odhadem/odvozením bez výslovného souhlasu. Žádný black box.
- Oceňovací logika je POUZE v `src/valuation.py` a `data/price_map.csv` — nikde jinde.
- Nikdy nemazat záznamy z tabulky `listings`; nabídky se jen deaktivují (`active=0`).
- Nepřepisovat `Report_pro_Codex_Google_Sheets_App.docx` (původní zadání, jen ke čtení).
- Všechny výstupy a komunikace česky.

## Klíčové příkazy
```
python -m src.main init                          # založí databázi + nahraje cenovou mapu
python -m src.main import-file <cesta.csv|xlsx>  # ruční import nabídek
python -m src.main import-sreality "<URL>"       # import ze Sreality vyhledávání
python -m src.main ocenit                        # spočítá tržní hodnotu všech aktivních nabídek
python -m src.main prilezitosti --min-sleva 10   # vypíše podhodnocené nabídky
python -m src.main prilezitosti --export out.xlsx
python -m src.main cenova-mapa                   # znovu nahraje data/price_map.csv do DB
python -m src.main app                           # spustí webovou aplikaci (localhost:8000)
python scripts/sreality_cenova_mapa.py           # ruční refresh price_map.csv ze Sreality
python scripts/cba_hypomonitor.py                # ruční refresh sazba_hypoteky.csv z ČBA
```

## Oceňovací model — CORNERSTONE
Logika je převzata 1:1 z uživatelova Google Sheetu (`Oceneni_Byt_InSheetTables.xlsx`,
list Praha). NIKDY ji neměnit bez výslovného souhlasu uživatele. Ověřeno proti
přepočtu v LibreOffice — výsledky sedí na korunu.

Tržní hodnota = plocha × základní_cena/m2 × Π(1+koef) + příplatek parkování, kde:
koef lokalita (+5/0/−5 %), koef stav (0/+3/+6 %), koef věk (interpolace dekádových
pásem +10 % → −12 %, věk = min(2025−rok, 80)), balkon +1,01 %, další koef ručně.
Základní cena/m2: ručně ze Sreality cenové mapy, jinak z `data/price_map.csv`
(~100 pražských čtvrtí, zdroj: sreality.cz/cenova-mapa — Atlas cen prodaných
bytů, 12měsíční klouzavý průměr; automaticky aktualizováno 1× měsíčně,
schváleno uživatelem 2026-07-10) × faktor velikosti bytu (40/57/75 m²).
Sleva = −(cena/tržní − 1). Příležitost = sleva ≥ práh (výchozí 10 %).
Navíc: nájemní výnos (obsazenost 10/12, růst 5 %, amortizace 0,3), IRR 20 let,
hypotéka (LTV 80 %, sazba dle `data/sazba_hypoteky.csv` — automaticky
aktualizováno 1× měsíčně z ČBA Hypomonitoru, záložní hodnota 4,2 % když
soubor chybí/je neplatný, schváleno uživatelem 2026-07-14; 30 let, pokrytí
splátky nájmem).
Všechny konstanty: `src/valuation.py` (s odkazy na buňky původního sheetu).
