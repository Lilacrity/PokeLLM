"""Interactive probe that runs the RAM reader against a live mGBA instance.

Purpose
-------
Sanity-check the numbers coming out of the reader *before* anything else
downstream (event detector, A*, LLM planner) starts to depend on them.
The fixed-address constants in ``ram/constants.py`` are marked
``TODO: verify``; this tool is how we verify them. Open mGBA's memory
viewer side-by-side with this output and confirm each value matches.

Usage
-----
Start mgba-http against a running FireRed ROM (default port 5000), then::

    ./.venv/Scripts/python.exe -m tools.ram_probe           # dump everything once
    ./.venv/Scripts/python.exe -m tools.ram_probe watch     # refresh every ~1s
    ./.venv/Scripts/python.exe -m tools.ram_probe player    # just one section
    ./.venv/Scripts/python.exe -m tools.ram_probe --help

Any subcommand accepts ``--url`` to point at a non-default mgba-http host.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Callable

from ram import MgbaHttpBackend, MgbaHttpError, RamReader
from ram import constants as C


# -----------------------------------------------------------------------------
# Section printers. Each takes a RamReader and prints one chunk. They swallow
# their own exceptions so a failure in one section doesn't kill the whole dump
# -- we want all diagnostic info in one pass, not a traceback on the first bad
# address.
# -----------------------------------------------------------------------------


def _section(title: str) -> None:
    print()
    print(f"=== {title} ===")


def _try(label: str, fn: Callable[[], object]) -> None:
    try:
        print(f"{label}: {fn()}")
    except Exception as exc:  # noqa: BLE001 -- we want to report, not raise
        print(f"{label}: <error: {exc}>")


def print_header(reader: RamReader, backend: MgbaHttpBackend) -> None:
    _section("Connection")
    _try("game title", backend.ping)
    _try("game code ", backend.game_code)
    # SaveBlock pointers are the single best liveness check for the reader.
    try:
        sb1 = reader._save_block1_base()  # noqa: SLF001 -- probe-only
        print(f"gSaveBlock1Ptr -> {sb1:#010x}")
    except Exception as exc:  # noqa: BLE001
        print(f"gSaveBlock1Ptr -> <error: {exc}>")
    try:
        sb2 = reader._save_block2_base()  # noqa: SLF001
        print(f"gSaveBlock2Ptr -> {sb2:#010x}")
    except Exception as exc:  # noqa: BLE001
        print(f"gSaveBlock2Ptr -> <error: {exc}>")


def print_player(reader: RamReader) -> None:
    _section("Player state (SaveBlock1 + gObjectEvents[0])")
    try:
        st = reader.read_player_state()
    except Exception as exc:  # noqa: BLE001
        print(f"<error: {exc}>")
        return
    print(f"  pos           : x={st.x}, y={st.y}")
    print(f"  map           : group={st.map_group}, num={st.map_num}, layoutId={st.map_layout_id:#06x}")
    print(f"  facing        : {st.facing.name}")
    print(f"  party_count   : {st.party_count}  (derived from live gPlayerParty)")


def print_object_events(reader: RamReader) -> None:
    _section("Object events (active only)")
    try:
        slots = reader.read_object_events()
    except Exception as exc:  # noqa: BLE001
        print(f"<error: {exc}>")
        return
    if not slots:
        print("  (no active slots)")
        return
    for s in slots:
        tags = []
        if s.is_player:
            tags.append("PLAYER")
        if s.invisible:
            tags.append("invis")
        if s.off_screen:
            tags.append("offscreen")
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        print(
            f"  slot {s.slot:2d}{tag_str}: gfx={s.graphics_id:3d} "
            f"mvmt={s.movement_type:3d} at ({s.x},{s.y}) "
            f"facing={s.facing.name:5s} mb={s.current_metatile_behavior:#04x}"
        )


def print_party(reader: RamReader) -> None:
    _section("Party (live gPlayerParty)")
    try:
        party = reader.read_party()
    except Exception as exc:  # noqa: BLE001
        print(f"<error: {exc}>")
        return
    if not party:
        print("  (empty)")
        return
    for m in party:
        egg = " (EGG)" if m.is_egg else ""
        print(
            f"  slot {m.slot}: L{m.level:<3d} HP {m.hp}/{m.max_hp}  "
            f"atk={m.attack} def={m.defense} spe={m.speed} "
            f"spa={m.sp_attack} spd={m.sp_defense}  "
            f"pid={m.personality:#010x} otid={m.ot_id:#010x} "
            f"status={m.status:#010x}{egg}"
        )


def print_game_mode(reader: RamReader) -> None:
    _section("Game mode")
    _try("mode", reader.read_game_mode)


def print_money(reader: RamReader) -> None:
    _section("Money (SaveBlock1.money ^ SaveBlock2.encryptionKey)")
    _try("key  ", lambda: f"{reader.read_encryption_key():#010x}")
    _try("money", reader.read_money)


def print_map(reader: RamReader) -> None:
    _section("Map grid (VMap / BackupMapLayout)")
    try:
        xsize, ysize = reader.read_map_dimensions()
    except Exception as exc:  # noqa: BLE001
        print(f"  dims: <error: {exc}>")
        return
    print(f"  dims: {xsize} x {ysize}  (includes {C.MAP_OFFSET}-tile border on each side)")
    # Decode the tile under the player's feet, if we can.
    try:
        st = reader.read_player_state()
        _, _, grid = reader.read_map_grid()
        gx = st.x + C.MAP_OFFSET
        gy = st.y + C.MAP_OFFSET
        mid, col, ele = reader.tile_cell_at(grid, xsize, gx, gy)
        print(
            f"  under player (grid {gx},{gy}): "
            f"metatileId={mid:#05x}, collision={col}, elevation={ele}"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  under player: <error: {exc}>")


# -----------------------------------------------------------------------------
# Command dispatch.
# -----------------------------------------------------------------------------


SECTIONS: dict[str, Callable[[RamReader], None]] = {
    "player": print_player,
    "objects": print_object_events,
    "party": print_party,
    "mode": print_game_mode,
    "money": print_money,
    "map": print_map,
}


def dump_all(reader: RamReader, backend: MgbaHttpBackend) -> None:
    print_header(reader, backend)
    for fn in SECTIONS.values():
        fn(reader)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ram_probe",
        description=(
            "Probe a live mGBA instance (via mgba-http) using the PokeLLM RAM "
            "reader. Open mGBA's memory viewer alongside and confirm the "
            "values match before trusting the fixed-address constants."
        ),
    )
    parser.add_argument(
        "--url",
        default="http://localhost:5000",
        help="mgba-http base URL (default: %(default)s).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="HTTP timeout in seconds (default: %(default)s).",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="all",
        choices=["all", "watch", *SECTIONS.keys()],
        help=(
            "What to dump. 'all' (default) runs every section once; 'watch' "
            "re-runs 'all' on a loop. The other choices print a single "
            "section."
        ),
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Refresh interval in seconds for 'watch' mode (default: %(default)s).",
    )
    args = parser.parse_args(argv)

    backend = MgbaHttpBackend(base_url=args.url, timeout=args.timeout)
    reader = RamReader(backend)

    try:
        backend.ping()
    except MgbaHttpError as exc:
        print(f"Could not reach mgba-http at {args.url}: {exc}", file=sys.stderr)
        print(
            "Is mgba-http running and is a ROM loaded? See "
            "documentation/ApiDocumentation.md for the endpoint list.",
            file=sys.stderr,
        )
        return 2

    if args.command == "watch":
        try:
            while True:
                # ANSI clear-screen keeps the output readable; falls back
                # gracefully on terminals that ignore it.
                print("\x1b[2J\x1b[H", end="")
                dump_all(reader, backend)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print()
            return 0
    elif args.command == "all":
        dump_all(reader, backend)
    else:
        print_header(reader, backend)
        SECTIONS[args.command](reader)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
