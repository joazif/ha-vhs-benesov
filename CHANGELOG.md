# Změny

## 0.1.1

### Přidáno
- **Vlastní statistika spotřeby** `vhs_benesov:<měřidlo>_consumption` („Spotřeba vody (VHS
  Benešov)“, m³, hodinové řádky se stavem a součtem). Spotřeba je zapsaná do hodiny, do které
  patří, a ne do hodiny, kdy portál zveřejnil číslo (statistika senzoru stavu je kvůli tomu
  posunutá o hodiny až půl dne). Funguje v grafech i v Energy dashboardu.
- **Průběžné doplňování:** po každé změně dat na portálu se přepočítají poslední dny a zapíšou
  znovu. Počítají se jen dokončené šestihodinové kroky, neúplný poslední den se po zveřejnění
  zbytku dopíše, opakované zapsání týchž dat nic nezmění a součet nikdy neklesne. Na přelomu
  měsíce se předchozí měsíc dotáhne z portálu jen jednou, dokud není úplný.
- *Stav historie* ukazuje datum a čas posledního doběhnutí (`hotovo (04.10.2026 09:12)`) a má
  atributy `statistika`, `zapsano_do` a `dokonceno`.

### Opraveno
- Na začátku měsíce, než portál zveřejní první odečet, už načtení dat nekončí chybou (senzory
  nezůstanou na hodiny nedostupné): stavy se vezmou z měsíce posledního odečtu.
- Automatické opakování neúspěšného importu historie je řidší (po 6 hodinách), ať se portál
  nezatěžuje, když nejede. Po startu platí čtvrt hodiny jako dřív.
- Minimální verze Home Assistantu je 2026.9 (verze, se kterou je integrace testovaná; dřív
  zbytečně slibovala 2024.6, které nemá rozhraní statistik, jež kód používá).

### Změněno
- *Aktuální stav vodoměru* už nemá `state_class: total_increasing`, takže Home Assistant k němu
  nevede vlastní (kvůli zpoždění portálu posunutou) statistiku. Existující posunutou statistiku
  nabídne HA smazat ve Vývojářských nástrojích → Statistiky.
- Import historie plní novou statistiku od nuly (bez kotvy a hranice živých dat). Statistika
  senzoru stavu se už nezapisuje.
- Při aktualizaci ze starší verze se nová statistika sama naplní celou historií z portálu
  (po uplynutí odpočinku importu, nejvýš jednou za den).
- README a příklady karet používají novou statistiku (v kartě ApexCharts s `transform` na litry).

## 0.1.0

První verze.

- **Senzory:** aktuální stav vodoměru pro Energy dashboard, poslední odečet, spotřeba
  za poslední úplný den, tento týden a tento měsíc, změna oproti loňsku v %, v objemu
  a za období (počet měsíců 1, 3 nebo 6 v nastavení).
- **Jednotky objemu:** litry (výchozí) nebo m³, při přidání integrace i později v nastavení.
- **Diagnostika:** spotřeba po částech dne (`00–06 h` až `18–24 h`), poslední kontrola
  portálu a stav importu historie.
- **Historie:** jednorázový import celé historie spotřeby do statistik, s ochranou portálu
  (znovu až za den), pomalejším stahováním a průběhem viditelným v HA.
- **Více měřidel:** služba se identifikuje číslem měřidla.
- **Tlačítka:** Aktualizovat (stáhne vše hned, nejvýš jednou za minutu) a Stáhnout historii.
- Přihlášení a nové přihlášení při změně hesla.
- **Stahování podle změny odečtu:** každou hodinu se zjistí na úvodní stránce portálu (jeden
  požadavek), jestli je nový odečet, a všechna data se stáhnou jen při změně (a aspoň jednou denně). Data jsou v Home Assistantu do hodiny po zveřejnění na portálu.
- Nástroje: ověření proti živému portálu, sledování aktualizací portálu na pozadí
  (`tools/watch_updates.py`), rozbor noční spotřeby.
