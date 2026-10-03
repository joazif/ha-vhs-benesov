#!/usr/bin/env bash
# Zkopíruje vybranou sadu ikon (A, B nebo C) do custom_components/vhs_benesov/brand/.
#
#   ./tools/pouzit_ikonu.sh C
#
# Nejdřív vygeneruj sady: .venv/bin/python tools/make_icons.py
# Sada bez tmavé varianty (C) smaže dark_icon*.png, jinak by přebíjely vybranou ikonu.
# Po změně ikony restartuj Home Assistant (a případně vymaž mezipaměť prohlížeče).
set -eu
cd "$(dirname "$0")/.."
VARIANT="${1:-}"
SRC="tools/ikony_varianty/$VARIANT"
DST="custom_components/vhs_benesov/brand"
[ -n "$VARIANT" ] && [ -d "$SRC" ] || { echo "Použití: $0 A|B|C (nejdřív spusť tools/make_icons.py)"; exit 2; }
rm -f "$DST"/icon.png "$DST"/icon@2x.png "$DST"/dark_icon.png "$DST"/dark_icon@2x.png
cp "$SRC"/*.png "$DST"/
echo "Použita sada $VARIANT:"; ls "$DST"/icon*.png "$DST"/dark_icon*.png 2>/dev/null
