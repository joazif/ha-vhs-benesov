# Změny

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
