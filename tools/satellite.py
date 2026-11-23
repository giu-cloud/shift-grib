"""Vento dal satellite sul Mediterraneo per l'app Shift e per il banco di prova.

Fonte: ASCAT costiero a 12,5 km su Metop-B e Metop-C (EUMETSAT OSI SAF, prodotto KNMI), da due posti:
- NASA PO.DAAC: in rete circa 1,5-2,5 ore dopo la fine dell'orbita. Serve un account Earthdata gratuito (segreti
  EARTHDATA_USER e EARTHDATA_PASS, scritti nel ~/.netrc dal lavoro); senza, questa fonte si salta;
- KNMI Data Platform (Open Data API): lo stesso prodotto, ma caricato una volta al giorno (verso le 04 UTC): serve per
  l'archivio e per riempire i buchi. Chiave: il segreto KNMI_API_KEY se c'è (chiave personale, gratuita), altrimenti
  la chiave anonima pubblicata dal KNMI per i client pubblici (limiti condivisi, scade ogni anno).
Lo stesso passaggio da due fonti dà lo stesso nome e si scrive una volta sola.

Per ogni passaggio che tocca il riquadro scrive scat_<satellite>_<AAAAMMGGHHMM>.scw.gz, un gzip di:
  "SCW1" (4 byte), numero di record (int32 little endian), poi per ogni record:
  minuti dal 2020-01-01 00:00 UTC (int32), lat*500 e lon*500 (int16), u e v a 10 m *100 in m/s (int16, verso cui
  soffia il vento: u verso est, v verso nord), satellite (byte: 1 Metop-B, 2 Metop-C, 3 HY-2B, 4 HY-2C, 5 Oceansat-3),
  flag (byte: bit 0 direzione incerta, vento sotto 3 m/s; bit 1 cella che tocca la costa).
Si tengono solo le celle buone: niente controllo di qualità fallito, niente ghiaccio, niente pioggia segnalata.

Licenza dei dati: CC BY 4.0, EUMETSAT OSI SAF ("copyright (anno) EUMETSAT").

Aree chieste dall'app (23/11/2026): oltre al Mediterraneo (SAT_BOX, area "med"), le aree dell'elenco aree_satellite.json
(release satellite), aggiunte e tolte dal workflow satellite_area.yml. Ogni orbita si scarica una volta e si ritaglia
per tutte le aree. I passaggi del Mediterraneo si chiamano come prima (scat_<satellite>_<ora>), quelli delle altre aree
scat_<id>_<satellite>_<ora>; nell'indice ogni passaggio ha "area", e accanto al "box" di prima ci sono "areas" e
"refused". Un'area appena aggiunta ha "backfill": il giro dopo prende anche le orbite delle 72 ore prima, solo per lei.
Limiti: al più 5 aree chieste attive, 40° di latitudine per 60 di longitudine, fino a 90 giorni; e dentro ±65° di
latitudine e longitudine, perché il formato SCW1 tiene lat*500 e lon*500 in 16 bit.

Uso:
  python tools/satellite.py --out sat --index indice.json [--areas aree_satellite.json] [--hours 30] [--probe]
  python tools/satellite.py --merge-index indice.json sat/nuovi.json indice_nuovo.json <giorni> [aree_satellite.json]
  python tools/satellite.py --area-request aree_satellite.json <add|remove> <id> <titolo> <lat0,lat1,lon0,lon1> <AAAA-MM-GG>
  python tools/satellite.py --area-finish aree_satellite.json sat/fatte.txt
  python tools/satellite.py --backfill AAAA-MM-GG AAAA-MM-GG --out sat [--have nomi.txt]
--backfill recupera dall'archivio della KNMI Data Platform (che tiene gli anni passati) le orbite con inizio nei giorni
indicati, inclusi; nomi.txt elenca i passaggi già in archivio, da non riscrivere. Serve al banco di prova.
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
    "metopb": ("osisaf_ascat_b_coa", "nrt", 1, "ASCATB-L2-Coastal"),
    "metopc": ("osisaf_ascat_c_coa", "nrt", 2, "ASCATC-L2-Coastal"),
}
CMR = "https://cmr.earthdata.nasa.gov/search/granules.json"
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
MED = {"id": "med", "title": "Mediterranean", "box": list(BOX)}
MAX_AREAS, MAX_DLAT, MAX_DLON, MAX_DAYS, LIMIT = 5, 40.0, 60.0, 90, 65.0
ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,22}[a-z0-9])?$")


def today():
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def load_areas(path):
    j = json.load(open(path)) if path and os.path.exists(path) else {}
    return {"areas": j.get("areas", []), "refused": j.get("refused", [])}


def active_areas(path):
    """Il Mediterraneo e le aree chieste ancora valide (fino al giorno 'until' compreso)."""
    t = today()
    return [dict(MED)] + [a for a in load_areas(path)["areas"] if a.get("until", "") >= t]


def area_request(path, action, aid, title, box, until):
    """Aggiorna l'elenco con una richiesta dell'app. Scrive 'fetch=1' (o 0) per il workflow; un rifiuto va nell'elenco."""
    j = load_areas(path)
    t = today()
    j["refused"] = [r for r in j["refused"] if r.get("when", "") >= (dt.date.fromisoformat(t) - dt.timedelta(days=3)).isoformat()]
    j["areas"] = [a for a in j["areas"] if a.get("until", "") >= t]
    def refuse(why):
        j["refused"] = [r for r in j["refused"] if r.get("id") != aid] + [{"id": aid, "why": why, "when": t}]
        print(f"rifiutata: {why}", file=sys.stderr)
    aid = (aid or "").strip()
    if not ID_RE.match(aid) or aid in ("med", "metopb", "metopc"):
        refuse("bad id: lowercase letters, digits and hyphens, at most 24")
    elif action == "remove":
        j["areas"] = [a for a in j["areas"] if a["id"] != aid]
        j["refused"] = [r for r in j["refused"] if r.get("id") != aid]
    elif action != "add":
        refuse(f"unknown action {action!r}")
    else:
        try:
            b = [float(x) for x in box.split(",")]
            assert len(b) == 4
        except Exception:
            b = None
        try:
            u = dt.date.fromisoformat((until or "").strip())
        except Exception:
            u = None
        t0 = dt.date.fromisoformat(t)
        if b is None:
            refuse("bad box: lat0,lat1,lon0,lon1")
        elif not (b[0] < b[1] and b[2] < b[3]):
            refuse("bad box: lat0 < lat1 and lon0 < lon1 (no areas across 180 degrees)")
        elif b[1] - b[0] > MAX_DLAT or b[3] - b[2] > MAX_DLON:
            refuse(f"too big: at most {MAX_DLAT:.0f} degrees of latitude and {MAX_DLON:.0f} of longitude")
        elif max(abs(x) for x in b) > LIMIT:
            refuse(f"outside {LIMIT:.0f} degrees north, south, east or west: the satellite file format does not reach there")
        elif u is None or u < t0:
            refuse("bad date: until must be today or later (YYYY-MM-DD)")
        elif (u - t0).days > MAX_DAYS:
            refuse(f"too long: at most {MAX_DAYS} days from today")
        elif aid not in {a["id"] for a in j["areas"]} and len(j["areas"]) >= MAX_AREAS:
            refuse(f"already {MAX_AREAS} areas: drop one first")
        else:
            old = next((a for a in j["areas"] if a["id"] == aid), None)
            new = {"id": aid, "title": (title or aid).strip()[:60], "box": [round(x, 2) for x in b], "until": u.isoformat(),
                   "added": (old or {}).get("added", t)}
            # i passaggi dei 3 giorni prima: per un'area nuova o spostata
            new["backfill"] = old is None or old["box"] != new["box"] or bool(old.get("backfill"))
            j["areas"] = [a for a in j["areas"] if a["id"] != aid] + [new]
            j["refused"] = [r for r in j["refused"] if r.get("id") != aid]
    json.dump(j, open(path, "w"), indent=1)
    print("fetch=1")


def area_finish(path, done_path):
    """Dopo un giro: toglie le aree scadute e il segno 'backfill' a quelle servite. Si rilegge l'elenco appena prima."""
    j = load_areas(path)
    done = set(open(done_path).read().split()) if done_path and os.path.exists(done_path) else set()
    t = today()
    before = json.dumps(j, sort_keys=True)
    j["areas"] = [a for a in j["areas"] if a.get("until", "") >= t]
    for a in j["areas"]:
        if a["id"] in done:
            a.pop("backfill", None)
    j["refused"] = [r for r in j["refused"] if r.get("when", "") >= (dt.date.fromisoformat(t) - dt.timedelta(days=3)).isoformat()]
    json.dump(j, open(path, "w"), indent=1)
    print("changed=" + ("1" if json.dumps(j, sort_keys=True) != before else "0"))


def pass_name(aid, sat, t0):
    return f"scat_{sat}_{t0:%Y%m%d%H%M}.scw.gz" if aid == "med" else f"scat_{aid}_{sat}_{t0:%Y%m%d%H%M}.scw.gz"


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


def podaac_files(short, hours):
    """Le orbite PO.DAAC finite nelle ultime [hours] ore: (nome, link, ora in cui sono comparse)."""
    now = dt.datetime.now(dt.timezone.utc)
    r = requests.get(CMR, params={"short_name": short, "sort_key": "-start_date", "page_size": 100,
                                  "temporal": f"{now - dt.timedelta(hours=hours):%Y-%m-%dT%H:%M:%SZ},"}, timeout=60)
    r.raise_for_status()
    out = []
    for g in r.json()["feed"]["entry"]:
        links = [l["href"] for l in g.get("links", []) if l["href"].startswith("https") and l["href"].endswith((".nc", ".nc.gz"))]
        if links:
            out.append((links[0].rsplit("/", 1)[-1], links[0], dt.datetime.fromisoformat(g["updated"].replace("Z", "+00:00"))))
    return out


_EDL = None


def podaac_download(url):
    """Scarica da PO.DAAC con l'account Earthdata del ~/.netrc (requests lo usa da sé sul rimando a urs.earthdata)."""
    global _EDL
    if _EDL is None:
        _EDL = requests.Session()
    for attempt in range(4):
        r = _EDL.get(url, timeout=300)
        if r.ok and r.content[:4] in (b"CDF\x01", b"CDF\x02", b"\x89HDF") or (r.ok and r.content[:2] == b"\x1f\x8b"):
            return r.content
        if r.status_code in (401, 403) or (r.ok and b"<html" in r.content[:500].lower()):
            raise RuntimeError(f"Earthdata: accesso non riuscito (HTTP {r.status_code}), controllare utente e password")
        time.sleep(15 * (attempt + 1))
    raise RuntimeError(f"PO.DAAC: HTTP {r.status_code}")


def earthdata():
    netrc = os.path.expanduser("~/.netrc")
    return os.path.exists(netrc) and "urs.earthdata.nasa.gov" in open(netrc).read()


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


def read_pass(data, sat_code, probe=False, areas=None):
    """Le celle buone di ogni area: {id: ((tempo in minuti SCW, lat, lon, u, v, flag), inizio, fine, verso)} (UTC).
    Senza [areas], il solo Mediterraneo (SAT_BOX)."""
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
    good = ~bad & np.isfinite(spd) & np.isfinite(wdir) & np.isfinite(tsec) & np.isfinite(lat) & np.isfinite(lon)
    out = {}
    for area in (areas or [MED]):
        b = area["box"]
        inside = (lat >= b[0]) & (lat <= b[1]) & (lon >= b[2]) & (lon <= b[3]) & (np.abs(lat) <= LIMIT) & (np.abs(lon) <= LIMIT)
        got = cells(inside & good, lat, lon, spd, wdir, tsec, fl, bits, sat_code, toward)
        if got:
            out[area["id"]] = got
    return out


def cells(ok, lat, lon, spd, wdir, tsec, fl, bits, sat_code, toward):
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
    ap.add_argument("--areas", default="", help="aree_satellite.json: le aree chieste dall'app, oltre al Mediterraneo")
    a = ap.parse_args()
    areas = active_areas(a.areas)
    backfill_ids = {x["id"] for x in areas if x.get("backfill")}
    print("aree: " + ", ".join(x["id"] + (" (anche le 72 ore prima)" if x["id"] in backfill_ids else "") for x in areas))
    os.makedirs(a.out, exist_ok=True)
    idx = json.load(open(a.index)) if a.index and os.path.exists(a.index) else {}
    have = {p["file"] for p in idx.get("passes", [])}
    seen_before = set(idx.get("seen", []))
    now = dt.datetime.now(dt.timezone.utc)
    hours = a.hours
    if idx.get("updated"):
        last = dt.datetime.strptime(idx["updated"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=dt.timezone.utc)
        hours = min(a.hours, max(6.0, (now - last).total_seconds() / 3600 + 1.0))
    window = 72.0 if backfill_ids else 30.0
    if backfill_ids:
        hours = 72.0
    new, seen, summary = [], [], []
    edl = earthdata()
    if not edl:
        print("PO.DAAC saltato: nessun account Earthdata nel ~/.netrc (segreti EARTHDATA_USER e EARTHDATA_PASS)")
    for sat, (dataset, version, code, short) in SOURCES.items():
        cand = []  # (nome sorgente, fonte, come scaricarlo, ora di comparsa)
        if edl:
            try:
                pf = podaac_files(short, window)
                print(f"{sat}: {len(pf)} orbite PO.DAAC finite nelle ultime {window:.0f} ore ({short})")
                cand += [(n, "podaac", u, c) for n, u, c in pf]
            except Exception as e:
                print(f"{sat}: ricerca PO.DAAC non riuscita ({e})", file=sys.stderr)
        try:
            kf = recent_files(dataset, version, hours)
            print(f"{sat}: {len(kf)} file KNMI creati nelle ultime {hours:.1f} ore ({dataset}/{version})")
            cand += [(f["filename"], "knmi", f["filename"], dt.datetime.fromisoformat(f["created"].replace("Z", "+00:00"))) for f in kf]
        except Exception as e:
            print(f"{sat}: elenco KNMI non riuscito ({e})", file=sys.stderr)
            summary.append(f"| {sat} | elenco KNMI non riuscito: {e} | | |")
        probed = False
        done_here = set()
        for fname, src, how, created in cand:
            m = FNAME.search(fname)
            base = re.sub(r"\.gz$", "", fname)
            if not m or base in done_here:
                continue
            # orbite nuove (non viste, delle ultime 30 ore): per tutte le aree; le altre solo per le aree appena aggiunte
            start = dt.datetime.strptime(m[1] + m[2], "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc)
            fresh = base not in seen_before and (now - start).total_seconds() <= 30 * 3600
            targets = areas if fresh else [x for x in areas if x["id"] in backfill_ids]
            if not targets:
                continue
            try:
                data = podaac_download(how) if src == "podaac" else download(dataset, version, how)
                got = read_pass(data, code, probe=a.probe and not probed, areas=targets)
                probed = True
            except Exception as e:
                print(f"  {fname} ({src}): non letto ({e})", file=sys.stderr)
                continue
            done_here.add(base)
            if fresh:
                seen.append(base)
            for aid, (rec, t0, t1, toward) in got.items():
                name = pass_name(aid, sat, t0)
                late = (created - t1).total_seconds() / 60
                summary.append(f"| {name} | {t0:%d/%m %H:%M}-{t1:%H:%M} | {len(rec['minutes'])} | {late:.0f} min ({src}) |")
                if name in have:
                    continue
                path = os.path.join(a.out, name)
                write_scw(path, rec)
                new.append(dict(file=name, area=aid, satellite=sat, start=t0.strftime("%Y-%m-%dT%H:%MZ"), end=t1.strftime("%Y-%m-%dT%H:%MZ"),
                                box=[round(float(rec["lat"].min()) / 500, 2), round(float(rec["lat"].max()) / 500, 2),
                                     round(float(rec["lon"].min()) / 500, 2), round(float(rec["lon"].max()) / 500, 2)],
                                cells=int(len(rec["minutes"])), source=fname, origin=src, published=now.strftime("%Y-%m-%dT%H:%MZ"),
                                delay_min=int(late), toward=toward))
                print(f"  {name}: {len(rec['minutes'])} celle, {os.path.getsize(path) / 1024:.0f} KB, in rete {late:.0f} min dopo la fine del passaggio ({src})")
    with open(os.path.join(a.out, "nuovi.json"), "w") as f:
        json.dump({"passes": new, "seen": seen}, f, indent=1)
    with open(os.path.join(a.out, "fatte.txt"), "w") as f:
        f.write("\n".join(sorted(backfill_ids)) + "\n")
    s = os.environ.get("GITHUB_STEP_SUMMARY")
    if s:
        with open(s, "a") as f:
            f.write("### Vento dal satellite\n\n| passaggio | ora (UTC) | celle | pronto dopo |\n|---|---|---|---|\n" + "\n".join(summary) + "\n")
    print(f"{len(new)} passaggi nuovi")


def podaac_range(short, d0, d1):
    """Le orbite PO.DAAC che cominciano tra i giorni d0 e d1 inclusi: (nome, link)."""
    out, page = [], 1
    while page < 20:
        r = requests.get(CMR, params={"short_name": short, "sort_key": "start_date", "page_size": 200, "page_num": page,
                                      "temporal": f"{d0:%Y-%m-%d}T00:00:00Z,{d1:%Y-%m-%d}T23:59:59Z"}, timeout=60)
        r.raise_for_status()
        es = r.json()["feed"]["entry"]
        for g in es:
            links = [l["href"] for l in g.get("links", []) if l["href"].startswith("https") and l["href"].endswith((".nc", ".nc.gz"))]
            if links:
                out.append((links[0].rsplit("/", 1)[-1], links[0]))
        if len(es) < 200:
            break
        page += 1
    return out


def backfill(day_from, day_to, out, have_path):
    os.makedirs(out, exist_ok=True)
    have = set(open(have_path).read().split()) if have_path and os.path.exists(have_path) else set()
    d0, d1 = dt.date.fromisoformat(day_from), dt.date.fromisoformat(day_to)
    new = []
    if earthdata():
        # PO.DAAC: più svelto del KNMI (niente limiti condivisi della chiave anonima)
        for sat, (dataset, version, code, short) in SOURCES.items():
            files = podaac_range(short, d0, d1)
            print(f"{sat}: {len(files)} orbite PO.DAAC dal {d0} al {d1}")
            for fname, url in files:
                if not FNAME.search(fname):
                    continue
                try:
                    got = read_pass(podaac_download(url), code).get("med")
                except Exception as e:
                    print(f"  {fname}: non letto ({e})", file=sys.stderr)
                    continue
                if got is None:
                    continue
                rec, t0, t1, toward = got
                name = f"scat_{sat}_{t0:%Y%m%d%H%M}.scw.gz"
                if name in have or name in new:
                    continue
                write_scw(os.path.join(out, name), rec)
                new.append(name)
                print(f"  {name}: {len(rec['minutes'])} celle")
        print(f"{len(new)} passaggi recuperati")
        return
    for sat, (dataset, version, code, short) in SOURCES.items():
        names, token = [], None
        for _ in range(40):
            p = {"orderBy": "filename", "sorting": "asc", "maxKeys": 100, "begin": f"ascat_{d0:%Y%m%d}"}
            if token:
                p["nextPageToken"] = token
            j = kdp(f"/datasets/{dataset}/versions/{version}/files", **p)
            files = j.get("files", [])
            stop = False
            for f in files:
                m = FNAME.search(f["filename"])
                if not m:
                    continue
                d = dt.datetime.strptime(m[1], "%Y%m%d").date()
                if d > d1:
                    stop = True
                    break
                if d >= d0:
                    names.append(f["filename"])
            token = j.get("nextPageToken")
            if stop or not token or not files:
                break
        print(f"{sat}: {len(names)} orbite dal {d0} al {d1}")
        for fname in names:
            try:
                got = read_pass(download(dataset, version, fname), code).get("med")
            except Exception as e:
                print(f"  {fname}: non letto ({e})", file=sys.stderr)
                continue
            if got is None:
                continue
            rec, t0, t1, toward = got
            name = f"scat_{sat}_{t0:%Y%m%d%H%M}.scw.gz"
            if name in have:
                continue
            write_scw(os.path.join(out, name), rec)
            new.append(name)
            print(f"  {name}: {len(rec['minutes'])} celle")
    print(f"{len(new)} passaggi recuperati")


def merge_index(old_path, new_path, out_path, keep_days, areas_path=""):
    """indice.json della release: i passaggi vecchi ancora tenuti più quelli nuovi, dal più recente, delle aree attive."""
    old = json.load(open(old_path)) if old_path and os.path.exists(old_path) else {"passes": [], "seen": []}
    new = json.load(open(new_path)) if os.path.exists(new_path) else {"passes": [], "seen": []}
    now = dt.datetime.now(dt.timezone.utc)
    cut = (now - dt.timedelta(days=keep_days)).strftime("%Y-%m-%dT%H:%MZ")
    areas = active_areas(areas_path)
    ids = {x["id"] for x in areas}
    keep = lambda p: p["end"] >= cut and p.get("area", "med") in ids
    by = {p["file"]: p for p in old.get("passes", []) if keep(p)}
    for p in new["passes"]:
        by[p["file"]] = p
    passes = sorted(by.values(), key=lambda p: p["start"], reverse=True)
    # file sorgente già visti (anche quelli senza passaggio sul riquadro), finché sono nella finestra
    def recent(name):
        m = FNAME.search(name)
        return bool(m) and dt.datetime.strptime(m[1] + m[2], "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc) >= now - dt.timedelta(days=2)
    seen = sorted({re.sub(r"\.gz$", "", n) for n in old.get("seen", []) + new["seen"] if recent(n)})
    for p in passes:
        p.setdefault("area", "med")
    lim = (now.date() - dt.timedelta(days=3)).isoformat()
    refused = [r for r in load_areas(areas_path)["refused"] if r.get("when", "") >= lim]
    json.dump({"format": "SCW1", "updated": now.strftime("%Y-%m-%dT%H:%MZ"), "box": list(BOX),
               "areas": [{k: x[k] for k in ("id", "title", "box", "until") if k in x} for x in areas], "refused": refused,
               "credit": "EUMETSAT OSI SAF, CC BY 4.0", "passes": passes, "seen": seen}, open(out_path, "w"), indent=1)
    for p in old.get("passes", []):
        if not keep(p):
            print(p["file"])  # da togliere dalla release


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--merge-index":
        merge_index(sys.argv[2], sys.argv[3], sys.argv[4], float(sys.argv[5]), sys.argv[6] if len(sys.argv) > 6 else "")
    elif len(sys.argv) > 1 and sys.argv[1] == "--area-request":
        area_request(*sys.argv[2:8])
    elif len(sys.argv) > 1 and sys.argv[1] == "--area-finish":
        area_finish(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
    elif len(sys.argv) > 1 and sys.argv[1] == "--backfill":
        b = argparse.ArgumentParser()
        b.add_argument("--backfill", nargs=2)
        b.add_argument("--out", default="sat")
        b.add_argument("--have", default="")
        x = b.parse_args()
        backfill(x.backfill[0], x.backfill[1], x.out, x.have)
    else:
        main()
