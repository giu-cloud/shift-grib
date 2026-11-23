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
  --prova        stampa che cosa c'è sulle fonti (cartelle di Amazon, file delle boe) senza scaricare dati.

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


def ecmwf_step(root, run, step, stream="oper", typ="fc", wanted=("10u", "10v", "msl")):
    base = f"{root}/{run:%Y%m%d}/{run:%H}z/ifs/0p25/{stream}/{run:%Y%m%d%H}0000-{step}h-{stream}-{typ}"
    parts = []
    for line in bg.http_get(base + ".index").decode().splitlines():
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


def one_gfs(run, out):
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
    fn = {"ecmwf": one_ecmwf, "gfs": one_gfs, "ens": one_ens}[kind]
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
    for k in ("ecmwf", "gfs", "ens", "metar", "boe"):
        ap.add_argument(f"--{k}", nargs=2, metavar=("DA", "A"))
    ap.add_argument("--prova", action="store_true")
    ap.add_argument("--prova-boe", action="store_true")
    ap.add_argument("--out", default="storico")
    ap.add_argument("--have", default="")
    x = ap.parse_args()
    have = set(l.strip() for l in open(x.have)) if x.have and os.path.exists(x.have) else set()
    ok = True
    if x.prova:
        probe()
    if x.prova_boe:
        do_boe("2026-11-01", "2026-11-30", x.out, probe=True)
    for kind in ("ecmwf", "gfs", "ens"):
        if getattr(x, kind):
            ok &= do_gribs(kind, *getattr(x, kind), x.out, have)
    if x.metar:
        ok &= do_metar(*x.metar, x.out)
    if x.boe:
        ok &= do_boe(*x.boe, x.out)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
