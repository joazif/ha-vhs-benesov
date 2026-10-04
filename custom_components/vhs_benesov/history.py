"""Skládání hodinových statistik z dat portálu (čistá logika bez závislosti na HA).

Portál dává dvě řady:

* stav měřidla na konci každého dne (m³) - ``IndexJour``. Rozdíl dvou po sobě
  jdoucích dní je spotřeba toho druhého (ověřeno: 892,442 - 891,795 = 647 l
  proti 645 l v denní spotřebě);
* spotřeba po 6 hodinách (litry) - ``CourbeMois``. Popisek ``06:00`` je
  *začátek* intervalu (součet čtyř kroků dne = denní spotřeba).

Statistiky HA jsou hodinové. Hodinu, kterou portál nezná, proto dopočítáme:
spotřeba šestihodinového kroku se rozloží rovnoměrně do jeho hodin a součet
za den se srovná na denní stav měřidla. Podrobnější než 6 hodin data nejsou.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo

BUCKET_HOURS = (0, 6, 12, 18)
BUCKET_STEP_HOURS = 6


@dataclass(slots=True, frozen=True)
class HourRow:
    """Jedna hodina: začátek (UTC), stav měřidla na jejím konci a přírůstek."""

    start: datetime
    state: float
    increase: float


def _hours(start: datetime, end: datetime) -> list[datetime]:
    """Začátky všech celých hodin v ``[start, end)`` (UTC)."""
    out: list[datetime] = []
    cur = start
    while cur < end:
        out.append(cur)
        cur += timedelta(hours=1)
    return out


def _day_buckets(day: date, tz: tzinfo) -> list[list[datetime]]:
    """Hodiny dne rozdělené na čtyři šestihodinové kroky podle místního času.

    V den změny času má některý krok o hodinu méně nebo více.
    """
    edges = [
        datetime(day.year, day.month, day.day, h, tzinfo=tz).astimezone(UTC)
        for h in BUCKET_HOURS
    ]
    nxt = day + timedelta(days=1)
    edges.append(datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz).astimezone(UTC))
    return [_hours(edges[i], edges[i + 1]) for i in range(4)]


def build_hourly(
    index_end: Mapping[date, float],
    curve: Mapping[datetime, float],
    tz: tzinfo,
) -> list[HourRow]:
    """Hodinové řádky ze stavů měřidla a šestihodinové křivky.

    ``index_end`` je stav na konci dne (u posledního dne jen stav posledního
    odečtu), ``curve`` spotřeba v litrech s klíčem
    v místním čase (naivní datetime, začátek kroku).
    Den bez použitelného stavu (chybí, měřidlo se vynulovalo) se přeskočí.
    """
    rows: list[HourRow] = []
    last_known: float | None = None
    days = sorted(index_end)
    for day in days:
        end_reading = index_end[day]
        buckets = _day_buckets(day, tz)
        litres = [
            curve.get(datetime(day.year, day.month, day.day, h)) for h in BUCKET_HOURS
        ]
        previous = index_end.get(day - timedelta(days=1))
        partial_last = day == days[-1] and any(v is None for v in litres)
        if partial_last:
            # Poslední bod řady je stav posledního odečtu, ne konce dne (v době
            # stahování den ještě neskončil), a křivka zná jen dokončené kroky.
            # Spotřebu proto bereme přímo z kroků, které portál zná, a na stav
            # odečtu ji nenatahujeme; zbytek dne doplní živá data. Bez kroků
            # nejde spotřebu do času zařadit, takový den se vynechá.
            known = [v for v in litres if v is not None]
            last_known = end_reading
            if not known or previous is None:
                continue
            litres = [v if v is not None else 0.0 for v in litres]
            total = sum(litres) / 1000
            have_curve = True
        else:
            have_curve = all(v is not None for v in litres)
            if previous is None:
                if have_curve:
                    previous = end_reading - sum(litres) / 1000  # type: ignore[arg-type]
                elif last_known is not None:
                    # Mezera v datech: spotřeba za chybějící dny se přičte k tomuto dni.
                    previous = last_known
                else:
                    last_known = end_reading
                    continue
            total = end_reading - previous
            last_known = end_reading
            if total < 0:
                continue  # výměna nebo vynulování měřidla

        if have_curve and sum(litres) > 0:  # type: ignore[arg-type]
            weights = [float(v) for v in litres]  # type: ignore[arg-type]
        else:
            weights = [float(len(b)) for b in buckets]  # rovnoměrně podle počtu hodin
        weight_sum = sum(weights)

        state = previous
        for bucket, weight in zip(buckets, weights, strict=True):
            if not bucket:
                continue
            per_hour = total * (weight / weight_sum) / len(bucket)
            for hour in bucket:
                state += per_hour
                rows.append(HourRow(hour, state, per_hour))

    rows.sort(key=lambda r: r.start)
    return _with_gap_increase(rows)


def _with_gap_increase(rows: list[HourRow]) -> list[HourRow]:
    """Hodina po mezeře (chybějící dny) dostane přírůstek podle změny stavu.

    Bez toho by se spotřeba za chybějící dny ztratila; záporná změna
    (výměna měřidla) se bere jako nula.
    """
    out: list[HourRow] = []
    prev: HourRow | None = None
    for row in rows:
        if prev is not None and row.start - prev.start > timedelta(hours=1):
            jump = max(0.0, row.state - prev.state)
            row = HourRow(row.start, row.state, jump)
        out.append(row)
        prev = row
    return out


@dataclass(slots=True, frozen=True)
class Checkpoint:
    """Do kam je statistika hotová a už se nemění.

    Řádky před ``day`` jsou konečné. ``state`` je stav měřidla na konci předchozího dne (m³)
    a ``total`` součet spotřeby (m³) od začátku statistiky do té doby; od nich se pokračuje,
    aniž by se musela číst databáze.
    """

    day: date
    state: float
    total: float


@dataclass(slots=True)
class SeriesUpdate:
    """Hodinové řádky statistiky a nový kontrolní bod (původní, když nic nepřibylo)."""

    rows: list[dict]
    checkpoint: Checkpoint | None


def _local_midnight(day: date, tz: tzinfo) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(UTC)


def series_update(
    index_end: Mapping[date, float],
    curve: Mapping[datetime, float],
    tz: tzinfo,
    checkpoint: Checkpoint | None = None,
) -> SeriesUpdate:
    """Hodinové řádky externí statistiky spotřeby (``state`` = stav měřidla, ``sum`` = součet).

    Bez kontrolního bodu se řada postaví z celých dat od nuly. S ním se přepočítají jen dny
    od ``checkpoint.day`` a součet naváže na ``checkpoint.total``. Zapsání týchž hodin znovu
    je bezpečné (HA řádky se stejným začátkem přepíše), proto se neúplný poslední den
    zapisuje a po dopočtu portálu se přepíše správnými hodnotami.

    Kontrolní bod se posune za poslední den, který je v křivce celý (všechny čtyři kroky) a není
    posledním dnem řady stavů (ten je jen odhad z posledního odečtu), takže už se nezmění.
    """
    if not index_end:
        return SeriesUpdate([], checkpoint)
    index = dict(index_end)
    if checkpoint is not None:
        index = {d: v for d, v in index_end.items() if d >= checkpoint.day}
        index[checkpoint.day - timedelta(days=1)] = checkpoint.state
    hourly = build_hourly(index, curve, tz)
    if checkpoint is not None:
        cut = _local_midnight(checkpoint.day, tz)
        hourly = [row for row in hourly if row.start >= cut]

    # Poslední den řady je neúplný, dokud portál nezveřejní všechny kroky; hodiny po posledním
    # známém kroku se nezapisují (nebyla by u nich pravda), doplní se po zveřejnění.
    last_day = max(index_end)
    known = [h for h in BUCKET_HOURS if datetime(last_day.year, last_day.month, last_day.day, h) in curve]
    if known and len(known) < len(BUCKET_HOURS):
        cutoff = datetime(last_day.year, last_day.month, last_day.day, max(known), tzinfo=tz)
        cutoff = cutoff.astimezone(UTC) + timedelta(hours=BUCKET_STEP_HOURS)
        hourly = [row for row in hourly if not (row.start.astimezone(tz).date() == last_day and row.start >= cutoff)]

    total = checkpoint.total if checkpoint is not None else 0.0
    rows: list[dict] = []
    for row in hourly:
        total += row.increase
        rows.append({"start": row.start, "state": row.state, "sum": total})

    complete = {
        at.date()
        for at in curve
        if all(datetime(at.year, at.month, at.day, h) in curve for h in BUCKET_HOURS)
    }
    first = checkpoint.day if checkpoint is not None else min(index_end)
    final = [d for d in complete if first <= d < last_day and d in index_end]
    if not final or not rows:
        return SeriesUpdate(rows, checkpoint)
    through = max(final)
    until = _local_midnight(through + timedelta(days=1), tz)
    done = [r for r in rows if r["start"] < until]
    if not done:
        return SeriesUpdate(rows, checkpoint)
    return SeriesUpdate(
        rows,
        Checkpoint(through + timedelta(days=1), float(index_end[through]), done[-1]["sum"]),
    )
