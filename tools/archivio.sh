#!/bin/bash
# Archivio permanente dei GRIB: carica ogni file nella release del mese della sua corsa. Le release d'archivio non si
# cancellano mai (servono al banco di prova). Uso: bash tools/archivio.sh gribs/*.grb*   (nel lavoro su GitHub, con GH_TOKEN)
#
# Una release tiene al massimo 1.000 file. Per questo (24/11/2026):
#   - ICON-2I-RUC (8 corse al giorno, circa 250 file al mese) va in archivio-ruc-AAAA-MM;
#   - dal dicembre 2026 il profilo verticale per Lens (profilo-ecmwf, profilo-icon-eu: 4 file al giorno) va in
#     archivio-profilo-AAAA-MM. Senza, archivio-AAAA-MM riceverebbe 34 file al giorno: 1.054 in un mese di 31 giorni.
#     Il profilo di novembre 2026 resta in archivio-2026-11;
#   - tutto il resto in archivio-AAAA-MM, come prima (circa 30 file al giorno, 930 in un mese di 31 giorni);
#   - rete di sicurezza: se una release arriva a 990 file, il resto del mese va in <release>-b, con un avviso.
set -euo pipefail
repo="${GITHUB_REPOSITORY:?}"
declare -A count

# quanti file ha la release (0 se non c'è ancora; con un errore gh scrive il corpo della risposta, che non è un numero)
assets() {
  local n
  n=$(gh api "repos/$repo/releases/tags/$1" --jq '.assets | length' 2>/dev/null || true)
  [[ "$n" =~ ^[0-9]+$ ]] && echo "$n" || echo 0
}

notes_main="Tutte le corse del mese, per il banco di prova. ECMWF: dati aperti, CC-BY-4.0. ICON-EU: dati aperti del DWD. ICON-2I: dati aperti ItaliaMeteo-Arpae (MeteoHub). GFS: dati aperti NOAA. AROME e ARPEGE: dati aperti Météo-France, Licence Ouverte 2.0. Correnti e onde del Mediterraneo: generated using E.U. Copernicus Marine Service Information."
notes_ruc="ICON-2I-RUC, tutte le corse del mese (ogni 3 ore), per il banco di prova. Dati aperti ItaliaMeteo-Arpae (MeteoHub)."
notes_prof="Profilo verticale per Lens (temperatura, quota geopotenziale e vento a 1000-700 hPa, temperatura a 2 m), tutte le corse del mese. ECMWF: dati aperti, CC-BY-4.0. ICON-EU: dati aperti del DWD."

for f in "$@"; do
  [ -e "$f" ] || continue
  n=$(basename "$f")
  d=$(echo "$n" | sed -E 's/.*_([0-9]{4})([0-9]{2})[0-9]{4}\..*/\1-\2/')
  case "$n" in
    icon-2i-ruc_*) tag="archivio-ruc-$d"; title="Archivio ICON-2I-RUC $d"; notes="$notes_ruc" ;;
    profilo-*)
      if [[ "$d" > "2026-11" ]]; then tag="archivio-profilo-$d"; title="Archivio profilo verticale $d"; notes="$notes_prof"
      else tag="archivio-$d"; title="Archivio GRIB $d"; notes="$notes_main"; fi ;;
    *) tag="archivio-$d"; title="Archivio GRIB $d"; notes="$notes_main" ;;
  esac
  if [ -z "${count[$tag]+x}" ]; then
    count[$tag]=$(assets "$tag")
  fi
  if [ "${count[$tag]}" -ge 990 ]; then
    echo "::warning::$tag ha ${count[$tag]} file: $n va in $tag-b"
    title="$title (seguito)"; tag="$tag-b"
    if [ -z "${count[$tag]+x}" ]; then
      count[$tag]=$(assets "$tag")
    fi
  fi
  gh release view "$tag" > /dev/null 2>&1 || gh release create "$tag" --latest=false --title "$title" --notes "$notes"
  gh release upload "$tag" "$f" --clobber
  count[$tag]=$(( ${count[$tag]} + 1 ))
  echo "$n -> $tag"
done
