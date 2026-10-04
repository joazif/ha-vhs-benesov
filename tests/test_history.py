"""Test skládání hodinových statistik."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from conftest import ROOT  # noqa: F401  (načte api a nastaví cestu)
import importlib.util
import sys

_spec = importlib.util.spec_from_file_location(
    "vhs_history", ROOT / "custom_components" / "vhs_benesov" / "history.py"
)
history = importlib.util.module_from_spec(_spec)
sys.modules["vhs_history"] = history
_spec.loader.exec_module(history)

PRAGUE = ZoneInfo("Europe/Prague")
D = date(2026, 9, 2)


def curve(day, values):
    return {
        datetime(day.year, day.month, day.day, h): v
        for h, v in zip((0, 6, 12, 18), values, strict=True)
    }


def test_day_is_distributed_by_curve_and_ends_at_index():
    rows = history.build_hourly(
        {date(2026, 9, 1): 100.0, D: 100.6}, curve(D, [100, 200, 100, 200]), PRAGUE
    )
    day = [r for r in rows if r.start.astimezone(PRAGUE).date() == D]
    assert len(day) == 24
    by_local = {r.start.astimezone(PRAGUE).hour: r.state for r in day}
    assert by_local[5] == pytest.approx(100.1)   # konec kroku 00-06
    assert by_local[11] == pytest.approx(100.3)  # + 0,2 m³
    assert by_local[17] == pytest.approx(100.4)
    assert by_local[23] == pytest.approx(100.6)
    assert sum(r.increase for r in day) == pytest.approx(0.6)


def _closing_day(day, reading):
    """Poslední den řady s úplnou křivkou; bez něj by byl testovaný den "poslední" a neúplný."""
    return {day: reading}, curve(day, [100, 100, 100, 100])


def test_missing_curve_spreads_evenly():
    tail_idx, tail_curve = _closing_day(date(2026, 9, 3), 10.5)
    rows = history.build_hourly({date(2026, 9, 1): 10.0, D: 10.24, **tail_idx}, tail_curve, PRAGUE)
    day = [r for r in rows if r.start.astimezone(PRAGUE).date() == D]
    assert {round(r.increase, 6) for r in day} == {0.01}
    assert day[-1].state == pytest.approx(10.24)


def test_first_day_uses_curve_to_find_start():
    rows = history.build_hourly({D: 50.6}, curve(D, [100, 200, 100, 200]), PRAGUE)
    assert rows[0].state - rows[0].increase == pytest.approx(50.0)
    assert history.build_hourly({D: 50.6}, {}, PRAGUE) == []  # bez kotvy nejde


def test_reset_day_is_skipped():
    tail_idx, tail_curve = _closing_day(date(2026, 9, 4), 1.5)
    rows = history.build_hourly(
        {date(2026, 9, 1): 900.0, D: 0.5, date(2026, 9, 3): 1.0, **tail_idx}, tail_curve, PRAGUE
    )
    days = {r.start.astimezone(PRAGUE).date() for r in rows}
    assert D not in days and date(2026, 9, 3) in days


@pytest.mark.parametrize(("day", "hours"), [
    (date(2026, 3, 29), 23), (date(2026, 10, 25), 25), (date(2026, 6, 1), 24),
])
def test_dst_days_have_right_number_of_hours(day, hours):
    prev = day - timedelta(days=1)
    rows = history.build_hourly(
        {prev: 1.0, day: 2.0}, curve(day, [10, 20, 30, 40]), PRAGUE
    )
    mine = [r for r in rows if r.start.astimezone(PRAGUE).date() == day]
    assert len(mine) == hours
    assert mine[-1].state == pytest.approx(2.0)
    assert all(a.state <= b.state for a, b in zip(mine, mine[1:]))


def test_gap_keeps_consumption():
    tail_idx, tail_curve = _closing_day(date(2026, 9, 6), 12.0)
    idx = {date(2026, 9, 1): 10.0, date(2026, 9, 2): 10.5, date(2026, 9, 5): 11.5, **tail_idx}
    rows = history.build_hourly(idx, tail_curve, PRAGUE)
    first_after_gap = next(r for r in rows if r.start.astimezone(PRAGUE).date() == date(2026, 9, 5))
    days_5 = [r for r in rows if r.start.astimezone(PRAGUE).date() == date(2026, 9, 5)]
    assert sum(r.increase for r in days_5) == pytest.approx(1.0)  # 11,5 - 10,5, nic se neztratí
    assert first_after_gap.state > 10.5


def test_unfinished_last_day_is_not_spread_over_future_hours():
    """Dnešek: portál zná jen kroky do poledne, po nich už spotřeba nepřibývá."""
    today = date(2026, 9, 29)
    partial = {
        datetime(2026, 9, 29, 0): 50.0,
        datetime(2026, 9, 29, 6): 150.0,
        datetime(2026, 9, 29, 12): 100.0,
    }  # chybí krok 18:00
    rows = history.build_hourly({date(2026, 9, 28): 911.0, today: 911.3}, partial, PRAGUE)
    mine = [r for r in rows if r.start.astimezone(PRAGUE).date() == today]
    late = [r for r in mine if r.start.astimezone(PRAGUE).hour >= 18]
    assert all(r.increase == 0 for r in late)
    assert mine[-1].state == pytest.approx(911.3)
    assert sum(r.increase for r in mine) == pytest.approx(0.3)


def test_missing_bucket_on_older_day_still_spreads_evenly():
    older, newer = date(2026, 9, 27), date(2026, 9, 28)
    partial = {datetime(2026, 9, 27, 0): 50.0}
    rows = history.build_hourly({date(2026, 9, 26): 10.0, older: 10.24, newer: 10.5}, partial, PRAGUE)
    day = [r for r in rows if r.start.astimezone(PRAGUE).date() == older]
    assert {round(r.increase, 6) for r in day} == {0.01}  # rovnoměrně, žádné dohady


# ---------------------------------------------------------------------------
# Poslední bod řady je stav posledního odečtu, ne konce dne
# ---------------------------------------------------------------------------


def test_partial_last_day_uses_published_buckets_instead_of_stretching_them_to_the_reading():
    """Skutečný případ: řada končí 29. 9. = 911,695 (odečet 17:42), křivka zná jen 81 a 153 l."""
    idx = {date(2026, 9, 28): 911.280, date(2026, 9, 29): 911.695}
    partial = {datetime(2026, 9, 29, 0): 81.0, datetime(2026, 9, 29, 6): 153.0}
    rows = [
        r for r in history.build_hourly(idx, partial, PRAGUE)
        if r.start.astimezone(PRAGUE).date() == date(2026, 9, 29)
    ]
    night = sum(r.increase for r in rows if r.start.astimezone(PRAGUE).hour < 6)
    morning = sum(r.increase for r in rows if 6 <= r.start.astimezone(PRAGUE).hour < 12)
    assert night == pytest.approx(0.081) and morning == pytest.approx(0.153)   # ne 144 a 271 l
    assert sum(r.increase for r in rows) == pytest.approx(0.234)
    # Stav roste jen do 12:00 a končí na 911,514, ne na 911,695 (to je až v 17:42).
    assert rows[-1].state == pytest.approx(911.280 + 0.234)
    assert rows[-1].state < 911.695


def test_partial_last_day_without_buckets_or_previous_reading_is_skipped():
    idx = {date(2026, 9, 28): 911.280, date(2026, 9, 29): 911.695}
    no_curve_today = history.build_hourly(idx, {}, PRAGUE)
    assert all(r.start.astimezone(PRAGUE).date() != date(2026, 9, 29) for r in no_curve_today)
    alone = history.build_hourly(
        {date(2026, 9, 29): 911.695}, {datetime(2026, 9, 29, 0): 81.0}, PRAGUE
    )
    assert alone == []


def test_complete_last_day_is_still_anchored_to_its_end_reading():
    idx = {date(2026, 9, 28): 911.280, date(2026, 9, 29): 911.988}          # po večerním doplnění
    full = curve(date(2026, 9, 29), [81, 153, 192, 280])
    rows = [
        r for r in history.build_hourly(idx, full, PRAGUE)
        if r.start.astimezone(PRAGUE).date() == date(2026, 9, 29)
    ]
    assert rows[-1].state == pytest.approx(911.988)
    assert sum(r.increase for r in rows) == pytest.approx(0.708)


# ---------------------------------------------------------------------------
# Externí statistika: řada, kontrolní bod a navazování
# ---------------------------------------------------------------------------

BASE = date(2026, 9, 1)


def _data(last_day: int, complete_through: int):
    """Stav roste o 0,5 m³ denně od 100; křivka (4 × 125 l) je úplná do ``complete_through``.

    Poslední den řady (``last_day``) je stav posledního odečtu, tedy odhad konce dne.
    """
    index = {BASE + timedelta(days=i): 100.0 + 0.5 * (i + 1) for i in range(last_day)}
    cv: dict = {}
    for i in range(complete_through):
        cv.update(curve(BASE + timedelta(days=i), [125, 125, 125, 125]))
    return index, cv


def _midnight(day):
    return datetime(day.year, day.month, day.day, tzinfo=PRAGUE).astimezone(UTC)


def test_series_from_scratch_sums_the_increases():
    index, cv = _data(last_day=6, complete_through=5)
    update = history.series_update(index, cv, PRAGUE)
    sums = [r["sum"] for r in update.rows]
    assert sums == sorted(sums)                              # součet jen roste
    assert update.rows[0]["sum"] > 0
    assert update.rows[-1]["sum"] == pytest.approx(0.5 * 5, abs=1e-9)    # pět úplných dnů
    # Poslední den řady není konečný, kontrolní bod stojí za posledním úplným dnem před ním.
    assert update.checkpoint.day == BASE + timedelta(days=5)
    assert update.checkpoint.state == pytest.approx(index[BASE + timedelta(days=4)])
    assert update.checkpoint.total == pytest.approx(update.rows[-1]["sum"])


def test_incremental_update_equals_full_rebuild_and_never_goes_back():
    idx_a, cv_a = _data(last_day=4, complete_through=3)
    first = history.series_update(idx_a, cv_a, PRAGUE)
    idx_b, cv_b = _data(last_day=8, complete_through=7)
    second = history.series_update(idx_b, cv_b, PRAGUE, first.checkpoint)
    full = history.series_update(idx_b, cv_b, PRAGUE)

    cut = _midnight(first.checkpoint.day)
    expected = [r for r in full.rows if r["start"] >= cut]
    assert [r["start"] for r in second.rows] == [r["start"] for r in expected]
    for got, want in zip(second.rows, expected, strict=True):
        assert got["state"] == pytest.approx(want["state"])
        assert got["sum"] == pytest.approx(want["sum"])
    assert second.checkpoint.day == full.checkpoint.day
    # Zapsané dřív + nové dohromady = celá řada bez skoku a bez záporného přírůstku.
    joined = [r["sum"] for r in first.rows if r["start"] < cut] + [r["sum"] for r in second.rows]
    assert joined == sorted(joined)


def test_repeating_the_same_data_changes_nothing():
    index, cv = _data(last_day=6, complete_through=5)
    once = history.series_update(index, cv, PRAGUE)
    again = history.series_update(index, cv, PRAGUE, once.checkpoint)
    twice = history.series_update(index, cv, PRAGUE, again.checkpoint)
    assert again.rows == twice.rows
    assert again.checkpoint == twice.checkpoint == once.checkpoint


def test_unfinished_day_is_rewritten_when_the_portal_completes_it():
    # Poslední úplný den je 3, den 4 má jen první dva kroky (06:00 a dál chybí).
    idx, cv = _data(last_day=4, complete_through=3)
    day4 = BASE + timedelta(days=3)
    cv.update({datetime(day4.year, day4.month, day4.day, 0): 125.0,
               datetime(day4.year, day4.month, day4.day, 6): 125.0})
    partial = history.series_update(idx, cv, PRAGUE)
    assert partial.checkpoint.day == day4                    # neúplný poslední den se nezafixuje
    partial_day = [r for r in partial.rows if r["start"] >= _midnight(day4)]
    assert sum(1 for _ in partial_day) == 12                 # jen hodiny známých kroků

    idx2, cv2 = _data(last_day=6, complete_through=5)
    later = history.series_update(idx2, cv2, PRAGUE, partial.checkpoint)
    rewritten = {r["start"]: r for r in later.rows}
    assert all(r["start"] in rewritten for r in partial_day)  # stejné hodiny se přepíšou
    assert later.rows[0]["sum"] >= partial.checkpoint.total   # navazuje, nevrací se
    assert later.rows[-1]["sum"] == pytest.approx(0.5 * 5, abs=1e-9)


def test_month_turn_continues_from_the_checkpoint_in_the_previous_month():
    sep30, oct1 = date(2026, 9, 30), date(2026, 10, 1)
    start = history.Checkpoint(sep30, 100.0, 5.0)
    # Nový měsíc ještě nemá žádnou křivku, říjnový stav je jen odhad posledního odečtu.
    index = {sep30: 100.5, oct1: 100.7}
    cv = curve(sep30, [125, 125, 125, 125])
    update = history.series_update(index, cv, PRAGUE, start)
    assert update.rows[0]["start"] == _midnight(sep30)
    assert update.rows[0]["sum"] > 5.0
    assert update.rows[-1]["sum"] == pytest.approx(5.5)
    assert update.checkpoint.day == oct1
    assert update.checkpoint.state == pytest.approx(100.5)
    assert update.checkpoint.total == pytest.approx(5.5)


def test_checkpoint_only_data_gives_no_rows_and_keeps_the_checkpoint():
    start = history.Checkpoint(date(2026, 9, 30), 100.0, 5.0)
    update = history.series_update({date(2026, 9, 1): 90.0}, {}, PRAGUE, start)
    assert update.rows == [] and update.checkpoint == start
    assert history.series_update({}, {}, PRAGUE).rows == []


def test_dst_change_day_with_25_hours_adds_up():
    day = date(2026, 10, 25)                                 # hodiny se vracejí zpět
    start = history.Checkpoint(day, 100.0, 0.0)
    index = {day: 100.5, day + timedelta(days=1): 100.6}
    cv = curve(day, [125, 125, 125, 125])
    update = history.series_update(index, cv, PRAGUE, start)
    day_rows = [r for r in update.rows if r["start"].astimezone(PRAGUE).date() == day]
    assert len(day_rows) == 25
    assert update.rows[-1]["sum"] == pytest.approx(0.5)
    sums = [r["sum"] for r in day_rows]
    assert sums == sorted(sums)
