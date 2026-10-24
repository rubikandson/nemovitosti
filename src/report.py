from pathlib import Path
import pandas as pd
from . import db

DOTAZ = """
SELECT l.id, l.nazev, l.ctvrt, l.dispozice, l.plocha_m2, l.cena_czk,
       v.trzni_hodnota, v.sleva_pct, v.prosty_vynos_pct, v.celkovy_vynos_pct,
       v.pokryti_splatky_pct, l.stav, l.url, l.source
FROM listings l JOIN valuations v ON v.listing_id = l.id
WHERE l.active = 1 AND v.sleva_pct >= ? ORDER BY v.sleva_pct DESC
"""

def prilezitosti(min_sleva=10.0, export=None):
    con = db.connect()
    df = pd.read_sql_query(DOTAZ, con, params=(min_sleva,))
    con.close()
    if df.empty:
        print(f"Žádná nabídka se slevou >= {min_sleva} % nenalezena.")
        return df
    print(f"\nPodhodnocené nabídky (cena >= {min_sleva} % pod tržní hodnotou):\n")
    for _, r in df.iterrows():
        vynos = f"  výnos {r['prosty_vynos_pct']:.1f} %" if pd.notna(r["prosty_vynos_pct"]) else ""
        print(f"  {r['sleva_pct']:5.1f} %  {str(r['nazev'])[:50]:<50} {r['cena_czk']:>12,.0f} Kč (tržní {r['trzni_hodnota']:>12,.0f} Kč){vynos}")
    if export:
        p = Path(export)
        if p.suffix.lower() == ".xlsx": df.to_excel(p, index=False)
        else: df.to_csv(p, index=False, encoding="utf-8-sig")
        print(f"\nExportováno do {p}")
    return df
