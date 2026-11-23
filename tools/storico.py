#!/usr/bin/env python3
"""
Archivio storico e misure per il banco di prova (23/11/2026). Richieste B1, B3 e B5 di
banco_di_prova/richiesta_app_23_11_sera.md (Project "Software regatta").

  --ecmwf DA A   corse 00 e 12 di ECMWF IFS a 0,25 gradi dall'archivio aperto su Amazon (bucket ecmwf-forecasts, licenza
                 CC-BY-4.0): vento a 10 m e pressione, ogni 3 ore fino a 144. File ecmwf_AAAAMMGGHH.grb2, uguali a quelli
                 dell'archivio vivo, nella release archivio-AAAA-MM.
  --gfs DA A     corse 00 e 12 di GFS a 0,25 gradi (bucket noaa-gfs-bdp-pds): vento a 10 m e pressione, ogni ora fino a 48 e
                 poi ogni 3 fino a 144, come l'archivio vivo. File gfs_AAAAMMGGHH.grb2, release archivio-AAAA-MM.
  --ens DA A     insieme di ECMWF (enfo, CC-BY-4.0): controllo e 50 membri, vento a 10 m, ogni 6 ore fino a 144, corse 00
                 e 12. Un file per corsa con tutti i membri (il membro è nel campo perturbationNumber, 0 = controllo):
                 ens_AAAAMMGGHH.grb2, release archivio-ens-AAAA-MM. Da Amazon; per gli ultimi giorni anche da
                 data.ecmwf.int.
  --metar DA A   METAR orari (direzione, vento medio, raffica) degli aeroporti nelle aree di Lens e lungo i percorsi del
                 banco, dall'archivio dell'Iowa Environmental Mesonet (Iowa State University). Un CSV per mese:
                 metar_AAAA-MM.csv, release archivio-misure. Ogni giro riscrive i mesi toccati.
  --boe DA A     anemometri di boe e piattaforme del Mediterraneo (Copernicus Marine, prodotto in situ NRT del
                 Mediterraneo; account nei segreti CMEMS_USER e CMEMS_PASS). Un CSV per mese: boe_AAAA-MM.csv, release
                 archivio-misure.
  --yb CODICI    tracciati YB di regate passate (B4), codici separati da virgole (per esempio rmsr2025,pm2025): il KML con
                 tutti i tracciati (https://yb.tl/<codice>.kml) e la classifica in testo, nella release archivio-misure.
  --prova        stampa che cosa c'è sulle fonti (cartelle di Amazon, file delle boe) senza scaricare dati.
  --era5 DA A    rianalisi ERA5 (ECMWF/Copernicus, CC-BY-4.0) dall'archivio NSF NCAR (d633000) col servizio di ritaglio del
                 suo THREDDS, senza account: vento a 10 m orario a 0,25°. Un file per giorno, era5_AAAAMMGG00.grb2 con i
                 passi 0-23, nella release archivio-AAAA-MM (regate vere, 23/11 notte).
  --gfs, prima di marzo 2021 (Amazon comincia lì): dall'archivio NSF NCAR di GFS 0,25° (d084001), stesso ritaglio THREDDS,
                 ogni 3 ore fino a 144.
  --ybprova CODICI  prova quali codici YB esistono (classifica in testo): una riga per codice in yb_prova_<ora>.tsv,
                 release archivio-misure, con le classi trovate.

Date nel formato AAAA-MM-GG, estremi compresi. --have FILE: nomi già pubblicati, uno per riga (non si rifanno).
Ogni file si pubblica appena pronto (gh release upload), così un lavoro interrotto non perde quello che ha fatto.
"""
import argparse, csv, datetime as dt, io, json, os, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_gribs as bg

ECMWF_AWS = "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com"
# Copie dei dati aperti di ECMWF: Amazon (regionale e globale), Azure (Planetary Computer), Google Cloud. Si usa la prima
# che risponde (23/11: Amazon regionale ha dato HTTP 503 su tutto).
# Prova del 23/11: Google risponde sempre, Amazon a tratti (503), Azure no (409), data.ecmwf.int solo gli ultimi giorni.
ECMWF_ROOTS = ["https://storage.googleapis.com/ecmwf-open-data", "https://ecmwf-forecasts.s3.amazonaws.com", ECMWF_AWS]
_root = {"ok": None}
ECMWF_LIVE = "https://data.ecmwf.int/forecasts"
GFS_AWS = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
GEFS_AWS = "https://noaa-gefs-pds.s3.amazonaws.com"
RDA = "https://thredds.rda.ucar.edu/thredds/ncss/grid/files/g"
BOX = [float(x) for x in os.environ.get("GRIB_BOX", "34,46,5,20").split(",")]

# Aeroporti (ICAO): le aree di Lens della richiesta B1, più Sardegna, Corsica e Calabria per i percorsi con rilievo forte.
METAR_STATIONS = {
    "Sicilia e Malta": ["LICT", "LICJ", "LMML", "LICD", "LICG", "LICC", "LICR"],
    "Italia settentrionale": ["LIRJ", "LIMJ", "LIPZ"],
    "Sardegna e Corsica": ["LIEO", "LIEA", "LIEE", "LFKF", "LFKJ", "LFKB"],
    "Baleari": ["LEPA", "LEMH", "LEIB"],
    "Grecia": ["LGAV", "LGSO", "LGPA", "LGNX", "LGKF", "LGPZ"],
    "Canarie": ["GCLP", "GCXO"],
    "Croazia": ["LDSP", "LDZD", "LDRI"],
}


def days(a, b):
    d0, d1 = dt.date.fromisoformat(a), dt.date.fromisoformat(b)
    while d0 <= d1:
        yield d0
        d0 += dt.timedelta(days=1)


def gh(*args, check=True):
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


_made = set()


def publish(path, tag, title, notes):
    if os.environ.get("STORICO_NO_UPLOAD"):
        print(f"(niente pubblicazione) {path} -> {tag}")
        return
    if tag not in _made:
        if subprocess.run(["gh", "release", "view", tag], capture_output=True).returncode != 0:
            gh("release", "create", tag, "--latest=false", "--title", title, "--notes", notes)
        _made.add(tag)
    # Il gettone del lavoro ha un limite di richieste all'ora (23/11: superato con molti lavori in parallelo): si aspetta
    # fino a un'ora intera prima di arrendersi.
    waited = 0
    for k in range(40):
        r = subprocess.run(["gh", "release", "upload", tag, path, "--clobber"], capture_output=True, text=True)
        if r.returncode == 0:
            return
        if "rate limit" in r.stderr.lower() and waited < 3900:
            print(f"limite di richieste di GitHub: aspetto 5 minuti ({path})"); time.sleep(300); waited += 300
            continue
        if k >= 3:
            break
        time.sleep(5 + 10 * k)
    raise RuntimeError(f"upload {path}: {r.stderr.strip()}")


NOTE_GRIB = ("File per il banco di prova di Shift: campi ritagliati sul Mediterraneo centrale. ECMWF: dati aperti, licenza "
             "CC-BY-4.0, copyright ECMWF. GFS: NOAA, pubblico dominio.")


def ecmwf_paths(run):
    """Le cartelle dei dati aperti ECMWF secondo la data: dal 1/2/2024 a 0,25° (con 'ifs/' da marzo 2024), prima a
    0,4° (0p4-beta, sulle copie dal gennaio 2023). Si provano in quest'ordine."""
    if run >= dt.datetime(2024, 2, 1):
        return ["ifs/0p25", "0p25"]
    return ["0p4-beta", "ifs/0p4-beta"]


def ecmwf_step(root, run, step, stream="oper", typ="fc", wanted=("10u", "10v", "msl")):
    last = None
    for sub in ecmwf_paths(run):
        base = f"{root}/{run:%Y%m%d}/{run:%H}z/{sub}/{stream}/{run:%Y%m%d%H}0000-{step}h-{stream}-{typ}"
        try:
            index = bg.http_get(base + ".index").decode()
            break
        except IOError as e:
            last = e
    else:
        raise last
    parts = []
    for line in index.splitlines():
        rec = json.loads(line)
        if rec.get("param") in wanted and rec.get("levtype", "sfc") == "sfc":
            a, n = int(rec["_offset"]), int(rec["_length"])
            parts.append(bg.http_get(base + ".grib2", headers={"Range": f"bytes={a}-{a + n - 1}"}))
    return b"".join(parts)


def ecmwf_any(run, step, **kw):
    roots = ([_root["ok"]] if _root["ok"] else []) + [r for r in ECMWF_ROOTS + [ECMWF_LIVE] if r != _root["ok"]]
    last = None
    for root in roots:
        try:
            data = ecmwf_step(root, run, step, **kw)
            _root["ok"] = root
            return data
        except IOError as e:
            last = e
    raise last


def one_ecmwf(run, out):
    steps = list(range(0, 145, 3))
    msgs = []
    ecmwf_any(run, 0)                              # sceglie la copia che risponde prima di partire in parallelo
    for k in range(0, len(steps), bg.WORKERS):
        for raw in bg.fetch_all([lambda s=s: ecmwf_any(run, s) for s in steps[k:k + bg.WORKERS]]):
            msgs += bg.crop_bytes(raw, BOX)
    return bg.write(out, "ecmwf", run, msgs)


def uv_grib(lat, lon, u, v, ref, step, kind="fc"):
    """Due messaggi GRIB2 (10u e 10v) su griglia regolare da matrici [lat, lon] (lat dall'alto o dal basso)."""
    import numpy as np
    lat = np.asarray(lat, dtype=float); lon = np.asarray(lon, dtype=float)
    lon = ((lon + 180.0) % 360.0) - 180.0
    oi = np.argsort(lon, kind="stable"); lon = lon[oi]; u = np.asarray(u)[:, oi]; v = np.asarray(v)[:, oi]
    if lat[0] < lat[-1]:
        lat = lat[::-1]; u = u[::-1]; v = v[::-1]
    out = []
    for name, f in (("10u", u), ("10v", v)):
        h = bg.ec.codes_grib_new_from_samples("regular_ll_sfc_grib2")
        try:
            bg.ec.codes_set(h, "centre", 98)
            bg.ec.codes_set(h, "dataDate", int(ref.strftime("%Y%m%d"))); bg.ec.codes_set(h, "dataTime", int(ref.strftime("%H%M")))
            bg.ec.codes_set(h, "typeOfFirstFixedSurface", 103); bg.ec.codes_set(h, "scaleFactorOfFirstFixedSurface", 0)
            bg.ec.codes_set(h, "scaledValueOfFirstFixedSurface", 10)
            bg.ec.codes_set(h, "discipline", 0); bg.ec.codes_set(h, "parameterCategory", 2)
            bg.ec.codes_set(h, "parameterNumber", 2 if name == "10u" else 3)
            bg.ec.codes_set(h, "stepUnits", 1); bg.ec.codes_set(h, "forecastTime", int(step))
            bg.ec.codes_set(h, "Ni", len(lon)); bg.ec.codes_set(h, "Nj", len(lat))
            bg.ec.codes_set(h, "latitudeOfFirstGridPointInDegrees", float(lat[0])); bg.ec.codes_set(h, "latitudeOfLastGridPointInDegrees", float(lat[-1]))
            bg.ec.codes_set(h, "longitudeOfFirstGridPointInDegrees", float(lon[0] % 360.0)); bg.ec.codes_set(h, "longitudeOfLastGridPointInDegrees", float(lon[-1] % 360.0))
            bg.ec.codes_set(h, "iDirectionIncrementInDegrees", float(abs(lon[1] - lon[0]))); bg.ec.codes_set(h, "jDirectionIncrementInDegrees", float(abs(lat[1] - lat[0])))
            bg.ec.codes_set(h, "jScansPositively", 0)
            bg.ec.codes_set(h, "packingType", "grid_simple"); bg.ec.codes_set(h, "bitsPerValue", 12)
            bg.ec.codes_set_values(h, np.asarray(f, dtype=float).flatten())
            out.append(bg.ec.codes_get_message(h))
        finally:
            bg.ec.codes_release(h)
    return out


def ncss(url, tries=4):
    """Una richiesta al servizio di ritaglio THREDDS: il netCDF in memoria."""
    import netCDF4
    last = None
    for k in range(tries):
        try:
            data = bg.http_get(url, tries=1, timeout=300)
            return netCDF4.Dataset("x", memory=data)
        except Exception as e:
            last = e; time.sleep(10 * (k + 1))
    raise IOError(f"{url}: {last}")


def box_q():
    return f"north={BOX[1]}&south={BOX[0]}&west={BOX[2]}&east={BOX[3]}&horizStride=1"


def one_gfs_rda(run, out):
    """GFS 0,25° dall'archivio NSF NCAR (d084001), per le corse prima di Amazon: OPeNDAP (dodsC) del THREDDS, che legge
    solo il riquadro (il ritaglio NCSS dei GRIB risponde 503; i file interi pesano ~330 MB a passo)."""
    import numpy as np, netCDF4
    la_all = 90.0 - 0.25 * np.arange(721); lo_all = 0.25 * np.arange(1440)
    j0, j1 = int(round((90.0 - BOX[1]) / 0.25)), int(round((90.0 - BOX[0]) / 0.25))
    lons = (BOX[2] % 360.0, BOX[3] % 360.0)
    def get_step(step):
        url = f"https://thredds.rda.ucar.edu/thredds/dodsC/files/g/d084001/{run:%Y}/{run:%Y%m%d}/gfs.0p25.{run:%Y%m%d%H}.f{step:03d}.grib2"
        last = None
        for k in range(5):
            try:
                ds = netCDF4.Dataset(url)
                try:
                    res = {}
                    for name in ("u-component_of_wind_height_above_ground", "v-component_of_wind_height_above_ground"):
                        var = ds[name]
                        hz = var.dimensions[1]
                        hv = np.asarray(ds[hz][:]).astype(float)
                        kz = int(np.argmin(np.abs(hv - 10.0)))
                        if lons[0] <= lons[1]:
                            i0, i1 = int(round(lons[0] / 0.25)), int(round(lons[1] / 0.25))
                            a = np.asarray(var[0, kz, j0:j1 + 1, i0:i1 + 1]); lo = lo_all[i0:i1 + 1]
                        else:   # il riquadro passa per il meridiano 0
                            i0, i1 = int(round(lons[0] / 0.25)), int(round(lons[1] / 0.25))
                            a = np.concatenate([np.asarray(var[0, kz, j0:j1 + 1, i0:]), np.asarray(var[0, kz, j0:j1 + 1, :i1 + 1])], axis=1)
                            lo = np.concatenate([lo_all[i0:], lo_all[:i1 + 1]])
                        res[name] = (a, lo)
                finally:
                    ds.close()
                u, lo = res["u-component_of_wind_height_above_ground"]; v, _ = res["v-component_of_wind_height_above_ground"]
                return uv_grib(la_all[j0:j1 + 1], lo, u, v, run, step)
            except Exception as e:
                last = e; time.sleep(15 * (k + 1))
        raise IOError(f"{url}: {last}")
    msgs = []
    steps = list(range(0, 145, 3))
    for k in range(0, len(steps), 3):              # pochi alla volta: il server è condiviso
        for res in bg.fetch_all([lambda s=s: get_step(s) for s in steps[k:k + 3]]):
            msgs += res
    return bg.write(out, "gfs", run, msgs)


def do_era5(a, b, out, have):
    """ERA5 orario dal THREDDS di NSF NCAR (d633000): un ritaglio per mese e per componente, poi un file per giorno."""
    import numpy as np
    d0, d1 = dt.date.fromisoformat(a), dt.date.fromisoformat(b)
    ok = bad = 0
    m = d0.replace(day=1)
    while m <= d1:
        nxt = (m.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        last = nxt - dt.timedelta(days=1)
        lo_d, hi_d = max(d0, m), min(d1, last)
        if all(f"era5_{d:%Y%m%d}00.grb2" in have for d in days(str(lo_d), str(hi_d))):
            m = nxt; continue
        try:
            fields = {}
            for code, var in (("128_165_10u", "VAR_10U"), ("128_166_10v", "VAR_10V")):
                url = (f"{RDA}/d633000/e5.oper.an.sfc/{m:%Y%m}/e5.oper.an.sfc.{code}.ll025sc.{m:%Y%m}0100_{last:%Y%m%d}23.nc?"
                       f"var={var}&{box_q()}&time_start={lo_d}T00:00:00Z&time_end={hi_d}T23:00:00Z&accept=netcdf3")
                ds = ncss(url)
                try:
                    tv = ds["time"]
                    import netCDF4
                    times = netCDF4.num2date(tv[:], tv.units, only_use_cftime_datetimes=False)
                    fields[var] = (ds["latitude"][:], ds["longitude"][:], [dt.datetime(t.year, t.month, t.day, t.hour) for t in times], np.asarray(ds[var][:]))
                finally:
                    ds.close()
        except Exception as e:
            print(f"era5 {m:%Y-%m}: non riuscito: {e}"); bad += 1; m = nxt; continue
        la, lo, times, U = fields["VAR_10U"]; _, _, _, V = fields["VAR_10V"]
        byday = {}
        for k, t in enumerate(times):
            byday.setdefault(t.date(), []).append(k)
        for day, ks in sorted(byday.items()):
            name = f"era5_{day:%Y%m%d}00.grb2"
            if name in have:
                continue
            ref = dt.datetime(day.year, day.month, day.day)
            msgs = []
            for k in ks:
                msgs += uv_grib(la, lo, U[k], V[k], ref, times[k].hour)
            path = bg.write(out, "era5", ref, msgs)
            publish(path, f"archivio-{day:%Y-%m}", f"Archivio {day:%Y-%m}",
                    "Corse archiviate per il banco di prova. " + NOTE_GRIB + " ERA5: Copernicus Climate Change Service, CC-BY-4.0, dall'archivio NSF NCAR (d633000).")
            os.remove(path); ok += 1
        m = nxt
    print(f"era5: {ok} giorni fatti, {bad} mesi non riusciti")
    return bad == 0 or ok > 0


def do_ybprova(codes, out):
    """Quali codici YB esistono: la classifica in testo, 8 richieste alla volta, 20 secondi al massimo ciascuna."""
    import requests
    from concurrent.futures import ThreadPoolExecutor
    os.makedirs(out, exist_ok=True)
    def one(code):
        try:
            r = requests.get(f"https://yb.tl/l/{code}", timeout=20, headers=bg.UA)
            txt = r.text if r.status_code == 200 else ""
            ok = r.status_code == 200 and "Rank" in txt and len(txt) > 300
            heads = [l.strip() for l in txt.splitlines() if l.strip() and not l[:1].isdigit() and "," not in l and not l.startswith("THESE")][:12]
            n = sum(1 for l in txt.splitlines() if l[:1].isdigit())
            return f"{code}\t{r.status_code}\t{'si' if ok else 'no'}\t{n}\t{' | '.join(heads)}"
        except Exception as e:
            return f"{code}\terr\tno\t0\t{type(e).__name__}"
    cs = [c.strip() for c in codes.split(",") if c.strip()]
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(one, cs))
    for r in rows:
        if "\tsi\t" in r:
            print(r)
    path = os.path.join(out, f"yb_prova_{dt.datetime.utcnow():%Y%m%d%H%M%S}.tsv")
    open(path, "w").write("codice\thttp\tesiste\trighe\tclassi\n" + "\n".join(rows) + "\n")
    publish(path, "archivio-misure", "Misure per il banco di prova", "Misure per il banco di prova.")
    return True


def one_gefs(run, out):
    """Controllo di GEFS (gec00) da Amazon (noaa-gefs-pds, dal 2017): 1° ogni 6 ore fino al 23/9/2020 (GEFS v11), poi
    0,5° ogni 3 ore (v12). Vento a 10 m fino a 144 ore. Per gli anni senza GFS e senza ECMWF (2018-2020)."""
    v12 = run >= dt.datetime(2020, 9, 23)
    def name(step):
        if v12:
            return f"{GEFS_AWS}/gefs.{run:%Y%m%d}/{run:%H}/atmos/pgrb2ap5/gec00.t{run:%H}z.pgrb2a.0p50.f{step:03d}"
        return f"{GEFS_AWS}/gefs.{run:%Y%m%d}/{run:%H}/pgrb2a/gec00.t{run:%H}z.pgrb2af{step:02d}" if step < 100 else \
            f"{GEFS_AWS}/gefs.{run:%Y%m%d}/{run:%H}/pgrb2a/gec00.t{run:%H}z.pgrb2af{step:03d}"
    wanted = {("UGRD", "10 m above ground"), ("VGRD", "10 m above ground")}
    def get_step(step):
        idx = bg.http_get(name(step) + ".idx").decode()
        return b"".join(bg.http_get(name(step), headers={"Range": f"bytes={a}-{b if b is not None else ''}"})
                        for a, b in bg.parse_idx(idx, wanted))
    steps = list(range(0, 145, 3 if v12 else 6))
    msgs = []
    for raw in bg.fetch_all([lambda s=s: get_step(s) for s in steps]):
        msgs += bg.crop_bytes(raw, BOX)
    return bg.write(out, "gefs", run, msgs)


def one_gfs(run, out):
    if run < dt.datetime(2021, 3, 1):
        return one_gfs_rda(run, out)
    def name(step):
        return f"{GFS_AWS}/gfs.{run:%Y%m%d}/{run:%H}/atmos/gfs.t{run:%H}z.pgrb2.0p25.f{step:03d}"
    wanted = {("UGRD", "10 m above ground"), ("VGRD", "10 m above ground"), ("PRMSL", "mean sea level")}
    def get_step(step):
        idx = bg.http_get(name(step) + ".idx").decode()
        return b"".join(bg.http_get(name(step), headers={"Range": f"bytes={a}-{b if b is not None else ''}"})
                        for a, b in bg.parse_idx(idx, wanted))
    steps = list(range(0, 49)) + list(range(51, 145, 3))
    msgs = []
    for raw in bg.fetch_all([lambda s=s: get_step(s) for s in steps]):
        msgs += bg.crop_bytes(raw, BOX)
    return bg.write(out, "gfs", run, msgs)


def one_ens(run, out):
    steps = list(range(0, 145, 6))
    def get(step):
        return ecmwf_any(run, step, stream="enfo", typ="ef", wanted=("10u", "10v"))
    msgs = []
    get(0)
    for k in range(0, len(steps), 4):            # un passo dell'insieme sono ~100 campi globali: pochi alla volta
        for raw in bg.fetch_all([lambda s=s: get(s) for s in steps[k:k + 4]]):
            msgs += bg.crop_bytes(raw, BOX)
    return bg.write(out, "ens", run, msgs)


def runs_between(a, b, hours=(0, 12)):
    for d in days(a, b):
        for h in hours:
            yield dt.datetime(d.year, d.month, d.day, h)


def do_gribs(kind, a, b, out, have):
    fn = {"ecmwf": one_ecmwf, "gfs": one_gfs, "ens": one_ens, "gefs": one_gefs}[kind]
    ok = bad = 0
    for run in runs_between(a, b):
        name = f"{kind}_{run:%Y%m%d%H}.grb2"
        if name in have:
            continue
        if run > dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(hours=8):
            continue
        try:
            path = fn(run, out)
        except Exception as e:
            print(f"{kind}: corsa {run:%Y-%m-%d %H}Z non riuscita: {e}")
            bad += 1
            continue
        tag = (f"archivio-ens-{run:%Y-%m}" if kind == "ens" else f"archivio-{run:%Y-%m}")
        publish(path, tag, f"Archivio {'insieme ECMWF ' if kind == 'ens' else ''}{run:%Y-%m}",
                "Corse archiviate per il banco di prova. " + NOTE_GRIB)
        os.remove(path)
        ok += 1
    print(f"{kind}: {ok} corse fatte, {bad} non riuscite")
    return bad == 0 or ok > 0


def do_metar(a, b, out):
    import requests
    d0, d1 = dt.date.fromisoformat(a), dt.date.fromisoformat(b) + dt.timedelta(days=1)
    rows = []
    for zone, sts in METAR_STATIONS.items():
        q = [("station", s) for s in sts] + [("data", x) for x in ("drct", "sknt", "gust", "metar")]
        q += [("year1", d0.year), ("month1", d0.month), ("day1", d0.day), ("year2", d1.year), ("month2", d1.month), ("day2", d1.day),
              ("tz", "Etc/UTC"), ("format", "onlycomma"), ("latlon", "yes"), ("elev", "yes"), ("missing", "M"), ("trace", "T"),
              ("direct", "no"), ("report_type", "3"), ("report_type", "4")]
        for k in range(4):
            r = requests.get("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py", params=q, timeout=300,
                             headers=bg.UA)
            if r.status_code == 200 and r.text.startswith("station"):
                break
            print(f"metar {zone}: HTTP {r.status_code}, riprovo"); time.sleep(20 + 30 * k)
        else:
            print(f"metar {zone}: non riuscito"); continue
        rd = csv.DictReader(io.StringIO(r.text))
        n = 0
        for row in rd:
            row["zona"] = zone
            rows.append(row); n += 1
        print(f"metar {zone}: {n} righe")
        time.sleep(5)
    by = {}
    for row in rows:
        by.setdefault(row["valid"][:7], []).append(row)
    os.makedirs(out, exist_ok=True)
    for month, rs in sorted(by.items()):
        path = os.path.join(out, f"metar_{month}.csv")
        old = []
        if not os.environ.get("STORICO_NO_UPLOAD"):
            subprocess.run(["gh", "release", "download", "archivio-misure", "-p", f"metar_{month}.csv", "-D", out, "--clobber"],
                           capture_output=True)
            if os.path.exists(path):
                old = list(csv.DictReader(open(path)))
        keys = {(r["station"], r["valid"]) for r in rs}
        merged = [r for r in old if (r["station"], r["valid"]) not in keys] + rs
        merged.sort(key=lambda r: (r["station"], r["valid"]))
        cols = ["station", "zona", "valid", "lon", "lat", "elevation", "drct", "sknt", "gust", "metar"]
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader(); w.writerows(merged)
        print(f"metar {month}: {len(merged)} righe -> {path}")
        publish(path, "archivio-misure", "Misure per il banco di prova",
                "METAR dall'archivio dell'Iowa Environmental Mesonet (Iowa State University) e anemometri in mare da "
                "Copernicus Marine (prodotto in situ del Mediterraneo). Per il confronto con Lens e con i modelli.")
    return True


BOE_DATASET = os.environ.get("BOE_DATASET", "cmems_obs-ins_med_phybgcwav_mynrt_na_irr")


def do_boe(a, b, out, probe=False):
    import copernicusmarine as cm
    os.makedirs(out, exist_ok=True)
    lst = os.path.join(out, "elenco.txt")
    cm.get(dataset_id=BOE_DATASET, filter="*latest*", create_file_list=lst, username=os.environ.get("CMEMS_USER"),
           password=os.environ.get("CMEMS_PASS"))
    names = [l.strip() for l in open(lst) if l.strip()]
    print(f"boe: {len(names)} file nella parte latest")
    want = [n for n in names if "_MO_" in n or "/MO_" in n]
    print(f"boe: {len(want)} di ormeggi (MO)")
    if probe:
        for n in names[:40]:
            print("  ", n)
        for n in want[:40]:
            print("  MO", n)
        return True
    d0, d1 = a.replace("-", ""), b.replace("-", "")
    want = [n for n in want if any(d0 <= tok[:8] <= d1 for tok in os.path.basename(n).replace(".", "_").split("_") if tok[:8].isdigit() and len(tok) >= 8)]
    print(f"boe: {len(want)} file nei giorni chiesti")
    import netCDF4, numpy as np
    rows = []
    for k in range(0, len(want), 50):
        flt = None
        files = want[k:k + 50]
        fl = os.path.join(out, "scarica.txt")
        with open(fl, "w") as f:
            f.write("\n".join(files))
        cm.get(dataset_id=BOE_DATASET, file_list=fl, output_directory=os.path.join(out, "nc"), no_directories=True,
               username=os.environ.get("CMEMS_USER"), password=os.environ.get("CMEMS_PASS"))
    for fn in sorted(os.listdir(os.path.join(out, "nc"))):
        try:
            rows += boe_file(os.path.join(out, "nc", fn), fn, netCDF4, np)
        except Exception as e:
            print(f"boe: {fn} saltato ({e})")
    finish_boe(rows, out)
    return True


def boe_file(path, fn, netCDF4, np):
    rows = []
    if True:
        ds = netCDF4.Dataset(path)
        v = ds.variables
        if "WSPD" not in v:
            ds.close(); return rows
        t = netCDF4.num2date(v["TIME"][:], v["TIME"].units, only_use_cftime_datetimes=False)
        lat = np.ravel(v["LATITUDE"][:]); lon = np.ravel(v["LONGITUDE"][:])
        nt = len(t)
        def col(name):
            """Una serie per istante: se la variabile ha anche la quota, la colonna con più dati validi."""
            if name not in v:
                return None
            x = np.ma.filled(v[name][:].astype(float), np.nan)
            if x.ndim == 1:
                return x if len(x) == nt else None
            if x.shape[0] != nt and x.ndim == 2 and x.shape[1] == nt:
                x = x.T
            if x.shape[0] != nt:
                return None
            x = x.reshape(nt, -1)
            best = int(np.argmax(np.sum(~np.isnan(x), axis=0)))
            return x[:, best]
        ws, wd, gs = col("WSPD"), col("WDIR"), col("GSPD")
        h = col("DEPH")
        pid = getattr(ds, "platform_code", fn.split("_")[-2] if "_" in fn else fn)
        for i in range(len(t)):
            if ws is None or np.isnan(ws[i]):
                continue
            la = lat[i] if len(lat) == len(t) else lat[0]; lo = lon[i] if len(lon) == len(t) else lon[0]
            if not (BOX[0] - 4 <= la <= BOX[1] + 1 and BOX[2] - 10 <= lo <= BOX[3] + 17):
                continue
            rows.append({"boa": pid, "valid": t[i].strftime("%Y-%m-%d %H:%M"), "lat": f"{la:.4f}", "lon": f"{lo:.4f}",
                         "quota": "" if h is None or np.isnan(h[i]) else f"{-h[i]:.1f}",
                         "wspd_ms": f"{ws[i]:.2f}", "wdir": "" if wd is None or np.isnan(wd[i]) else f"{wd[i]:.0f}",
                         "gust_ms": "" if gs is None or np.isnan(gs[i]) else f"{gs[i]:.2f}"})
        ds.close()
    return rows


def finish_boe(rows, out):
    by = {}
    for r in rows:
        by.setdefault(r["valid"][:7], []).append(r)
    for month, rs in sorted(by.items()):
        path = os.path.join(out, f"boe_{month}.csv")
        old = []
        if not os.environ.get("STORICO_NO_UPLOAD"):
            subprocess.run(["gh", "release", "download", "archivio-misure", "-p", f"boe_{month}.csv", "-D", out, "--clobber"],
                           capture_output=True)
            if os.path.exists(path):
                old = list(csv.DictReader(open(path)))
        keys = {(r["boa"], r["valid"]) for r in rs}
        merged = [r for r in old if (r["boa"], r["valid"]) not in keys] + rs
        merged.sort(key=lambda r: (r["boa"], r["valid"]))
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["boa", "valid", "lat", "lon", "quota", "wspd_ms", "wdir", "gust_ms"])
            w.writeheader(); w.writerows(merged)
        print(f"boe {month}: {len(merged)} righe, {len({r['boa'] for r in merged})} piattaforme -> {path}")
        publish(path, "archivio-misure", "Misure per il banco di prova",
                "METAR dall'archivio dell'Iowa Environmental Mesonet (Iowa State University) e anemometri in mare da "
                "Copernicus Marine (prodotto in situ del Mediterraneo). Per il confronto con Lens e con i modelli.")
    return True


def do_yb(codes, out):
    import requests
    os.makedirs(out, exist_ok=True)
    ok = True
    for code in [c.strip() for c in codes.split(",") if c.strip()]:
        for url, name in ((f"https://yb.tl/{code}.kml", f"yb_{code}.kml"), (f"https://yb.tl/l/{code}", f"yb_{code}_classifica.txt"),
                          (f"https://cf.yb.tl/JSON/{code}/RaceSetup", f"yb_{code}_setup.json")):
            try:
                r = requests.get(url, timeout=300, headers=bg.UA)
                print(f"yb {url}: HTTP {r.status_code}, {len(r.content)} byte")
                if r.status_code != 200 or len(r.content) < 200:
                    ok = False; continue
                path = os.path.join(out, name)
                with open(path, "wb") as f:
                    f.write(r.content)
                publish(path, "archivio-misure", "Misure per il banco di prova",
                        "METAR dall'archivio dell'Iowa Environmental Mesonet (Iowa State University), anemometri in mare da "
                        "Copernicus Marine, tracciati delle regate da YB Tracking. Per il banco di prova.")
            except Exception as e:
                print(f"yb {url}: {e}"); ok = False
    return ok


def probe():
    """Che cosa c'è su Amazon: una corsa d'estate 2025 e una di questo mese, oper ed enfo."""
    for run in (dt.datetime(2025, 7, 1, 0), dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0) - dt.timedelta(days=3)):
        for stream, typ in (("oper", "fc"), ("enfo", "ef")):
            for root in ECMWF_ROOTS + [ECMWF_LIVE]:
                u = f"{root}/{run:%Y%m%d}/{run:%H}z/ifs/0p25/{stream}/{run:%Y%m%d%H}0000-24h-{stream}-{typ}.index"
                try:
                    t = bg.http_get(u, tries=1, timeout=60).decode().splitlines()
                    n10 = sum(1 for l in t if '"10u"' in l)
                    print(f"ECMWF {stream} {run:%Y-%m-%d %H}Z {root}: indice con {len(t)} righe, 10u: {n10}")
                except Exception as e:
                    print(f"ECMWF {stream} {run:%Y-%m-%d %H}Z {root}: {e}")
    for run in (dt.datetime(2025, 7, 1, 0),):
        u = f"{GFS_AWS}/gfs.{run:%Y%m%d}/{run:%H}/atmos/gfs.t{run:%H}z.pgrb2.0p25.f024.idx"
        try:
            print(f"GFS {run:%Y-%m-%d}: indice {len(bg.http_get(u, tries=1, timeout=60))} byte")
        except Exception as e:
            print(f"GFS {run:%Y-%m-%d}: {e}")
    import requests
    r = requests.get("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py", timeout=120, headers=bg.UA,
                     params=[("station", "LICJ"), ("data", "sknt"), ("data", "gust"), ("year1", 2025), ("month1", 7), ("day1", 1),
                             ("year2", 2025), ("month2", 7), ("day2", 2), ("tz", "Etc/UTC"), ("format", "onlycomma")])
    print(f"IEM: HTTP {r.status_code}, {len(r.text.splitlines())} righe; prime: {r.text.splitlines()[:3]}")


def main():
    ap = argparse.ArgumentParser()
    for k in ("ecmwf", "gfs", "ens", "metar", "boe", "era5", "gefs"):
        ap.add_argument(f"--{k}", nargs=2, metavar=("DA", "A"))
    ap.add_argument("--yb", default="")
    ap.add_argument("--ybprova", default="")
    ap.add_argument("--prova", action="store_true")
    ap.add_argument("--prova-boe", action="store_true")
    ap.add_argument("--out", default="storico")
    ap.add_argument("--have", default="")
    x = ap.parse_args()
    have = set(l.strip() for l in open(x.have)) if x.have and os.path.exists(x.have) else set()
    ok = True
    if x.prova:
        probe()
        probe_rda()
    if x.prova_boe:
        do_boe("2026-11-01", "2026-11-30", x.out, probe=True)
    for kind in ("ecmwf", "gfs", "ens", "gefs"):
        if getattr(x, kind):
            ok &= do_gribs(kind, *getattr(x, kind), x.out, have)
    if x.metar:
        ok &= do_metar(*x.metar, x.out)
    if x.boe:
        ok &= do_boe(*x.boe, x.out)
    if x.yb:
        ok &= do_yb(x.yb, x.out)
    if x.ybprova:
        ok &= do_ybprova(x.ybprova, x.out)
    if x.era5:
        ok &= do_era5(*x.era5, x.out, have)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
