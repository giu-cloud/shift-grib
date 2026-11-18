# shift-grib

Server di file GRIB del Mediterraneo centrale per l'app di routing **Shift**. Un lavoro di GitHub Actions, ogni tre ore, scarica le ultime corse dei modelli meteo e marini aperti. Le ritaglia sull'area 34-46 N e 5-20 E (ICON-2I ha un'area sua) e le pubblica nelle release di questo deposito.

I dati sono quelli dei centri che li producono, ritagliati e convertiti, **senza garanzie**. Non sostituiscono le informazioni ufficiali per la sicurezza della navigazione.

## Release
- **`gribs`**: le ultime 8 corse di ogni modello, file interi `<modello>_<AAAAMMGGHH>.grb2`.
- **`tiles`**: l'ultima corsa completa dei modelli fini, tagliata a quadratini fissi.
  - Il lato è di 1° per icon-2i e arome, di 2° per icon-eu, arpege, ecmwf-0p1 e cmems.
  - Nome: `<modello>_<AAAAMMGGHH>_t<lato>_<N|S><LL>_<E|W><LLL>.grb2`, dove l'angolo sud-ovest è in gradi interi.
  - Una corsa è completa quando c'è il segnaposto `<modello>_<AAAAMMGGHH>_tiles.ok`. La prima riga dice quanti sono i quadratini, le righe seguenti li elencano per nome.
  - Ogni quadratino contiene tutti i campi del file intero.
- **`archivio-AAAA-MM`**: tutte le corse del mese. Non si cancella mai e serve al banco di prova.

## Modelli, fonti e licenze
| file | modello | fonte | licenza |
|---|---|---|---|
| `ecmwf`, `ecmwf-0p1`, `ecmwf-wave` | IFS e onde, 0,25° e 0,1° | ECMWF, dati aperti | CC BY 4.0, © ECMWF |
| `icon-eu` | ICON-EU, circa 7 km | Deutscher Wetterdienst (DWD), open data | dati aperti DWD, fonte da citare |
| `icon-2i` | ICON-2I, circa 2,2 km | ItaliaMeteo / Arpae Emilia-Romagna, MeteoHub | dati aperti, attribuzione come da https://meteohub.agenziaitaliameteo.it/app/license |
| `gfs`, `gfs-wave` | GFS e WAVEWATCH III | NOAA / NCEP | dominio pubblico (dati del governo USA) |
| `arome`, `arpege` | AROME 0,025° e ARPEGE 0,1° | Météo-France, data.gouv.fr | Licence Ouverte / Open Licence 2.0 Etalab |
| `cmems-cur`, `cmems-wav` | correnti e onde del Mediterraneo, 1/24° | Copernicus Marine Service | "Generated using E.U. Copernicus Marine Service Information" |

I file sono ritagliati e, per Copernicus, convertiti da NetCDF a GRIB2. I valori non vengono modificati, salvo la compressione GRIB semplice.

## Come funziona
- `tools/build_gribs.py` scarica, ritaglia, taglia i quadratini e li pubblica. Comprende le prove senza rete: `--selftest` e `--tiles-check sintetico`.
- `.github/workflows/gribs.yml` è il giro ogni tre ore, un giro alla volta.
- In ogni release c'è `indice.json` (gribs: i file interi per modello, dal più recente; tiles: per modello la corsa completa, il lato e i quadratini con il loro peso). Si scarica senza credenziali e senza usare l'API: `https://github.com/giu-cloud/shift-grib/releases/download/<release>/indice.json`.
- `.github/workflows/mantieni.yml` salva un file una volta al mese. GitHub spegne i lavori programmati dei depositi pubblici dopo 60 giorni senza attività.
- Per Copernicus Marine servono un account gratuito e i Secrets `CMEMS_USER` e `CMEMS_PASS`. Senza, quel passo si salta.
