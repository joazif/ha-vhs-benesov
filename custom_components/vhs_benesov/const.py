"""Konstanty integrace VHS Benešov."""

DOMAIN = "vhs_benesov"

# Při přidání integrace: stáhnout celou historii z portálu do statistik.
CONF_IMPORT_HISTORY = "import_history"

# Za kolik posledních úplných měsíců se srovnává spotřeba s loňskem. Portál nabízí
# 24 měsíců včetně neúplného, takže 12 (potřeba 24 úplných) nejde.
CONF_COMPARE_MONTHS = "compare_months"
COMPARE_MONTHS_CHOICES = (1, 3, 6)
DEFAULT_COMPARE_MONTHS = 3

# Jednotky zobrazení pro senzory s objemem vody. Senzory mají nativně m³ (stav) a litry
# (den, části dne); zvolená jednotka se nastaví všem hromadně a jde ji změnit i u jednotlivé
# entity v jejím nastavení.
CONF_UNITS = "units"
# Klíče jsou malými písmeny (hassfest vyžaduje [a-z0-9_-] u překladů výběrů).
UNITS_CHOICES = ("l", "m3")
DEFAULT_UNITS = "l"
# Jednotka, kterou už integrace entitám nastavila; brání přepisování ručních změn při restartu.
DATA_UNITS_APPLIED = "units_applied"
