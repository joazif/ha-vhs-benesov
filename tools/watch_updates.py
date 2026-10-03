"""Sleduje, kdy a jak se na portálu objevují nová data.

Dvě varianty:

1) Nepřetržitě v terminálu (Ctrl+C ukončí):

    export VHS_LOGIN='...' VHS_PASSWORD='...'
    .venv/bin/python tools/watch_updates.py               # dotaz každých 10 minut
    .venv/bin/python tools/watch_updates.py --interval 5  # jiný odstup v minutách

2) Jeden dotaz na spuštění (pro cron nebo launchd), údaje ze souboru:

    .venv/bin/python tools/watch_updates.py --once --env tools/watch.local.env

   Soubor s údaji (`tools/watch.local.env`, je v .gitignore, dej mu `chmod 600`):

    VHS_LOGIN=...
    VHS_PASSWORD=...

   Stav mezi spuštěními si pamatuje v tools/watch_updates.state.local.json, takže
   ho lze pouštět klidně každých 10 minut; zapíše řádek jen při změně.

Při každém dotazu stáhne jen hlavní stránku a křivku (dva lehké požadavky),
zaznamená si čas posledního odečtu, stav číselníku a poslední krok křivky a
vypíše řádek jen když se něco změnilo (a jednou za hodinu "beze změny"). Záznam
jde i do tools/watch_updates.local.log (v .gitignore). Přesnost času změny je
tak velká jako odstup dotazů.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import pathlib
import sys
from dataclasses import asdict, dataclass
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOG = ROOT / "tools" / "watch_updates.local.log"
STATE = ROOT / "tools" / "watch_updates.state.local.json"
HEARTBEAT_SECONDS = 3600
PAUSE_BETWEEN_REQUESTS = 1.5   # sekund mezi dvěma stránkami jednoho dotazu


def _load_api():
    spec = importlib.util.spec_from_file_location(
        "vhs_api", ROOT / "custom_components" / "vhs_benesov" / "api.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["vhs_api"] = module
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class Snapshot:
    last_reading: datetime | None
    reading_m3: float | None
    last_bucket: datetime | None
    bucket_count: int

    def line(self) -> str:
        return (
            f"odečet {self.last_reading:%d.%m. %H:%M}" if self.last_reading else "odečet ?"
        ) + (
            f" | stav {self.reading_m3:.3f} m³" if self.reading_m3 is not None else " | stav ?"
        ) + (
            f" | poslední krok křivky {self.last_bucket:%d.%m. %H:%M} ({self.bucket_count} bodů)"
            if self.last_bucket else " | křivka prázdná"
        )


def snapshot(api, home: str, curve_page: str) -> Snapshot:
    """Stav portálu z hlavní stránky a křivky."""
    curve = api.parse_curve(curve_page)
    return Snapshot(
        last_reading=api.parse_last_reading(home),
        reading_m3=api.parse_meter_reading(home),
        last_bucket=curve[-1].at if curve else None,
        bucket_count=len(curve),
    )


def what_changed(old: Snapshot | None, new: Snapshot) -> list[str]:
    """Popis změn mezi dvěma stavy (prázdný seznam = beze změny)."""
    if old is None:
        return ["první dotaz"]
    changes = []
    if old.last_reading != new.last_reading:
        changes.append("nový odečet")
    if old.reading_m3 != new.reading_m3:
        changes.append("změna číselníku")
    if old.last_bucket != new.last_bucket or old.bucket_count != new.bucket_count:
        changes.append("nový krok křivky")
    return changes


def parse_env(text: str) -> dict[str, str]:
    """Řádky KEY=VALUE (bez uvozovek nebo v nich); prázdné řádky a # se přeskočí."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_env(path: pathlib.Path) -> None:
    """Načíst údaje ze souboru do prostředí (už nastavené proměnné se nepřepisují)."""
    for key, value in parse_env(path.read_text(encoding="utf-8")).items():
        os.environ.setdefault(key, value)


def snapshot_to_dict(snap: Snapshot) -> dict:
    data = asdict(snap)
    for key in ("last_reading", "last_bucket"):
        data[key] = data[key].isoformat() if data[key] else None
    return data


def snapshot_from_dict(data: dict) -> Snapshot:
    def when(value):
        return datetime.fromisoformat(value) if value else None

    return Snapshot(
        last_reading=when(data.get("last_reading")),
        reading_m3=data.get("reading_m3"),
        last_bucket=when(data.get("last_bucket")),
        bucket_count=int(data.get("bucket_count", 0)),
    )


def load_state(path: pathlib.Path) -> tuple[Snapshot | None, datetime | None]:
    """Poslední známý stav a čas posledního zápisu; bez souboru nebo s vadným souborem nic."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        heartbeat = raw.get("last_heartbeat")
        return snapshot_from_dict(raw["snapshot"]), datetime.fromisoformat(heartbeat) if heartbeat else None
    except (OSError, ValueError, KeyError):
        return None, None


def save_state(path: pathlib.Path, snap: Snapshot, heartbeat: datetime) -> None:
    path.write_text(
        json.dumps({"snapshot": snapshot_to_dict(snap), "last_heartbeat": heartbeat.isoformat()}),
        encoding="utf-8",
    )


def decide(
    previous: Snapshot | None, current: Snapshot, now: datetime, last_heartbeat: datetime | None
) -> tuple[str | None, bool]:
    """Co zapsat do záznamu (nebo None) a jestli se tím posouvá čas "tep" (heartbeat)."""
    changes = what_changed(previous, current)
    if changes:
        return f"{now:%d.%m. %H:%M} | {', '.join(changes)} | {current.line()}", True
    if last_heartbeat is None or (now - last_heartbeat).total_seconds() >= HEARTBEAT_SECONDS:
        return f"{now:%d.%m. %H:%M} | beze změny | {current.line()}", True
    return None, False


def _write(text: str) -> None:
    print(text, flush=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(text + "\n")


async def _poll(api, client) -> Snapshot:
    home = await client.async_get(api.HOME_URL)
    await asyncio.sleep(PAUSE_BETWEEN_REQUESTS)
    curve = await client.async_get(f"{api.ENERGY_URL}?Affichage=CourbeMois")
    return snapshot(api, home, curve)


async def run_once(api, client, state_path: pathlib.Path, now: datetime) -> str | None:
    """Jeden dotaz: porovná se stavem ze souboru, zapíše řádek při změně, stav uloží."""
    previous, heartbeat = load_state(state_path)
    try:
        current = await _poll(api, client)
    except api.VhsError as err:
        line = f"{now:%d.%m. %H:%M} | chyba: {err}"
        _write(line)
        return line
    line, moved = decide(previous, current, now, heartbeat)
    if line:
        _write(line)
    save_state(state_path, current, now if moved else (heartbeat or now))
    return line


async def main() -> None:
    args = sys.argv[1:]
    if "--env" in args:
        load_env(pathlib.Path(args[args.index("--env") + 1]).expanduser())
    api = _load_api()
    client = api.VhsBenesovClient(os.environ["VHS_LOGIN"], os.environ["VHS_PASSWORD"])
    try:
        if "--once" in args:
            await run_once(api, client, STATE, datetime.now())
            return
        minutes = float(args[args.index("--interval") + 1]) if "--interval" in args else 10.0
        previous: Snapshot | None = None
        heartbeat: datetime | None = None
        _write(f"# start {datetime.now():%d.%m.%Y %H:%M}, dotaz každých {minutes:g} min")
        while True:
            now = datetime.now()
            try:
                current = await _poll(api, client)
            except api.VhsError as err:
                _write(f"{now:%d.%m. %H:%M} | chyba: {err}")
            else:
                line, moved = decide(previous, current, now, heartbeat)
                if line:
                    _write(line)
                if moved:
                    heartbeat = now
                previous = current
            await asyncio.sleep(minutes * 60)
    finally:
        await client.async_close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nKonec.")
