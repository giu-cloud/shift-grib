"""Vento dal satellite sul Mediterraneo per l'app Shift e per il banco di prova.

Fonte: ASCAT costiero a 12,5 km su Metop-B e Metop-C (EUMETSAT OSI SAF, prodotto KNMI), dalla KNMI Data Platform
(Open Data API). Chiave: il segreto KNMI_API_KEY se c'è (chiave personale, gratuita), altrimenti la chiave anonima
pubblicata da KNMI per i client pubblici (limiti condivisi, scade ogni anno).

Per ogni passaggio che tocca il riquadro scrive scat_<satellite>_<AAAAMMGGHHMM>.scw.gz, un gzip di:
  "SCW1" (4 byte), numero di record (int32 little endian), poi per ogni record:
  minuti dal 2020-01-01 00:00 UTC (int32), lat*500 e lon*500 (int16), u e v a 10 m *100 in m/s (int16, verso cui
  soffia il vento: u verso est, v verso nord), satellite (byte: 1 Metop-B, 2 Metop-C, 3 HY-2B, 4 HY-2C, 5 Oceansat-3),
  flag (byte: bit 0 direzione incerta, vento sotto 3 m/s; bit 1 cella che tocca la costa).
Si tengono solo le celle buone: niente controllo di qualità fallito, niente ghiaccio, niente pioggia segnalata.

Licenza dei dati: CC BY 4.0, EUMETSAT OSI SAF ("copyright (anno) EUMETSAT").

Uso:
  python tools/satellite.py --out sat --index indice.json [--hours 30] [--probe]
  python tools/satellite.py --merge-index indice.json sat/nuovi.json indice_nuovo.json <giorni>
indice.json è quello della release (se c'è): i file sorgente già visti non si riscaricano, e si guardano solo i file
creati dopo l'ultimo giro (con un'ora di margine, al massimo [hours] ore). --probe stampa gli attributi del primo file.
"""
import argparse, datetime as dt, gzip, json, os, re, struct, sys, time

import numpy as np
import requests

API = "https://api.dataplatform.knmi.nl/open-data/v1"
# chiave anonima pubblica della KNMI Data Platform (developer.dataplatform.knmi.nl/open-data-api), valida fino al 1/8/2027
ANON_KEY = "eyJvcmciOiI1ZTU1NGUxOTI3NGE5NjAwMDEyYTNlYjEiLCJpZCI6IjUzYTg1ZDBhMmQ5YzRkYzJiYWNlNzQ4NTQ2Zjk4ODExIiwiaCI6Im11cm11cjEyOCJ9"
# nome del satellite nel file -> (dataset KDP, versione, codice nel formato SCW1)
SOURCES = {
    "metopb": ("osisaf_ascat_b_coa", "nrt", 1),
    "metopc": ("osisaf_ascat_c_coa", "nrt", 2),
}
BOX = tuple(float(x) for x in os.environ.get("SAT_BOX", "30,46.5,-6,37").split(","))  # latmin, latmax, lonmin, lonmax
EPOCH_SCW = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
EPOCH_NC = dt.datetime(1990, 1, 1, tzinfo=dt.timezone.utc)
FNAME = re.compile(r"ascat_(\d{8})_(\d{6})_(metop[abc])_")
# flag di qualità (nomi di flag_meanings nel file): si scarta la cella
REJECT = ("knmi_quality_control_fails", "variational_quality_control_fails", "some_portion_of_wvc_is_over_ice",
          "not_enough_good_sigma0_for_wind_retrieval", "wind_inversion_not_successful", "rain_detected",
          "product_monitoring_event_flag")
SMALL = "small_wind_less_than_or_equal_to_3_m_s"
LAND = "some_portion_of_wvc_is_over_land"
MIN_CELLS = 30


def key():
    return os.environ.get("KNMI_API_KEY") or ANON_KEY


def kdp(path, **params):
    for attempt in range(5):
        r = requests.get(API + path, headers={"Authorization": key()}, params=params, timeout=60)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(20 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def recent_files(dataset, version, hours):
    """I file creati nelle ultime [hours] ore, dal più nuovo."""
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    out, token = [], None
    for _ in range(10):
        p = {"orderBy": "created", "sorting": "desc", "maxKeys": 50}
        if token:
            p["nextPageToken"] = token
        j = kdp(f"/datasets/{dataset}/versions/{version}/files", **p)
        files = j.get("files", [])
        for f in files:
            c = dt.datetime.fromisoformat(f["created"].replace("Z", "+00:00"))
            if c < since:
                return out
            out.append(f)
        token = j.get("nextPageToken")
        if not token or not files:
            break
    return out


def download(dataset, version, filename):
    url = kdp(f"/datasets/{dataset}/versions/{version}/files/{filename}/url")["temporaryDownloadUrl"]
    for attempt in range(4):
        r = requests.get(url, timeout=180)
        if r.ok:
            return r.content
        time.sleep(10 * (attempt + 1))
    r.raise_for_status()


def flag_bits(var):
    masks = np.atleast_1d(var.getncattr("flag_masks")).astype(np.int64)
    names = var.getncattr("flag_meanings").split()
    return dict(zip(names, masks))


def read_pass(data, sat_code, probe=False):
    """Le celle buone del riquadro: (tempo in minuti SCW, lat, lon, u, v, flag), più l'ora di inizio e di fine (UTC)."""
    import netCDF4
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    ds = netCDF4.Dataset("passaggio", memory=data)
    try:
        lat = np.ma.filled(ds["lat"][:].astype(np.float64), np.nan)
        lon = np.ma.filled(ds["lon"][:].astype(np.float64), np.nan)
        lon = np.where(lon > 180, lon - 360, lon)
        spd = np.ma.filled(ds["wind_speed"][:].astype(np.float64), np.nan)
        wdir = np.ma.filled(ds["wind_dir"][:].astype(np.float64), np.nan)
        tsec = np.ma.filled(ds["time"][:].astype(np.float64), np.nan)
        fl = np.ma.filled(ds["wvc_quality_flag"][:], 0).astype(np.int64)
        bits = flag_bits(ds["wvc_quality_flag"])
        if probe:
            print("  variabili:", ", ".join(ds.variables))
            for v in ("wind_dir", "wind_speed", "time", "wvc_quality_flag"):
                print(f"  {v}:", {a: ds[v].getncattr(a) for a in ds[v].ncattrs() if a not in ("flag_masks",)})
            print("  attributi globali:", {a: ds.getncattr(a) for a in ds.ncattrs()})
        # NetCDF KNMI: convenzione oceanografica, cioè verso cui soffia ("0 deg. flowing North", manuale ASCAT,
        # appendice B). Si guarda solo l'attributo della variabile: i globali nominano EUMETSAT ("Meteorological Satellites").
        conv = " ".join(str(ds["wind_dir"].getncattr(x)) for x in ds["wind_dir"].ncattrs()).lower()
        toward = not ("meteorological convention" in conv or "coming from" in conv or "from which" in conv)
    finally:
        ds.close()
    bad = np.zeros(fl.shape, dtype=bool)
    for n in REJECT:
        if n in bits:
            bad |= (fl & bits[n]) != 0
    inside = (lat >= BOX[0]) & (lat <= BOX[1]) & (lon >= BOX[2]) & (lon <= BOX[3])
    ok = inside & ~bad & np.isfinite(spd) & np.isfinite(wdir) & np.isfinite(tsec) & np.isfinite(lat) & np.isfinite(lon)
    if ok.sum() < MIN_CELLS:
        return None
    th = np.radians(wdir[ok] if toward else wdir[ok] + 180.0)
    u = spd[ok] * np.sin(th)
    v = spd[ok] * np.cos(th)
    t = tsec[ok]
    flags = np.zeros(ok.sum(), dtype=np.uint8)
    if SMALL in bits:
        flags |= ((fl[ok] & bits[SMALL]) != 0).astype(np.uint8)
    flags |= (spd[ok] <= 3.0).astype(np.uint8)
    if LAND in bits:
        flags |= (((fl[ok] & bits[LAND]) != 0).astype(np.uint8) << 1)
    t0 = EPOCH_NC + dt.timedelta(seconds=float(t.min()))
    t1 = EPOCH_NC + dt.timedelta(seconds=float(t.max()))
    shift = (EPOCH_SCW - EPOCH_NC).total_seconds()
    minutes = np.round((t - shift) / 60.0).astype(np.int32)
    rec = dict(minutes=minutes, lat=np.round(lat[ok] * 500).astype(np.int16), lon=np.round(lon[ok] * 500).astype(np.int16),
               u=np.round(u * 100).astype(np.int16), v=np.round(v * 100).astype(np.int16), sat=sat_code, flags=flags)
    return rec, t0, t1, toward


def write_scw(path, rec):
    n = len(rec["minutes"])
    dt_rec = np.dtype([("m", "<i4"), ("la", "<i2"), ("lo", "<i2"), ("u", "<i2"), ("v", "<i2"), ("s", "u1"), ("f", "u1")])
    arr = np.zeros(n, dtype=dt_rec)
    arr["m"], arr["la"], arr["lo"], arr["u"], arr["v"] = rec["minutes"], rec["lat"], rec["lon"], rec["u"], rec["v"]
    arr["s"], arr["f"] = rec["sat"], rec["flags"]
    with gzip.open(path, "wb", compresslevel=9) as f:
        f.write(b"SCW1" + struct.pack("<i", n) + arr.tobytes())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="sat")
    ap.add_argument("--index", default="")
    ap.add_argument("--hours", type=float, default=30.0)
    ap.add_argument("--probe", action="store_true")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    idx = json.load(open(a.index)) if a.index and os.path.exists(a.index) else {}
    have = {p["file"] for p in idx.get("passes", [])}
    seen_before = set(idx.get("seen", []))
    now = dt.datetime.now(dt.timezone.utc)
    hours = a.hours
    if idx.get("updated"):
        last = dt.datetime.strptime(idx["updated"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=dt.timezone.utc)
        hours = min(a.hours, (now - last).total_seconds() / 3600 + 1.0)
    new, seen, summary = [], [], []
    for sat, (dataset, version, code) in SOURCES.items():
        try:
            files = recent_files(dataset, version, hours)
        except Exception as e:
            print(f"{sat}: elenco non riuscito ({e})", file=sys.stderr)
            summary.append(f"| {sat} | elenco non riuscito: {e} | | |")
            continue
        print(f"{sat}: {len(files)} file creati nelle ultime {hours:.1f} ore ({dataset}/{version})")
        probed = False
        for f in files:
            m = FNAME.search(f["filename"])
            if not m or f["filename"] in seen_before:
                continue
            created = dt.datetime.fromisoformat(f["created"].replace("Z", "+00:00"))
            try:
                data = download(dataset, version, f["filename"])
                got = read_pass(data, code, probe=a.probe and not probed)
                probed = True
            except Exception as e:
                print(f"  {f['filename']}: non letto ({e})", file=sys.stderr)
                continue
            seen.append(f["filename"])
            if got is None:
                continue
            rec, t0, t1, toward = got
            name = f"scat_{sat}_{t0:%Y%m%d%H%M}.scw.gz"
            late = (created - t1).total_seconds() / 60
            summary.append(f"| {name} | {t0:%d/%m %H:%M}-{t1:%H:%M} | {len(rec['minutes'])} | {late:.0f} min |")
            if name in have:
                continue
            path = os.path.join(a.out, name)
            write_scw(path, rec)
            new.append(dict(file=name, satellite=sat, start=t0.strftime("%Y-%m-%dT%H:%MZ"), end=t1.strftime("%Y-%m-%dT%H:%MZ"),
                            box=[round(float(rec["lat"].min()) / 500, 2), round(float(rec["lat"].max()) / 500, 2),
                                 round(float(rec["lon"].min()) / 500, 2), round(float(rec["lon"].max()) / 500, 2)],
                            cells=int(len(rec["minutes"])), source=f["filename"], published=now.strftime("%Y-%m-%dT%H:%MZ"),
                            delay_min=int(late), toward=toward))
            print(f"  {name}: {len(rec['minutes'])} celle, {os.path.getsize(path) / 1024:.0f} KB, pronto {late:.0f} min dopo la fine del passaggio")
    with open(os.path.join(a.out, "nuovi.json"), "w") as f:
        json.dump({"passes": new, "seen": seen}, f, indent=1)
    s = os.environ.get("GITHUB_STEP_SUMMARY")
    if s:
        with open(s, "a") as f:
            f.write("### Vento dal satellite\n\n| passaggio | ora (UTC) | celle | pronto dopo |\n|---|---|---|---|\n" + "\n".join(summary) + "\n")
    print(f"{len(new)} passaggi nuovi")


def merge_index(old_path, new_path, out_path, keep_days):
    """indice.json della release: i passaggi vecchi ancora tenuti più quelli nuovi, dal più recente."""
    old = json.load(open(old_path)) if old_path and os.path.exists(old_path) else {"passes": [], "seen": []}
    new = json.load(open(new_path)) if os.path.exists(new_path) else {"passes": [], "seen": []}
    now = dt.datetime.now(dt.timezone.utc)
    cut = (now - dt.timedelta(days=keep_days)).strftime("%Y-%m-%dT%H:%MZ")
    by = {p["file"]: p for p in old.get("passes", []) if p["end"] >= cut}
    for p in new["passes"]:
        by[p["file"]] = p
    passes = sorted(by.values(), key=lambda p: p["start"], reverse=True)
    # file sorgente già visti (anche quelli senza passaggio sul riquadro), finché sono nella finestra
    def recent(name):
        m = FNAME.search(name)
        return bool(m) and dt.datetime.strptime(m[1] + m[2], "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc) >= now - dt.timedelta(days=2)
    seen = sorted({n for n in old.get("seen", []) + new["seen"] if recent(n)})
    json.dump({"format": "SCW1", "updated": now.strftime("%Y-%m-%dT%H:%MZ"), "box": list(BOX),
               "credit": "EUMETSAT OSI SAF, CC BY 4.0", "passes": passes, "seen": seen}, open(out_path, "w"), indent=1)
    for p in old.get("passes", []):
        if p["end"] < cut:
            print(p["file"])  # da togliere dalla release


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--merge-index":
        merge_index(sys.argv[2], sys.argv[3], sys.argv[4], float(sys.argv[5]))
    else:
        main()
