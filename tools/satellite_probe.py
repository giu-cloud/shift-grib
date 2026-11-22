"""Prova delle fonti del vento dal satellite: quanto arrivano in ritardo i passaggi, senza credenziali.

- NASA PO.DAAC (ricerca CMR, pubblica): ASCAT costiero Metop-B e Metop-C, il prodotto OSI SAF del KNMI. Per scaricare
  serve un account Earthdata gratuito; per cercare no.
- EUMETSAT Data Store (ricerca pubblica): HY-2B, HY-2C e Oceansat-3 di OSI SAF, e l'ASCAT costiero se c'è.
Stampa, per gli ultimi passaggi, l'ora di fine misura e l'ora in cui il file è comparso.
"""
import datetime as dt, sys
import requests

now = dt.datetime.now(dt.timezone.utc)


def cmr(short):
    r = requests.get("https://cmr.earthdata.nasa.gov/search/granules.json",
                     params={"short_name": short, "sort_key": "-start_date", "page_size": 8,
                             "temporal": f"{(now - dt.timedelta(hours=30)):%Y-%m-%dT%H:%M:%SZ},"}, timeout=60)
    r.raise_for_status()
    print(f"\nPO.DAAC {short}:")
    for g in r.json()["feed"]["entry"]:
        end = dt.datetime.fromisoformat(g["time_end"].replace("Z", "+00:00"))
        upd = dt.datetime.fromisoformat(g["updated"].replace("Z", "+00:00"))
        links = [l["href"] for l in g.get("links", []) if l["href"].endswith((".nc", ".nc.gz"))][:1]
        print(f"  {g['title']}: fine {end:%d/%m %H:%M}, comparso {upd:%d/%m %H:%M} (+{(upd - end).total_seconds() / 60:.0f} min) {links}")


def eumetsat(pi):
    r = requests.get("https://api.eumetsat.int/data/search-products/1.0.0/os",
                     params={"format": "json", "pi": pi, "si": 0, "c": 6, "sort": "start,time,0"}, timeout=60)
    print(f"\nEUMETSAT {pi}: HTTP {r.status_code}")
    if not r.ok:
        print("  " + r.text[:300])
        return
    for f in r.json().get("features", [])[:6]:
        p = f.get("properties", {})
        d = p.get("date", "")
        upd = p.get("updated", "")
        print(f"  {f.get('id')}: misura {d}, pubblicato {upd}")


for s in ("ASCATB-L2-Coastal", "ASCATC-L2-Coastal"):
    try:
        cmr(s)
    except Exception as e:
        print(f"PO.DAAC {s}: {e}")
for pi in ("EO:EUM:DAT:0538", "EO:EUM:DAT:0539", "EO:EUM:DAT:0540", "EO:EUM:DAT:0541"):
    try:
        eumetsat(pi)
    except Exception as e:
        print(f"EUMETSAT {pi}: {e}")
r = requests.get("https://api.eumetsat.int/data/browse/1.0.0/collections", params={"format": "json", "q": "OSI SAF wind"}, timeout=60)
print(f"\nEUMETSAT collezioni 'OSI SAF wind': HTTP {r.status_code}")
if r.ok:
    try:
        for c in r.json().get("links", [])[:60]:
            t = c.get("title", "")
            if any(k in t.lower() for k in ("wind", "ascat", "hy-2", "hscat", "oceansat", "scat")):
                print(f"  {c.get('href', '').split('/')[-1]}  {t}")
    except Exception as e:
        print("  ", e, r.text[:300])
