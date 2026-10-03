#!/usr/bin/env bash
# Kontrola, jestli v repu nezůstaly konkrétní údaje z účtu.
# Pustit před každým pushem:  ./tools/kontrola-udaju.sh
set -u
cd "$(dirname "$0")/.."

nalezeno=0

# Kontrolují se jen soubory, které git sleduje - tedy to, co se dostane
# na GitHub. Stažené stránky (*.local.html) jsou ignorované a tvoje data
# v nich jsou v pořádku, dokud zůstanou na disku.
soubory=$(git ls-files 2>/dev/null | grep -v "^tools/kontrola-udaju.sh$")
if [ -z "$soubory" ]; then
  echo "Nejsem v git repozitáři, kontroluji všechny soubory ve složce."
  soubory=$(find . -type f -not -path "./.git/*" -not -name "kontrola-udaju.sh")
fi

hledej() {
  local popis="$1" vzor="$2" vyjimka="${3:-}"
  local vysledek
  vysledek=$(echo "$soubory" | tr "\n" "\0" \
    | xargs -0 grep -InE "$vzor" 2>/dev/null | grep -v "example\.com" || true)
  if [ -n "$vyjimka" ] && [ -n "$vysledek" ]; then
    vysledek=$(echo "$vysledek" | grep -vE "$vyjimka" || true)
  fi
  if [ -n "$vysledek" ]; then
    echo "NÁLEZ – $popis:"
    echo "$vysledek" | sed 's/^/  /'
    nalezeno=1
  fi
}

# Názvy souborů jako icon@2x.png vypadají jako e-mail, proto ta výjimka;
# noreply adresa GitHubu je záměrná a veřejná.
hledej "e-mailová adresa" \
  "[A-Za-z0-9._%+-]+(@|&#64;|&commat;)[A-Za-z0-9.-]+\.[A-Za-z]{2,}" \
  "@[0-9]+x\.|\.(png|jpe?g|gif|svg|webp|ico)"
# Číslo měřidla (12345-XX-0000001 je vymyšlené z testů) a přihlašovací login
# (10 číslic začínajících 00; 0000000000 je vymyšlený z testů).
hledej "číslo měřidla" "[0-9]{5}-[A-Z]{2}-[0-9]{7}" "12345-XX-000000[0-9]|12345-XX-0000099"
hledej "login k portálu" "\\b00[0-9]{8}\\b" "00000000[0-9][0-9]"
hledej "telefonní číslo" "(\+420 ?)?[0-9]{3} ?[0-9]{3} ?[0-9]{3}" "00000000[0-9][0-9]"
# "heslo" je testovací hodnota, ne skutečné heslo.
hledej "heslo nebo token v kódu" "(password|heslo|token|secret|api_key) *[=:] *[\"'][^\"']+[\"']" "[\"']heslo[\"']"
# Hodnota cookie, ne jen její název - o tom se v dokumentaci píše běžně.
hledej "hodnota session cookie" "(PHPSESSID|nette-browser|_nss)=[A-Za-z0-9]{8,}|sess_[A-Za-z0-9]{10,}"

if [ "$nalezeno" -eq 0 ]; then
  echo "Čisté – nic podezřelého ke zveřejnění jsem nenašel."
fi
exit "$nalezeno"
