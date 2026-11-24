"""Atlante del vento sotto costa (24/11/2026): vento dal radar dei Sentinel-1 per le aree di Lens.

Studio nel Project di Shift: routing/vento_sotto_costa.md, strada A. Questo programma scarica soltanto: dal Copernicus
Marine Service i campi di vento SAR a 0,01 gradi (circa 1 km) sul Mediterraneo, con il vento del modello (ECMWF) alla
stessa ora e nello stesso punto, ritagliati su un'area; li salva compatti (.npz) per l'analisi, che si fa fuori.

  python tools/costa.py --descrivi --out costa           elenco dei dataset SAR e delle loro variabili
  python tools/costa.py --area eolie --da 2024-04-01 --a 2026-11-24 --out costa

Credenziali: CMEMS_USER e CMEMS_PASS (le stesse delle correnti). Dati: E.U. Copernicus Marine Service Information.
"""
import argparse
import datetime as dt
import json
import os
import sys
import traceback

# Le aree di Lens nell'app (core/Lens.kt): sud, nord, ovest, est.
AREE = {
    "egadi": (37.70, 38.15, 11.95, 12.60),
    "eolie": (38.00, 38.86, 14.25, 15.30),
    "palermo": (37.945, 38.287, 13.108, 13.612),
    "messina": (37.90, 38.35, 15.40, 15.75),
    "malta": (35.68, 36.20, 14.05, 14.75),
    "napoli": (40.50, 40.90, 13.80, 14.62),
    "cagliari": (38.85, 39.25, 8.85, 9.65),
    "maddalena-bonifacio": (41.00, 41.50, 8.95, 9.75),
    "toscano": (42.20, 43.35, 9.75, 11.30),
    "genova": (44.00, 44.47, 8.40, 9.50),
}

# I prodotti SAR del Mediterraneo (manuale CMEMS-WIND-PUM-012-100); i dataset si cercano dentro.
PRODOTTI = ["WIND_MED_PHY_HR_L3_NRT_012_104", "WIND_MED_PHY_HR_L3_MY_012_104"]
# Se la ricerca non trova niente, questi (gli unici scritti nel manuale).
NOTI = ["cmems_obs-wind_med_phy_nrt_l3-s1a-sar-asc-0.01deg_P1D-i", "cmems_obs-wind_med_phy_nrt_l3-s1a-sar-desc-0.01deg_P1D-i"]

LOG = []


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    LOG.append(s)


def credenziali():
    return dict(username=os.environ.get("CMEMS_USER"), password=os.environ.get("CMEMS_PASS"))


def cerca(obj, chiave, out):
    """Tutti i valori di [chiave] in un dizionario annidato."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == chiave and isinstance(v, str):
                out.append(v)
            cerca(v, chiave, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            cerca(v, chiave, out)
    return out


def come_dizionario(x):
    for m in ("model_dump", "dict"):
        if hasattr(x, m):
            try:
                return getattr(x, m)()
            except Exception:
                pass
    return x


def descrivi(out):
    import copernicusmarine as cm
    trovati, varianti = [], {}
    for pid in PRODOTTI:
        cat = None
        for kw in (dict(product_id=pid, disable_progress_bar=True), dict(product_id=pid), dict(contains=[pid])):
            try:
                cat = cm.describe(**kw)
                break
            except Exception as e:
                log(f"describe {pid} {list(kw)}: {e}")
        if cat is None:
            continue
        d = come_dizionario(cat)
        ids = sorted(set(cerca(d, "dataset_id", [])))
        log(f"{pid}: {len(ids)} dataset")
        for i in ids:
            log("   ", i)
        trovati += ids
        with open(os.path.join(out, f"descrizione_{pid}.json"), "w") as f:
            json.dump(d, f, default=str, indent=1)
        nomi = sorted(set(cerca(d, "short_name", [])))
        varianti[pid] = nomi
        log(f"   variabili: {', '.join(nomi)}")
    sar = sorted({i for i in trovati if "sar" in i.lower()})
    if not sar:
        log("nessun dataset SAR dalla ricerca: uso quelli del manuale")
        sar = NOTI
    log("dataset SAR:", ", ".join(sar))
    return sar


def scarica(did, area, da, a, out):
    import copernicusmarine as cm
    s, n, w, e = AREE[area]
    fn = f"raw_{area}_{did}.nc"
    base = dict(dataset_id=did, minimum_longitude=w, maximum_longitude=e, minimum_latitude=s, maximum_latitude=n,
                start_datetime=f"{da}T00:00:00", end_datetime=f"{a}T23:59:59", output_directory=out, output_filename=fn,
                **credenziali())
    tentativi = [dict(disable_progress_bar=True), dict(disable_progress_bar=True, coordinates_selection_method="inside"),
                 dict(disable_progress_bar=True, coordinates_selection_method="nearest"), dict()]
    for extra in tentativi:
        try:
            cm.subset(**base, **extra)
            p = os.path.join(out, fn)
            if os.path.exists(p):
                log(f"{area} {did}: scaricato {os.path.getsize(p) / 1e6:.1f} MB")
                return p
        except Exception as ex:
            log(f"{area} {did} {list(extra)}: {str(ex)[:400]}")
    return None


def compatta(path, area, did, out):
    """Le scene con almeno 20 celle misurate; tutte le variabili (tempo, lat, lon), in float32 compresso."""
    import numpy as np
    import xarray as xr
    ds = xr.open_dataset(path)
    log(f"{area} {did}: dimensioni {dict(ds.sizes)}; variabili {list(ds.data_vars)}")
    for name, v in ds.data_vars.items():
        attrs = {k: str(v.attrs[k]) for k in ("long_name", "standard_name", "units") if k in v.attrs}
        log(f"   {name} {v.dims} {v.dtype} {attrs}")
    lat = ds["latitude"].values.astype("float32")
    lon = ds["longitude"].values.astype("float32")
    tim = ds["time"].values.astype("datetime64[s]").astype("int64")
    arr = {}
    for name, v in ds.data_vars.items():
        if "time" not in v.dims or "latitude" not in v.dims or "longitude" not in v.dims:
            continue
        x = v.transpose("time", "latitude", "longitude", ...).values
        while x.ndim > 3:
            x = x[..., 0]
        if np.issubdtype(x.dtype, np.datetime64):
            nat = np.isnat(x)
            y = x.astype("datetime64[s]").astype("int64").astype("float64") / 3600.0
            y[nat] = np.nan
            x = y
        arr[name] = x.astype("float32")
    if not arr:
        log(f"{area} {did}: nessuna variabile (tempo, lat, lon)")
        return None
    ref = next((arr[k] for k in ("wind_speed", "eastward_wind") if k in arr), next(iter(arr.values())))
    ok = np.isfinite(ref).sum(axis=(1, 2)) >= 20
    log(f"{area} {did}: {int(ok.sum())} scene su {len(ok)} giorni")
    tag = did.replace("cmems_obs-wind_med_phy_", "").replace("-0.01deg_P1D-i", "")
    dst = os.path.join(out, f"costa_{area}_{tag}.npz")
    np.savez_compressed(dst, lat=lat, lon=lon, time=tim[ok], **{k: v[ok] for k, v in arr.items()},
                        meta=np.array(json.dumps({"area": area, "box": AREE[area], "dataset": did,
                                                  "variabili": {k: {a: str(ds[k].attrs.get(a, "")) for a in ("long_name", "standard_name", "units")} for k in arr}})))
    log(f"{area} {did}: {dst} {os.path.getsize(dst) / 1e6:.1f} MB")
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--descrivi", action="store_true")
    ap.add_argument("--area", default="")
    ap.add_argument("--da", default="2024-04-01")
    ap.add_argument("--a", default=str(dt.date.today()))
    ap.add_argument("--out", default="costa")
    o = ap.parse_args()
    os.makedirs(o.out, exist_ok=True)
    fatti = []
    try:
        sar = descrivi(o.out)
        if o.area:
            for area in [x.strip() for x in o.area.split(",") if x.strip()]:
                if area not in AREE:
                    log(f"area sconosciuta: {area}")
                    continue
                for did in sar:
                    p = scarica(did, area, o.da, o.a, o.out)
                    if p:
                        try:
                            q = compatta(p, area, did, o.out)
                            if q:
                                fatti.append(q)
                        except Exception:
                            log(traceback.format_exc())
                        os.remove(p)
    except Exception:
        log(traceback.format_exc())
    finally:
        nome = f"log_{o.area.replace(',', '-') or 'descrizione'}.txt"
        with open(os.path.join(o.out, nome), "w") as f:
            f.write("\n".join(LOG) + "\n")
    log(f"fatti: {len(fatti)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
