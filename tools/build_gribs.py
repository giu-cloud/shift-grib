#!/usr/bin/env python3
"""
Piccolo "server" di GRIB su GitHub.

Scarica le ultime corse di alcuni modelli liberi, ritaglia l'area che interessa, riscrive i campi con la
compressione semplice (che l'app legge senza problemi) e produce un file per modello e per corsa.
Il lavoro programmato di GitHub (.github/workflows/gribs.yml) lo lancia ogni sei ore e pubblica i file.

Modelli:
  ecmwf    IFS dell'ECMWF, dati aperti a 0,25 gradi (licenza CC-BY-4.0), vento a 10 m e pressione, ogni 3 ore fino a 144.
  ecmwf-0p1  la stessa previsione IFS distribuita a 0,1 gradi (circa 9 km, dal 20 ottobre 2026): vento a 10 m, raffiche
           e pressione, ogni 3 ore fino a 144. Esce circa due ore dopo la 0,25.
  icon-eu  ICON-EU del servizio meteorologico tedesco (DWD, dati aperti), 0,0625 gradi (circa 7 km), vento a 10 m e
           raffiche, ogni ora fino a 48 ore e poi ogni 3 fino a 120.

  arome    AROME di Météo-France (dati aperti, Licence Ouverte 2.0), 0,025 gradi (circa 2,5 km), solo vento a 10 m,
           ogni ora fino a 51 ore. Dominio 37,5-55,4 N e 12 O-16 E. I file si trovano chiedendo l'elenco del jeu de
           données "Paquets Arome - Résolution 0,025°" a data.gouv.fr (pacchetto SP1, nove gruppi di ore per corsa).
  arpege   ARPEGE di Météo-France (dati aperti, Licence Ouverte 2.0), griglia europea 0,1 gradi (circa 11 km), solo vento
           a 10 m, ogni ora fino a 48 ore e poi ogni 3 fino a 102. Copre tutto il Mediterraneo. Stesso metodo di AROME
           (jeu de données "Paquets Arpège - Résolution 0,1°", pacchetto SP1).
  ecmwf-wave  modello d'onda dell'ECMWF (dati aperti, CC-BY-4.0), 0,25 gradi: altezza significativa, direzione
           e periodo medi, ogni 3 ore fino a 144.
  gfs-wave GFS-Wave (WAVEWATCH III) della NOAA, 0,16 gradi: altezza significativa, direzione e periodo dell'onda
           principale, ogni ora fino a 72 ore e poi ogni 3 fino a 144.
  cmems    Copernicus Marine, prodotti del Mediterraneo a 1/24 di grado (circa 4 km): correnti di superficie
           (con le maree) ogni ora e onde ogni 3 ore, 5 giorni. Serve un account gratuito Copernicus Marine:
           Secrets CMEMS_USER e CMEMS_PASS. Una volta al giorno, dopo le 12 UTC.
  icon-2i  ICON-2I di ItaliaMeteo-Arpae (dati aperti MeteoHub, senza account), circa 2,2 km, vento a 10 m e raffiche,
           ogni ora fino a 72, corse 00 e 12. Area propria (ICON2I_BOX, di default 35-45 N e 7-16,5 E: Liguria-Malta), circa
           60 MB a corsa.
  gfs      GFS della NOAA (dati aperti), 0,25 gradi, vento a 10 m e pressione, ogni ora fino a 48 ore e poi ogni 3
           fino a 144. Si prende dall'archivio pubblico su Amazon, scaricando solo i campi che servono.

Archivio: ogni modello guarda le ultime corse disponibili (non solo l'ultima) e costruisce quelle che mancano.
L'elenco dei file già in archivio arriva nel file indicato dalla variabile GRIB_HAVE (un nome per riga).

  youmeteo HRRR Italia di youmeteo.com (circa 4 km), per abbonati: richiede le credenziali nei Secrets e un deposito privato.

Uso:  python tools/build_gribs.py --out gribs [--models ecmwf,ecmwf-0p1,icon-eu,icon-2i,gfs,arome,arpege,ecmwf-wave,gfs-wave,cmems,youmeteo] [--box latmin,latmax,lonmin,lonmax]
      python tools/build_gribs.py --selftest un_file.grb      (prova il solo ritaglio, senza rete)
      python tools/build_gribs.py --tiles-check sintetico [file interi...]   (taglia a quadratini, ricompone e confronta)
      python tools/build_gribs.py --tiles --out gribs --tiles-out tiles     (pubblica i quadratini, nel lavoro su GitHub)

Quadratini: i modelli fitti si pubblicano anche tagliati a quadratini fissi (1 o 2 gradi) nella release tiles,
così l'app scarica solo il mare che serve; i file interi restano come sono. Regole sopra tile_side().
"""
import argparse, bz2, datetime as dt, io, json, math, os, re, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import eccodes as ec

UA = {"User-Agent": "shift-gribs/1 (personal, non-commercial)"}


def http_get(url, headers=None, tries=3, timeout=120):
    import requests
    last = None
    for k in range(tries):
        try:
            r = requests.get(url, headers={**UA, **(headers or {})}, timeout=timeout)
            if r.status_code in (200, 206):
                return r.content
            last = f"HTTP {r.status_code}"
            if r.status_code == 404:
                break
        except Exception as e:  # rete instabile: si riprova
            last = str(e)
        time.sleep(2 + 3 * k)
    raise IOError(f"{url}: {last}")


def crop_message(h, box):
    """Ritaglia un campo su griglia regolare lat/lon. Restituisce un nuovo messaggio GRIB (bytes) o None."""
    if ec.codes_get(h, "gridType") != "regular_ll":
        return None
    ni, nj = ec.codes_get(h, "Ni"), ec.codes_get(h, "Nj")
    la1 = ec.codes_get(h, "latitudeOfFirstGridPointInDegrees"); la2 = ec.codes_get(h, "latitudeOfLastGridPointInDegrees")
    lo1 = ec.codes_get(h, "longitudeOfFirstGridPointInDegrees")
    di = ec.codes_get(h, "iDirectionIncrementInDegrees"); dj = ec.codes_get(h, "jDirectionIncrementInDegrees")
    if ec.codes_get(h, "iScansNegatively") or ec.codes_get(h, "jPointsAreConsecutive"):
        return None
    lats = la1 + np.arange(nj) * (dj if la2 > la1 else -dj)
    lons = ((lo1 + np.arange(ni) * di + 180.0) % 360.0) - 180.0          # da -180 a 180
    latmin, latmax, lonmin, lonmax = box
    jj = np.where((lats >= latmin - 1e-6) & (lats <= latmax + 1e-6))[0]
    order = np.argsort(lons, kind="stable")
    ii = order[(lons[order] >= lonmin - 1e-6) & (lons[order] <= lonmax + 1e-6)]
    if len(jj) < 2 or len(ii) < 2:
        return None
    vals = ec.codes_get_values(h).reshape(nj, ni)
    missing = ec.codes_get(h, "missingValue")
    sub = vals[np.ix_(jj, ii)]
    c = ec.codes_clone(h)
    try:
        ec.codes_set(c, "packingType", "grid_simple")
        ec.codes_set(c, "Ni", len(ii)); ec.codes_set(c, "Nj", len(jj))
        ec.codes_set(c, "latitudeOfFirstGridPointInDegrees", float(lats[jj[0]]))
        ec.codes_set(c, "latitudeOfLastGridPointInDegrees", float(lats[jj[-1]]))
        ec.codes_set(c, "longitudeOfFirstGridPointInDegrees", float(lons[ii[0]] % 360.0))
        ec.codes_set(c, "longitudeOfLastGridPointInDegrees", float(lons[ii[-1]] % 360.0))
        if np.any(sub == missing):
            ec.codes_set(c, "bitmapPresent", 1)
        ec.codes_set(c, "bitsPerValue", 12)
        ec.codes_set_values(c, sub.flatten())
        return ec.codes_get_message(c)
    finally:
        ec.codes_release(c)


def crop_bytes(data, box, keep=None):
    """Tutti i messaggi GRIB contenuti in 'data', ritagliati. Con 'keep' si tengono solo quei nomi brevi (es. 10u, 10v)."""
    out = []
    pos = 0
    while True:
        i = data.find(b"GRIB", pos)
        if i < 0:
            break
        h = ec.codes_new_from_message(data[i:])
        try:
            n = ec.codes_get(h, "totalLength")
            if keep is not None and ec.codes_get(h, "shortName") not in keep:
                pos = i + max(int(n), 4)
                continue
            m = crop_message(h, box)
            if m:
                out.append(m)
        finally:
            ec.codes_release(h)
        pos = i + max(int(n), 4)
    return out


def latest_run(probe, hours, back=6):
    """La corsa più recente per cui 'probe(data, ora)' riesce."""
    now = dt.datetime.utcnow()
    t = now.replace(minute=0, second=0, microsecond=0)
    for _ in range(back * 24):
        if t.hour in hours:
            try:
                if probe(t):
                    return t
            except Exception:
                pass
        t -= dt.timedelta(hours=1)
    return None


WORKERS = int(os.environ.get("GRIB_WORKERS", "8"))


def fetch_all(jobs):
    """Scarica in parallelo: 'jobs' è una lista di funzioni senza argomenti che restituiscono bytes.
    I risultati tornano nello stesso ordine. Il ritaglio (ecCodes) resta nel processo principale, uno alla volta."""
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return list(pool.map(lambda f: f(), jobs))


def recent_runs(probe, hours, n=3, back_hours=54):
    """Le ultime 'n' corse disponibili, dalla più recente."""
    t = dt.datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    found = []
    for _ in range(back_hours):
        if t.hour in hours:
            try:
                if probe(t):
                    found.append(t)
                    if len(found) >= n:
                        break
            except Exception:
                pass
        t -= dt.timedelta(hours=1)
    return found


def have():
    """I file già in archivio (nomi), per non rifarli."""
    path = os.environ.get("GRIB_HAVE", "")
    if path and os.path.exists(path):
        return {l.strip() for l in open(path) if l.strip()}
    return set()


def missing_runs(model, runs, ext="grb2"):
    """Le corse da costruire: quelle non ancora in archivio. Se sono tutte in archivio, nessuna."""
    got = have()
    todo = [r for r in runs if f"{model}_{r:%Y%m%d%H}.{ext}" not in got]
    for r in runs:
        if r not in todo:
            print(f"{model}: corsa {r:%Y-%m-%d %H}Z già in archivio")
    return todo


def build_all(model, runs, one):
    """Costruisce le corse mancanti; una corsa che fallisce non ferma le altre."""
    if not runs:
        raise IOError(f"nessuna corsa {model} trovata")
    made, errors = [], []
    for run in missing_runs(model, runs):
        try:
            made.append(one(run))
        except Exception as e:
            errors.append(f"{run:%Y-%m-%d %H}Z: {e}")
            print(f"ATTENZIONE: {model} corsa {run:%Y-%m-%d %H}Z non riuscita: {e}", file=sys.stderr)
    if errors and not made:
        raise IOError("; ".join(errors))
    return made


def parse_idx(text, wanted):
    """Indice dei file GFS: righe 'n:posizione:d=data:VARIABILE:livello:...'. Restituisce gli intervalli di byte."""
    rows = []
    for line in text.splitlines():
        p = line.split(":")
        if len(p) >= 5 and p[1].isdigit():
            rows.append((int(p[1]), p[3], p[4]))
    out = []
    for k, (off, var, lev) in enumerate(rows):
        if (var, lev) in wanted:
            end = rows[k + 1][0] - 1 if k + 1 < len(rows) else None
            out.append((off, end))
    return out


def build_gfs(box, out_dir):
    root = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
    def name(t, step):
        return f"{root}/gfs.{t:%Y%m%d}/{t:%H}/atmos/gfs.t{t:%H}z.pgrb2.0p25.f{step:03d}"
    wanted = {("UGRD", "10 m above ground"), ("VGRD", "10 m above ground"), ("PRMSL", "mean sea level")}
    runs = recent_runs(lambda t: len(http_get(name(t, 144) + ".idx", tries=1, timeout=30)) > 100, hours=(0, 6, 12, 18))
    def one(run):
        steps = list(range(0, 49)) + list(range(51, 145, 3))
        def get_step(step):
            idx = http_get(name(run, step) + ".idx").decode()
            return b"".join(http_get(name(run, step), headers={"Range": f"bytes={a}-{b if b is not None else ''}"})
                            for a, b in parse_idx(idx, wanted))
        msgs = []
        for raw in fetch_all([lambda s=s: get_step(s) for s in steps]):
            msgs += crop_bytes(raw, box)
        return write(out_dir, "gfs", run, msgs)
    return build_all("gfs", runs, one)


AROME_DATASET = "https://www.data.gouv.fr/api/1/datasets/paquets-arome-resolution-0-025deg/"
AROME_GROUPS = ["00H06H", "07H12H", "13H18H", "19H24H", "25H30H", "31H36H", "37H42H", "43H48H", "49H51H"]
ARPEGE_DATASET = "https://www.data.gouv.fr/api/1/datasets/paquets-arpege-resolution-0-1deg/"
ARPEGE_GROUPS = ["000H012H", "013H024H", "025H036H", "037H048H", "049H060H", "061H072H", "073H084H", "085H096H", "097H102H"]


def build_meteofrance(model, dataset, groups, box, out_dir):
    """Modelli di Météo-France su data.gouv.fr: dall'elenco del jeu de données si ricava come sono fatti gli indirizzi
    dei file SP1 dell'ultima corsa, poi si costruiscono quelli delle corse precedenti ancora in linea.
    Si tiene solo il vento a 10 m."""
    import re
    info = json.loads(http_get(dataset, headers={"Accept": "application/json"}).decode())
    sp1 = [r.get("url") or "" for r in info.get("resources", [])]
    sp1 = [u for u in sp1 if "SP1" in u and ".grib2" in u]
    if not sp1:
        names = [((r.get("title") or "") + " | " + (r.get("url") or ""))[:160] for r in info.get("resources", [])[:6]]
        raise IOError("nell'elenco di data.gouv.fr non trovo file SP1. Primi elementi: " + " ;; ".join(names))
    print(f"{model}: {len(sp1)} file SP1 nell'elenco, per esempio {sp1[0]}")
    # la corsa compare nell'indirizzo come 2026-11-14T18:00:00Z (i due punti a volte codificati come %3A)
    m = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}(?::|%3A)00(?::|%3A)00Z", sp1[0])
    # i gruppi di ore si leggono dall'elenco stesso (es. 00H06H per AROME, 000H012H per ARPEGE)
    found = sorted({g for u in sp1 for g in re.findall(r"__(\d+H\d+H)__", u)}, key=lambda g: int(g.split("H")[0]))
    if len(found) >= len(groups):      # un elenco incompleto (corsa in pubblicazione) non deve accorciare la corsa
        groups = found
    group = next((g for g in groups if f"__{g}__" in sp1[0]), None)
    if not m or group is None:
        raise IOError("non riconosco corsa o gruppo di ore nell'indirizzo " + sp1[0])
    sep = "%3A" if "%3A" in m.group(0) else ":"
    template = sp1[0].replace(m.group(0), "{RUN}").replace(f"__{group}__", "__{GROUP}__")
    def url(run, g):
        return template.replace("{RUN}", f"{run:%Y-%m-%dT%H}{sep}00{sep}00Z").replace("{GROUP}", g)
    def probe(run):
        return len(http_get(url(run, groups[-1]), headers={"Range": "bytes=0-99"}, tries=1, timeout=30)) > 10
    runs = recent_runs(probe, hours=(0, 6, 12, 18), n=2)
    def one(run):
        raws = fetch_all([lambda g=g: http_get(url(run, g), timeout=300) for g in groups])
        msgs = []
        for raw in raws:
            msgs += crop_bytes(raw, box, keep={"10u", "10v"})
        return write(out_dir, model, run, msgs)
    return build_all(model, runs, one)


def build_arome(box, out_dir):
    return build_meteofrance("arome", AROME_DATASET, AROME_GROUPS, box, out_dir)


def build_arpege(box, out_dir):
    return build_meteofrance("arpege", ARPEGE_DATASET, ARPEGE_GROUPS, box, out_dir)


def build_ecmwf_wave(box, out_dir):
    root = "https://data.ecmwf.int/forecasts"
    def name(t, step, ext):
        return f"{root}/{t:%Y%m%d}/{t:%H}z/ifs/0p25/wave/{t:%Y%m%d%H}0000-{step}h-wave-fc.{ext}"
    runs = recent_runs(lambda t: len(http_get(name(t, 144, "index"), tries=1, timeout=30)) > 100, hours=(0, 12))
    wanted = {"swh", "mwd", "mwp"}
    def one(run):
        def get_step(step):
            parts = []
            for line in http_get(name(run, step, "index")).decode().splitlines():
                rec = json.loads(line)
                if rec.get("param") in wanted:
                    a, n = int(rec["_offset"]), int(rec["_length"])
                    parts.append(http_get(name(run, step, "grib2"), headers={"Range": f"bytes={a}-{a + n - 1}"}))
            return b"".join(parts)
        msgs = []
        for raw in fetch_all([lambda s=s: get_step(s) for s in range(0, 145, 3)]):
            msgs += crop_bytes(raw, box)
        return write(out_dir, "ecmwf-wave", run, msgs)
    return build_all("ecmwf-wave", runs, one)


def build_gfs_wave(box, out_dir):
    root = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
    def name(t, step):
        return f"{root}/gfs.{t:%Y%m%d}/{t:%H}/wave/gridded/gfswave.t{t:%H}z.global.0p16.f{step:03d}.grib2"
    wanted = {("HTSGW", "surface"), ("DIRPW", "surface"), ("PERPW", "surface")}
    runs = recent_runs(lambda t: len(http_get(name(t, 144) + ".idx", tries=1, timeout=30)) > 100, hours=(0, 6, 12, 18))
    def one(run):
        steps = list(range(0, 73)) + list(range(75, 145, 3))
        def get_step(step):
            idx = http_get(name(run, step) + ".idx").decode()
            return b"".join(http_get(name(run, step), headers={"Range": f"bytes={a}-{b if b is not None else ''}"})
                            for a, b in parse_idx(idx, wanted))
        msgs = []
        for raw in fetch_all([lambda s=s: get_step(s) for s in steps]):
            msgs += crop_bytes(raw, box)
        return write(out_dir, "gfs-wave", run, msgs)
    return build_all("gfs-wave", runs, one)


# Copernicus Marine, Mediterraneo: (nome nell'archivio, dataset, variabili -> codici GRIB2, ogni quante ore)
CMEMS = [
    ("cmems-cur", "cmems_mod_med_phy-cur_anfc_4.2km-2D_PT1H-m", {"uo": (10, 1, 2), "vo": (10, 1, 3)}, 1),
    ("cmems-wav", "cmems_mod_med_wav_anfc_4.2km_PT1H-i", {"VHM0": (10, 0, 3), "VMDR": (10, 0, 14), "VTM10": (10, 0, 15)}, 3),
]


def nc_to_grib(ds, variables, run, every):
    """Un NetCDF di Copernicus Marine (tempo, lat, lon) -> messaggi GRIB2 che l'app legge: correnti 10/1/2-3, onde 10/0/3."""
    import numpy as np
    lat = np.asarray(ds["latitude"].values, dtype=float)
    lon = np.asarray(ds["longitude"].values, dtype=float)
    msgs = []
    for tv in ds["time"].values:
        t = dt.datetime.utcfromtimestamp((np.datetime64(tv, "s") - np.datetime64(0, "s")).astype(int))
        step = (t - run).total_seconds() / 3600.0
        if step < 0 or step != int(step) or int(step) % every:
            continue
        for var, (d, c, n) in variables.items():
            a = ds[var].sel(time=tv)
            for dim in [x for x in a.dims if x not in ("latitude", "longitude")]:
                a = a.isel({dim: 0})
            a = np.asarray(a.transpose("latitude", "longitude").values, dtype=float)
            h = ec.codes_grib_new_from_samples("regular_ll_sfc_grib2")
            try:
                ec.codes_set(h, "Ni", len(lon)); ec.codes_set(h, "Nj", len(lat))
                ec.codes_set(h, "jScansPositively", 1 if lat[-1] > lat[0] else 0)
                ec.codes_set(h, "latitudeOfFirstGridPointInDegrees", float(lat[0]))
                ec.codes_set(h, "latitudeOfLastGridPointInDegrees", float(lat[-1]))
                ec.codes_set(h, "longitudeOfFirstGridPointInDegrees", float(lon[0] % 360.0))
                ec.codes_set(h, "longitudeOfLastGridPointInDegrees", float(lon[-1] % 360.0))
                ec.codes_set(h, "iDirectionIncrementInDegrees", float(abs(lon[1] - lon[0])))
                ec.codes_set(h, "jDirectionIncrementInDegrees", float(abs(lat[1] - lat[0])))
                ec.codes_set(h, "discipline", d); ec.codes_set(h, "parameterCategory", c); ec.codes_set(h, "parameterNumber", n)
                if c == 1:
                    ec.codes_set(h, "typeOfFirstFixedSurface", 160)     # profondità sotto il livello del mare: 0 m
                    ec.codes_set(h, "scaleFactorOfFirstFixedSurface", 0); ec.codes_set(h, "scaledValueOfFirstFixedSurface", 0)
                ec.codes_set(h, "dataDate", int(f"{run:%Y%m%d}")); ec.codes_set(h, "dataTime", int(f"{run:%H}00"))
                ec.codes_set(h, "indicatorOfUnitOfTimeRange", 1); ec.codes_set(h, "forecastTime", int(step))
                ec.codes_set(h, "packingType", "grid_simple"); ec.codes_set(h, "bitsPerValue", 12)
                if np.isnan(a).any():
                    ec.codes_set(h, "bitmapPresent", 1); ec.codes_set(h, "missingValue", 9999.0)
                    a = np.where(np.isnan(a), 9999.0, a)
                ec.codes_set_values(h, a.flatten())
                msgs.append(ec.codes_get_message(h))
            finally:
                ec.codes_release(h)
    return msgs


def build_cmems(box, out_dir):
    user = os.environ.get("CMEMS_USER", "").strip()
    pwd = os.environ.get("CMEMS_PASS", "")
    if not user or not pwd:
        raise IOError("credenziali di Copernicus Marine non impostate (Secrets CMEMS_USER e CMEMS_PASS): salto")
    import tempfile
    import copernicusmarine
    import xarray
    now = dt.datetime.utcnow()
    run = now.replace(hour=12, minute=0, second=0, microsecond=0)
    if now.hour < 12:
        print("cmems: si scarica una volta al giorno, dopo le 12 UTC: salto questo giro")
        return []
    got = have()
    made = []
    tmp = tempfile.mkdtemp()
    latmin, latmax, lonmin, lonmax = box
    for model, dataset, variables, every in CMEMS:
        if f"{model}_{run:%Y%m%d%H}.grb2" in got:
            print(f"{model}: giorno {run:%Y-%m-%d} già in archivio")
            continue
        try:
            res = copernicusmarine.subset(
                dataset_id=dataset, variables=list(variables),
                minimum_longitude=lonmin, maximum_longitude=lonmax, minimum_latitude=latmin, maximum_latitude=latmax,
                start_datetime=f"{run:%Y-%m-%dT%H:%M:%S}", end_datetime=f"{run + dt.timedelta(days=5):%Y-%m-%dT%H:%M:%S}",
                username=user, password=pwd, output_directory=tmp, output_filename=f"{model}.nc")
            path = getattr(res, "file_path", res)
            with xarray.open_dataset(path) as ds:
                msgs = nc_to_grib(ds, variables, run, every)
            made.append(write(out_dir, model, run, msgs))
        except Exception as e:
            print(f"ATTENZIONE: {model} non riuscito: {e}", file=sys.stderr)
    if not made and any(f"{m}_{run:%Y%m%d%H}.grb2" not in got for m, *_ in CMEMS):
        raise IOError("nessun prodotto di Copernicus Marine scaricato: vedi gli avvisi sopra")
    return made


def build_ecmwf(box, out_dir, resol="0p25"):
    """ECMWF IFS. A 0,25 gradi esce prima; a 0,1 gradi (dal 20 ottobre 2026) esce circa due ore dopo, stessa
    previsione su una griglia 2,5 volte più fitta, con i campi di superficie in un file a parte (-sfc)."""
    root = "https://data.ecmwf.int/forecasts"
    fine = resol == "0p1"
    model = "ecmwf-0p1" if fine else "ecmwf"
    suffix = "-sfc" if fine else ""
    def name(t, step, ext):
        return f"{root}/{t:%Y%m%d}/{t:%H}z/ifs/{resol}/oper/{t:%Y%m%d%H}0000-{step}h-oper-fc{suffix}.{ext}"
    runs = recent_runs(lambda t: len(http_get(name(t, 144, "index"), tries=1, timeout=30)) > 100, hours=(0, 12))
    wanted = {"10u", "10v", "msl", "10fg"} if fine else {"10u", "10v", "msl"}
    def one(run):
        def get_step(step):
            parts = []
            for line in http_get(name(run, step, "index")).decode().splitlines():
                rec = json.loads(line)
                if rec.get("param") in wanted and rec.get("levtype", "sfc") == "sfc":
                    a, n = int(rec["_offset"]), int(rec["_length"])
                    parts.append(http_get(name(run, step, "grib2"), headers={"Range": f"bytes={a}-{a + n - 1}"}))
            return b"".join(parts)
        msgs = []
        steps = list(range(0, 145, 3))
        for k in range(0, len(steps), WORKERS):          # a blocchi: a 0,1 gradi ogni passo globale pesa ~15 MB
            for raw in fetch_all([lambda s=s: get_step(s) for s in steps[k:k + WORKERS]]):
                msgs += crop_bytes(raw, box)
        return write(out_dir, model, run, msgs)
    return build_all(model, runs, one)


def build_ecmwf_0p1(box, out_dir):
    return build_ecmwf(box, out_dir, resol="0p1")


def build_icon_eu(box, out_dir):
    root = "https://opendata.dwd.de/weather/nwp/icon-eu/grib"
    def name(t, var, step):
        return f"{root}/{t:%H}/{var.lower()}/icon-eu_europe_regular-lat-lon_single-level_{t:%Y%m%d%H}_{step:03d}_{var}.grib2.bz2"
    runs = recent_runs(lambda t: len(http_get(name(t, "U_10M", 120), tries=1, timeout=30)) > 1000, hours=(0, 6, 12, 18))
    steps = list(range(0, 49)) + list(range(51, 121, 3))
    def one(run):
        def get(step, var):
            try:
                return bz2.decompress(http_get(name(run, var, step)))
            except Exception:
                if var == "VMAX_10M":
                    return b""              # le raffiche non ci sono all'ora zero: non è un errore
                raise
        jobs = [lambda s=s, v=v: get(s, v) for s in steps for v in ("U_10M", "V_10M", "VMAX_10M")]
        msgs = []
        for raw in fetch_all(jobs):
            if raw:
                msgs += crop_bytes(raw, box)
        return write(out_dir, "icon-eu", run, msgs)
    return build_all("icon-eu", runs, one)


ICON2I_FILES = {"U_10M": "heightAboveGround-10", "V_10M": "heightAboveGround-10", "VMAX_10M": "heightAboveGround-10"}


def build_icon_2i(box, out_dir):
    """ICON-2I di ItaliaMeteo-Arpae da MeteoHub (dati aperti, senza account). Un file per variabile e per corsa
    (~85 MB, tutte le ore da 0 a 72), griglia regolare 0,025 x 0,02 gradi su 33,7-48,9 N e 3-22 E."""
    root = "https://meteohub.agenziaitaliameteo.it/nwp/ICON-2I_SURFACE_PRESSURE_LEVELS"
    def url(t, var):
        return f"{root}/{t:%Y%m%d%H}/{var}/ICON_2I_SURFACE_PRESSURE_LEVELS_{t:%Y%m%d%H}_{ICON2I_FILES[var]}.grib"
    def ready(t):
        for var in ICON2I_FILES:
            page = http_get(f"{root}/{t:%Y%m%d%H}/{var}/", tries=1, timeout=30).decode("utf-8", "replace")
            if url(t, var).rsplit("/", 1)[1] not in page:
                return False
        return True
    runs = recent_runs(ready, hours=(0, 12))
    def one(run):
        raws = fetch_all([lambda v=v: http_get(url(run, v), timeout=600) for v in ICON2I_FILES])
        msgs = []
        for var, raw in zip(ICON2I_FILES, raws):
            got = crop_bytes(raw, box)
            if var != "VMAX_10M" and len(got) < 73:
                raise IOError(f"{var}: {len(got)} ore su 73, corsa incompleta")
            msgs += got
        def valid_at(m):                      # l'ora zero è in minuti ("0m"), le altre in ore: si ordina per validità
            h = ec.codes_new_from_message(m)
            try:
                return (int(ec.codes_get(h, "validityDate")), int(ec.codes_get(h, "validityTime")))
            finally:
                ec.codes_release(h)
        msgs.sort(key=valid_at)               # in ordine di tempo, come gli altri modelli
        return write(out_dir, "icon-2i", run, msgs)
    return build_all("icon-2i", runs, one)


def build_youmeteo(box, out_dir):
    """
    GRIB HRRR Italia di youmeteo.com, riservato agli abbonati. Si entra nel sito con le credenziali dell'abbonato
    (variabili YOUMETEO_USER e YOUMETEO_PASS, da mettere nei Secrets di GitHub), come farebbe lui dal browser,
    si scarica il pacchetto e lo si ritaglia. Uso personale, con il consenso del gestore del sito.
    I file ritagliati vanno pubblicati solo in un deposito privato.
    """
    import re, tarfile
    import requests
    user = os.environ.get("YOUMETEO_USER", "").strip()
    pwd = os.environ.get("YOUMETEO_PASS", "")
    if not user or not pwd:
        raise IOError("credenziali di youmeteo non impostate (Secrets YOUMETEO_USER e YOUMETEO_PASS): salto")
    base = os.environ.get("YOUMETEO_BASE", "https://www.youmeteo.com")
    login_url = os.environ.get("YOUMETEO_LOGIN", base + "/accesso-utenti/")
    packs = [p.strip() for p in os.environ.get("YOUMETEO_PACKS", "96:60,48:30").split(",") if p.strip()]
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (personal subscriber script)"})

    def is_grib_payload(b):
        return b[:3] == b"BZh" or b[:4] == b"GRIB" or b[257:262] == b"ustar" or b[:2] == b"\x1f\x8b"

    def describe(r):
        """Poche righe per capire cosa ha risposto il sito, senza stampare nulla di riservato."""
        text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", r.text[:20000], flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return f"HTTP {r.status_code}, tipo {r.headers.get('content-type', '?')}, indirizzo finale {r.url.split('?')[0]}, testo: {text[:300]}"

    # 1. La pagina di accesso: si cerca il modulo che contiene un campo password e lo si compila.
    page = s.get(login_url, timeout=60)
    forms = re.findall(r"<form\b.*?</form>", page.text, flags=re.S | re.I)
    form = next((f for f in forms if re.search(r'type=["\']password["\']', f, re.I)), None)
    if form is None:
        raise IOError("nella pagina di accesso non trovo un modulo con la password. " + describe(page))
    if re.search(r"recaptcha|hcaptcha|turnstile|captcha", form, re.I):
        print("ATTENZIONE: il modulo di accesso sembra avere un controllo anti-robot: l'accesso automatico potrebbe non riuscire.", file=sys.stderr)
    action = re.search(r'<form[^>]*\baction=["\']([^"\']*)["\']', form, re.I)
    action = requests.compat.urljoin(page.url, action.group(1)) if action and action.group(1) else page.url
    data = {}
    user_field = None
    pass_field = None
    for tag in re.findall(r"<input\b[^>]*>", form, flags=re.I):
        name = re.search(r'\bname=["\']([^"\']+)["\']', tag, re.I)
        if not name:
            continue
        typ = (re.search(r'\btype=["\']([^"\']+)["\']', tag, re.I) or [None, "text"])[1].lower()
        val = re.search(r'\bvalue=["\']([^"\']*)["\']', tag, re.I)
        if typ == "password":
            pass_field = name.group(1)
        elif typ in ("text", "email") and user_field is None:
            user_field = name.group(1)
        elif typ in ("hidden", "submit"):
            data[name.group(1)] = val.group(1) if val else ""
        elif typ == "checkbox":
            data[name.group(1)] = val.group(1) if val else "on"        # "ricordami"
    if user_field is None or pass_field is None:
        raise IOError("non riconosco i campi del modulo di accesso (nome utente o password).")
    data[user_field] = user
    data[pass_field] = pwd
    print(f"youmeteo: modulo di accesso trovato (campi '{user_field}' e '{pass_field}', {len(data) - 2} campi nascosti), invio a {action.split('?')[0]}")
    r = s.post(action, data=data, timeout=60, headers={"Referer": page.url})
    print("youmeteo: risposta all'accesso: " + describe(r)[:160])

    # 2. I pacchetti: ore e passo in minuti, come nell'indirizzo che si usa a mano.
    made = []
    for pack in packs:
        hours, step = pack.split(":")
        url = f"{base}/velisti.php?grib={hours}&min={step}&muv=0"
        g = s.get(url, timeout=300)
        if g.status_code != 200 or not is_grib_payload(g.content):
            print(f"ATTENZIONE: youmeteo {hours} ore passo {step}: non è arrivato un GRIB. {describe(g)}", file=sys.stderr)
            continue
        raw = g.content
        # Il pacchetto è un archivio compresso con dentro un solo GRIB.
        if raw[:4] != b"GRIB":
            try:
                with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as tf:
                    member = next(m for m in tf.getmembers() if m.isfile())
                    raw = tf.extractfile(member).read()
            except tarfile.TarError:
                raw = bz2.decompress(raw)
        msgs = crop_bytes(raw, box)
        if not msgs:
            print(f"ATTENZIONE: youmeteo {hours} ore: nessun campo dentro l'area {box}.", file=sys.stderr)
            continue
        # La corsa si legge dal primo campo.
        h = ec.codes_new_from_message(raw[raw.find(b"GRIB"):])
        try:
            run = dt.datetime.strptime(f"{ec.codes_get(h, 'dataDate')}{ec.codes_get(h, 'dataTime'):04d}", "%Y%m%d%H%M")
        finally:
            ec.codes_release(h)
        made.append(write(out_dir, f"youmeteo-{hours}h", run, msgs, ext="grb"))
    if not made:
        raise IOError("nessun pacchetto di youmeteo scaricato: vedi gli avvisi sopra")
    return made


def write(out_dir, model, run, msgs, ext="grb2"):
    if not msgs:
        raise IOError(f"{model}: nessun campo utilizzabile")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{model}_{run:%Y%m%d%H}.{ext}")
    with open(path, "wb") as f:
        for m in msgs:
            f.write(m)
    print(f"{model}: corsa {run:%Y-%m-%d %H}Z, {len(msgs)} campi, {os.path.getsize(path) / 1e6:.1f} MB -> {path}")
    return path


# ------------------------------------------------------------------------------------------------- quadratini
# Oltre al file intero, i modelli fitti si pubblicano tagliati a quadratini fissi, uguali per tutti i modelli, così
# l'app scarica solo quelli che toccano il riquadro tracciato sulla carta.
#   - lato 1 grado per i modelli fini (icon-2i, arome, youmeteo), 2 gradi per gli altri (icon-eu, arpege, ecmwf-0p1,
#     e cmems per stare sotto i 1.000 file della release); niente quadratini da 0,15 gradi di maglia in su;
#   - angoli sud-ovest su multipli interi del lato; un quadratino (LAT, LON) contiene i punti con
#     LAT <= lat < LAT+lato e LON <= lon < LON+lato, assegnati per indice: affiancati ridanno la griglia del modello;
#   - dentro, tutti i campi del file intero, nello stesso ordine e con la stessa compressione semplice;
#   - non si pubblicano i quadratini con meno di 2 punti per lato o tutti mancanti (per esempio il mare fuori da AROME);
#   - nome: <modello>_<AAAAMMGGHH>_t<lato>_<N|S><LL>_<E|W><LLL>.<ext>, e per ultimo <modello>_<AAAAMMGGHH>_tiles.ok.
TILE_MODELS = ["icon-2i", "arome", "youmeteo-96h", "youmeteo-48h", "cmems-cur", "cmems-wav", "icon-eu", "ecmwf-0p1", "arpege"]
TILE_RELEASE = "tiles"
TILE_SIDES = {"icon-2i": 1, "arome": 1, "youmeteo-96h": 1, "youmeteo-48h": 1,
              "icon-eu": 2, "arpege": 2, "ecmwf-0p1": 2, "cmems-cur": 2, "cmems-wav": 2}
# Una sola corsa per modello: con i lati di 1 e 2 gradi una corsa di tutti i modelli fa circa 570 file e una release
# ne tiene al massimo 1.000. Si lavora un modello alla volta (quadratini, segnaposto, poi via la corsa vecchia),
# così il massimo momentaneo è circa 570 più i quadratini di un modello solo.
TILE_KEEP_RUNS = 1
TILE_MAX_ASSETS = 1000
TILE_EPS = 1e-3          # in frazioni di maglia: un punto a meno di un millesimo di maglia da un bordo sta sul bordo


def tile_side(di, model=None):
    """Lato del quadratino in gradi interi: dalla tabella per i modelli noti, altrimenti dalla maglia del modello
    (iDirectionIncrement). None: basta il file intero."""
    if di >= 0.15:
        return None
    if model in TILE_SIDES:
        return TILE_SIDES[model]
    return 1 if di < 0.06 else 2


def tile_axis(start, step, n, side):
    """Lungo un asse con n punti (start + k*step, crescente): {angolo: (k0, k1)}, k0 incluso e k1 escluso.
    I confini si calcolano una volta per angolo, poi ogni punto cade in un solo quadratino per indice."""
    def first_at_or_after(x):
        return max(0, min(n, math.ceil((x - start) / step - TILE_EPS)))
    out = {}
    c = int(math.floor((start + TILE_EPS * step) / side)) * side
    end = start + (n - 1) * step
    while c <= end + TILE_EPS * step:
        k0, k1 = first_at_or_after(c), first_at_or_after(c + side)
        if k1 > k0:
            out[c] = (k0, k1)
        c += side
    return out


def tile_grid(h):
    """La griglia di un campo regolare, riportata da sud verso nord e da ovest verso est."""
    if ec.codes_get(h, "gridType") != "regular_ll":
        raise ValueError("griglia non regolare")
    if ec.codes_get(h, "iScansNegatively") or ec.codes_get(h, "jPointsAreConsecutive"):
        raise ValueError("ordine dei punti non previsto")
    ni, nj = ec.codes_get(h, "Ni"), ec.codes_get(h, "Nj")
    la1 = ec.codes_get(h, "latitudeOfFirstGridPointInDegrees"); la2 = ec.codes_get(h, "latitudeOfLastGridPointInDegrees")
    lo1 = ec.codes_get(h, "longitudeOfFirstGridPointInDegrees"); lo2 = ec.codes_get(h, "longitudeOfLastGridPointInDegrees")
    if lo2 < lo1:
        lo2 += 360.0
    # passo dalle due estremità (più preciso dell'incremento scritto, che è arrotondato al milionesimo o al millesimo)
    dlon = (lo2 - lo1) / (ni - 1)
    dlat = abs(la2 - la1) / (nj - 1)
    return {"ni": ni, "nj": nj, "lat_s": min(la1, la2), "dlat": dlat, "lon_w": ((lo1 + 180.0) % 360.0) - 180.0,
            "dlon": dlon, "north_first": la1 > la2, "di": ec.codes_get(h, "iDirectionIncrementInDegrees")}


def tile_name(model, run, side, lat, lon, ext):
    return f"{model}_{run}_t{side}_{'N' if lat >= 0 else 'S'}{abs(lat):02d}_{'E' if lon >= 0 else 'W'}{abs(lon):03d}{ext}"


def tile_file(path, out_dir):
    """Taglia un file intero (<modello>_<corsa>.<ext>) in quadratini dentro out_dir.
    Restituisce (lato, [percorsi], note): lato None vuol dire niente quadratini per quel modello."""
    stem, ext = os.path.splitext(os.path.basename(path))
    model, run = stem.rsplit("_", 1)
    tiles = {}                     # (lat, lon) -> [messaggi]
    filled = {}                    # (lat, lon) -> almeno un punto non mancante
    side = None
    n_fields = 0
    thin = set()
    with open(path, "rb") as f:
        while True:
            h = ec.codes_grib_new_from_file(f)
            if h is None:
                break
            try:
                g = tile_grid(h)
                s_ = tile_side(g["di"], model)
                if n_fields == 0:
                    side = s_
                    if side is None:
                        return None, [], f"maglia {g['di']:g} gradi: basta il file intero"
                elif s_ != side:
                    raise ValueError(f"campo {n_fields + 1}: maglia diversa dal primo")
                n_fields += 1
                ni, nj = g["ni"], g["nj"]
                vals = ec.codes_get_values(h).reshape(nj, ni)
                if g["north_first"]:
                    vals = vals[::-1]                          # da qui in poi righe da sud a nord
                bitmap = ec.codes_get(h, "bitmapPresent")
                missing = ec.codes_get(h, "missingValue")
                bits = ec.codes_get(h, "bitsPerValue")
                for lat, (j0, j1) in tile_axis(g["lat_s"], g["dlat"], nj, side).items():
                    for lon, (i0, i1) in tile_axis(g["lon_w"], g["dlon"], ni, side).items():
                        if j1 - j0 < 2 or i1 - i0 < 2:
                            thin.add((lat, lon, j1 - j0, i1 - i0))
                            continue
                        sub = vals[j0:j1, i0:i1]
                        miss = (sub == missing) if bitmap else np.zeros(sub.shape, bool)
                        if g["north_first"]:
                            sub, miss = sub[::-1], miss[::-1]          # nel file, lo stesso verso del file intero
                        s_lat, n_lat = g["lat_s"] + j0 * g["dlat"], g["lat_s"] + (j1 - 1) * g["dlat"]
                        w_lon, e_lon = g["lon_w"] + i0 * g["dlon"], g["lon_w"] + (i1 - 1) * g["dlon"]
                        c = ec.codes_clone(h)
                        try:
                            ec.codes_set(c, "packingType", "grid_simple")
                            ec.codes_set(c, "Ni", i1 - i0); ec.codes_set(c, "Nj", j1 - j0)
                            ec.codes_set(c, "latitudeOfFirstGridPointInDegrees", round(n_lat if g["north_first"] else s_lat, 6))
                            ec.codes_set(c, "latitudeOfLastGridPointInDegrees", round(s_lat if g["north_first"] else n_lat, 6))
                            ec.codes_set(c, "longitudeOfFirstGridPointInDegrees", round(w_lon % 360.0, 6))
                            ec.codes_set(c, "longitudeOfLastGridPointInDegrees", round(e_lon % 360.0, 6))
                            ec.codes_set(c, "bitmapPresent", 1 if miss.any() else 0)
                            ec.codes_set(c, "bitsPerValue", bits)
                            ec.codes_set_values(c, np.where(miss, missing, sub).flatten())
                            tiles.setdefault((lat, lon), []).append(ec.codes_get_message(c))
                        finally:
                            ec.codes_release(c)
                        if not miss.all():
                            filled[(lat, lon)] = True
            finally:
                ec.codes_release(h)
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    empty = 0
    for (lat, lon), msgs in sorted(tiles.items()):
        if not filled.get((lat, lon)):
            empty += 1
            continue
        if len(msgs) != n_fields:
            raise ValueError(f"quadratino {lat},{lon}: {len(msgs)} campi su {n_fields}")
        p = os.path.join(out_dir, tile_name(model, run, side, lat, lon, ext))
        with open(p, "wb") as f:
            for m in msgs:
                f.write(m)
        paths.append(p)
    note = []
    if empty:
        note.append(f"{empty} quadratini tutti mancanti non pubblicati")
    if thin:
        note.append(f"{len(thin)} quadratini con meno di 2 punti per lato non pubblicati")
    return side, paths, "; ".join(note)


def tiles_check(path):
    """Prova senza rete: taglia un file intero, rimette insieme i quadratini usando solo le coordinate scritte nei
    quadratini (come farà l'app) e confronta i valori punto per punto con il file intero."""
    import tempfile
    def load(fn):
        fields = []
        with open(fn, "rb") as f:
            while True:
                h = ec.codes_grib_new_from_file(f)
                if h is None:
                    break
                try:
                    g = tile_grid(h)
                    v = ec.codes_get_values(h).reshape(g["nj"], g["ni"])
                    m = (v == ec.codes_get(h, "missingValue")) if ec.codes_get(h, "bitmapPresent") else np.zeros(v.shape, bool)
                    if g["north_first"]:
                        v, m = v[::-1], m[::-1]
                    def get(k):
                        try:
                            return ec.codes_get(h, k)
                        except Exception:
                            return None
                    if get("edition") == 1:          # youmeteo: nome breve "unknown", ore in minuti
                        names = ("indicatorOfParameter", "level", "dataDate", "dataTime", "P1", "P2", "timeRangeIndicator")
                    else:
                        names = ("discipline", "parameterCategory", "parameterNumber", "level", "dataDate", "dataTime", "forecastTime")
                    key = tuple(get(k) for k in names)
                    e, d = get("binaryScaleFactor"), get("decimalScaleFactor")
                    step = 2.0 ** (e or 0) / 10.0 ** (d or 0)                     # passo della compressione semplice
                    fields.append((key, g, v, m, step))
                finally:
                    ec.codes_release(h)
        return fields
    whole = load(path)
    g0 = whole[0][1]
    with tempfile.TemporaryDirectory() as tmp:
        side, paths, note = tile_file(path, tmp)
        if side is None:
            print(f"{os.path.basename(path)}: {note}")
            return True
        cover = np.zeros((g0["nj"], g0["ni"]), int)
        got = [np.full((g0["nj"], g0["ni"]), np.nan) for _ in whole]
        gotm = [np.zeros((g0["nj"], g0["ni"]), bool) for _ in whole]
        sizes = []
        for p in paths:
            sizes.append(os.path.getsize(p))
            part = load(p)
            assert len(part) == len(whole), f"{p}: {len(part)} campi invece di {len(whole)}"
            for k, (key, g, v, m, _) in enumerate(part):
                assert key == whole[k][0], f"{p}: campo {k + 1} è {key}, nel file intero {whole[k][0]}"
                j0 = int(round((g["lat_s"] - g0["lat_s"]) / g0["dlat"]))
                i0 = int(round((g["lon_w"] - g0["lon_w"]) / g0["dlon"]))
                assert 0 <= j0 and j0 + g["nj"] <= g0["nj"] and 0 <= i0 and i0 + g["ni"] <= g0["ni"], f"{p}: fuori dalla griglia"
                if k == 0:
                    cover[j0:j0 + g["nj"], i0:i0 + g["ni"]] += 1
                got[k][j0:j0 + g["nj"], i0:i0 + g["ni"]] = v
                gotm[k][j0:j0 + g["nj"], i0:i0 + g["ni"]] = m
        assert cover.max() <= 1, "due quadratini hanno punti in comune"
        holes = cover == 0
        worst = 0.0          # differenza massima, nelle unità del campo
        ratio = 0.0          # la stessa, in frazioni del passo della compressione del file intero
        for k, (key, g, v, m, step) in enumerate(whole):
            c = ~holes
            assert np.array_equal(gotm[k][c], m[c]), f"campo {k + 1}: punti mancanti diversi"
            ok = c & ~m
            if ok.any():
                d = float(np.max(np.abs(got[k][ok] - v[ok])))
                worst = max(worst, d)
                ratio = max(ratio, d / step)
        lost_missing = int(sum((holes & f[3]).sum() for f in whole) // len(whole))
        lost_edge = int(holes.sum()) - lost_missing
        print(f"{os.path.basename(path)}: lato {side} gradi, {len(paths)} quadratini, {len(whole)} campi x "
              f"{g0['nj']}x{g0['ni']} punti; sovrapposizioni 0; differenza massima {worst:.2g} "
              f"({ratio:.1e} del passo di compressione); "
              f"punti non coperti {int(holes.sum())} (tutti mancanti nel file intero: {lost_missing}, bordo sottile: {lost_edge}); "
              f"peso {min(sizes) / 1e6:.2f}-{max(sizes) / 1e6:.2f} MB, totale {sum(sizes) / 1e6:.1f} MB"
              + (f"; {note}" if note else ""))
        # Ogni quadratino ha il suo valore di riferimento, scritto in 32 bit: quando non è rappresentabile esatto
        # i valori si spostano di un'unità dell'ultima cifra (circa un milionesimo per il vento), molto sotto il passo.
        return ratio <= 1e-3


def synthetic_files(out_dir):
    """Tre file finti per la prova senza rete: GRIB2 da nord a sud con punti mancanti e meridiano zero,
    GRIB2 da sud a nord a 0,0625 gradi con punti esattamente sui bordi, GRIB1 a 0,055 gradi come youmeteo."""
    os.makedirs(out_dir, exist_ok=True)
    rng = np.random.default_rng(7)
    cases = [("sint-a_2026010100.grb2", "regular_ll_sfc_grib2", 39.1, 35.95, -1.3, 3.1, 0.025, 0.05, True),
             ("sint-b_2026010100.grb2", "regular_ll_sfc_grib2", 34.0, 46.0, 5.0, 20.0, 0.0625, 0.0625, False),
             ("sint-c_2026010100.grb", "regular_ll_sfc_grib1", 36.035, 45.99, 6.025, 16.97, 0.055, 0.055, True)]
    out = []
    for name, sample, la1, la2, lo1, lo2, di, dj, holes in cases:
        ni, nj = int(round((lo2 - lo1) / di)) + 1, int(round(abs(la2 - la1) / dj)) + 1
        with open(os.path.join(out_dir, name), "wb") as f:
            for step in (0, 3, 6):
                for par in ("10u", "10v"):
                    h = ec.codes_grib_new_from_samples(sample)
                    try:
                        ec.codes_set(h, "Ni", ni); ec.codes_set(h, "Nj", nj)
                        ec.codes_set(h, "jScansPositively", 1 if la2 > la1 else 0)
                        ec.codes_set(h, "latitudeOfFirstGridPointInDegrees", la1); ec.codes_set(h, "latitudeOfLastGridPointInDegrees", la2)
                        ec.codes_set(h, "longitudeOfFirstGridPointInDegrees", lo1 % 360.0); ec.codes_set(h, "longitudeOfLastGridPointInDegrees", lo2 % 360.0)
                        ec.codes_set(h, "iDirectionIncrementInDegrees", di); ec.codes_set(h, "jDirectionIncrementInDegrees", dj)
                        ec.codes_set(h, "shortName", par); ec.codes_set(h, "typeOfLevel", "heightAboveGround"); ec.codes_set(h, "level", 10)
                        ec.codes_set(h, "stepRange", str(step))
                        ec.codes_set(h, "packingType", "grid_simple"); ec.codes_set(h, "bitsPerValue", 12)
                        v = rng.normal(0, 8, (nj, ni))
                        if holes:
                            v[: nj // 3, -ni // 4:] = 9999.0              # un angolo senza dati, come AROME
                            ec.codes_set(h, "bitmapPresent", 1); ec.codes_set(h, "missingValue", 9999.0)
                        ec.codes_set_values(h, v.flatten())
                        f.write(ec.codes_get_message(h))
                    finally:
                        ec.codes_release(h)
        out.append(os.path.join(out_dir, name))
    return out


def gh(*args, check=True):
    """Il comando gh di GitHub (nel lavoro programmato è già autenticato con GH_TOKEN)."""
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and r.returncode:
        raise IOError(f"gh {' '.join(args[:3])}...: {r.stderr.strip()[:300]}")
    return r


def release_assets(repo, tag):
    """{nome: (id, peso)} degli allegati di una release, tutte le pagine. None se la release non c'è."""
    r = gh("api", f"repos/{repo}/releases/tags/{tag}", "--jq", ".id", check=False)
    if r.returncode:
        return None
    rid = r.stdout.strip()
    out = gh("api", "--paginate", f"repos/{repo}/releases/{rid}/assets?per_page=100", "--jq", '.[] | "\\(.id) \\(.size) \\(.name)"').stdout
    assets = {}
    for line in out.splitlines():
        aid, size, name = line.split(" ", 2)
        assets[name] = (aid, int(size))
    return assets


TILE_RE = re.compile(r"^(?P<model>.+)_(?P<run>\d{10})_(?:t\d+_[NS]\d{2}_[EW]\d{3}\.\w+|tiles\.ok)$")
WHOLE_RE = re.compile(r"^(?P<model>.+)_(?P<run>\d{10})\.(?P<ext>grb2?|grb)$")


TILE_SIDE_RE = re.compile(r"_t(\d+)_[NS]\d{2}_[EW]\d{3}\.\w+$")


def publish_tiles(gribs_dir, tiles_dir):
    """Quadratini dell'ultima corsa di ogni modello fitto, nella release 'tiles'.
    Un modello alla volta: si taglia l'ultima corsa (nuova di questo giro o già nella release 'gribs') se non ha ancora
    il segnaposto .ok oppure se i suoi quadratini sono di una misura diversa da quella di adesso; si caricano i
    quadratini con un solo comando, poi il segnaposto, e solo dopo si tolgono la corsa vecchia e i quadratini della
    misura vecchia. youmeteo solo se il deposito è privato."""
    repo = os.environ["GITHUB_REPOSITORY"]
    private = gh("api", f"repos/{repo}", "--jq", ".private").stdout.strip() == "true"
    t_start = time.time()
    tiles = release_assets(repo, TILE_RELEASE)
    if tiles is None:
        gh("release", "create", TILE_RELEASE, "--latest=false", "--title", "GRIB a quadratini",
           "--notes", "Gli stessi GRIB della release gribs, tagliati a quadratini fissi di 1 o 2 gradi: l'app scarica solo "
                      "quelli che toccano la sua area. Una corsa è completa quando c'è il suo file <modello>_<corsa>_tiles.ok. "
                      "Si tiene l'ultima corsa completa di ogni modello. Fonti e licenze come nella release gribs.")
        tiles = {}
    whole = release_assets(repo, "gribs") or {}
    local = {os.path.basename(f): f for f in (os.path.join(gribs_dir, n) for n in os.listdir(gribs_dir))} if os.path.isdir(gribs_dir) else {}
    report = []
    peak = len(tiles)

    def delete(names):
        ids = [tiles[n][0] for n in names if n in tiles]
        if ids:
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda aid: gh("api", "-X", "DELETE", f"repos/{repo}/releases/assets/{aid}", check=False), ids))
            for n in names:
                tiles.pop(n, None)
        return len(ids)

    def runs_of(model):
        """{corsa: [nomi]} dei file del modello nella release tiles."""
        out = {}
        for n in tiles:
            m = TILE_RE.match(n)
            if m and m["model"] == model:
                out.setdefault(m["run"], []).append(n)
        return out

    for model in TILE_MODELS:
        if model.startswith("youmeteo") and not private:
            print(f"{model}: deposito pubblico, niente quadratini")
            continue
        runs = {}
        for name in list(whole) + list(local):
            m = WHOLE_RE.match(name)
            if m and m["model"] == model:
                runs[m["run"]] = name
        if not runs:
            print(f"{model}: nessuna corsa")
            continue
        run = max(runs)
        side_now = TILE_SIDES.get(model)
        have = runs_of(model).get(run, [])
        sides_have = {int(TILE_SIDE_RE.search(n)[1]) for n in have if TILE_SIDE_RE.search(n)}
        if f"{model}_{run}_tiles.ok" in tiles and sides_have == {side_now}:
            print(f"{model} {run}: quadratini già pubblicati")
        else:
            name = runs[run]
            path = local.get(name)
            if path is None:                                  # corsa già nella release gribs: la si riprende da lì
                tmp = os.path.join(tiles_dir, "_interi")
                os.makedirs(tmp, exist_ok=True)
                gh("release", "download", "gribs", "-p", name, "-D", tmp, "--clobber")
                path = os.path.join(tmp, name)
            t0 = time.time()
            side, paths, note = tile_file(path, os.path.join(tiles_dir, model))
            if side is None:
                print(f"{model} {run}: {note}")
                continue
            if len(tiles) + len(paths) + 1 > TILE_MAX_ASSETS:
                print(f"ATTENZIONE: {model}: {len(tiles)} file nella release più {len(paths)} supererebbero {TILE_MAX_ASSETS}, salto", file=sys.stderr)
                continue
            sizes = [os.path.getsize(x) for x in paths]
            cut = time.time() - t0
            t1 = time.time()
            r = gh("release", "upload", TILE_RELEASE, *paths, "--clobber", check=False)       # un solo comando per i quadratini
            if r.returncode:
                print(f"ATTENZIONE: {model} {run}: caricamento non riuscito, niente segnaposto: {r.stderr.strip()[:300]}", file=sys.stderr)
                continue
            for x, sz in zip(paths, sizes):
                tiles[os.path.basename(x)] = ("?", sz)
            peak = max(peak, len(tiles) + 1)
            ok = os.path.join(tiles_dir, f"{model}_{run}_tiles.ok")
            with open(ok, "w") as f:                          # prima riga il numero, poi i nomi
                f.write(f"{len(paths)}\n" + "".join(os.path.basename(x) + "\n" for x in paths))
            r = gh("release", "upload", TILE_RELEASE, ok, "--clobber", check=False)          # per ultimo: la corsa è completa
            if r.returncode:
                print(f"ATTENZIONE: {model} {run}: segnaposto non caricato: {r.stderr.strip()[:300]}", file=sys.stderr)
                continue
            print(f"{model} {run}: lato {side}, {len(paths)} quadratini, {min(sizes) / 1e6:.2f}-{max(sizes) / 1e6:.2f} MB, "
                  f"totale {sum(sizes) / 1e6:.1f} MB; taglio {cut:.0f} s, caricamento {time.time() - t1:.0f} s" + (f" ({note})" if note else ""))
            report.append((model, run, side, len(paths), min(sizes), max(sizes), sum(sizes), note))
            tiles = release_assets(repo, TILE_RELEASE) or {}          # id veri dei file appena caricati
        # pulizia del modello: si tiene l'ultima corsa completa, con i soli quadratini della misura di adesso
        rs = runs_of(model)
        complete = sorted((r_ for r_, names in rs.items() if f"{model}_{r_}_tiles.ok" in names), reverse=True)
        if complete:
            keep = set(complete[:TILE_KEEP_RUNS])
            old = [n for r_, names in rs.items() if r_ not in keep and r_ < min(keep)
                   for n in sorted(names, key=lambda x: not x.endswith("_tiles.ok"))]     # prima i segnaposto
            stale = [n for r_ in keep for n in rs[r_] if TILE_SIDE_RE.search(n) and int(TILE_SIDE_RE.search(n)[1]) != side_now]
            k = delete(old) + delete(stale)
            if k:
                print(f"{model}: tolti {k} file (corse vecchie o misura vecchia)")
    left = release_assets(repo, TILE_RELEASE) or {}
    print(f"release {TILE_RELEASE}: {len(left)} file, {sum(sz for _, sz in left.values()) / 1e6:.0f} MB "
          f"(massimo durante il giro {max(peak, len(left))}, limite {TILE_MAX_ASSETS}); giro dei quadratini {time.time() - t_start:.0f} s")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and report:
        with open(summary, "a") as f:
            f.write("### Quadratini\n\n| modello | corsa | lato | quadratini | min MB | max MB | totale MB | note |\n|---|---|---|---|---|---|---|---|\n")
            for model, run, side, n, mn, mx, tot, note in report:
                f.write(f"| {model} | {run} | {side}° | {n} | {mn / 1e6:.2f} | {mx / 1e6:.2f} | {tot / 1e6:.1f} | {note} |\n")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="gribs")
    ap.add_argument("--models", default="ecmwf,ecmwf-0p1,icon-eu,icon-2i,gfs,arome,arpege,ecmwf-wave,gfs-wave")
    ap.add_argument("--box", default=os.environ.get("GRIB_BOX", "34,46,5,20"))
    ap.add_argument("--selftest")
    ap.add_argument("--tiles", action="store_true", help="pubblica i quadratini delle ultime corse nella release tiles")
    ap.add_argument("--tiles-out", default="tiles")
    ap.add_argument("--tiles-check", nargs="+", metavar="FILE", help="prova senza rete: taglia e ricomponi (o 'sintetico')")
    a = ap.parse_args()
    box = tuple(float(x) for x in a.box.split(","))
    if a.tiles_check:
        import tempfile
        files = []
        with tempfile.TemporaryDirectory() as tmp:
            for f in a.tiles_check:
                files += synthetic_files(tmp) if f == "sintetico" else [f]
            good = all([tiles_check(f) for f in files])
        print("prova dei quadratini: " + ("riuscita" if good else "NON riuscita"))
        return 0 if good else 1
    if a.tiles:
        return publish_tiles(a.out, a.tiles_out)
    if a.selftest:
        data = open(a.selftest, "rb").read()
        msgs = crop_bytes(data, box)
        os.makedirs(a.out, exist_ok=True)
        path = os.path.join(a.out, "selftest.grb2")
        open(path, "wb").write(b"".join(msgs))
        print(f"selftest: {len(msgs)} campi ritagliati su {box}, {os.path.getsize(path) / 1e6:.2f} MB -> {path}")
        return 0
    ok = 0
    for model in a.models.split(","):
        try:
            name = model.strip()
            if name == "icon-2i":
                # 2,2 km: su tutta l'area sarebbero ~150 MB a corsa, quindi un riquadro proprio (Liguria-Malta, per la Middle Sea Race)
                ibox = tuple(float(x) for x in os.environ.get("ICON2I_BOX", "35,45,7,16.5").split(","))
                build_icon_2i(ibox, a.out)
            elif name == "youmeteo":
                # area propria, più piccola: è un modello fitto e i file devono restare leggeri
                ybox = tuple(float(x) for x in os.environ.get("YOUMETEO_BOX", "36,40,11,17").split(","))
                build_youmeteo(ybox, a.out)
            else:
                {"ecmwf": build_ecmwf, "ecmwf-0p1": build_ecmwf_0p1, "icon-eu": build_icon_eu, "gfs": build_gfs, "arome": build_arome, "arpege": build_arpege,
                 "ecmwf-wave": build_ecmwf_wave, "gfs-wave": build_gfs_wave, "cmems": build_cmems}[name](box, a.out)
            ok += 1
        except Exception as e:
            print(f"ATTENZIONE: {model} non riuscito: {e}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
