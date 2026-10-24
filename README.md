# Ocenění nemovitostí

Vlastní „Sreality" nad vlastním oceňovacím modelem: tržní hodnota, podhodnocené
příležitosti, peer group po čtvrtích. **Běží samo na GitHubu** — Actions každý
den stáhnou nabídky ze Sreality, přepočítají ocenění a publikují appku na
GitHub Pages. Není potřeba nic spouštět.

## Zprovoznění (jednorázově, ~3 minuty)
1. Založ repozitář na github.com a pushni tam tuto složku:
   ```
   git init
   git add -A
   git commit -m "Ocenění nemovitostí"
   git branch -M main
   git remote add origin https://github.com/rubikandson/nemovitosti.git
   git push -u origin main
   ```
2. Na GitHubu: **Settings → Pages → Source: Deploy from a branch → main, složka /docs → Save**
3. **Actions → Denní aktualizace dat → Run workflow** (první naplnění dat)

Appka pak žije na `https://rubikandson.github.io/nemovitosti/`
a data se obnovují každý den ve 4:00 UTC automaticky.

## Co appka umí
Karty bytů s fotkou, celkovou cenou a odhadem tržní hodnoty. Nahoře rychlé
filtry (čtvrť, počet pokojů, cena, „jen výhodné“, řazení), zbytek pod
„Další filtry“. Klik na kartu = detail: cena vs. odhad, srovnání se čtvrtí,
nájem a hypotéka, postup výpočtu, oprava vstupů.

**Zálohy a doplatky:** u družstevních novostaveb Sreality uvádí jako cenu jen
převod podílu; doplatek (anuitu) appka dohledá v poznámce k ceně / popisu
a počítá s celkovou cenou. Když doplatek v textu je, ale bez částky, nabídka
je označená „cena neúplná“ a ve výchozím pohledu se nezobrazuje
(`src/sreality_detail.py`, `_doplatek`).

## Lokální běh (volitelné)
```
pip install -r requirements.txt
python -m src.main init
python -m src.main import-sreality "https://www.sreality.cz/hledani/prodej/byty/praha"
python -m src.main ocenit
python -m src.main app          # http://localhost:8000
```

## Oceňovací model — CORNERSTONE
Převzat 1:1 z `Oceneni_Byt_InSheetTables.xlsx` (list Praha): koeficienty
lokalita/stav/věk, balkon +1,01 %, parkování 400 tis. Kč, nájemní výnos, IRR
20 let, hypotéka (LTV 80 %, 4,2 %, 30 let). Konstanty s odkazy na buňky sheetu:
`src/valuation.py`. Cenová mapa 99 pražských čtvrtí: `data/price_map.csv`.
Sleva = o kolik % je nabídková cena pod tržní hodnotou dle modelu.
