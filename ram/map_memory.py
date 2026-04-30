"""The agent's accumulated world knowledge, built through observation.

MapMemory sits *above* the reader layer. It consumes reader output and
tracks what the agent has personally seen; it never makes HTTP calls
itself.

Design rules (all load-bearing, do not quietly relax):

1. **A* runs on ``explored_tiles`` only.** The reader's cached grid
   exists as a lookup table so we can classify tiles the moment they
   enter the visible window -- it never feeds pathfinding or the LLM
   directly. If the agent hasn't seen a tile, that tile does not exist
   in the navigation graph.
2. **Coordinate space is logical / SaveBlock1 everywhere.** Convert
   from grid space (subtract ``C.MAP_OFFSET``) at the boundary and
   never store grid-space coordinates in MapMemory.
3. **Reader caches are per-map; MapMemory persists the whole session.**
   A map transition drops the reader's grid / events / tileset-ptr
   caches but leaves ``_behavior_cache`` (ROM data is permanent) and
   the WorldMemory's per-map knowledge intact.

Public surface:
    ExploredTile        -- one tile the agent has seen
    SeenInteractable    -- one on-screen thing the agent has noticed
    WarpConnection      -- a warp observed or used
    MapMemory           -- per-map knowledge (explored tiles, frontier, ...)
    WorldMemory         -- cross-map wrapper, keyed on (group, num)
    compute_visible_tiles -- pure-Python helper: cached grid -> visible dict
"""

from __future__ import annotations

import dataclasses
import json
from collections import deque
from pathlib import Path

from . import constants as C
from .reader import Interactable, MapConnection, RamReader


@dataclasses.dataclass
class ExploredTile:
    """A tile the agent has personally seen on screen.

    Coordinates in logical / SaveBlock1 space. ``behavior`` is the raw
    9-bit ``MetatileBehavior`` value -- compare to
    ``C.MetatileBehavior`` enum members.
    """

    x: int
    y: int
    metatile_id: int
    behavior: int
    collision: int
    elevation: int
    first_seen_step: int
    last_seen_step: int


@dataclasses.dataclass
class SeenInteractable:
    """An interactable the agent has spotted in view.

    NPC identity / label is snapshotted on first sight (the reader's
    classification at that tick); warps and signs are naturally
    persistent since they're static map data. ``interacted`` flips to
    true once the agent has pressed A on this tile.
    """

    x: int
    y: int
    kind: str
    label: str
    first_seen_step: int
    interacted: bool
    interaction_result: str | None


@dataclasses.dataclass(frozen=True)
class WarpConnection:
    """A warp the agent has either observed on screen or walked through.

    Stored frozen so WorldMemory.warp_graph can be treated as a set /
    de-duped without surprises. ``used`` flips true the first time the
    agent actually crosses it (recorded via ``mark_warp_used`` /
    ``WorldMemory.record_map_transition``).
    """

    from_map: tuple[int, int]
    from_tile: tuple[int, int]
    to_map: tuple[int, int]
    to_warp_id: int
    used: bool


@dataclasses.dataclass(frozen=True)
class MapEdgeConnection:
    """A seamless edge-to-edge link between two maps (no warp event).

    FRLG keeps both maps loaded at the seam and slides the connected
    map's coordinate frame by ``offset`` along the shared axis. Walking
    off the source map's edge in ``direction`` puts the player on the
    other map -- no doorway, no animation. Kyogre-dive transitions are
    modelled the same way with CONNECTION_DIVE / CONNECTION_EMERGE.

    Fields:
        from_map:  (group, num) of the source map.
        to_map:    (group, num) of the destination map.
        direction: ``C.CONNECTION_{SOUTH,NORTH,WEST,EAST,DIVE,EMERGE}``.
        offset:    signed shift (tiles) along the shared axis. Only
                   meaningful for cardinal directions.
        used:      has the agent actually crossed this seam at least once?
    """

    from_map: tuple[int, int]
    to_map: tuple[int, int]
    direction: int
    offset: int
    used: bool


class MapMemory:
    """The agent's accumulated knowledge about one map.

    Built incrementally via ``update_visibility`` every tick. Only
    tiles the agent has personally seen appear in ``explored_tiles`` --
    that set is exactly what A* pathfinds on. If a tile isn't here,
    it doesn't exist for navigation.
    """

    def __init__(
        self, map_group: int, map_num: int, width: int, height: int
    ) -> None:
        self.map_group = map_group
        self.map_num = map_num
        self.width = width
        self.height = height

        self.explored_tiles: dict[tuple[int, int], ExploredTile] = {}
        self.seen_interactables: dict[tuple[int, int], SeenInteractable] = {}
        self.known_warps: dict[tuple[int, int], WarpConnection] = {}
        # Seamless edge connections to neighbouring maps, keyed on the
        # direction (CONNECTION_NORTH/SOUTH/EAST/WEST/DIVE/EMERGE) since
        # at most one connection exists per direction. Populated once
        # per map visit from ``reader.read_map_connections_cached()``
        # via :meth:`set_connections`.
        self.connections: dict[int, MapEdgeConnection] = {}
        # Frontier: walkable explored tiles with at least one unexplored
        # neighbour. The LLM picks from this set when it wants to push
        # further into the map.
        self.exploration_frontier: set[tuple[int, int]] = set()

    def update_visibility(
        self,
        player_x: int,
        player_y: int,
        visible_tiles: dict[tuple[int, int], tuple[int, int, int, int]],
        interactables: list[Interactable],
        step: int,
    ) -> None:
        """Fold a tick's observations into the agent's memory.

        ``visible_tiles`` maps ``(x, y)`` (logical space) to
        ``(metatile_id, behavior, collision, elevation)``. The caller
        computes it from the reader's cached grid -- no HTTP here.
        ``interactables`` is the list returned by
        ``reader.read_visible_interactables``.
        """
        for (tx, ty), (mid, behavior, collision, elevation) in visible_tiles.items():
            existing = self.explored_tiles.get((tx, ty))
            if existing is not None:
                existing.last_seen_step = step
                # Tile contents can change (scripted cutscene tile swaps)
                # so refresh the snapshot -- first_seen_step stays pinned.
                existing.metatile_id = mid
                existing.behavior = behavior
                existing.collision = collision
                existing.elevation = elevation
            else:
                self.explored_tiles[(tx, ty)] = ExploredTile(
                    x=tx,
                    y=ty,
                    metatile_id=mid,
                    behavior=behavior,
                    collision=collision,
                    elevation=elevation,
                    first_seen_step=step,
                    last_seen_step=step,
                )

        for inter in interactables:
            key = (inter.x, inter.y)
            if key not in self.seen_interactables:
                self.seen_interactables[key] = SeenInteractable(
                    x=inter.x,
                    y=inter.y,
                    kind=inter.kind,
                    label=inter.label,
                    first_seen_step=step,
                    interacted=False,
                    interaction_result=None,
                )
            if inter.kind == "warp":
                dest = inter.details
                prior = self.known_warps.get(key)
                self.known_warps[key] = WarpConnection(
                    from_map=(self.map_group, self.map_num),
                    from_tile=key,
                    to_map=(dest["dest_map_group"], dest["dest_map_num"]),
                    to_warp_id=dest["dest_warp_id"],
                    used=prior.used if prior is not None else False,
                )

        self._rebuild_frontier()

    def mark_interacted(self, x: int, y: int, result: str | None = None) -> None:
        """Record a successful press-A on this tile and (optionally) its outcome."""
        key = (x, y)
        seen = self.seen_interactables.get(key)
        if seen is None:
            return
        seen.interacted = True
        seen.interaction_result = result

    def mark_warp_used(self, x: int, y: int) -> None:
        """Flip the ``used`` bit on a known warp. No-op if the warp isn't seen."""
        key = (x, y)
        warp = self.known_warps.get(key)
        if warp is None:
            return
        self.known_warps[key] = dataclasses.replace(warp, used=True)

    def set_connections(self, raw: list[MapConnection]) -> None:
        """Seed / refresh the edge connections for this map.

        Called once per map visit with the ROM-side ``MapConnection``
        records (see :meth:`RamReader.read_map_connections_cached`).
        Existing ``used`` flags are preserved so a prior crossing isn't
        forgotten when the player walks back into the same map.
        """
        new_conns: dict[int, MapEdgeConnection] = {}
        for conn in raw:
            prior = self.connections.get(conn.direction)
            new_conns[conn.direction] = MapEdgeConnection(
                from_map=(self.map_group, self.map_num),
                to_map=(conn.dest_map_group, conn.dest_map_num),
                direction=conn.direction,
                offset=conn.offset,
                used=prior.used if prior is not None else False,
            )
        self.connections = new_conns
        self._rebuild_frontier()

    def mark_connection_used(self, direction: int) -> None:
        """Flip the ``used`` bit on a known edge connection."""
        conn = self.connections.get(direction)
        if conn is None:
            return
        self.connections[direction] = dataclasses.replace(conn, used=True)

    def _rebuild_frontier(self) -> None:
        """Walkable explored tiles adjacent to something worth exploring.

        "Worth exploring" means one of:

        1. An in-bounds neighbour that the agent hasn't seen yet, or
        2. An out-of-bounds neighbour in a direction that has a
           ``MapEdgeConnection`` -- walking off that edge lands the
           player on the connected map, so the tile is a genuine
           gateway rather than a dead end.

        Out-of-bounds neighbours without a connection are skipped: they
        are the 7-tile render border, nothing's ever going to spawn
        there, and counting them pinned every wall-adjacent floor tile
        as a permanent frontier (the original bug).
        """
        self.exploration_frontier.clear()
        conn_dirs = self._connected_directions()
        for (tx, ty), tile in self.explored_tiles.items():
            # Non-walkable tiles are dead ends for exploration, not frontier.
            if not _is_walkable(tile.collision, tile.behavior):
                continue
            for dx, dy, conn_dir in _NEIGHBOR_DIRS:
                nx, ny = tx + dx, ty + dy
                if 0 <= nx < self.width and 0 <= ny < self.height:
                    if (nx, ny) not in self.explored_tiles:
                        self.exploration_frontier.add((tx, ty))
                        break
                else:
                    # Stepping off the logical map is only meaningful
                    # when a connection exists in that direction -- that
                    # edge tile then acts as a seamless crossing.
                    if conn_dir in conn_dirs:
                        self.exploration_frontier.add((tx, ty))
                        break

    def _connected_directions(self) -> set[int]:
        """Directions (CONNECTION_*) this map has edge connections on."""
        return {conn.direction for conn in self.connections.values()}

    def edge_connection_at(
        self, x: int, y: int
    ) -> MapEdgeConnection | None:
        """Return the connection the agent would cross by walking off (x, y).

        Only returns a hit when ``(x, y)`` sits exactly on the map edge
        that matches a known connection's direction. Inland tiles and
        edges without a connection return ``None``.
        """
        for direction, conn in self.connections.items():
            if direction == C.CONNECTION_NORTH and y == 0:
                return conn
            if direction == C.CONNECTION_SOUTH and y == self.height - 1:
                return conn
            if direction == C.CONNECTION_WEST and x == 0:
                return conn
            if direction == C.CONNECTION_EAST and x == self.width - 1:
                return conn
        return None

    def nearest_frontier(
        self, from_x: int, from_y: int
    ) -> tuple[int, int] | None:
        """Closest frontier tile by Manhattan distance. None if fully explored."""
        if not self.exploration_frontier:
            return None
        return min(
            self.exploration_frontier,
            key=lambda t: abs(t[0] - from_x) + abs(t[1] - from_y),
        )

    def uninteracted(self) -> list[SeenInteractable]:
        """Seen interactables the agent hasn't yet pressed A on."""
        return [s for s in self.seen_interactables.values() if not s.interacted]

    def unused_warps(self) -> list[WarpConnection]:
        """Warps the agent has observed but not walked through."""
        return [w for w in self.known_warps.values() if not w.used]

    @property
    def exploration_percentage(self) -> float:
        """Fraction of the map's tiles explored, 0.0 - 100.0."""
        total = self.width * self.height
        if total <= 0:
            return 0.0
        return (len(self.explored_tiles) / total) * 100.0

    def is_tile_walkable(self, x: int, y: int) -> bool | None:
        """True / False if the tile is known; None if the agent hasn't seen it.

        A tile is walkable iff its collision bits are zero *and* its
        behavior isn't one of the omnidirectionally-blocking flavours
        (IMPASSABLE_*, ledges with no explicit jump context, deep water
        without Surf, ...). The behavior check lets us flag tiles the
        game draws as floor but refuses to let the player step on --
        collision alone misses those.
        """
        tile = self.explored_tiles.get((x, y))
        if tile is None:
            return None
        return _is_walkable(tile.collision, tile.behavior)

    def get_walkable_neighbors(
        self,
        x: int,
        y: int,
        object_positions: set[tuple[int, int]] | None = None,
    ) -> list[tuple[int, int, int]]:
        """Neighbor function A* calls: (nx, ny, cost) per walkable neighbor.

        Only returns tiles that exist in ``explored_tiles`` AND are
        walkable. Unexplored tiles are *never* returned -- they simply
        don't exist in the navigation graph. A* returning "no path"
        means the agent needs to explore further, not that the
        destination is unreachable in the real world.

        ``object_positions`` is the set of tiles currently occupied by
        NPCs / other dynamic object events. Those tiles are excluded
        from the neighbor set so the path routes around them.
        """
        if object_positions is None:
            object_positions = set()

        neighbors: list[tuple[int, int, int]] = []
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            nx, ny = x + dx, y + dy
            tile = self.explored_tiles.get((nx, ny))
            if tile is None:
                continue
            if not _is_walkable(tile.collision, tile.behavior):
                continue
            if (nx, ny) in object_positions:
                continue
            # TODO: directional rules go here later:
            #   - ledges: only passable in the jump direction
            #   - deep water: only with Surf + the right badge
            #   - cuttable trees: only with Cut + the right badge
            # Until then ``_is_walkable`` treats them as blocked so the
            # agent never builds a plan that would require a HM it hasn't
            # earned.
            neighbors.append((nx, ny, 1))

        return neighbors


class WorldMemory:
    """Cross-map memory: one ``MapMemory`` per visited map plus a warp graph."""

    def __init__(self) -> None:
        self._maps: dict[tuple[int, int], MapMemory] = {}
        # Edges the agent has *actually traversed*, appended on each
        # observed map transition. Distinct from MapMemory.known_warps
        # (which includes warps merely spotted on screen).
        self.warp_graph: list[WarpConnection] = []

    def get_or_create(
        self, map_group: int, map_num: int, width: int, height: int
    ) -> MapMemory:
        """Return the MapMemory for this map, creating it lazily on first visit."""
        key = (map_group, map_num)
        mem = self._maps.get(key)
        if mem is None:
            mem = MapMemory(map_group, map_num, width, height)
            self._maps[key] = mem
        return mem

    def get_map(self, map_group: int, map_num: int) -> MapMemory | None:
        return self._maps.get((map_group, map_num))

    def record_map_transition(
        self,
        from_map: tuple[int, int],
        from_tile: tuple[int, int],
        to_map: tuple[int, int],
        to_warp_id: int,
    ) -> None:
        """Log that the agent walked through a warp between maps."""
        self.warp_graph.append(
            WarpConnection(
                from_map=from_map,
                from_tile=from_tile,
                to_map=to_map,
                to_warp_id=to_warp_id,
                used=True,
            )
        )

    def maps_visited(self) -> list[tuple[int, int]]:
        return list(self._maps.keys())

    def total_tiles_explored(self) -> int:
        return sum(len(m.explored_tiles) for m in self._maps.values())

    # --- Cross-map navigation ------------------------------------------------

    def map_adjacency(
        self,
    ) -> dict[
        tuple[int, int], list[WarpConnection | MapEdgeConnection]
    ]:
        """Adjacency list over the map-level graph.

        Unifies two kinds of inter-map edge:

        * **Warps** -- tile-specific teleports the agent has seen on
          screen or actually crossed (``MapMemory.known_warps`` plus
          the cross-map ``warp_graph``).
        * **Edge connections** -- seamless map-to-map transitions from
          ``MapMemory.connections`` (populated via ``set_connections``).

        Both surface as edges out of the source map keyed only on
        destination/identity so duplicates collapse. Callers that need
        to distinguish them can ``isinstance`` on the result.
        """
        adj: dict[tuple[int, int], dict[tuple, object]] = {}
        for (g, n), mem in self._maps.items():
            bucket = adj.setdefault((g, n), {})
            for warp in mem.known_warps.values():
                key = ("warp", warp.from_tile, warp.to_map, warp.to_warp_id)
                existing = bucket.get(key)
                if existing is None or (
                    warp.used and not getattr(existing, "used", False)
                ):
                    bucket[key] = warp
            for conn in mem.connections.values():
                key = ("edge", conn.direction, conn.to_map)
                existing = bucket.get(key)
                if existing is None or (
                    conn.used and not getattr(existing, "used", False)
                ):
                    bucket[key] = conn
        for warp in self.warp_graph:
            bucket = adj.setdefault(warp.from_map, {})
            key = ("warp", warp.from_tile, warp.to_map, warp.to_warp_id)
            existing = bucket.get(key)
            if existing is None or (
                warp.used and not getattr(existing, "used", False)
            ):
                bucket[key] = warp
        return {
            src: list(edges.values())  # type: ignore[list-item]
            for src, edges in adj.items()
        }

    # Old name kept for compatibility with callers expecting warps only.
    warp_adjacency = map_adjacency

    def find_map_path(
        self,
        from_map: tuple[int, int],
        to_map: tuple[int, int],
    ) -> list[WarpConnection | MapEdgeConnection] | None:
        """BFS on the map graph. Returns the ordered list of edges to
        traverse (warps + edge connections, mixed), ``[]`` if already
        on the target map, or ``None`` if no path is known through the
        currently-observed edges.
        """
        if from_map == to_map:
            return []
        adj = self.map_adjacency()
        queue: deque[
            tuple[tuple[int, int], list[WarpConnection | MapEdgeConnection]]
        ] = deque()
        queue.append((from_map, []))
        visited: set[tuple[int, int]] = {from_map}
        while queue:
            current, path = queue.popleft()
            for edge in adj.get(current, ()):
                dest = edge.to_map
                if dest in visited:
                    continue
                new_path = path + [edge]
                if dest == to_map:
                    return new_path
                visited.add(dest)
                queue.append((dest, new_path))
        return None

    # --- Persistence ---------------------------------------------------------

    def save(self, base_path: str | Path) -> None:
        """Serialise the whole WorldMemory to ``base_path``.

        Layout::

            <base_path>/
              world.json          -- warp_graph + visited maps
              maps/
                <group:03d>_<num:03d>.json   -- one MapMemory per file
        """
        base = Path(base_path)
        maps_dir = base / "maps"
        maps_dir.mkdir(parents=True, exist_ok=True)

        world_doc = {
            "warp_graph": [dataclasses.asdict(w) for w in self.warp_graph],
            "maps_visited": [list(k) for k in self._maps.keys()],
        }
        (base / "world.json").write_text(
            json.dumps(world_doc, indent=2), encoding="utf-8"
        )

        for (g, n), mem in self._maps.items():
            map_doc = {
                "map_group": mem.map_group,
                "map_num": mem.map_num,
                "width": mem.width,
                "height": mem.height,
                "explored_tiles": [
                    dataclasses.asdict(t) for t in mem.explored_tiles.values()
                ],
                "seen_interactables": [
                    dataclasses.asdict(s) for s in mem.seen_interactables.values()
                ],
                "known_warps": [
                    dataclasses.asdict(w) for w in mem.known_warps.values()
                ],
                "connections": [
                    dataclasses.asdict(c) for c in mem.connections.values()
                ],
            }
            (maps_dir / f"{g:03d}_{n:03d}.json").write_text(
                json.dumps(map_doc, indent=2), encoding="utf-8"
            )

    @classmethod
    def load(cls, base_path: str | Path) -> WorldMemory:
        """Inverse of :meth:`save`. Returns an empty WorldMemory if the
        directory doesn't exist yet.
        """
        base = Path(base_path)
        world = cls()
        world_file = base / "world.json"
        if not world_file.exists():
            return world

        doc = json.loads(world_file.read_text(encoding="utf-8"))
        world.warp_graph = [
            _warp_from_dict(w) for w in doc.get("warp_graph", [])
        ]

        maps_dir = base / "maps"
        if not maps_dir.exists():
            return world

        for map_file in sorted(maps_dir.glob("*.json")):
            map_doc = json.loads(map_file.read_text(encoding="utf-8"))
            mem = MapMemory(
                map_doc["map_group"],
                map_doc["map_num"],
                map_doc["width"],
                map_doc["height"],
            )
            for t in map_doc.get("explored_tiles", []):
                tile = ExploredTile(**t)
                mem.explored_tiles[(tile.x, tile.y)] = tile
            for s in map_doc.get("seen_interactables", []):
                inter = SeenInteractable(**s)
                mem.seen_interactables[(inter.x, inter.y)] = inter
            for w in map_doc.get("known_warps", []):
                warp = _warp_from_dict(w)
                mem.known_warps[warp.from_tile] = warp
            for c in map_doc.get("connections", []):
                conn = _conn_from_dict(c)
                mem.connections[conn.direction] = conn
            mem._rebuild_frontier()
            world._maps[(mem.map_group, mem.map_num)] = mem
        return world


def _warp_from_dict(d: dict) -> WarpConnection:
    """Rebuild a WarpConnection after a JSON round-trip (tuples -> lists)."""
    return WarpConnection(
        from_map=tuple(d["from_map"]),
        from_tile=tuple(d["from_tile"]),
        to_map=tuple(d["to_map"]),
        to_warp_id=d["to_warp_id"],
        used=d["used"],
    )


def _conn_from_dict(d: dict) -> MapEdgeConnection:
    """Rebuild a MapEdgeConnection after a JSON round-trip."""
    return MapEdgeConnection(
        from_map=tuple(d["from_map"]),
        to_map=tuple(d["to_map"]),
        direction=d["direction"],
        offset=d["offset"],
        used=d["used"],
    )


# Cardinal neighbour offsets paired with the CONNECTION_* that would
# apply if that step crossed a map edge. ``None`` means "this
# direction doesn't correspond to a map-edge connection" -- unused
# today (all four cardinals can be edge-crossings) but kept as a
# placeholder if we ever add diagonal moves.
_NEIGHBOR_DIRS: tuple[tuple[int, int, int], ...] = (
    (0, -1, C.CONNECTION_NORTH),
    (0,  1, C.CONNECTION_SOUTH),
    (-1, 0, C.CONNECTION_WEST),
    (1,  0, C.CONNECTION_EAST),
)


# Behaviors that block a tile outright, regardless of what the 2-bit
# collision field says. These are the tiles the game draws as floor
# but refuses to let the player step onto (or step across, in the
# directional cases). Treating them as walls is the conservative
# choice: A* will refuse to plan through them until we learn the
# directional / HM context.
_BLOCKING_BEHAVIORS: frozenset[int] = frozenset(
    int(b)
    for b in (
        *C.IMPASSABLE_BEHAVIORS,
        *C.LEDGE_JUMP_BEHAVIORS,
        # WATER_BEHAVIORS is the authoritative surfable set
        # (pret/src/metatile_behavior.c sBehaviorSurfable[]). It excludes
        # SHALLOW_WATER (0x17) and PUDDLE (0x16) on purpose -- the engine
        # walks straight through both, just plays a splash effect, so they
        # are NOT impassable for A* either.
        *C.WATER_BEHAVIORS,
        C.MetatileBehavior.UNDERWATER_BLOCKED_ABOVE,
        C.MetatileBehavior.COUNTER,
        C.MetatileBehavior.BOOKSHELF,
        C.MetatileBehavior.POKEMART_SHELF,
        C.MetatileBehavior.PC,
        C.MetatileBehavior.TELEVISION,
        C.MetatileBehavior.POKEMON_CENTER_SIGN,
        C.MetatileBehavior.POKEMART_SIGN,
        C.MetatileBehavior.SIGNPOST,
        C.MetatileBehavior.REGION_MAP,
    )
)


def _is_walkable(collision: int, behavior: int) -> bool:
    """Tile is walkable iff collision bits are clear and the behavior
    isn't one of the blanket-blocking flavours.

    This is intentionally conservative: a tile only gets a ``True``
    verdict if both the 2-bit collision field AND the 9-bit behavior
    agree it's fair game. The set of blocked behaviors covers the
    flavours that the game draws as floor but the engine treats as
    impassable -- counters / PCs / shelves / signs (you press A, you
    don't walk onto them), water (needs Surf), ledges (one-way jumps
    we don't plan through yet), and the IMPASSABLE_* directional blocks.
    """
    if collision != 0:
        return False
    return behavior not in _BLOCKING_BEHAVIORS


def compute_visible_tiles(
    reader: RamReader,
    player_x: int,
    player_y: int,
) -> dict[tuple[int, int], tuple[int, int, int, int]]:
    """Return per-tile ``(metatile_id, behavior, collision, elevation)``.

    Covers the 15x11 window centred on the player (5 tiles above, 5
    below), in logical / SaveBlock1 coordinates. The window matches
    ``RamReader.read_visible_tiles`` and ``read_visible_interactables``
    exactly so MapMemory and the interactable sweep agree on what's
    "on screen". Pure Python once the reader's per-map caches are warm
    -- ``read_map_grid_cached`` populates the behavior cache for every
    id the map uses on first call, so this function makes zero
    additional HTTP calls per tick.

    Tiles outside the *logical* map (i.e. inside the 7-tile border the
    game pads around the real layout) are skipped. The border is
    rendered void-like on screen and its collision bits are unrelated
    to gameplay; including it would pollute ``MapMemory.explored_tiles``
    with phantom floors the agent can never actually stand on.
    """
    xsize, ysize, grid = reader.read_map_grid_cached()
    logical_w = max(0, xsize - C.MAP_OFFSET_W)
    logical_h = max(0, ysize - C.MAP_OFFSET_H)
    min_x, max_x = player_x - 7, player_x + 7
    min_y, max_y = player_y - 5, player_y + 5

    visible: dict[tuple[int, int], tuple[int, int, int, int]] = {}
    for ly in range(min_y, max_y + 1):
        for lx in range(min_x, max_x + 1):
            if not (0 <= lx < logical_w and 0 <= ly < logical_h):
                continue
            gx = lx + C.MAP_OFFSET
            gy = ly + C.MAP_OFFSET
            if not (0 <= gx < xsize and 0 <= gy < ysize):
                continue
            mid, collision, elevation = reader.tile_cell_at(grid, xsize, gx, gy)
            behavior = reader.get_metatile_behavior_cached(mid)
            visible[(lx, ly)] = (mid, behavior, collision, elevation)
    return visible
