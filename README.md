# shift-grib

Server di file GRIB del Mediterraneo centrale per l'app di routing **Shift**. Un lavoro di GitHub Actions, ogni tre ore, scarica le ultime corse dei modelli meteo e marini aperti. Le ritaglia sull'area 34-46 N e 5-20 E (ICON-2I ha un'area sua) e le pubblica nelle release di questo deposito.

I dati sono quelli dei centri che li producono, ritagliati e convertiti, **senza garanzie**. Non sostituiscono le informazioni ufficiali per la sicurezza della navigazione.

## Release
- **`gribs`**: le ultime 8 corse di ogni modello, file interi `<modello>_<AAAAMMGGHH>.grb2`.
  - Dal 23/11/2026 anche il profilo verticale per Lens: `profilo-ecmwf_<corsa>.grb2` e `profilo-icon-eu_<corsa>.grb2`.
  - Dentro: temperatura, quota geopotenziale (gpm) e vento u e v a 1000, 925, 850 e 700 hPa (ICON-EU anche 950 e 900), più la temperatura a 2 m.
  - Maglia 0,25°, passo di 3 ore fino a 72 e di 6 dopo, area 34-46 N, 2 W-20 E (variabile `PROFILE_BOX`).
- **`tiles`**: l'ultima corsa completa dei modelli fini, tagliata a quadratini fissi.
  - Il lato è di 1° per icon-2i, icon-2i-ruc e arome, di 2° per icon-eu, arpege, ecmwf-0p1 e cmems.
  - Nome: `<modello>_<AAAAMMGGHH>_t<lato>_<N|S><LL>_<E|W><LLL>.grb2`, dove l'angolo sud-ovest è in gradi interi.
  - Una corsa è completa quando c'è il segnaposto `<modello>_<AAAAMMGGHH>_tiles.ok`. La prima riga dice quanti sono i quadratini, le righe seguenti li elencano per nome.
  - Ogni quadratino contiene tutti i campi del file intero.
- **`archivio-AAAA-MM`**: tutte le corse del mese. Non si cancella mai e serve al banco di prova.
- **`archivio-ruc-AAAA-MM`** (dal 24/11/2026): tutte le corse di ICON-2I-RUC del mese, a parte per il limite di 1.000 file per release.
- **`archivio-profilo-AAAA-MM`** (dal dicembre 2026): il profilo verticale per Lens, per lo stesso motivo; quello di novembre 2026 è in `archivio-2026-11`.
- Se una release d'archivio arriva a 990 file, il resto del mese va in `<release>-b` (`tools/archivio.sh`).
- **`archivio-ovest-AAAA-MM`** (dal 22/11/2026): le stesse corse di ECMWF 0,25 e 0,1, ICON-EU, GFS, AROME e ARPEGE ritagliate sul Mediterraneo occidentale (36-44,5 N, 2 W-10 E: Barcellona, Valencia, Baleari), con nomi `<modello>-ovest_<corsa>.grb2`. Solo per il banco di prova: non sono in `gribs` né in `tiles`. L'area si cambia con la variabile `GRIB_EXTRA`.

- **`satellite`** (dal 22/11/2026): il vento misurato dai satelliti nelle ultime 72 ore, un file per passaggio `scat_<satellite>_<AAAAMMGGHHMM>.scw.gz` (formato SCW1, descritto in `tools/satellite.py`), con `indice.json`. Si aggiorna ogni ora con il lavoro "Vento dal satellite" (ogni tre ore fino al 23/11/2026).
  - **Aree chieste dall'app** (dal 23/11/2026): oltre al Mediterraneo, fino a 5 aree, ciascuna per al più 90 giorni.
    - L'app le aggiunge e le toglie avviando il lavoro "Vento dal satellite, aree dell'app" (`satellite_area.yml`). L'elenco è `aree_satellite.json` nella stessa release.
    - I passaggi delle altre aree si chiamano `scat_<area>_<satellite>_<AAAAMMGGHHMM>.scw.gz`.
    - Nell'indice: `areas` (con il Mediterraneo, id `med`), `refused` (richieste rifiutate, per 3 giorni) e, per ogni passaggio, `area`.
- **`archivio-satellite-AAAA-MM`**: tutti i passaggi del mese, per il banco di prova. Non si cancella mai.

## Modelli, fonti e licenze
| file | modello | fonte | licenza |
|---|---|---|---|
| `ecmwf`, `ecmwf-0p1`, `ecmwf-wave` | IFS e onde, 0,25° e 0,1° | ECMWF, dati aperti | CC BY 4.0, © ECMWF |
| `icon-eu` | ICON-EU, circa 7 km | Deutscher Wetterdienst (DWD), open data | dati aperti DWD, fonte da citare |
| `icon-2i` | ICON-2I, circa 2,2 km | ItaliaMeteo / Arpae Emilia-Romagna, MeteoHub | dati aperti, attribuzione come da https://meteohub.agenziaitaliameteo.it/app/license |
| `icon-2i-ruc` | ICON-2I-RUC, circa 2,2 km, corse ogni 3 ore (giro a parte, `ruc.yml`) | ItaliaMeteo / Arpae Emilia-Romagna, MeteoHub | dati aperti, attribuzione come da https://meteohub.agenziaitaliameteo.it/app/license |
| `gfs`, `gfs-wave` | GFS e WAVEWATCH III | NOAA / NCEP | dominio pubblico (dati del governo USA) |
| `arome`, `arpege` | AROME 0,025° e ARPEGE 0,1° | Météo-France, data.gouv.fr | Licence Ouverte / Open Licence 2.0 Etalab |
| `cmems-cur`, `cmems-wav` | correnti e onde del Mediterraneo, 1/24° | Copernicus Marine Service | "Generated using E.U. Copernicus Marine Service Information" |
| `scat_metopb`, `scat_metopc` | vento a 10 m dei diffusometri ASCAT, prodotto costiero a 12,5 km | EUMETSAT OSI SAF, elaborato dal KNMI, dalla KNMI Data Platform | CC BY 4.0, "copyright (anno) EUMETSAT" |

I file sono ritagliati e, per Copernicus, convertiti da NetCDF a GRIB2. I valori non vengono modificati, salvo la compressione GRIB semplice.

## Come funziona
- `tools/build_gribs.py` scarica, ritaglia, taglia i quadratini e li pubblica. Comprende le prove senza rete: `--selftest` e `--tiles-check sintetico`.
- `.github/workflows/gribs.yml` è il giro ogni tre ore, un giro alla volta.
- In ogni release c'è `indice.json` (gribs: i file interi per modello, dal più recente; tiles: per modello la corsa completa, il lato e i quadratini con il loro peso). Si scarica senza credenziali e senza usare l'API: `https://github.com/giu-cloud/shift-grib/releases/download/<release>/indice.json`.
- `.github/workflows/mantieni.yml` salva un file una volta al mese. GitHub spegne i lavori programmati dei depositi pubblici dopo 60 giorni senza attività.
- Per Copernicus Marine servono un account gratuito e i Secrets `CMEMS_USER` e `CMEMS_PASS`. Senza, quel passo si salta.
- `tools/satellite.py` e `.github/workflows/satellite.yml` fanno il vento dal satellite. Si tengono solo le celle buone: niente controllo di qualità fallito, niente ghiaccio, niente pioggia. Le fonti sono due. NASA PO.DAAC mette i file in rete 1,5-2,5 ore dopo l'orbita e richiede un account Earthdata gratuito (Secrets `EARTHDATA_USER` e `EARTHDATA_PASS`). La KNMI Data Platform carica una volta al giorno e serve per l'archivio e per riempire i buchi: la chiave è il Secret `KNMI_API_KEY` (gratuita); senza, si usa la chiave anonima pubblica del KNMI, che ha limiti condivisi.
