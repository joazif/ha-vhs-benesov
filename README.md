<p align="center">
  <img src="https://raw.githubusercontent.com/joazif/ha-vhs-benesov/main/docs/logo-hacs.png" alt="VHS Benešov" width="320">
</p>

<p align="center"><strong>VHS Benešov – dálkové odečty vodoměru pro Home Assistant</strong></p>

<p align="center">
  Stav vodoměru, spotřeba za poslední úplný den, týden a měsíc a celá historie spotřeby<br>
  z portálu dálkových odečtů Vodohospodářské společnosti Benešov přímo v Home Assistantu.
</p>

<p align="center">
  <img alt="Verze" src="https://img.shields.io/github/v/release/joazif/ha-vhs-benesov?style=flat-square&color=00529c&label=verze">
  <img alt="Stažení" src="https://img.shields.io/github/downloads/joazif/ha-vhs-benesov/total?style=flat-square&color=00529c&label=sta%C5%BEen%C3%AD">
  <img alt="HACS" src="https://img.shields.io/badge/HACS-vlastn%C3%AD%20repozit%C3%A1%C5%99-00529c?style=flat-square">
  <img alt="Home Assistant" src="https://img.shields.io/badge/Home%20Assistant-2024.6%2B-00529c?style=flat-square">
</p>

> [!WARNING]
> **Neoficiální integrace.** Nevytvořila ji Vodohospodářská společnost Benešov
> ani dodavatel portálu, není jimi vyvíjená ani podporovaná. Přihlašuješ se
> vlastními údaji na vlastní odpovědnost — autor nenese odpovědnost za to, co
> se s tvým účtem stane, ani za škody vzniklé používáním integrace. Logo patří
> Vodohospodářské společnosti Benešov.

## Co to umí

- **Stav vodoměru** v m³ pro Energy dashboard a **spotřeba** za poslední úplný den, týden
  a měsíc, včetně **změny oproti loňsku**.
- **Celá historie spotřeby** od začátku dálkových odečtů: při přidání se jednorázově stáhne
  a zapíše do grafů, takže je vidět vývoj v čase.
- **Víc měřidel:** každé přihlášení je samostatná služba.
- Při vypršení nebo změně hesla nabídne Home Assistant nové přihlášení.

## Instalace

[![Otevřít repozitář v HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=joazif&repository=ha-vhs-benesov&category=integration)

1. HACS → ⋮ → **Custom repositories** → `https://github.com/joazif/ha-vhs-benesov`, kategorie **Integration**
2. Najdi *VHS Benešov*, klikni **Download** a restartuj Home Assistant
3. **Nastavení → Zařízení a služby → Přidat integraci → VHS Benešov**

[![Přidat integraci](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=vhs_benesov)

Ručně: zkopíruj `custom_components/vhs_benesov/` do `/config/custom_components/` a restartuj.

## Nastavení

Zadáš **login a heslo** z portálu dálkových odečtů a vybereš:

- **Jednotky objemu:** litry (výchozí) nebo m³ pro stav vodoměru a spotřeby.
- **Stáhnout historická data:** výchozí Ano, běží na pozadí několik minut.

Později jde v **Nastavení → Zařízení a služby → VHS Benešov → ⚙** změnit počet
měsíců pro srovnání s loňskem (1, 3 nebo 6) a **jednotky objemu** (litry jsou výchozí, nebo m³).

### Jednotky

Volba **Jednotky objemu** nastaví litry nebo m³ všem senzorům s vodou najednou (stav vodoměru,
spotřeby, změna, části dne); převod hodnot i statistik dělá Home Assistant. U jednotlivé
entity jde jednotku změnit i v jejím nastavení (⚙ → Jednotka měření) a integrace ji při
restartu nepřepíše, přenastaví ji jen při změně volby.

### Víc měřidel

Další měřidlo přidáš v **Zařízení a služby → VHS Benešov → Přidat službu**. Služba se
identifikuje **číslem měřidla**, které portál ukáže po přihlášení: stejné měřidlo pod druhým
loginem se odmítne, jiné se přidá jako samostatná služba s vlastními entitami
i historií. Při novém přihlášení (změna hesla) se odmítne login jiného měřidla, aby se
vodoměry nezaměnily.

## Senzory

Zařízení se jmenuje podle čísla měřidla. Názvy se na stránce zařízení řadí abecedně zároveň
logicky: stav, poslední odečet, den, týden, měsíc. Objemy se zobrazují v jednotce, kterou jsi
zvolil (výchozí litry, viz Jednotky); atributy s `_m3` v názvu jsou vždy v m³.

| Senzor | Stav | Atributy |
|---|---|---|
| Aktuální stav vodoměru | `912 027 l` (`912,027 m³`) | `stav_k_datu`, `stav_k_casu` |
| Poslední odečet | datum a čas | — |
| Spotřeba poslední úplný den | `875 l` | `den` |
| Spotřeba tento týden | `1 581 l` | — |
| Spotřeba v tomto měsíci | `20 751 l` | — |
| Změna oproti loňsku v % | `68 %` | `obdobi`, `spotreba_m3`, `loni_m3`, `rozdil_m3` |
| Změna oproti loňsku v objemu | `27 000 l` (`27 m³`) | `obdobi`, `spotreba_m3`, `loni_m3`, `zmena_procent` |
| Změna oproti loňsku za období | `6–8/2026 vs 6–8/2025` | — |

- **Aktuální stav vodoměru** je stav z číselníku na hlavní stránce portálu, tedy stejné číslo
  jako u *Poslední odečet*. Má `state_class: total_increasing`, takže ho Home Assistant
  ukládá do dlouhodobých statistik a jde přidat do Energy dashboardu.
- **Spotřeba poslední úplný den** je součet čtyř šestihodinových kroků dne, který portál zná
  celý. Den, ke kterému patří, ukazuje atribut `den` a v Diagnostice senzor *Den spotřeby*.
- **Spotřeba tento týden** se počítá od pondělí do posledního dne, který portál zná, včetně
  neúplného, takže do dalšího odečtu ještě roste.
- **Změna oproti loňsku** srovnává poslední tři úplné měsíce se stejnými třemi o rok dřív
  (např. 6–8/2026 proti 6–8/2025; které měsíce to jsou, ukazuje *Změna oproti loňsku za období*); měsíc posledního odečtu je neúplný a nepočítá se. Kladné
  číslo je růst, záporné pokles. Bez dat za loňský rok je stav `unknown`. Počet měsíců (1, 3
  nebo 6) jde změnit v nastavení; 12 nejde, portál nabízí jen 24 měsíců.
- Čas z portálu je místní. *Poslední odečet* je časové razítko, jeho tvar určuje tvůj profil.

### Ovládací prvky

- **Aktualizovat:** stáhne všechna data hned (i když se odečet nezměnil), nejvýš jednou za minutu.
- **Stáhnout historii:** znovu načte celou historii do statistik, nejdřív za den (viz níže).

### Diagnostika

| Entita | Stav | Atributy |
|---|---|---|
| `00–06 h`, `06–12 h`, `12–18 h`, `18–24 h` | `65 l`, `282 l`, `270 l`, `258 l` | `den`, `prumer_l`, `dnu_v_prumeru`, `podil_dne_procent` |
| Den spotřeby | `28. 9. 2026` | — |
| Poslední kontrola portálu | `30.09.2026 14:05` | `cas`, `posledni_pokus_uspesny`, `posledni_stazeni_dat`, `interval_kontroly_hodin` |
| Stav historie | `stahuji z portálu (38/53 měsíců)` | `postup`, `postup_procent`, `od`, `do`, `zaznamu`, `poznamka` |

**Části dne** ukazují litry v šestihodinovém kroku za poslední úplný den. `prumer_l` je průměr
téže části dne z posledních až 30 úplných dnů (noc se srovnává jen s dřívějšími nocemi), takže
vidíš, jestli je den neobvyklý. Šestihodinový krok je hrubý a **nepozná malý stálý únik** od
jedné sprchy nebo pár spláchnutí. *Poslední kontrola portálu* zůstává dostupná i po
neúspěšném stažení, právě tehdy je vidět, že poslední pokus selhal.

## Zpoždění dat

Portál zveřejňuje data **se zpožděním řádově hodin až půl dne**: odečet z 5:42 byl na portálu
vidět až večer. Podle sledování jednoho měřidla se čte dvakrát denně (v 5:42 a 17:42), na portálu
je vidět za 8 až 13 hodin a poslední úplný den bývá hotový až odpoledne až večer následujícího
dne. Nová data přibývají po dávkách, ne průběžně.

**Jak často se stahuje:** integrace se **každou hodinu** podívá jen na úvodní stránku portálu
(jeden požadavek) a zjistí, jestli je tam nový odečet. Až když se změní, stáhne všechna data
(pět stránek), takže data máš v Home Assistantu do hodiny po zveřejnění na portálu. Pro jistotu
se celé stažení udělá nejméně jednou denně i bez změny, a kdykoli tlačítkem *Aktualizovat*.
Interval se proto nenastavuje. *Poslední kontrola portálu* je čas posledního kontaktu s
portálem, kdy se naposled stáhla všechna data je v atributu `posledni_stazeni_dat`.

*Aktuální stav vodoměru* proto může být o toto zpoždění pozadu za skutečným
vodoměrem; kdy byl stav odečten, říká *Poslední odečet*. Navíc se zveřejňují jen dokončené
šestihodinové kroky, proto integrace ukazuje **poslední úplný den** místo „dnes" a „včera".
Časy si můžeš sledovat sám přes `tools/watch_updates.py`; jde o měření u jednoho měřidla a u jiného
se mohou lišit.

## Historie spotřeby

Při přidání s volbou **Stáhnout historická data** se na pozadí projdou všechny měsíce, které
portál nabízí (od května 2022), a zapíšou se do statistik senzoru *Aktuální stav vodoměru*.
Trvá to asi 3–4 minuty: požadavky jdou jeden po druhém s pauzou kolem 1,5 s a při odpovědi
429 nebo 503 klient počká a zkusí to znovu.

- **Průběh** je vidět v upozornění (zvoneček) a ve **Stavu historie**: `stahuji z portálu
  (38/53 měsíců)`, `zapisuji do statistik`, nakonec `hotovo` nebo `chyba`. Stav „hotovo"
  zůstane i po restartu.
- Nejjemnější data portálu jsou po **6 hodinách**. Hodinové statistiky se dopočítají, takže
  **den, týden, měsíc i rok jsou přesné**, jen rozlišení v rámci šesti hodin je odhad.
- Součet je ukotvený tak, aby na historii živá data navázala bez skoku (historie proto končí
  záporným součtem u nuly; Energy dashboard počítá jen rozdíly). Živá data se nepřepisují.
- **Ochrana portálu:** import je zátěž (řádově stovka požadavků), proto se nespustí znovu,
  dokud běží předchozí, a další je možný až **za den** po úspěšném (po chybě za čtvrt hodiny).
  Platí i pro tlačítko a restarty Home Assistantu. Když je import odmítnutý, ukáže to
  upozornění s časem, kdy bude možný.

## Energy dashboard a grafy

**Energy dashboard:** *Nastavení → Dashboardy → Energie → Přidat zdroj vody* a vyber
*Aktuální stav vodoměru*. Díky historii uvidíš spotřebu i za dobu před instalací.

`<měřidlo>` v příkladech nahraď číslem svého měřidla zapsaným jako ID (např. `12345-XX-0000001`
→ `12345_xx_0000001`). ID entit vznikají z názvů při prvním přidání a **v jazyce Home Assistantu**,
proto si je zkontroluj ve Vývojářských nástrojích → Stavy (filtr podle čísla měřidla).

**Karta přehledu:**

```yaml
type: entities
title: Voda
entities:
  - entity: sensor.<měřidlo>_aktualni_stav_vodomeru
    name: Stav vodoměru
  - entity: sensor.<měřidlo>_posledni_odecet
    name: Poslední odečet
  - type: divider
  - entity: sensor.<měřidlo>_den_spotreby
    name: Data za den
  - entity: sensor.<měřidlo>_spotreba_posledni_uplny_den
    name: Spotřeba
  - entity: sensor.<měřidlo>_spotreba_tento_tyden
    name: Tento týden
  - entity: sensor.<měřidlo>_spotreba_v_tomto_mesici
    name: Tento měsíc
  - entity: button.<měřidlo>_aktualizovat
    name: Aktualizovat
```

**Vývoj v čase a srovnání měsíců:** sloupce po měsících od roku 2022, stejné měsíce různých
let jsou vedle sebe (např. září 2025 a září 2026). Pro denní sloupce za 30 dní dej
`period: day` a `days_to_show: 30`.

```yaml
type: statistics-graph
title: Spotřeba po měsících
entities:
  - sensor.<měřidlo>_aktualni_stav_vodomeru
stat_types:
  - change
chart_type: bar
period: month
days_to_show: 730
```

V Energy dashboardu vyber měsíc a zapni **Porovnat**: srovnává se s **předchozím obdobím
stejné délky**, tedy s předchozím měsícem, ne se stejným měsícem loni. Nejstarší měsíc
(5/2022) je neúplný, data začínají 4. 5. 2022.

**Letos proti loňsku po dnech:** základní karty Home Assistantu nedovedou zobrazit stejné období
o rok dřív, proto je potřeba karta
[ApexCharts Card](https://github.com/RomRider/apexcharts-card) (*HACS → Frontend → Stáhnout*,
pak obnovit stránku). Řada *Loni* se posune o 365 dní a leží přes letošní.

```yaml
type: custom:apexcharts-card
header:
  show: true
  title: Spotřeba po dnech (poslední 2 měsíce vs. loni)
graph_span: 60d
span:
  end: day
chart_type: line
apex_config:
  chart:
    height: 220
  legend:
    fontSize: 12px
series:
  - entity: sensor.<měřidlo>_aktualni_stav_vodomeru
    name: Letos
    type: area
    color: "#4fc3f7"
    stroke_width: 2
    opacity: 0.45
    curve: smooth
    statistics:
      type: change
      period: day
      align: start
    group_by:
      func: last
      duration: 1d
  - entity: sensor.<měřidlo>_aktualni_stav_vodomeru
    name: Loni
    type: line
    color: "#ff9800"
    stroke_width: 2
    stroke_dash: 3
    curve: smooth
    offset: -365d
    statistics:
      type: change
      period: day
      align: start
    group_by:
      func: last
      duration: 1d
```

Loňská data tam jsou jen po stažení historie. Poslední den bývá neúplný (portál data doplňuje se
zpožděním), proto čára na konci krátce klesne; po dopočtu portálu se to srovná.

## Automatizace

Upozornění na neobvykle velkou denní spotřebu (například prasklá hadice). Spustí se až po
odečtu, který den dokončí, protože portál zná spotřebu se zpožděním:

```yaml
automation:
  - alias: Velká spotřeba vody
    triggers:
      - trigger: numeric_state
        entity_id: sensor.<měřidlo>_spotreba_posledni_uplny_den
        above: 1500
    actions:
      - action: notify.mobile_app
        data:
          message: >-
            Spotřeba vody za
            {{ state_attr('sensor.<měřidlo>_spotreba_posledni_uplny_den', 'den') }}
            byla {{ states('sensor.<měřidlo>_spotreba_posledni_uplny_den') }} l.
```

`numeric_state` se spustí jednou při překročení hranice, ne při každém stažení. Hranici uprav
podle své spotřeby.

Upozornění, když vodoměr dlouho neposlal data (práh 36 hodin, protože portál zveřejňuje
data se zpožděním):

```yaml
automation:
  - alias: Vodoměr mlčí
    triggers:
      - trigger: template
        value_template: >-
          {{ now() - states('sensor.<měřidlo>_posledni_odecet') | as_datetime > timedelta(hours=36) }}
    actions:
      - action: notify.mobile_app
        data:
          message: Vodoměr neposlal data už přes 36 hodin.
```

## Jak to funguje

Portál je ASP.NET WebForms aplikace bez API. Klient se přihlásí formulářem `Login.aspx`
(`__VIEWSTATE`, `__EVENTVALIDATION`), autentizační cookie `SE_Pilote_Cookie` drží sám, protože
ji `aiohttp` CookieJar odmítá, a po vypršení session se přihlásí znovu a původní požadavek
zopakuje. Data jsou vložená v HTML jako pole grafů jqPlot (`Site_Energie.aspx?Affichage=…`):

| Údaj | Stránka | Jednotka |
|---|---|---|
| Denní spotřeba | `ConsoJour` | litry |
| Měsíční spotřeba | `ConsoMois` | m³ |
| Stav měřidla na konci dne | `IndexJour` | m³ |
| Spotřeba po 6 hodinách | `CourbeMois` | litry |

Starší měsíce jdou načíst parametry `Annee` a `Mois`. Stav vodoměru se čte z číselníku na
hlavní stránce. Alarmy z portálu (netěsnost, nadměrná spotřeba) integrace zatím nečte.

## Vývoj

```bash
python3 -m venv .venv && .venv/bin/pip install aiohttp -r requirements-test.txt
.venv/bin/python -m pytest
```

Testy ověřují parsery na markupu ze skutečného portálu (osobní údaje nahrazené), klienta proti
falešnému portálu včetně vypršení session a integraci proti skutečnému jádru Home Assistantu.

Nástroje pro ověření proti živému portálu (údaje jen z proměnných prostředí, do repa se
neukládají):

```bash
export VHS_LOGIN='…' VHS_PASSWORD='…'
.venv/bin/python tools/live_check.py --dump   # stránky uloží do tools/*.local.html
.venv/bin/python tools/live_history.py        # zkouška stažení celé historie
.venv/bin/python tools/diagnose_login.py      # diagnostika přihlášení
.venv/bin/python tools/analyze_night.py       # rozbor noční spotřeby z celé historie
.venv/bin/python tools/compare_invoice.py 2026-05-22 817 2026-08-24 884  # stavy z faktury proti portálu
.venv/bin/python tools/watch_updates.py       # sleduje, kdy se na portálu objevují nová data
./tools/install_watch_cron.sh                 # totéž na pozadí přes cron (každých 10 minut)
```

Před zveřejněním změn pusť `./tools/kontrola-udaju.sh`.

## Přístup k datům

Data poskytuje portál dálkových odečtů Vodohospodářské společnosti Benešov. Integrace je
neoficiální, přihlašuje se uživatelskými údaji, čte běžné stránky portálu a nepoužívá žádné
neveřejné rozhraní. Běžná kontrola (každou hodinu) je jeden požadavek, celé stažení pět stránek jen při novém
odečtu, historie se stahuje s pauzami jen
na požádání.

Pokud si provozovatel nepřeje, aby integrace existovala, otevřete prosím
[issue](https://github.com/joazif/ha-vhs-benesov/issues), repozitář bude stažen.
