"""Live explored-tiles viewer for PokeLLM.

Connects to a running mGBA via mgba-http, polls the RamReader every
~250 ms, feeds each tick into a WorldMemory, and redraws the current
map's ExploredTile set on a tkinter canvas. As the agent walks into
a new map, the view swaps to that map's accumulated knowledge.

Usage
-----
Start mgba-http against a running FireRed ROM (default port 5000), then::

    ./.venv/Scripts/python.exe -m tools.map_viewer
    ./.venv/Scripts/python.exe -m tools.map_viewer --url http://localhost:5000
    ./.venv/Scripts/python.exe -m tools.map_viewer --cache cache/world_memory
    ./.venv/Scripts/python.exe -m tools.map_viewer --interval 200
    ./.venv/Scripts/python.exe -m tools.map_viewer --no-save

The viewer loads any prior WorldMemory from ``--cache`` on start and
saves to the same path every ~10 s (and on window close). Pass
``--no-save`` to keep the session purely in-memory.

Colour key
----------
    black           unexplored
    light gray      walkable floor (incl. shallow water / puddle splash)
    dark gray       impassable wall (IMPASSABLE_*, generic collision)
    green           tall grass / indoor encounter
    blue            surfable water (POND/DEEP/OCEAN/CURRENT/...)
    brown + arrow   directional ledge jump (arrow points the jump way)
    cyan square     PC
    pink dot        PokeCenter heal counter (overlaid on top of gold)
    gold square     generic counter (Mart shop counter has cyan-dot too)
    distinct walls  bookshelf / TV / signpost / mart shelf / region map
    orange dot      warp (destination known; centred on door-strip
                    centroid so 2-tile mats render between the tiles)
    cyan dot        sign / hidden item (incl. PokeCenter / PokeMart signs)
    yellow dot      NPC currently on screen
    yellow ring     frontier tile (explored, has an unexplored neighbour)
    red disc        player
"""

from __future__ import annotations

import argparse
import sys
import tkinter as tk
from pathlib import Path

if __package__ in (None, ""):  # allow `python tools/map_viewer.py` too
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ram import (
    MgbaHttpBackend,
    MgbaHttpError,
    RamReader,
    WorldMemory,
    compute_visible_tiles,
)
from ram import constants as C


# -----------------------------------------------------------------------------
# Colour helpers
# -----------------------------------------------------------------------------

# Surfable / impassable water -- the authoritative set lives in
# C.WATER_BEHAVIORS (mirrors the decomp's sBehaviorSurfable[]). Currents
# count as surfable water too. SHALLOW_WATER (0x17) and PUDDLE (0x16)
# are intentionally *not* in here: the engine walks the player straight
# through both, so they should render as floor, not as solid blue water.
_WATER_BEHAVIORS = {*C.WATER_BEHAVIORS}
_GRASS_BEHAVIORS = {
    C.MetatileBehavior.TALL_GRASS,
    C.MetatileBehavior.INDOOR_ENCOUNTER,
}
# Directional ledge jumps. The colour is the same brown across all four,
# but each gets a small triangular arrow overlay so the agent's view
# mirrors the data side -- A* already has the per-direction behaviour
# values it needs to reason about jumps.
_LEDGE_ARROW_DIR: dict[int, str] = {
    int(C.MetatileBehavior.JUMP_NORTH): "N",
    int(C.MetatileBehavior.JUMP_SOUTH): "S",
    int(C.MetatileBehavior.JUMP_EAST):  "E",
    int(C.MetatileBehavior.JUMP_WEST):  "W",
}
_LEDGE_BEHAVIORS = set(_LEDGE_ARROW_DIR.keys())

# Behaviours the engine refuses to walk onto, but which represent
# meaningful interactables (the agent presses A toward them). Each
# gets its own colour so the viewer doesn't drown them in the generic
# wall gray -- the user calls out the PokeCenter PC specifically.
_INTERACTABLE_TILE_COLORS: dict[int, str] = {
    int(C.MetatileBehavior.PC):                  "#1ec8d4",  # bright cyan
    int(C.MetatileBehavior.COUNTER):             "#d4a44e",  # gold
    int(C.MetatileBehavior.BOOKSHELF):           "#8a6038",  # wood brown
    int(C.MetatileBehavior.POKEMART_SHELF):      "#a07840",  # light brown
    int(C.MetatileBehavior.TELEVISION):          "#7a7aa0",  # blue-gray
    int(C.MetatileBehavior.SIGNPOST):            "#c8b94e",  # yellow
    int(C.MetatileBehavior.REGION_MAP):          "#9c7c50",  # tan
    # POKEMON_CENTER_SIGN / POKEMART_SIGN deliberately omitted -- the
    # reader reports them as kind="sign" alongside ordinary BgEvent
    # signs, so they get the same cyan dot overlay and need no special
    # base-tile colour. Letting them fall through to the wall-gray
    # default also avoids the "Mart sign looks like water" confusion
    # an earlier blue assignment introduced.
}

# Plain impassable walls -- get the generic dark-gray colour. Anything
# in _INTERACTABLE_TILE_COLORS is handled before we hit this set.
_IMPASSABLE_WALL_BEHAVIORS = {int(b) for b in C.IMPASSABLE_BEHAVIORS}


_CONNECTION_LABEL = {
    C.CONNECTION_NORTH: "N",
    C.CONNECTION_SOUTH: "S",
    C.CONNECTION_WEST: "W",
    C.CONNECTION_EAST: "E",
    C.CONNECTION_DIVE: "dive",
    C.CONNECTION_EMERGE: "emerge",
}


def _connection_band(mem, conn, sx, sy):
    """Canvas-space rectangle for a connection's edge band, or None
    if the connection is dive/emerge (no meaningful 2D edge)."""
    d = conn.direction
    w, h = mem.width, mem.height
    if d == C.CONNECTION_NORTH:
        return sx(-1), sy(-1), sx(w), sy(0)
    if d == C.CONNECTION_SOUTH:
        return sx(-1), sy(h - 1), sx(w), sy(h)
    if d == C.CONNECTION_WEST:
        return sx(-1), sy(-1), sx(0), sy(h)
    if d == C.CONNECTION_EAST:
        return sx(w - 1), sy(-1), sx(w), sy(h)
    return None


def tile_color(behavior: int, collision: int) -> str:
    if behavior in _WATER_BEHAVIORS:
        return "#3a5fcd"
    if behavior in _GRASS_BEHAVIORS:
        return "#4a8a3a"
    if behavior in _LEDGE_BEHAVIORS:
        return "#7b5a2e"
    interact = _INTERACTABLE_TILE_COLORS.get(behavior)
    if interact is not None:
        return interact
    if collision != 0 or behavior in _IMPASSABLE_WALL_BEHAVIORS:
        return "#404040"
    return "#a8a8a8"


# -----------------------------------------------------------------------------
# Viewer
# -----------------------------------------------------------------------------

TILE_PX = 10
PAD_TILES = 2
SIDEBAR_W = 260
SAVE_EVERY_MS = 10_000


class MapViewer:
    def __init__(
        self,
        reader: RamReader,
        world: WorldMemory,
        cache_path: Path | None,
        interval_ms: int,
    ) -> None:
        self.reader = reader
        self.world = world
        self.cache_path = cache_path
        self.interval_ms = interval_ms
        self.step = 0
        self._last_map: tuple[int, int] | None = None
        self._last_error: str = ""
        self._ms_since_save = 0

        self.root = tk.Tk()
        self.root.title("PokeLLM — explored tiles (live)")
        self.root.configure(bg="#101010")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Canvas (map) on the left, sidebar on the right.
        self.canvas = tk.Canvas(
            self.root, bg="#000000", highlightthickness=0, width=800, height=600
        )
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.sidebar = tk.Text(
            self.root,
            width=36,
            bg="#151515",
            fg="#e0e0e0",
            font=("Consolas", 10),
            highlightthickness=0,
            borderwidth=0,
            state=tk.DISABLED,
        )
        self.sidebar.pack(side=tk.RIGHT, fill=tk.Y)

    # --- event loop ---------------------------------------------------------

    def run(self) -> None:
        self.root.after(50, self._tick)
        self.root.mainloop()

    def _tick(self) -> None:
        try:
            player = self.reader.read_player_state()
            interactables = self.reader.read_visible_interactables(
                player.x, player.y
            )
            visible = compute_visible_tiles(self.reader, player.x, player.y)
            self._update_memory(player, visible, interactables)
            self._last_error = ""
        except MgbaHttpError as exc:
            self._last_error = f"mgba-http: {exc}"
        except Exception as exc:  # noqa: BLE001 -- keep the window alive
            self._last_error = f"{type(exc).__name__}: {exc}"

        try:
            self._redraw()
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"redraw: {exc}"

        self._ms_since_save += self.interval_ms
        if self.cache_path is not None and self._ms_since_save >= SAVE_EVERY_MS:
            self._save()
            self._ms_since_save = 0

        self.step += 1
        self.root.after(self.interval_ms, self._tick)

    # --- memory wiring ------------------------------------------------------

    def _update_memory(self, player, visible, interactables) -> None:
        map_key = (player.map_group, player.map_num)
        xsize, ysize, _ = self.reader.read_map_grid_cached()
        width = max(0, xsize - C.MAP_OFFSET_W)
        height = max(0, ysize - C.MAP_OFFSET_H)
        mem = self.world.get_or_create(
            player.map_group, player.map_num, width, height
        )
        # Keep dimensions fresh in case this is the first visit where we
        # only had placeholder values previously.
        mem.width = width
        mem.height = height

        # Seed edge connections on first visit (or refresh if empty --
        # e.g. a previously-loaded snapshot that predates connection
        # support).
        if not mem.connections:
            mem.set_connections(self.reader.read_map_connections_cached())

        if self._last_map is not None and self._last_map != map_key:
            # Figure out whether this transition rode an edge connection
            # (seamless) or a warp. If a connection on the source map
            # points at the destination, mark it used; otherwise fall
            # back to the tile-level warp record.
            prev_mem = self.world.get_map(*self._last_map)
            crossed_conn = None
            if prev_mem is not None:
                for conn in prev_mem.connections.values():
                    if conn.to_map == map_key:
                        crossed_conn = conn
                        break
            if crossed_conn is not None:
                prev_mem.mark_connection_used(crossed_conn.direction)
            else:
                # Record the transition as a tile warp. We don't know
                # the warp id from RAM after the fact, so use 0 as a
                # placeholder; the detail lives in the source map's
                # known_warps anyway.
                self.world.record_map_transition(
                    from_map=self._last_map,
                    from_tile=(player.x, player.y),
                    to_map=map_key,
                    to_warp_id=0,
                )
        self._last_map = map_key

        mem.update_visibility(player.x, player.y, visible, interactables, self.step)
        self._cached_player = player
        self._cached_interactables = interactables
        self._cached_visible = visible

    # --- rendering ----------------------------------------------------------

    def _redraw(self) -> None:
        self.canvas.delete("all")
        player = getattr(self, "_cached_player", None)
        if player is None:
            self._write_sidebar(["Waiting for first tick..."])
            return

        mem = self.world.get_map(player.map_group, player.map_num)
        if mem is None or not mem.explored_tiles:
            self._write_sidebar(
                [
                    f"Map ({player.map_group}, {player.map_num})",
                    f"Player: ({player.x}, {player.y})",
                    "No tiles explored yet.",
                ]
            )
            return

        xs = [t.x for t in mem.explored_tiles.values()]
        ys = [t.y for t in mem.explored_tiles.values()]
        min_x, max_x = min(xs) - PAD_TILES, max(xs) + PAD_TILES
        min_y, max_y = min(ys) - PAD_TILES, max(ys) + PAD_TILES

        w = (max_x - min_x + 1) * TILE_PX
        h = (max_y - min_y + 1) * TILE_PX
        self.canvas.config(scrollregion=(0, 0, w, h))

        def sx(tx: int) -> int:
            return (tx - min_x) * TILE_PX

        def sy(ty: int) -> int:
            return (ty - min_y) * TILE_PX

        # Base tiles (explored).
        for (tx, ty), tile in mem.explored_tiles.items():
            color = tile_color(tile.behavior, tile.collision)
            x0, y0 = sx(tx), sy(ty)
            self.canvas.create_rectangle(
                x0, y0, x0 + TILE_PX, y0 + TILE_PX,
                fill=color, outline="",
            )
            # Directional ledge arrow on top of the brown ledge tile.
            arrow = _LEDGE_ARROW_DIR.get(tile.behavior)
            if arrow is not None:
                cx = x0 + TILE_PX // 2
                cy = y0 + TILE_PX // 2
                s = max(2, TILE_PX // 3)
                if arrow == "N":
                    pts = (cx, cy - s, cx - s, cy + s, cx + s, cy + s)
                elif arrow == "S":
                    pts = (cx, cy + s, cx - s, cy - s, cx + s, cy - s)
                elif arrow == "E":
                    pts = (cx + s, cy, cx - s, cy - s, cx - s, cy + s)
                else:  # "W"
                    pts = (cx - s, cy, cx + s, cy - s, cx + s, cy + s)
                self.canvas.create_polygon(*pts, fill="#1a1408", outline="")

        # Edge connections: a thick band along the relevant side of
        # the logical map. Solid band if used, dashed if only known.
        for conn in mem.connections.values():
            band = _connection_band(mem, conn, sx, sy)
            if band is None:
                continue
            x0, y0, x1, y1 = band
            color = "#ff9d3a" if conn.used else "#b6651a"
            dash = () if conn.used else (4, 3)
            self.canvas.create_rectangle(
                x0, y0, x1, y1, outline=color, width=2, dash=dash,
            )

        # Frontier rings.
        for (tx, ty) in mem.exploration_frontier:
            x0, y0 = sx(tx), sy(ty)
            self.canvas.create_rectangle(
                x0 + 1, y0 + 1, x0 + TILE_PX - 1, y0 + TILE_PX - 1,
                fill="", outline="#e8d44c", width=1,
            )

        # Interactables (warps / signs / NPCs / heal counters / HM obstacles).
        # Tile-behaviour kinds (pc, bookshelf, signpost, shop_counter, ...)
        # already paint distinct base-tile colours from `tile_color`, so we
        # skip dot overlays for those -- heal_counter is the exception
        # because it shares COUNTER (gold) base colour with shop_counter
        # and the user explicitly asked for the heal counter to be called
        # out distinctly.
        _DECORATED_TILE_KINDS = {
            "pc", "bookshelf", "pokemart_shelf", "signpost", "region_map",
            "television",
            "shop_counter", "counter",  # plain counters keep their gold base only
        }
        inters = getattr(self, "_cached_interactables", []) or []
        for inter in inters:
            if inter.kind in _DECORATED_TILE_KINDS:
                continue
            # Warps: draw the dot at the centroid of all tiles in the
            # door strip (passed through in details). For a 2-tile mat
            # that's literally between the two tiles -- avoids the
            # "off by half a tile to the left/right" bias that any
            # single-tile choice would hand-pick.
            if inter.kind == "warp":
                centroid = inter.details.get("centroid", (inter.x, inter.y))
                cx_tile, cy_tile = centroid
                cx = (cx_tile + 0.5 - min_x) * TILE_PX
                cy = (cy_tile + 0.5 - min_y) * TILE_PX
            else:
                x0, y0 = sx(inter.x), sy(inter.y)
                cx, cy = x0 + TILE_PX // 2, y0 + TILE_PX // 2
            r = max(2, TILE_PX // 3)
            if inter.kind == "warp":
                dot = "#ff8c1a"
            elif inter.kind in ("sign", "hidden_item"):
                dot = "#4fd1e0"
            elif inter.kind in ("npc", "trainer"):
                dot = "#f2e34c"
            elif inter.kind == "heal_counter":
                dot = "#ff70a0"  # bright pink overlaid on the gold counter
            else:
                dot = "#c0c0c0"
            self.canvas.create_oval(
                cx - r, cy - r, cx + r, cy + r, fill=dot, outline=""
            )

        # Persistent markers for seen-but-offscreen warps / signs.
        visible_keys = {(i.x, i.y) for i in inters}
        for key, seen in mem.seen_interactables.items():
            if key in visible_keys:
                continue
            if seen.kind not in ("warp", "sign", "hidden_item"):
                continue
            x0, y0 = sx(seen.x), sy(seen.y)
            cx, cy = x0 + TILE_PX // 2, y0 + TILE_PX // 2
            r = max(1, TILE_PX // 4)
            dot = "#ff8c1a" if seen.kind == "warp" else "#4fd1e0"
            self.canvas.create_oval(
                cx - r, cy - r, cx + r, cy + r, fill=dot, outline=""
            )

        # Player.
        px, py = sx(player.x), sy(player.y)
        r = max(3, TILE_PX // 2)
        self.canvas.create_oval(
            px + TILE_PX // 2 - r, py + TILE_PX // 2 - r,
            px + TILE_PX // 2 + r, py + TILE_PX // 2 + r,
            fill="#ff3333", outline="#000000", width=1,
        )

        self._write_sidebar(self._sidebar_lines(player, mem))

    def _sidebar_lines(self, player, mem) -> list[str]:
        lines: list[str] = []
        lines.append(f"step:     {self.step}")
        lines.append(f"map:      ({player.map_group}, {player.map_num})")
        lines.append(f"player:   ({player.x}, {player.y})")
        lines.append(f"facing:   {player.facing.name}")
        lines.append(f"explored: {len(mem.explored_tiles):>5}"
                     f" / {mem.width * mem.height:<5}"
                     f"  ({mem.exploration_percentage:5.1f}%)")
        lines.append(f"frontier: {len(mem.exploration_frontier)}")
        lines.append(f"warps:    {len(mem.known_warps)} seen,"
                     f" {len(mem.unused_warps())} unused")
        lines.append(f"signs/NPC:{len(mem.seen_interactables)} total,"
                     f" {len(mem.uninteracted())} uninteracted")
        if mem.connections:
            lines.append(f"edges:    {len(mem.connections)} connections")
            for conn in mem.connections.values():
                tag = _CONNECTION_LABEL.get(conn.direction, f"?{conn.direction}")
                mark = "*" if conn.used else " "
                lines.append(
                    f"  {mark}{tag:<6} -> ({conn.to_map[0]:>2},{conn.to_map[1]:>3})"
                    f" off={conn.offset:+d}"
                )
        else:
            lines.append("edges:    (none)")
        lines.append("")
        lines.append("--- WorldMemory ---")
        visited = self.world.maps_visited()
        lines.append(f"maps visited:   {len(visited)}")
        lines.append(f"total tiles:    {self.world.total_tiles_explored()}")
        lines.append(f"warp edges:     {len(self.world.warp_graph)}")
        for (g, n) in visited[-8:]:
            here = " <-- here" if (g, n) == (player.map_group, player.map_num) else ""
            m = self.world.get_map(g, n)
            pct = m.exploration_percentage if m else 0.0
            lines.append(f"  ({g:>2},{n:>3})  {pct:5.1f}%{here}")
        lines.append("")
        lines.append("--- Status ---")
        if self._last_error:
            lines.append(f"ERROR: {self._last_error}")
        else:
            lines.append("OK")
        if self.cache_path is not None:
            lines.append(f"cache: {self.cache_path}")
        else:
            lines.append("cache: (not persisted)")
        return lines

    def _write_sidebar(self, lines: list[str]) -> None:
        self.sidebar.configure(state=tk.NORMAL)
        self.sidebar.delete("1.0", tk.END)
        self.sidebar.insert(tk.END, "\n".join(lines))
        self.sidebar.configure(state=tk.DISABLED)

    # --- lifecycle ----------------------------------------------------------

    def _save(self) -> None:
        if self.cache_path is None:
            return
        try:
            self.world.save(self.cache_path)
        except Exception as exc:  # noqa: BLE001 -- don't kill the UI
            self._last_error = f"save: {exc}"

    def _on_close(self) -> None:
        self._save()
        self.root.destroy()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--url", default="http://localhost:5000",
        help="mgba-http base URL (default: http://localhost:5000)",
    )
    parser.add_argument(
        "--cache", default="cache/world_memory", type=Path,
        help="directory to persist WorldMemory (default: cache/world_memory)",
    )
    parser.add_argument(
        "--no-save", action="store_true",
        help="don't persist anything; run in-memory for this session only",
    )
    parser.add_argument(
        "--interval", type=int, default=250,
        help="poll interval in milliseconds (default: 250)",
    )
    args = parser.parse_args(argv)

    backend = MgbaHttpBackend(args.url)
    try:
        title = backend.ping()
    except MgbaHttpError as exc:
        print(f"Could not reach mgba-http at {args.url}: {exc}", file=sys.stderr)
        return 1
    print(f"Connected to mgba-http: {title!r}")

    reader = RamReader(backend)
    cache_path: Path | None = None if args.no_save else args.cache
    if cache_path is not None:
        world = WorldMemory.load(cache_path)
        print(f"Loaded {len(world.maps_visited())} maps from {cache_path}")
    else:
        world = WorldMemory()
        print("Running without persistence (--no-save).")

    MapViewer(reader, world, cache_path, args.interval).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
