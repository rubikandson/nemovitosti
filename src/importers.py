import hashlib
from pathlib import Path
import pandas as pd
from . import db

POVINNE = {"ctvrt", "plocha_m2", "cena_czk"}
CISELNE = {"plocha_m2", "cena_czk", "zakladni_cena_m2", "rok_vystavby",
           "dalsi_koef_pct", "najem_m2_mesic", "najem_priplatky_rocni"}

def _external_id(row):
    if row.get("url"): return str(row["url"])
    klic = f"{row.get('nazev')}|{row.get('ctvrt')}|{row.get('plocha_m2')}"
    return hashlib.md5(klic.encode()).hexdigest()

def import_file(path):
    p = Path(path)
    df = pd.read_excel(p) if p.suffix.lower() in (".xlsx", ".xls") else pd.read_csv(p, encoding="utf-8-sig")
    df.columns = [str(c).strip().lower() for c in df.columns]
    chybi = POVINNE - set(df.columns)
    if chybi: raise SystemExit(f"Chybí povinné sloupce: {', '.join(sorted(chybi))}")
    con = db.connect()
    n = 0
    for _, r in df.iterrows():
        row = {k: (None if pd.isna(v) else v) for k, v in r.to_dict().items()}
        for k in CISELNE:
            if row.get(k) is not None: row[k] = float(row[k])
        if row.get("rok_vystavby"): row["rok_vystavby"] = int(row["rok_vystavby"])
        db.upsert_listing(con, {**row, "source": "soubor", "external_id": _external_id(row),
                                "nazev": str(row.get("nazev") or row.get("url") or "").strip(),
                                "ctvrt": str(row["ctvrt"]).strip()})
        n += 1
    con.commit()
    con.close()
    print(f"Importováno {n} nabídek ze souboru {p.name}.")
    return n
