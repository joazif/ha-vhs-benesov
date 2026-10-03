"""Test rozboru noční spotřeby (na vymyšlených datech)."""

import importlib.util
import pathlib
import sys
from datetime import date, timedelta

from conftest import ROOT

_spec = importlib.util.spec_from_file_location("analyze_night", ROOT / "tools" / "analyze_night.py")
analyze = importlib.util.module_from_spec(_spec)
sys.modules["analyze_night"] = analyze
_spec.loader.exec_module(analyze)


def make_days(start, count, night):
    return {
        start + timedelta(days=i): {0: night(i), 6: 200.0, 12: 200.0, 18: 200.0}
        for i in range(count)
    }


def test_percentile_and_night_values():
    assert analyze.percentile([1, 2, 3, 4, 5], 0) == 1
    assert analyze.percentile([1, 2, 3, 4, 5], 100) == 5
    assert analyze.percentile([10, 20], 50) == 15
    days = make_days(date(2026, 9, 1), 3, lambda i: 50.0 + i)
    assert analyze.night_values(days) == {
        date(2026, 9, 1): 50.0, date(2026, 9, 2): 51.0, date(2026, 9, 3): 52.0,
    }


def test_month_and_weekday_grouping():
    nights = analyze.night_values(make_days(date(2026, 8, 30), 5, lambda i: 100.0 + i))
    months = analyze.by_month(nights)
    assert [m[0] for m in months] == ["2026-08", "2026-09"]
    assert months[0][1] == 2 and months[1][1] == 3  # 30.-31. 8. a 1.-3. 9.
    assert months[0][2] == 100.5                    # medián dvou hodnot
    weekdays = analyze.by_weekday(nights)
    assert sum(w[1] for w in weekdays) == 5


def test_recent_vs_last_year_detects_rising_baseline():
    # Loni noční základ kolem 50 l, letos kolem 90 l - jako vznikající stálý odběr.
    last_year = make_days(date(2025, 6, 1), 120, lambda i: 50.0 + (i % 7))
    this_year = make_days(date(2026, 6, 1), 120, lambda i: 90.0 + (i % 7))
    nights = analyze.night_values({**last_year, **this_year})
    cmp_ = analyze.recent_vs_last_year(nights, days=90)
    assert cmp_["nyni_p10"] - cmp_["loni_p10"] > 30
    assert cmp_["nyni_median"] > cmp_["loni_median"]


def test_recent_vs_last_year_needs_enough_data():
    nights = analyze.night_values(make_days(date(2026, 9, 1), 10, lambda i: 60.0))
    assert analyze.recent_vs_last_year(nights) is None


def test_report_is_readable_and_handles_empty():
    days = make_days(date(2025, 6, 1), 200, lambda i: 70.0)
    text = analyze.report(days)
    assert "PO MĚSÍCÍCH" in text and "PO DNECH V TÝDNU" in text
    assert "Noc tvoří obvykle" in text
    assert analyze.report({}).startswith("Žádné úplné dny")


# ---------------------------------------------------------------------------
# Sledování aktualizací portálu
# ---------------------------------------------------------------------------

_wspec = importlib.util.spec_from_file_location("watch_updates", ROOT / "tools" / "watch_updates.py")
watch = importlib.util.module_from_spec(_wspec)
sys.modules["watch_updates"] = watch
_wspec.loader.exec_module(watch)

from datetime import datetime  # noqa: E402

from conftest import api  # noqa: E402

HOME = (
    "<div>Poslední odečet z <span>29.09.2026 17:42</span></div>"
    "<script>var val = 0 + 1;if (val > 911.69500) val = 911.69500;</script>"
)
CURVE = "<script>$.jqplot('x', [[['2026-09-29 00:00', 81],['2026-09-29 06:00', 153]]], {});</script>"


def test_snapshot_reads_portal_state():
    snap = watch.snapshot(api, HOME, CURVE)
    assert snap.last_reading == datetime(2026, 9, 29, 17, 42)
    assert snap.reading_m3 == 911.695
    assert snap.last_bucket == datetime(2026, 9, 29, 6, 0) and snap.bucket_count == 2
    assert "911.695" in snap.line() and "29.09. 17:42" in snap.line()


def test_what_changed_detects_each_kind_of_update():
    base = watch.snapshot(api, HOME, CURVE)
    assert watch.what_changed(None, base) == ["první dotaz"]
    assert watch.what_changed(base, base) == []

    new_home = HOME.replace("29.09.2026 17:42", "30.09.2026 05:42").replace("911.69500", "912.02700")
    new_curve = CURVE.replace("]]]", "],['2026-09-29 12:00', 230]]]")
    changed = watch.what_changed(base, watch.snapshot(api, new_home, new_curve))
    assert changed == ["nový odečet", "změna číselníku", "nový krok křivky"]
    assert watch.what_changed(base, watch.snapshot(api, HOME, new_curve)) == ["nový krok křivky"]


def test_snapshot_with_empty_pages_does_not_crash():
    snap = watch.snapshot(api, "<html></html>", "<html></html>")
    assert snap.last_reading is None and snap.reading_m3 is None and snap.bucket_count == 0
    assert "?" in snap.line() and "prázdná" in snap.line()


# ---------------------------------------------------------------------------
# Režim --once pro cron
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402
import json  # noqa: E402


def test_parse_env_handles_quotes_comments_and_blank_lines():
    text = "# komentář\n\nVHS_LOGIN=abc\nVHS_PASSWORD='s = tajné'\nX=\"dvojité\"\nbez rovnítka\n"
    assert watch.parse_env(text) == {"VHS_LOGIN": "abc", "VHS_PASSWORD": "s = tajné", "X": "dvojité"}


def test_load_env_does_not_override_existing_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("VHS_LOGIN", "uz-nastaveno")
    monkeypatch.delenv("VHS_PASSWORD", raising=False)
    env = tmp_path / "watch.env"
    env.write_text("VHS_LOGIN=ze-souboru\nVHS_PASSWORD=heslo\n", encoding="utf-8")
    watch.load_env(env)
    assert __import__("os").environ["VHS_LOGIN"] == "uz-nastaveno"
    assert __import__("os").environ["VHS_PASSWORD"] == "heslo"


def test_state_roundtrip_and_bad_file(tmp_path):
    snap = watch.snapshot(api, HOME, CURVE)
    path = tmp_path / "state.json"
    watch.save_state(path, snap, datetime(2026, 9, 30, 19, 40))
    loaded, heartbeat = watch.load_state(path)
    assert loaded == snap and heartbeat == datetime(2026, 9, 30, 19, 40)
    path.write_text("{rozbité", encoding="utf-8")
    assert watch.load_state(path) == (None, None)
    assert watch.load_state(tmp_path / "neexistuje.json") == (None, None)


def test_decide_logs_changes_and_hourly_heartbeat_only():
    snap = watch.snapshot(api, HOME, CURVE)
    t = datetime(2026, 9, 30, 19, 0)
    line, moved = watch.decide(None, snap, t, None)
    assert "první dotaz" in line and moved
    assert watch.decide(snap, snap, datetime(2026, 9, 30, 19, 10), t) == (None, False)   # beze změny
    line, moved = watch.decide(snap, snap, datetime(2026, 9, 30, 20, 1), t)              # hodina uplynula
    assert "beze změny" in line and moved


class _FakePortal:
    """Klient s dvěma stránkami; stránky se dají měnit mezi dotazy."""

    def __init__(self):
        self.home, self.curve, self.fail = HOME, CURVE, False

    async def async_get(self, url):
        if self.fail:
            raise api.VhsError("portál nejede")
        return self.curve if "CourbeMois" in url else self.home


def test_run_once_writes_only_on_change_and_remembers_state(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "LOG", tmp_path / "log.txt")
    monkeypatch.setattr(watch, "PAUSE_BETWEEN_REQUESTS", 0)
    state, portal = tmp_path / "state.json", _FakePortal()

    def run(minute):
        return asyncio.run(watch.run_once(api, portal, state, datetime(2026, 9, 30, 19, minute)))

    assert "první dotaz" in run(0)
    assert run(10) is None and run(20) is None                     # stejný stav, nic nezapisuje
    portal.home = HOME.replace("29.09.2026 17:42", "30.09.2026 05:42").replace("911.69500", "912.02700")
    changed = run(30)
    assert "nový odečet" in changed and "912.027" in changed       # stav si pamatuje mezi spuštěními
    assert run(40) is None
    text = (tmp_path / "log.txt").read_text(encoding="utf-8")
    assert text.count("\n") == 2                                    # jen dva zápisy, žádný šum


def test_run_once_logs_error_and_keeps_previous_state(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "LOG", tmp_path / "log.txt")
    monkeypatch.setattr(watch, "PAUSE_BETWEEN_REQUESTS", 0)
    state, portal = tmp_path / "state.json", _FakePortal()
    asyncio.run(watch.run_once(api, portal, state, datetime(2026, 9, 30, 19, 0)))
    before = state.read_text(encoding="utf-8")
    portal.fail = True
    line = asyncio.run(watch.run_once(api, portal, state, datetime(2026, 9, 30, 19, 10)))
    assert "chyba" in line and "portál nejede" in line
    assert state.read_text(encoding="utf-8") == before            # chyba stav nepřepíše
