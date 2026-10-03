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
    *,
    before: datetime | None = None,
) -> list[HourRow]:
    """Hodinové řádky ze stavů měřidla a šestihodinové křivky.

    ``index_end`` je stav na konci dne (u posledního dne jen stav posledního
    odečtu), ``curve`` spotřeba v litrech s klíčem
    v místním čase (naivní datetime, začátek kroku). ``before`` (UTC) odřízne
    hodiny od tohoto okamžiku výš; tam už statistiky zapisuje HA samo.
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
                if before is None or hour < before:
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


# Rozdíl stavů, který se ještě bere jako shoda (plovoucí desetinná čárka).
ANCHOR_TOLERANCE = 1e-6


def _up_to_anchor(rows: list[HourRow], anchor_reading: float) -> list[HourRow]:
    """Odříznout řádky, jejichž stav je nad kotvou.

    Kotva je stav senzoru v okamžiku, kdy začala živá data, a ten kvůli zpoždění
    portálu (hodiny až půl dne) odpovídá starší době než konec historie. Historie
    by pak skončila kladným součtem a živá data (začínají nulou) by ho "smazala"
    záporným skokem. Zbytek spotřeby po kotvě doplní živá data.
    """
    end = len(rows)
    while end and rows[end - 1].state > anchor_reading + ANCHOR_TOLERANCE:
        end -= 1
    return rows[:end]


def flat_rows(after: datetime, before: datetime, anchor_reading: float) -> list[dict]:
    """Hodiny po konci historie až před živá data se stavem kotvy a nulovým součtem.

    Jimi se při opakovaném importu přepíšou řádky, které starší verze zapsala nad
    kotvou (jinak by v databázi zůstaly s chybným součtem).
    """
    out: list[dict] = []
    cur = after + timedelta(hours=1)
    while cur < before:
        out.append({"start": cur, "state": anchor_reading, "sum": 0.0})
        cur += timedelta(hours=1)
    return out


def to_statistics(rows: list[HourRow], anchor_reading: float) -> list[dict]:
    """Řádky pro ``async_import_statistics`` se součtem ukotveným na konci.

    HA u živých dat začíná součet nulou v okamžiku, kdy senzor poprvé zapíše
    stav ``anchor_reading``. Historie proto musí končit součtem
    ``poslední stav - anchor_reading`` (záporným nebo nulovým) a před ním se
    odečítají přírůstky. Tak součet navazuje bez skoku. Řádky se stavem nad
    kotvou se odříznou (viz ``_up_to_anchor``).
    """
    rows = _up_to_anchor(rows, anchor_reading)
    if not rows:
        return []
    sums = [0.0] * len(rows)
    sums[-1] = rows[-1].state - anchor_reading
    for i in range(len(rows) - 2, -1, -1):
        sums[i] = sums[i + 1] - rows[i + 1].increase
    return [
        {"start": row.start, "state": row.state, "sum": total}
        for row, total in zip(rows, sums, strict=True)
    ]
