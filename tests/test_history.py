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


def test_before_cuts_rows_and_gap_keeps_consumption():
    tail_idx, tail_curve = _closing_day(date(2026, 9, 6), 12.0)
    idx = {date(2026, 9, 1): 10.0, date(2026, 9, 2): 10.5, date(2026, 9, 5): 11.5, **tail_idx}
    rows = history.build_hourly(idx, tail_curve, PRAGUE)
    first_after_gap = next(r for r in rows if r.start.astimezone(PRAGUE).date() == date(2026, 9, 5))
    days_5 = [r for r in rows if r.start.astimezone(PRAGUE).date() == date(2026, 9, 5)]
    assert sum(r.increase for r in days_5) == pytest.approx(1.0)  # 11,5 - 10,5, nic se neztratí
    assert first_after_gap.state > 10.5
    cut = datetime(2026, 9, 2, 12, tzinfo=UTC)
    limited = history.build_hourly(idx, tail_curve, PRAGUE, before=cut)
    assert max(r.start for r in limited) < cut


def test_statistics_are_anchored_so_live_sum_continues_from_zero():
    rows = history.build_hourly(
        {date(2026, 9, 1): 100.0, D: 100.6}, curve(D, [100, 200, 100, 200]), PRAGUE
    )
    stats = history.to_statistics(rows, anchor_reading=100.6)
    assert stats[-1]["sum"] == pytest.approx(0.0)
    assert stats[-1]["state"] == pytest.approx(100.6)
    # součet roste o přírůstky, tedy sum = state - kotva
    for s in stats:
        assert s["sum"] == pytest.approx(s["state"] - 100.6)
    assert history.to_statistics([], 1.0) == []


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


def test_history_ending_above_the_anchor_is_cut_so_live_data_has_no_negative_jump():
    """Kotva (stav senzoru při instalaci) bývá o hodiny starší než konec historie."""
    rows = history.build_hourly(
        {date(2026, 9, 1): 100.0, D: 100.6}, curve(D, [100, 200, 100, 200]), PRAGUE
    )
    anchor = 100.3                                   # starší než konec historie (100,6)
    stats = history.to_statistics(rows, anchor)
    assert stats and len(stats) < len(rows)
    assert all(s["state"] <= anchor + 1e-6 for s in stats)
    assert all(s["sum"] <= 1e-9 for s in stats)       # žádný kladný součet
    assert stats[-1]["sum"] == pytest.approx(stats[-1]["state"] - anchor)
    # Přírůstky v historii zůstávají nezáporné, na hranici není záporný skok.
    sums = [s["sum"] for s in stats]
    assert all(b >= a - 1e-9 for a, b in zip(sums, sums[1:]))


def test_history_entirely_above_the_anchor_gives_nothing():
    rows = history.build_hourly(
        {date(2026, 9, 1): 100.0, D: 100.6}, curve(D, [100, 200, 100, 200]), PRAGUE
    )
    assert history.to_statistics(rows, 90.0) == []


def test_flat_rows_fill_the_hours_up_to_the_live_data():
    last = datetime(2026, 9, 29, 17, tzinfo=UTC)
    live_from = datetime(2026, 9, 30, 11, tzinfo=UTC)
    flat = history.flat_rows(last, live_from, 911.695)
    assert [r["start"] for r in flat][0] == datetime(2026, 9, 29, 18, tzinfo=UTC)
    assert [r["start"] for r in flat][-1] == datetime(2026, 9, 30, 10, tzinfo=UTC)
    assert len(flat) == 17
    assert all(r["state"] == 911.695 and r["sum"] == 0.0 for r in flat)
    assert history.flat_rows(last, last + timedelta(hours=1), 1.0) == []
