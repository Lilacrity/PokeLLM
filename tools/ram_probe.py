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
from pathlib import Path
from typing import Callable

# Allow `python tools/ram_probe.py` as well as `python -m tools.ram_probe`.
# When launched as a script, the repo root isn't on sys.path, so the
# top-level `ram` package can't be imported without this nudge.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
    _section("Object events (active, in 15x11 window around player)")
    try:
        slots = reader.read_object_events()
        st = reader.read_player_state()
    except Exception as exc:  # noqa: BLE001
        print(f"<error: {exc}>")
        return
    if not slots:
        print("  (no active slots)")
        return
    # Same window as RamReader.read_visible_interactables: 15 wide
    # (player_x +/- 7) by 11 tall (player_y - 5 .. + 5). Slots that
    # fall outside it get reported separately so the probe still shows
    # everything active without burying what's actually on screen.
    min_x, max_x = st.x - 7, st.x + 7
    min_y, max_y = st.y - 5, st.y + 5

    def _format(s) -> str:
        tags = []
        if s.is_player:
            tags.append("PLAYER")
        if s.invisible:
            tags.append("invis")
        if s.off_screen:
            tags.append("offscreen")
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        return (
            f"  slot {s.slot:2d}{tag_str}: gfx={s.graphics_id:3d} "
            f"mvmt={s.movement_type:3d} at ({s.x},{s.y}) "
            f"facing={s.facing.name:5s} mb={s.current_metatile_behavior:#04x}"
        )

    in_view = []
    out_view = []
    for s in slots:
        if s.is_player:
            in_view.append(s)
            continue
        if min_x <= s.x <= max_x and min_y <= s.y <= max_y:
            in_view.append(s)
        else:
            out_view.append(s)
    if not in_view:
        print("  (no slots in view)")
    else:
        for s in in_view:
            print(_format(s))
    if out_view:
        print(f"  -- {len(out_view)} other active slot(s) outside window --")
        for s in out_view:
            print(_format(s))


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
            f"  slot {m.slot}: \"{m.nickname}\" L{m.level:<3d} HP {m.hp}/{m.max_hp}  "
            f"atk={m.attack} def={m.defense} spe={m.speed} "
            f"spa={m.sp_attack} spd={m.sp_defense}  "
            f"status={m.status_text}  "
            f"pid={m.personality:#010x} otid={m.ot_id:#010x}{egg}"
        )


def print_game_mode(reader: RamReader) -> None:
    _section("Game mode")
    _try("mode", reader.read_game_mode)


def print_money(reader: RamReader) -> None:
    _section("Money (SaveBlock1.money ^ SaveBlock2.encryptionKey)")
    _try("key  ", lambda: f"{reader.read_encryption_key():#010x}")
    _try("money", reader.read_money)


def print_battle(reader: RamReader) -> None:
    _section("Battle state (gBattleMons + battle globals)")
    try:
        bs = reader.read_battle_state()
    except Exception as exc:  # noqa: BLE001
        print(f"<error: {exc}>")
        return
    if not bs.active:
        print("  (not in battle)")
        return
    print(f"  battlers      : {bs.battlers_count}")
    print(f"  outcome       : {bs.outcome_text}")
    print(f"  weather       : {bs.weather_text}")
    print(f"  current move  : {bs.current_move_name} (#{bs.current_move})")
    print(f"  alive (ours)  : {bs.player_party_alive}   alive (theirs): {bs.enemy_party_alive}")
    for label, mon in [("Player", bs.player), ("Enemy", bs.enemy)]:
        if mon is None:
            continue
        moves_str = ", ".join(
            f"{n}({p})" for n, p in zip(mon.move_names, mon.pp) if n != "-"
        )
        print(
            f"  {label:6s}: {mon.nickname} ({mon.species_name}) L{mon.level}  "
            f"HP {mon.hp}/{mon.max_hp}  "
            f"atk={mon.attack} def={mon.defense} spe={mon.speed} "
            f"spa={mon.sp_attack} spd={mon.sp_defense}  "
            f"status={mon.status_text}"
        )
        if moves_str:
            print(f"          moves: {moves_str}")


def print_dialogue(reader: RamReader) -> None:
    _section("Dialogue / text state")
    _try("movement locked    ", reader.is_movement_locked)
    _try("field msg visible  ", reader.is_field_message_visible)
    _try("last talked NPC    ", reader.read_last_talked_npc)
    try:
        text = reader.read_dialogue_text()
    except Exception as exc:  # noqa: BLE001
        print(f"  text: <error: {exc}>")
        return
    if text is None:
        print("  text: (none)")
    else:
        for line in text.split("\n"):
            print(f"  text: {line}")


def print_interactables(reader: RamReader) -> None:
    _section("Visible interactables (15x11 window)")
    try:
        st = reader.read_player_state()
        inter = reader.read_visible_interactables(st.x, st.y)
    except Exception as exc:  # noqa: BLE001
        print(f"  <error: {exc}>")
        return
    if not inter:
        print("  (none in view)")
        return
    # Sort by (y, x) so the output roughly matches reading order on screen.
    for it in sorted(inter, key=lambda i: (i.y, i.x)):
        dx, dy = it.x - st.x, it.y - st.y
        extras: list[str] = []
        if it.kind == "warp":
            extras.append(
                f"-> map({it.details['dest_map_group']},"
                f"{it.details['dest_map_num']}) "
                f"warp#{it.details['dest_warp_id']}"
            )
        elif it.kind in ("sign", "hidden_item", "secret_base"):
            extras.append(f"data={it.details['data']:#010x}")
        elif "graphics_id" in it.details:
            extras.append(f"gfx={it.details['graphics_id']}")
            if it.details.get("trainer_type"):
                extras.append(f"trainer_type={it.details['trainer_type']}")
        extra_str = f"  [{', '.join(extras)}]" if extras else ""
        print(
            f"  ({it.x:3d},{it.y:3d}) d=({dx:+d},{dy:+d})  "
            f"{it.kind:12s} {it.label}{extra_str}"
        )


def print_tiles(reader: RamReader) -> None:
    """Dump the 15x11 visible window as a grid of (metatile_id, behavior).

    This is the debug aid for "why doesn't the PokeCenter counter show
    up as an interactable?" -- the behavior printed here is the raw
    9-bit value we classify against. If (7,3) doesn't show 0x80, the
    interactables sweep won't either; compare against mGBA's memory
    viewer to locate the attribute word for that metatile id.
    """
    _section("Visible tiles (15x11 window, metatile_id/behavior)")
    try:
        st = reader.read_player_state()
        tiles = reader.read_visible_tiles(st.x, st.y)
    except Exception as exc:  # noqa: BLE001
        print(f"  <error: {exc}>")
        return
    if not tiles:
        print("  (no tiles in view)")
        return
    by_pos = {(t.x, t.y): t for t in tiles}
    ys = sorted({t.y for t in tiles})
    xs = sorted({t.x for t in tiles})
    # Header row with x values.
    header = "       " + " ".join(f"{x:>8d}" for x in xs)
    print(header)
    for y in ys:
        cells = []
        for x in xs:
            t = by_pos.get((x, y))
            if t is None:
                cells.append("        ")
            elif x == st.x and y == st.y:
                cells.append(f"[P{t.behavior:#04x}]")  # mark player cell
            else:
                cells.append(f"{t.metatile_id:03x}/{t.behavior:#04x}")
        print(f"  y={y:3d}  " + " ".join(f"{c:>8s}" for c in cells))


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


def print_hex_dump(reader: RamReader, addr: int, length: int) -> None:
    """Classic 16-cols hex-dump of `length` bytes starting at `addr`.

    Handy for confirming a fixed-address constant against mGBA's memory
    viewer, and for sharing a snippet of raw bytes when a decoded value
    looks wrong.
    """
    _section(f"Hex dump {addr:#010x} .. {addr + length:#010x} ({length} bytes)")
    try:
        raw = reader.read_bytes(addr, length)
    except Exception as exc:  # noqa: BLE001
        print(f"<error: {exc}>")
        return
    for row in range(0, len(raw), 16):
        chunk = raw[row : row + 16]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"  {addr + row:#010x}  {hex_part:<47s}  |{ascii_part}|")


# -----------------------------------------------------------------------------
# Command dispatch.
# -----------------------------------------------------------------------------


SECTIONS: dict[str, Callable[[RamReader], None]] = {
    "player": print_player,
    "objects": print_object_events,
    "interactables": print_interactables,
    "tiles": print_tiles,
    "party": print_party,
    "battle": print_battle,
    "dialogue": print_dialogue,
    "mode": print_game_mode,
    "money": print_money,
    "map": print_map,
}


# Sections skipped in the default "all" dump -- still runnable explicitly.
# ``tiles`` is a debug aid that overlaps with ``interactables`` (same grid
# read, same prefetch) so running both doubles the HTTP cost for no win.
_DUMP_ALL_SKIP = {"tiles"}


def dump_all(reader: RamReader, backend: MgbaHttpBackend) -> None:
    print_header(reader, backend)
    for name, fn in SECTIONS.items():
        if name in _DUMP_ALL_SKIP:
            continue
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
        choices=["all", "watch", "bytes", *SECTIONS.keys()],
        help=(
            "What to dump. 'all' (default) runs every section once; 'watch' "
            "re-runs 'all' on a loop; 'bytes' prints a hex dump of "
            "--address for --length bytes. The other choices print a "
            "single section."
        ),
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Refresh interval in seconds for 'watch' mode (default: %(default)s).",
    )
    parser.add_argument(
        "--address",
        type=lambda s: int(s, 0),
        default=None,
        help="For 'bytes' mode: GBA bus address (hex ok, e.g. 0x02023BE4).",
    )
    parser.add_argument(
        "--length",
        type=lambda s: int(s, 0),
        default=64,
        help="For 'bytes' mode: number of bytes to dump (default: %(default)s).",
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
    elif args.command == "bytes":
        if args.address is None:
            parser.error("'bytes' requires --address, e.g. --address 0x02023BE4")
        print_hex_dump(reader, args.address, args.length)
    else:
        print_header(reader, backend)
        SECTIONS[args.command](reader)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
