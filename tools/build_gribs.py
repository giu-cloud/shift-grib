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
  icon-2i-ruc  ICON-2I-RUC di ItaliaMeteo-Arpae (dati aperti MeteoHub), la stessa ICON-2I a ciclo rapido: corse ogni 3
           ore (00, 03, ..., 21), ognuna dall'analisi più fresca; stessi campi e stessa area di ICON-2I, 0-24 ore (stima).
           Ha un giro suo (.github/workflows/ruc.yml), un'ora e mezza dopo l'ora della corsa.
  gfs      GFS della NOAA (dati aperti), 0,25 gradi, vento a 10 m e pressione, ogni ora fino a 48 ore e poi ogni 3
           fino a 144. Si prende dall'archivio pubblico su Amazon, scaricando solo i campi che servono.

Archivio: ogni modello guarda le ultime corse disponibili (non solo l'ultima) e costruisce quelle che mancano.
L'elenco dei file già in archivio arriva nel file indicato dalla variabile GRIB_HAVE (un nome per riga).

  youmeteo HRRR Italia di youmeteo.com (circa 4 km), per abbonati: richiede le credenziali nei Secrets e un deposito privato.

Uso:  python tools/build_gribs.py --out gribs [--models ecmwf,ecmwf-0p1,icon-eu,icon-2i,icon-2i-ruc,gfs,arome,arpege,ecmwf-wave,gfs-wave,cmems,youmeteo] [--box latmin,latmax,lonmin,lonmax]
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

# Aree in più, solo per l'archivio del banco di prova (22/11/2026: Mediterraneo occidentale per Palermo-Barcellona e
# Palermo-Valencia). Si ritagliano dagli stessi dati già scaricati per l'area principale, quindi senza scaricare di più.
# GRIB_EXTRA: "nome=latmin,latmax,lonmin,lonmax;nome2=..."; i file si chiamano <modello>-<nome>_<corsa>.grb2, finiscono
# nella cartella GRIB_EXTRA_OUT e il workflow li carica solo nella release archivio-<nome>-AAAA-MM: niente gribs, niente
# quadratini, l'app non li vede.
def _parse_extra(text):
    out = []
    for part in (text or "").split(";"):
        if "=" in part:
            name, b = part.split("=", 1)
            out.append((name.strip(), tuple(float(x) for x in b.split(","))))
    return out


EXTRA = _parse_extra(os.environ.get("GRIB_EXTRA", ""))
EXTRA_MODELS = {m.strip() for m in os.environ.get("GRIB_EXTRA_MODELS", "ecmwf,ecmwf-0p1,icon-eu,gfs,arome,arpege").split(",") if m.strip()}
EXTRA_OUT = os.environ.get("GRIB_EXTRA_OUT", "extra")
_extra = {"on": False, "msgs": {}}


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


def crop_message(h, box, stride=1):
    """Ritaglia un campo su griglia regolare lat/lon. Restituisce un nuovo messaggio GRIB (bytes) o None.
    Con [stride] > 1 tiene un punto ogni [stride] per lato (maglia più larga, per il profilo verticale)."""
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
    if stride > 1:
        jj, ii = jj[::stride], ii[::stride]
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
        if stride > 1:
            ec.codes_set(c, "iDirectionIncrementInDegrees", di * stride)
            ec.codes_set(c, "jDirectionIncrementInDegrees", dj * stride)
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
            if _extra["on"]:
                for name, eb in EXTRA:
                    em = crop_message(h, eb)
                    if em:
                        _extra["msgs"].setdefault(name, []).append(em)
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
    """Le corse da costruire: quelle non ancora in archivio. Se sono tutte in archivio, nessuna. Se manca solo il file di
    un'area in più, si rifà soltanto la corsa più recente (le corse arrivano dalla più recente): così il primo giro dopo
    aver aggiunto un'area non riscarica tutte le corse vecchie."""
    got = have()
    names = [n for n, _ in EXTRA] if model in EXTRA_MODELS else []
    todo = [r for k, r in enumerate(runs) if f"{model}_{r:%Y%m%d%H}.{ext}" not in got
            or (k == 0 and any(f"{model}-{n}_{r:%Y%m%d%H}.{ext}" not in got for n in names))]
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
        _extra["on"] = bool(EXTRA) and model in EXTRA_MODELS
        _extra["msgs"] = {}
        try:
            made.append(one(run))
        except Exception as e:
            errors.append(f"{run:%Y-%m-%d %H}Z: {e}")
            print(f"ATTENZIONE: {model} corsa {run:%Y-%m-%d %H}Z non riuscita: {e}", file=sys.stderr)
        finally:
            _extra["on"] = False
            _extra["msgs"] = {}
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
METEOHUB = "https://meteohub.agenziaitaliameteo.it/nwp"


def build_icon_2i(box, out_dir):
    """ICON-2I di ItaliaMeteo-Arpae da MeteoHub (dati aperti, senza account). Un file per variabile e per corsa
    (~85 MB, tutte le ore da 0 a 72), griglia regolare 0,025 x 0,02 gradi su 33,7-48,9 N e 3-22 E."""
    return build_meteohub_icon("icon-2i", "ICON-2I_SURFACE_PRESSURE_LEVELS", "ICON_2I_SURFACE_PRESSURE_LEVELS",
                               hours=(0, 12), keep=3, min_hours=73, box=box, out_dir=out_dir)


# ICON-2I-RUC (24/11/2026): la stessa ICON-2I a ciclo rapido, una corsa ogni 3 ore, ognuna dall'analisi più fresca del
# ciclo di assimilazione. Durata stimata 0-24 ore (25 campi per variabile, dal peso del file): ICON2I_RUC_ORE la cambia.
# Il primo giro scrive nel registro le ore che trova davvero.
ICON2I_RUC_HOURS = int(os.environ.get("ICON2I_RUC_ORE", "25"))
# quante corse guardare all'indietro (di solito 4; di più per recuperare i giorni che MeteoHub tiene ancora, circa tre)
ICON2I_RUC_BACK = int(os.environ.get("ICON2I_RUC_INDIETRO", "4") or "4")


def build_icon_2i_ruc(box, out_dir):
    """ICON-2I-RUC di ItaliaMeteo-Arpae da MeteoHub (dati aperti, senza account): corse 00, 03, ..., 21, stessi campi e
    stesso ritaglio di ICON-2I. Si guardano le ultime 4 corse, così un giro saltato non lascia buchi nell'archivio."""
    return build_meteohub_icon("icon-2i-ruc", "ICON_2I_RUC", "ICON_2I_RUC", hours=tuple(range(0, 24, 3)), keep=ICON2I_RUC_BACK,
                               min_hours=ICON2I_RUC_HOURS, box=box, out_dir=out_dir)


def build_meteohub_icon(model, folder, prefix, hours, keep, min_hours, box, out_dir):
    """ICON-2I e ICON-2I-RUC da MeteoHub: <folder>/<corsa>/<VAR>/<prefix>_<corsa>_heightAboveGround-10.grib, un file per
    variabile con tutte le ore. La corsa è pronta quando i tre file sono nell'elenco delle cartelle; è completa quando
    vento u e v hanno almeno [min_hours] ore (la raffica può mancare all'ora zero)."""
    root = f"{METEOHUB}/{folder}"
    def url(t, var):
        return f"{root}/{t:%Y%m%d%H}/{var}/{prefix}_{t:%Y%m%d%H}_{ICON2I_FILES[var]}.grib"
    def ready(t):
        for var in ICON2I_FILES:
            page = http_get(f"{root}/{t:%Y%m%d%H}/{var}/", tries=1, timeout=30).decode("utf-8", "replace")
            if url(t, var).rsplit("/", 1)[1] not in page:
                return False
        return True
    runs = recent_runs(ready, hours=hours, n=keep, back_hours=max(54, 3 * keep + 12))
    def valid_at(m):                          # l'ora zero è in minuti ("0m"), le altre in ore: si ordina per validità
        h = ec.codes_new_from_message(m)
        try:
            return (int(ec.codes_get(h, "validityDate")), int(ec.codes_get(h, "validityTime")))
        finally:
            ec.codes_release(h)
    def lead_hours(msgs, run):
        out = []
        for m in msgs:
            d, t = valid_at(m)
            v = dt.datetime.strptime(f"{d:08d}{t:04d}", "%Y%m%d%H%M")
            out.append(round((v - run).total_seconds() / 3600))
        return sorted(out)
    def one(run):
        raws = fetch_all([lambda v=v: http_get(url(run, v), timeout=600) for v in ICON2I_FILES])
        msgs = []
        for var, raw in zip(ICON2I_FILES, raws):
            got = crop_bytes(raw, box)
            hrs = lead_hours(got, run)
            print(f"{model}: corsa {run:%Y-%m-%d %H}Z, {var}: {len(got)} campi, ore {hrs[0] if hrs else '-'}-{hrs[-1] if hrs else '-'}"
                  f", {len(raw) / 1e6:.1f} MB scaricati")
            if var != "VMAX_10M" and len(got) < min_hours:
                raise IOError(f"{var}: {len(got)} ore su {min_hours}, corsa incompleta")
            msgs += got
        msgs.sort(key=valid_at)               # in ordine di tempo, come gli altri modelli
        return write(out_dir, model, run, msgs)
    return build_all(model, runs, one)


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


# ------------------------------------------------------------------------------------------------- profilo verticale
# Per Lens nell'app (dalla 0.48): la stabilità dell'aria sopra i rilievi dal modello stesso, anche senza rete.
# File a parte nella release gribs, accanto al vento: profilo-<modello>_<corsa>.grb2, con temperatura (0,0,0), quota
# geopotenziale (0,3,5, gpm), vento u e v (0,2,2 e 0,2,3) sui livelli isobarici 1000, 925, 850, 700 hPa (ICON-EU anche
# 950 e 900) e la temperatura a 2 m. Maglia 0,25° (ICON-EU sfoltito da 0,0625°), passo di 3 ore fino a 72, poi di 6.
# Area: PROFILE_BOX (di default Mediterraneo centrale e occidentale, dove ci sono le aree di Lens).
PROFILE_BOX = tuple(float(x) for x in os.environ.get("PROFILE_BOX", "34,46,-2,20").split(","))
PROFILE_STEPS = list(range(0, 73, 3)) + list(range(78, 145, 6))
G0 = 9.80665


def build_profilo_ecmwf(box, out_dir):
    """ECMWF IFS 0,25°, dati aperti: t, gh, u, v a 1000-925-850-700 hPa e 2t, dagli stessi file del vento."""
    root = "https://data.ecmwf.int/forecasts"
    def name(t, step, ext):
        return f"{root}/{t:%Y%m%d}/{t:%H}z/ifs/0p25/oper/{t:%Y%m%d%H}0000-{step}h-oper-fc.{ext}"
    runs = recent_runs(lambda t: len(http_get(name(t, 144, "index"), tries=1, timeout=30)) > 100, hours=(0, 12))
    levels = {"1000", "925", "850", "700"}
    def one(run):
        def get_step(step):
            parts = []
            for line in http_get(name(run, step, "index")).decode().splitlines():
                rec = json.loads(line)
                pl = rec.get("levtype") == "pl" and rec.get("param") in ("t", "gh", "u", "v") and str(rec.get("levelist")) in levels
                sfc = rec.get("levtype", "sfc") == "sfc" and rec.get("param") == "2t"
                if pl or sfc:
                    a, n = int(rec["_offset"]), int(rec["_length"])
                    parts.append(http_get(name(run, step, "grib2"), headers={"Range": f"bytes={a}-{a + n - 1}"}))
            return b"".join(parts)
        msgs = []
        for k in range(0, len(PROFILE_STEPS), WORKERS):
            for raw in fetch_all([lambda s=s: get_step(s) for s in PROFILE_STEPS[k:k + WORKERS]]):
                msgs += crop_bytes(raw, PROFILE_BOX)
        return write(out_dir, "profilo-ecmwf", run, msgs)
    return build_all("profilo-ecmwf", runs, one)


def fi_to_gh(msg):
    """Geopotenziale (0,3,4, m2/s2, come lo dà il DWD) in quota geopotenziale (0,3,5, gpm), come la vuole l'app."""
    h = ec.codes_new_from_message(msg)
    try:
        if ec.codes_get(h, "parameterCategory") != 3 or ec.codes_get(h, "parameterNumber") != 4:
            return msg
        v = ec.codes_get_values(h)
        miss = ec.codes_get(h, "missingValue")
        ec.codes_set(h, "parameterNumber", 5)
        ec.codes_set_values(h, np.where(v == miss, miss, v / G0))
        return ec.codes_get_message(h)
    finally:
        ec.codes_release(h)


def build_profilo_icon_eu(box, out_dir):
    """ICON-EU (DWD, 0,0625° sfoltito a 0,25°): T, FI (in gh), U, V a 1000-950-925-900-850-700 hPa e T_2M."""
    root = "https://opendata.dwd.de/weather/nwp/icon-eu/grib"
    def pl(t, var, step, lev):
        return f"{root}/{t:%H}/{var.lower()}/icon-eu_europe_regular-lat-lon_pressure-level_{t:%Y%m%d%H}_{step:03d}_{lev}_{var}.grib2.bz2"
    def sl(t, var, step):
        return f"{root}/{t:%H}/{var.lower()}/icon-eu_europe_regular-lat-lon_single-level_{t:%Y%m%d%H}_{step:03d}_{var}.grib2.bz2"
    runs = recent_runs(lambda t: len(http_get(pl(t, "T", 120, 850), tries=1, timeout=30)) > 1000, hours=(0, 12))
    steps = [s for s in PROFILE_STEPS if s <= 120]
    levels = (1000, 950, 925, 900, 850, 700)
    def one(run):
        def get(url):
            try:
                return bz2.decompress(http_get(url, tries=2))
            except Exception as e:
                print(f"  profilo-icon-eu: manca {url.rsplit('/', 1)[-1]} ({e})", file=sys.stderr)
                return b""
        msgs, missing, total = [], 0, 0
        for k in range(0, len(steps), 2):           # due passi alla volta: un campo intero pesa ~2 MB
            jobs = [lambda u=pl(run, v, s, lev): get(u) for s in steps[k:k + 2] for lev in levels for v in ("T", "FI", "U", "V")]
            jobs += [lambda u=sl(run, "T_2M", s): get(u) for s in steps[k:k + 2]]
            for raw in fetch_all(jobs):
                total += 1
                missing += not raw
                msgs += profile_crop(raw)
        if missing > 0.3 * total:
            raise IOError(f"mancano {missing} file su {total}")
        return write(out_dir, "profilo-icon-eu", run, msgs)
    return build_all("profilo-icon-eu", runs, one)


def profile_crop(raw):
    out = []
    pos = 0
    while True:
        i = raw.find(b"GRIB", pos)
        if i < 0:
            break
        h = ec.codes_new_from_message(raw[i:])
        try:
            n = ec.codes_get(h, "totalLength")
            m = crop_message(h, PROFILE_BOX, stride=4)
        finally:
            ec.codes_release(h)
        if m:
            out.append(fi_to_gh(m))
        pos = i + max(int(n), 4)
    return out


def write(out_dir, model, run, msgs, ext="grb2"):
    if not msgs:
        raise IOError(f"{model}: nessun campo utilizzabile")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{model}_{run:%Y%m%d%H}.{ext}")
    with open(path, "wb") as f:
        for m in msgs:
            f.write(m)
    print(f"{model}: corsa {run:%Y-%m-%d %H}Z, {len(msgs)} campi, {os.path.getsize(path) / 1e6:.1f} MB -> {path}")
    if _extra["on"]:
        for name, ems in _extra["msgs"].items():
            if not ems:
                continue
            os.makedirs(EXTRA_OUT, exist_ok=True)
            ep = os.path.join(EXTRA_OUT, f"{model}-{name}_{run:%Y%m%d%H}.{ext}")
            with open(ep, "wb") as f:
                for m in ems:
                    f.write(m)
            print(f"{model}-{name}: corsa {run:%Y-%m-%d %H}Z, {len(ems)} campi, {os.path.getsize(ep) / 1e6:.1f} MB -> {ep}")
        _extra["msgs"] = {}
    return path


# ------------------------------------------------------------------------------------------------- quadratini
# Oltre al file intero, i modelli fitti si pubblicano tagliati a quadratini fissi, uguali per tutti i modelli, così
# l'app scarica solo quelli che toccano il riquadro tracciato sulla carta.
#   - lato 1 grado per i modelli fini (icon-2i, icon-2i-ruc, arome, youmeteo), 2 gradi per gli altri (icon-eu, arpege, ecmwf-0p1,
#     e cmems per stare sotto i 1.000 file della release); niente quadratini da 0,15 gradi di maglia in su;
#   - angoli sud-ovest su multipli interi del lato; un quadratino (LAT, LON) contiene i punti con
#     LAT <= lat < LAT+lato e LON <= lon < LON+lato, assegnati per indice: affiancati ridanno la griglia del modello;
#   - dentro, tutti i campi del file intero, nello stesso ordine e con la stessa compressione semplice;
#   - non si pubblicano i quadratini con meno di 2 punti per lato o tutti mancanti (per esempio il mare fuori da AROME);
#   - nome: <modello>_<AAAAMMGGHH>_t<lato>_<N|S><LL>_<E|W><LLL>.<ext>, e per ultimo <modello>_<AAAAMMGGHH>_tiles.ok.
# modelli a quadratini; la variabile TILE_MODELS li restringe (nel deposito privato di Shift: solo youmeteo)
TILE_MODELS = [m for m in os.environ.get("TILE_MODELS", "icon-2i,icon-2i-ruc,arome,youmeteo-96h,youmeteo-48h,cmems-cur,cmems-wav,icon-eu,ecmwf-0p1,arpege").split(",") if m]
TILE_RELEASE = "tiles"
TILE_SIDES = {"icon-2i": 1, "icon-2i-ruc": 1, "arome": 1, "youmeteo-96h": 1, "youmeteo-48h": 1,
              "icon-eu": 2, "arpege": 2, "ecmwf-0p1": 2, "cmems-cur": 2, "cmems-wav": 2}
# Una sola corsa per modello: con i lati di 1 e 2 gradi una corsa di tutti i modelli fa circa 810 file (con ICON-2I-RUC,
# dal 24/11/2026) e una release ne tiene al massimo 1.000. Si lavora un modello alla volta (quadratini, segnaposto, poi
# via la corsa vecchia). Per ICON-2I e ICON-2I-RUC (circa 200 file ciascuno) la corsa nuova accanto alla vecchia
# supererebbe il limite: lì si toglie prima la vecchia (vedi publish_tiles), e per un minuto il modello va col file intero.
TILE_KEEP_RUNS = 1
TILE_MAX_ASSETS = int(os.environ.get("TILE_MAX_ASSETS", "1000"))      # limite di GitHub per release (la variabile serve alle prove)
# Forma leggera (dal 18/11/2026): per questi modelli (GRIB2) i quadratini col nome di sempre portano solo il vento a 10 m
# (10u e 10v), in GRIB2 modello 5.3 (complex packing con differenze spaziali) a un decimo di m/s; tutto il resto
# (raffiche, pressione) va nei quadratini gemelli <modello>_<corsa>_t<lato>x_<N|S><LL>_<E|W><LLL>.<ext>, stessa griglia
# e stessa compressione. Gli altri (youmeteo GRIB1, cmems) restano come erano.
TILE_WIND_MODELS = {"icon-2i", "icon-2i-ruc", "arome", "icon-eu", "ecmwf-0p1", "arpege"}
TILE_DECIMALS = 1        # precisione di un decimo (m/s per il vento e le raffiche; Pa per la pressione)
TILE_WIND_TOL = 0.1      # scarto massimo ammesso dal file intero, nelle unità del campo
TILES_REDO = os.environ.get("TILES_REDO", "").lower() in ("1", "true", "si", "sì")   # rifà anche le corse già pubblicate
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


def tile_name(model, run, side, lat, lon, ext, extra=False):
    return (f"{model}_{run}_t{side}{'x' if extra else ''}_{'N' if lat >= 0 else 'S'}{abs(lat):02d}_"
            f"{'E' if lon >= 0 else 'W'}{abs(lon):03d}{ext}")


def is_wind_field(h):
    """Vento a 10 m (10u o 10v) di un messaggio GRIB2."""
    try:
        return (ec.codes_get(h, "edition") == 2 and ec.codes_get(h, "typeOfLevel") == "heightAboveGround"
                and ec.codes_get(h, "level") == 10 and ec.codes_get(h, "shortName") in ("10u", "10v"))
    except Exception:
        return False


TILE_STATS = {"messaggi": 0, "piu_fine": 0}


def tile_repack(msg, values, miss):
    """Da un messaggio in compressione semplice a GRIB2 5.3 (complex packing con differenze spaziali) a un decimo.
    L'ordine conta: prima la precisione, poi la compressione (al contrario eccodes dà campi costanti). La precisione di un
    decimo arrotonda due volte (al decimo, poi al passo binario di 1/8 o 1/16) e nel caso peggiore lo scarto supera
    TILE_WIND_TOL: allora si ricodifica con un decimale in più, finché lo scarto dal valore vero sta sotto la soglia."""
    TILE_STATS["messaggi"] += 1
    ok = ~miss
    for dec in range(TILE_DECIMALS, TILE_DECIMALS + 3):
        c = ec.codes_new_from_message(msg)
        try:
            ec.codes_set(c, "changeDecimalPrecision", dec)
            ec.codes_set(c, "packingType", "grid_complex_spatial_differencing")
            out = ec.codes_get_message(c)
        finally:
            ec.codes_release(c)
        d = ec.codes_new_from_message(out)
        try:
            back = ec.codes_get_values(d).reshape(values.shape)
        finally:
            ec.codes_release(d)
        err = float(np.max(np.abs(back[ok] - values[ok]))) if ok.any() else 0.0
        if err < TILE_WIND_TOL - 5e-4:           # un po' di margine: i lettori usano float a 32 bit
            break
    if dec > TILE_DECIMALS:
        TILE_STATS["piu_fine"] += 1
    return out


def tile_file(path, out_dir, legacy=False):
    """Taglia un file intero (<modello>_<corsa>.<ext>) in quadratini dentro out_dir.
    Restituisce (lato, [percorsi], note): lato None vuol dire niente quadratini per quel modello.
    Modelli di TILE_WIND_MODELS (GRIB2): quadratini del vento (10u, 10v) in 5.3 a un decimo, più i gemelli "x" con gli altri
    campi (raffiche, pressione) se il file intero ne ha. Gli altri modelli: tutti i campi, compressione semplice, come prima.
    legacy=True: la forma di prima per tutti (solo per misurare quanto pesava)."""
    stem, ext = os.path.splitext(os.path.basename(path))
    model, run = stem.rsplit("_", 1)
    split = model in TILE_WIND_MODELS and not legacy
    tiles = {}                     # (lat, lon, gruppo) -> [messaggi]; gruppo "w" (vento o tutto) o "x" (il resto)
    filled = {}                    # (lat, lon) -> almeno un punto del vento (o di tutto) non mancante
    side = None
    n_fields = {"w": 0, "x": 0}
    thin = set()
    with open(path, "rb") as f:
        while True:
            h = ec.codes_grib_new_from_file(f)
            if h is None:
                break
            try:
                g = tile_grid(h)
                s_ = tile_side(g["di"], model)
                n_all = n_fields["w"] + n_fields["x"]
                if n_all == 0:
                    side = s_
                    if side is None:
                        return None, [], f"maglia {g['di']:g} gradi: basta il file intero"
                elif s_ != side:
                    raise ValueError(f"campo {n_all + 1}: maglia diversa dal primo")
                if split and ec.codes_get(h, "edition") != 2:
                    raise ValueError("forma leggera solo per GRIB2")
                grp = "w" if (not split or is_wind_field(h)) else "x"
                n_fields[grp] += 1
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
                            if split and not miss.all():       # un campo tutto mancante resta in compressione semplice (eccodes non lo codifica)
                                msg = tile_repack(ec.codes_get_message(c), np.where(miss, missing, sub), miss)
                            else:
                                msg = ec.codes_get_message(c)
                            tiles.setdefault((lat, lon, grp), []).append(msg)
                        finally:
                            ec.codes_release(c)
                        if grp == "w" and not miss.all():
                            filled[(lat, lon)] = True
            finally:
                ec.codes_release(h)
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    empty = 0
    for (lat, lon, grp), msgs in sorted(tiles.items()):
        if not filled.get((lat, lon)):
            if grp == "w":
                empty += 1
            continue                                           # il gemello x esiste solo se esiste il quadratino del vento
        if len(msgs) != n_fields[grp]:
            raise ValueError(f"quadratino {lat},{lon} ({grp}): {len(msgs)} campi su {n_fields[grp]}")
        p = os.path.join(out_dir, tile_name(model, run, side, lat, lon, ext, extra=(grp == "x")))
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
    quadratini (come farà l'app) e confronta i valori punto per punto con il file intero.
    Forma leggera (TILE_WIND_MODELS): il quadratino del vento ha solo 10u e 10v in 5.3, il gemello x ha tutto il resto,
    scarto massimo ammesso TILE_WIND_TOL; stesse ore del gemello e stessi campi del file intero."""
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
                    step = 2.0 ** (e or 0) / 10.0 ** (d or 0)                     # passo della compressione
                    fields.append({"key": key, "g": g, "v": v, "m": m, "step": step, "wind": is_wind_field(h),
                                   "packing": get("packingType"), "tpl": get("dataRepresentationTemplateNumber"), "ed": get("edition")})
                finally:
                    ec.codes_release(h)
        return fields
    stem = os.path.splitext(os.path.basename(path))[0]
    model = stem.rsplit("_", 1)[0]
    whole = load(path)
    g0 = whole[0]["g"]
    split = model in TILE_WIND_MODELS and whole[0]["ed"] == 2
    whole_grp = {"w": [f for f in whole if f["wind"] or not split], "x": [f for f in whole if split and not f["wind"]]}
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as tmp_old:
        side, paths, note = tile_file(path, tmp)
        if side is None:
            print(f"{os.path.basename(path)}: {note}")
            return True
        old_sizes = [os.path.getsize(p) for p in tile_file(path, tmp_old, legacy=True)[1]] if split else []
        got = {grp: [np.full((g0["nj"], g0["ni"]), np.nan) for _ in fl] for grp, fl in whole_grp.items()}
        gotm = {grp: [np.zeros((g0["nj"], g0["ni"]), bool) for _ in fl] for grp, fl in whole_grp.items()}
        cover = np.zeros((g0["nj"], g0["ni"]), int)
        sizes = {"w": [], "x": []}
        names = {"w": set(), "x": set()}
        for p in paths:
            grp = "x" if re.search(r"_t\d+x_", os.path.basename(p)) else "w"
            assert split or grp == "w", f"{p}: quadratino x per un modello senza forma leggera"
            sizes[grp].append(os.path.getsize(p))
            names[grp].add(re.sub(r"_t(\d+)x?_", "_t_", os.path.basename(p)))
            part = load(p)
            ref = whole_grp[grp]
            assert len(part) == len(ref), f"{p}: {len(part)} campi invece di {len(ref)}"
            for k, pf in enumerate(part):
                assert pf["key"] == ref[k]["key"], f"{p}: campo {k + 1} è {pf['key']}, nel file intero {ref[k]['key']}"
                if split:
                    assert (pf["packing"] == "grid_complex_spatial_differencing" and pf["tpl"] == 3) or pf["m"].all(), \
                        f"{p}: non è GRIB2 5.3 ({pf['packing']})"
                g = pf["g"]
                j0 = int(round((g["lat_s"] - g0["lat_s"]) / g0["dlat"]))
                i0 = int(round((g["lon_w"] - g0["lon_w"]) / g0["dlon"]))
                assert 0 <= j0 and j0 + g["nj"] <= g0["nj"] and 0 <= i0 and i0 + g["ni"] <= g0["ni"], f"{p}: fuori dalla griglia"
                if k == 0 and grp == "w":
                    cover[j0:j0 + g["nj"], i0:i0 + g["ni"]] += 1
                got[grp][k][j0:j0 + g["nj"], i0:i0 + g["ni"]] = pf["v"]
                gotm[grp][k][j0:j0 + g["nj"], i0:i0 + g["ni"]] = pf["m"]
        assert cover.max() <= 1, "due quadratini hanno punti in comune"
        if split:
            # ogni quadratino del vento ha il suo gemello x (se il file intero ha campi oltre al vento) e viceversa
            if whole_grp["x"]:
                assert names["x"] == names["w"], f"gemelli: {len(names['w'])} del vento, {len(names['x'])} x, non coincidono"
            else:
                assert not names["x"], "quadratini x ma niente da metterci"
        holes = cover == 0
        res = {}
        for grp, fl in whole_grp.items():
            worst = ratio = 0.0
            for k, f in enumerate(fl):
                c = ~holes
                assert np.array_equal(gotm[grp][k][c], f["m"][c]), f"campo {k + 1} ({grp}): punti mancanti diversi"
                ok = c & ~f["m"]
                if ok.any():
                    d = float(np.max(np.abs(got[grp][k][ok] - f["v"][ok])))
                    worst = max(worst, d)
                    ratio = max(ratio, d / f["step"])
            res[grp] = (worst, ratio)
        lost_missing = int(sum((holes & f["m"]).sum() for f in whole) // len(whole))
        lost_edge = int(holes.sum()) - lost_missing
        head = (f"{os.path.basename(path)}: lato {side} gradi, {len(names['w'])} quadratini, {len(whole)} campi x "
                f"{g0['nj']}x{g0['ni']} punti; sovrapposizioni 0; "
                f"punti non coperti {int(holes.sum())} (tutti mancanti nel file intero: {lost_missing}, bordo sottile: {lost_edge})")
        if not split:
            print(head + f"; differenza massima {res['w'][0]:.2g} ({res['w'][1]:.1e} del passo di compressione); "
                  f"peso {min(sizes['w']) / 1e6:.2f}-{max(sizes['w']) / 1e6:.2f} MB, totale {sum(sizes['w']) / 1e6:.1f} MB"
                  + (f"; {note}" if note else ""))
            # Ogni quadratino ha il suo valore di riferimento, scritto in 32 bit: quando non è rappresentabile esatto
            # i valori si spostano di un'unità dell'ultima cifra (circa un milionesimo per il vento), molto sotto il passo.
            return res["w"][1] <= 1e-3
        def hours(fl):
            out = {}
            for f in fl:
                out[f["key"][:4]] = out.get(f["key"][:4], 0) + 1
            return out
        hw, hx = hours(whole_grp["w"]), hours(whole_grp["x"])
        same_hours = all(n == next(iter(hw.values())) for n in hx.values())
        line = (f"; PRIMA {len(old_sizes)} file, max {max(old_sizes) / 1e3:.0f} KB, totale {sum(old_sizes) / 1e6:.1f} MB"
                f"; DOPO vento {len(sizes['w'])} file, {min(sizes['w']) / 1e3:.0f}-{max(sizes['w']) / 1e3:.0f} KB, totale {sum(sizes['w']) / 1e6:.1f} MB"
                f", x {len(sizes['x'])} file" + (f", {min(sizes['x']) / 1e3:.0f}-{max(sizes['x']) / 1e3:.0f} KB, totale {sum(sizes['x']) / 1e6:.1f} MB" if sizes["x"] else "")
                + f"; scarto massimo vento {res['w'][0]:.3f}" + (f", x {res['x'][0]:.3f}" if whole_grp["x"] else "")
                + f"; ore: vento {next(iter(hw.values()))}" + (f", x {sorted(hx.values())}" + ("" if same_hours else " (come nel file intero)") if hx else "")
                + (f"; {note}" if note else ""))
        print(head + line)
        return res["w"][0] <= TILE_WIND_TOL and (not whole_grp["x"] or res["x"][0] <= TILE_WIND_TOL)


def synthetic_files(out_dir):
    """File finti per la prova senza rete: GRIB2 da nord a sud con punti mancanti, meridiano zero e raffiche (come ICON-2I),
    GRIB2 da sud a nord a 0,0625 gradi con punti esattamente sui bordi, raffiche e pressione (come ECMWF 0,1),
    GRIB2 solo vento (come ARPEGE), GRIB1 a 0,055 gradi come youmeteo."""
    os.makedirs(out_dir, exist_ok=True)
    rng = np.random.default_rng(7)
    cases = [("icon-2i_2026010100.grb2", "regular_ll_sfc_grib2", 39.1, 35.95, -1.3, 3.1, 0.025, 0.05, True, ("10fg",)),
             ("ecmwf-0p1_2026010100.grb2", "regular_ll_sfc_grib2", 34.0, 46.0, 5.0, 20.0, 0.0625, 0.0625, False, ("10fg", "msl")),
             ("arpege_2026010100.grb2", "regular_ll_sfc_grib2", 36.0, 44.0, 6.0, 15.0, 0.1, 0.1, True, ()),
             ("sint-c_2026010100.grb", "regular_ll_sfc_grib1", 36.035, 45.99, 6.025, 16.97, 0.055, 0.055, True, ())]
    out = []
    for name, sample, la1, la2, lo1, lo2, di, dj, holes, extras in cases:
        ni, nj = int(round((lo2 - lo1) / di)) + 1, int(round(abs(la2 - la1) / dj)) + 1
        with open(os.path.join(out_dir, name), "wb") as f:
            for step in (0, 3, 6):
                for par in ("10u", "10v") + tuple(extras):
                    h = ec.codes_grib_new_from_samples(sample)
                    try:
                        ec.codes_set(h, "Ni", ni); ec.codes_set(h, "Nj", nj)
                        ec.codes_set(h, "jScansPositively", 1 if la2 > la1 else 0)
                        ec.codes_set(h, "latitudeOfFirstGridPointInDegrees", la1); ec.codes_set(h, "latitudeOfLastGridPointInDegrees", la2)
                        ec.codes_set(h, "longitudeOfFirstGridPointInDegrees", lo1 % 360.0); ec.codes_set(h, "longitudeOfLastGridPointInDegrees", lo2 % 360.0)
                        ec.codes_set(h, "iDirectionIncrementInDegrees", di); ec.codes_set(h, "jDirectionIncrementInDegrees", dj)
                        ec.codes_set(h, "shortName", par)
                        if par == "msl":
                            ec.codes_set(h, "typeOfLevel", "meanSea")
                        else:
                            ec.codes_set(h, "typeOfLevel", "heightAboveGround"); ec.codes_set(h, "level", 10)
                        ec.codes_set(h, "stepRange", str(step))
                        ec.codes_set(h, "packingType", "grid_simple"); ec.codes_set(h, "bitsPerValue", 12)
                        v = rng.normal(0, 8, (nj, ni)) if par in ("10u", "10v") else (
                            rng.normal(12, 4, (nj, ni)) if par == "10fg" else 101325 + rng.normal(0, 300, (nj, ni)))
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


TILE_RE = re.compile(r"^(?P<model>.+)_(?P<run>\d{10})_(?:t\d+x?_[NS]\d{2}_[EW]\d{3}\.\w+|tiles\.ok)$")
WHOLE_RE = re.compile(r"^(?P<model>.+)_(?P<run>\d{10})\.(?P<ext>grb2?|grb)$")


TILE_SIDE_RE = re.compile(r"_t(\d+)x?_[NS]\d{2}_[EW]\d{3}\.\w+$")
TILE_EXTRA_RE = re.compile(r"_t\d+x_[NS]\d{2}_[EW]\d{3}\.\w+$")      # il gemello con raffiche e pressione


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
        if f"{model}_{run}_tiles.ok" in tiles and sides_have == {side_now} and not TILES_REDO:
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
                # Non c'è posto per la corsa nuova accanto alla vecchia dello stesso modello (succede nel deposito privato, che
                # tiene anche youmeteo e le correnti): si toglie prima la corsa vecchia, segnaposto per primo. Fino al
                # segnaposto nuovo (circa un minuto) il modello non ha una corsa completa: l'app usa i file interi.
                ro = runs_of(model)
                gone = [n for r_, names in ro.items() if r_ != run for n in sorted(names, key=lambda x: not x.endswith("_tiles.ok"))]
                if gone and len(tiles) - len(gone) + len(paths) + 1 <= TILE_MAX_ASSETS:
                    print(f"{model}: {len(tiles)} file nella release più {len(paths)} supererebbero {TILE_MAX_ASSETS}: tolgo prima i {len(gone)} della corsa vecchia")
                    delete(gone)
                    # l'indice non deve elencare i file appena tolti: lo si riscrive subito (il modello sparisce dall'indice
                    # finché non c'è la corsa nuova completa, e l'app usa il file intero)
                    publish_index(TILE_RELEASE)
                else:
                    print(f"ATTENZIONE: {model}: {len(tiles)} file nella release più {len(paths)} supererebbero {TILE_MAX_ASSETS}, salto", file=sys.stderr)
                    continue
            sizes = [os.path.getsize(x) for x in paths]
            wind = [x for x in paths if not TILE_EXTRA_RE.search(x)]
            extra = [x for x in paths if TILE_EXTRA_RE.search(x)]
            cut = time.time() - t0
            t1 = time.time()
            ok_name = f"{model}_{run}_tiles.ok"
            if ok_name in tiles:                              # corsa già completa da rifare: finché si carica non è più completa
                delete([ok_name])
            r = gh("release", "upload", TILE_RELEASE, *paths, "--clobber", check=False)       # un solo comando: vento e gemelli x insieme
            if r.returncode:
                print(f"ATTENZIONE: {model} {run}: caricamento non riuscito, niente segnaposto: {r.stderr.strip()[:300]}", file=sys.stderr)
                continue
            # prima del segnaposto: sono saliti tutti, con il peso giusto, i quadratini del vento e i gemelli x?
            now = release_assets(repo, TILE_RELEASE) or {}
            missing = [os.path.basename(x) for x in paths if os.path.basename(x) not in now or now[os.path.basename(x)][1] != os.path.getsize(x)]
            if missing:
                print(f"ATTENZIONE: {model} {run}: {len(missing)} file mancanti o di peso diverso nella release (per esempio {missing[0]}), niente segnaposto", file=sys.stderr)
                tiles = now
                continue
            tiles = now
            peak = max(peak, len(tiles) + 1)
            ok = os.path.join(tiles_dir, ok_name)
            with open(ok, "w") as f:                          # come sempre: prima riga il numero, poi i nomi (dei quadratini del vento)
                f.write(f"{len(wind)}\n" + "".join(os.path.basename(x) + "\n" for x in wind))
            r = gh("release", "upload", TILE_RELEASE, ok, "--clobber", check=False)          # per ultimo: la corsa è completa
            if r.returncode:
                print(f"ATTENZIONE: {model} {run}: segnaposto non caricato: {r.stderr.strip()[:300]}", file=sys.stderr)
                continue
            sw = [os.path.getsize(x) for x in wind]
            sx = [os.path.getsize(x) for x in extra]
            print(f"{model} {run}: lato {side}, {len(wind)} quadratini" + (f" + {len(extra)} gemelli x" if extra else "")
                  + f", {min(sw) / 1e3:.0f}-{max(sw) / 1e3:.0f} KB (vento), totale {sum(sw) / 1e6:.1f} MB"
                  + (f", x {sum(sx) / 1e6:.1f} MB" if extra else "")
                  + f"; taglio {cut:.0f} s, caricamento {time.time() - t1:.0f} s" + (f" ({note})" if note else ""))
            report.append((model, run, side, len(wind), len(extra), min(sw), max(sw), sum(sw), sum(sx), note))
            tiles = release_assets(repo, TILE_RELEASE) or {}          # id veri dei file appena caricati e del segnaposto
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
            f.write("### Quadratini\n\n| modello | corsa | lato | quadratini (vento) | gemelli x | min KB | max KB | vento MB | x MB | note |\n|---|---|---|---|---|---|---|---|---|---|\n")
            for model, run, side, n, nx, mn, mx, tot, totx, note in report:
                f.write(f"| {model} | {run} | {side}° | {n} | {nx} | {mn / 1e3:.0f} | {mx / 1e3:.0f} | {tot / 1e6:.1f} | {totx / 1e6:.1f} | {note} |\n")
    return 0


def publish_index(tag):
    """Scrive e carica <tag>/indice.json: l'elenco di ciò che la release contiene, da scaricare con un solo download
    (https://github.com/<deposito>/releases/download/<tag>/indice.json), senza usare l'API di GitHub.
    gribs: i file interi per modello, dal più recente. tiles: per modello la corsa completa, il lato e i quadratini."""
    repo = os.environ["GITHUB_REPOSITORY"]
    assets = release_assets(repo, tag)
    if assets is None:
        return 0
    idx = {"deposito": repo, "release": tag, "aggiornato": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
           "scarica": f"https://github.com/{repo}/releases/download/{tag}/<nome>", "modelli": {}}
    if tag == TILE_RELEASE:
        runs = {}
        for name in assets:
            m = TILE_RE.match(name)
            if m:
                runs.setdefault(m["model"], {}).setdefault(m["run"], []).append(name)
        for model, rs in sorted(runs.items()):
            complete = sorted((r for r, names in rs.items() if f"{model}_{r}_tiles.ok" in names), reverse=True)
            if not complete:
                continue
            run = complete[0]
            tiles = sorted(n for n in rs[run] if not n.endswith("_tiles.ok"))
            side = TILE_SIDE_RE.search(tiles[0])[1] if tiles else None
            idx["modelli"][model] = {"corsa": run, "lato": int(side) if side else None, "segnaposto": f"{model}_{run}_tiles.ok",
                                     "quadratini": [{"nome": n, "byte": assets[n][1]} for n in tiles]}
    else:
        for name, (_, size) in assets.items():
            m = WHOLE_RE.match(name)
            if m:
                idx["modelli"].setdefault(m["model"], []).append({"corsa": m["run"], "nome": name, "byte": size})
        for v in idx["modelli"].values():
            v.sort(key=lambda x: x["corsa"], reverse=True)
    path = os.path.join("/tmp", f"indice-{tag}", "indice.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(idx, f, ensure_ascii=False, indent=1)
    gh("release", "upload", tag, path, "--clobber")
    print(f"{tag}/indice.json: {len(idx['modelli'])} modelli")
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
    ap.add_argument("--index", metavar="RELEASE", help="scrive e carica l'indice.json di una release (gribs o tiles)")
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
        r = publish_tiles(a.out, a.tiles_out)
        publish_index(TILE_RELEASE)
        return r
    if a.index:
        return publish_index(a.index)
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
            elif name == "icon-2i-ruc":
                # stesso riquadro di ICON-2I, se non se ne dà uno proprio
                ibox = tuple(float(x) for x in os.environ.get("ICON2I_RUC_BOX", os.environ.get("ICON2I_BOX", "35,45,7,16.5")).split(","))
                build_icon_2i_ruc(ibox, a.out)
            elif name == "youmeteo":
                # area propria, più piccola: è un modello fitto e i file devono restare leggeri
                ybox = tuple(float(x) for x in os.environ.get("YOUMETEO_BOX", "36,40,11,17").split(","))
                build_youmeteo(ybox, a.out)
            else:
                {"ecmwf": build_ecmwf, "ecmwf-0p1": build_ecmwf_0p1, "icon-eu": build_icon_eu, "gfs": build_gfs, "arome": build_arome, "arpege": build_arpege,
                 "ecmwf-wave": build_ecmwf_wave, "gfs-wave": build_gfs_wave, "cmems": build_cmems,
                 "profilo-ecmwf": build_profilo_ecmwf, "profilo-icon-eu": build_profilo_icon_eu}[name](box, a.out)
            ok += 1
        except Exception as e:
            print(f"ATTENZIONE: {model} non riuscito: {e}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
