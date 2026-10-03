#!/usr/bin/env bash
# Nainstaluje sledování portálu do cronu: každých 10 minut jeden dotaz.
#
#   ./tools/install_watch_cron.sh            # zeptá se na login a heslo, přidá cron
#   ./tools/install_watch_cron.sh --remove   # odebere cron a smaže uložené údaje
#   ./tools/install_watch_cron.sh --status   # ukáže, jestli cron běží, a poslední záznam
#
# Údaje se zadávají skrytě a ukládají jen do tools/watch.local.env (práva 600,
# soubor je v .gitignore). Výsledek je v tools/watch_updates.local.log.
set -eu
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
ENV_FILE="tools/watch.local.env"
PY="$ROOT/.venv/bin/python"
MARK="# vhs-benesov-watch"
LINE="*/10 * * * * cd \"$ROOT\" && \"$PY\" tools/watch_updates.py --once --env $ENV_FILE >> tools/watch_updates.cron.local.log 2>&1 $MARK"

current_cron() { crontab -l 2>/dev/null || true; }

case "${1:-install}" in
  --remove)
    current_cron | grep -v "$MARK" | crontab - || true
    rm -f "$ENV_FILE" tools/watch_updates.state.local.json
    echo "Cron odebrán, údaje a uložený stav smazány. Záznam zůstal v tools/watch_updates.local.log."
    ;;
  --status)
    if current_cron | grep -q "$MARK"; then echo "Cron: nainstalován"; else echo "Cron: není nainstalován"; fi
    if [ -f tools/watch_updates.local.log ]; then
      echo "Posledních 5 řádků záznamu:"; tail -5 tools/watch_updates.local.log
    else
      echo "Záznam zatím neexistuje (první dotaz proběhne do 10 minut)."
    fi
    ;;
  install|"")
    [ -x "$PY" ] || { echo "Chybí $PY - nejdřív vytvoř prostředí (viz README, sekce Vývoj)."; exit 1; }
    printf "VHS_LOGIN: "; read -r LOGIN
    printf "VHS_PASSWORD (nezobrazuje se): "; read -rs PASSWORD; echo
    [ -n "$LOGIN" ] && [ -n "$PASSWORD" ] || { echo "Login i heslo jsou povinné."; exit 1; }
    umask 077
    printf 'VHS_LOGIN=%s\nVHS_PASSWORD=%s\n' "$LOGIN" "$PASSWORD" > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "Zkouším jeden dotaz..."
    "$PY" tools/watch_updates.py --once --env "$ENV_FILE"
    (current_cron | grep -v "$MARK"; echo "$LINE") | crontab -
    echo "Hotovo: cron běží každých 10 minut. Záznam: tools/watch_updates.local.log"
    echo "Pozor: když je Mac v režimu spánku, dotazy se nespouštějí."
    ;;
  *)
    echo "Použití: $0 [--remove|--status]"; exit 2
    ;;
esac
