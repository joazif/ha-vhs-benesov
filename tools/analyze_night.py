"""Rozbor noční spotřeby (krok 0-6 h) z celé historie portálu.

    export VHS_LOGIN='...' VHS_PASSWORD='...'
    .venv/bin/python tools/analyze_night.py            # stáhne historii (1-2 min) a uloží ji
    .venv/bin/python tools/analyze_night.py --cached   # použije dřív stažená data

Stažená křivka se uloží do tools/night_curve.local.json (v .gitignore).
Vypíše, jak se noční spotřeba vyvíjela po měsících (medián, nižší decil a
minimum), po dnech v týdnu a jak se liší poslední tři měsíce od stejného období
loni. Malý stálý únik se pozná podle toho, že roste **nižší decil** nočních
hodnot (to, pod co se noc skoro nikdy nedostane), ne jen průměr.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import pathlib
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

ROOT = pathlib.Path(__file__).resolve().parents[1]
CACHE = ROOT / "tools" / "night_curve.local.json"
WEEKDAYS = ("po", "út", "st", "čt", "pá", "so", "ne")


def _load_api():
    spec = importlib.util.spec_from_file_location(
        "vhs_api", ROOT / "custom_components" / "vhs_benesov" / "api.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["vhs_api"] = module
    spec.loader.exec_module(module)
    return module


def percentile(values: list[float], pct: float) -> float:
    """Jednoduchý percentil s lineární interpolací (pct 0-100)."""
    ordered = sorted(values)
    if not ordered:
        raise ValueError("žádné hodnoty")
    pos = (len(ordered) - 1) * pct / 100
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def night_values(days: dict[date, dict[int, float]]) -> dict[date, float]:
    """Noční krok (0-6 h) za každý úplný den."""
    return {day: parts[0] for day, parts in days.items()}


def by_month(nights: dict[date, float]) -> list[tuple[str, int, float, float, float]]:
    groups: dict[str, list[float]] = defaultdict(list)
    for day, litres in nights.items():
        groups[f"{day.year}-{day.month:02d}"].append(litres)
    return [
        (month, len(v), statistics.median(v), percentile(v, 10), min(v))
        for month, v in sorted(groups.items())
    ]


def by_weekday(nights: dict[date, float]) -> list[tuple[str, int, float]]:
    groups: dict[int, list[float]] = defaultdict(list)
    for day, litres in nights.items():
        groups[day.weekday()].append(litres)
    return [
        (WEEKDAYS[i], len(groups[i]), statistics.median(groups[i]))
        for i in range(7)
        if groups[i]
    ]


def recent_vs_last_year(nights: dict[date, float], days: int = 90) -> dict | None:
    """Posledních ``days`` dní proti stejnému období o rok dřív."""
    if not nights:
        return None
    end = max(nights)
    start = end - timedelta(days=days - 1)
    now = [v for d, v in nights.items() if start <= d <= end]
    ly_start, ly_end = start - timedelta(days=365), end - timedelta(days=365)
    then = [v for d, v in nights.items() if ly_start <= d <= ly_end]
    if len(now) < 20 or len(then) < 20:
        return None
    return {
        "od": start, "do": end,
        "nyni_median": statistics.median(now), "nyni_p10": percentile(now, 10),
        "loni_median": statistics.median(then), "loni_p10": percentile(then, 10),
    }


def night_share(days: dict[date, dict[int, float]]) -> float | None:
    """Jaký podíl denní spotřeby tvoří noc (medián přes všechny úplné dny)."""
    shares = [p[0] / sum(p.values()) for p in days.values() if sum(p.values()) > 0]
    return statistics.median(shares) if shares else None


def bar(value: float, scale: float, width: int = 30) -> str:
    return "█" * max(0, min(width, round(value / scale * width)))


def report(days: dict[date, dict[int, float]]) -> str:
    nights = night_values(days)
    lines: list[str] = []
    out = lines.append
    if not nights:
        return "Žádné úplné dny, není z čeho počítat."
    out(f"Úplných dnů: {len(nights)} ({min(nights)} až {max(nights)})")
    share = night_share(days)
    if share is not None:
        out(f"Noc tvoří obvykle {share * 100:.0f} % denní spotřeby (medián přes dny).")
    out("")
    out("PO MĚSÍCÍCH (litry za noc 0-6 h)")
    out("  měsíc    dnů  medián   dolní decil  minimum")
    months = by_month(nights)
    scale = max(m[2] for m in months) or 1
    for month, n, med, p10, low in months:
        out(f"  {month}  {n:3d}  {med:6.0f}  {p10:11.0f}  {low:7.0f}  {bar(med, scale)}")
    out("  (dolní decil = pod tuhle hodnotu spadne jen desetina nocí; roste-li,")
    out("   zvedá se noční základ - to je příznak stálého odběru)")
    out("")
    out("PO DNECH V TÝDNU (medián noci)")
    wd = by_weekday(nights)
    wscale = max(w[2] for w in wd) or 1
    for name, n, med in wd:
        out(f"  {name}  {n:3d}  {med:5.0f}  {bar(med, wscale)}")
    out("  (nižší noc o víkendu = ráno se vstává později, tedy lidé, ne únik)")
    out("")
    cmp_ = recent_vs_last_year(nights)
    if cmp_:
        out(f"POSLEDNÍCH 90 DNŮ ({cmp_['od']} až {cmp_['do']}) PROTI STEJNÉMU OBDOBÍ LONI")
        out(f"  medián:       {cmp_['nyni_median']:.0f} l  (loni {cmp_['loni_median']:.0f} l)")
        out(f"  dolní decil:  {cmp_['nyni_p10']:.0f} l  (loni {cmp_['loni_p10']:.0f} l)")
        diff = cmp_["nyni_p10"] - cmp_["loni_p10"]
        out(f"  změna základu noci: {diff:+.0f} l za noc")
    else:
        out("Pro srovnání s loňskem není dost dat.")
    return "\n".join(lines)


def _save(curve) -> None:
    CACHE.write_text(
        json.dumps({p.at.isoformat(): p.value for p in curve}), encoding="utf-8"
    )


def _load(api):
    raw = json.loads(CACHE.read_text(encoding="utf-8"))
    return [api.PointValue(datetime.fromisoformat(k), float(v)) for k, v in sorted(raw.items())]


async def _fetch(api):
    client = api.VhsBenesovClient(os.environ["VHS_LOGIN"], os.environ["VHS_PASSWORD"])
    try:
        data = await client.async_fetch_history(
            progress=lambda done, total: print(f"  měsíc {done}/{total}", end="\r", flush=True)
        )
    finally:
        await client.async_close()
    print()
    return [api.PointValue(at, v) for at, v in sorted(data.curve_liters.items())]


def main() -> None:
    api = _load_api()
    if "--cached" in sys.argv:
        if not CACHE.exists():
            sys.exit("Chybí uložená data, spusť nejdřív bez --cached.")
        curve = _load(api)
    else:
        curve = asyncio.run(_fetch(api))
        _save(curve)
        print(f"Data uložena do {CACHE.relative_to(ROOT)}")
    print(report(api.complete_days(curve)))


if __name__ == "__main__":
    main()
