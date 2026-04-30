"""Per-map cache for the cold, static data a planner re-reads most often.

The grid / event tables / tileset attribute pointers for a given map do
not change while the player is on that map -- they come straight from
ROM. But the ``reader`` is stateless across calls, so every A* step that
walked the map would otherwise pay the full pointer chain each time.
MapStore keeps one ``CachedMapData`` per ``(map_group, map_num)`` and
hands it back on the next visit.

Minimum useful surface:
    CachedMapData       -- the frozen snapshot returned by ``load_current``
    MapStore            -- simple in-memory cache keyed on (group, num)
    load_current_map    -- convenience wrapper that creates a one-shot store

Invalidation: call ``MapStore.invalidate()`` (whole cache) or
``invalidate(group, num)`` for one entry on map transitions. The live
tile grid is still included in the snapshot even though the engine
rewrites it between visits; the MapStore is intended as a per-visit
bundle, not a long-lived store of grid bytes. Callers that want the
absolute freshest grid should re-``load_current_map`` after transitions.
"""

from __future__ import annotations

import dataclasses

from .reader import MapEventData, RamReader


@dataclasses.dataclass(frozen=True)
class CachedMapData:
    """One map's ROM-sourced data, read in one pass.

    ``grid`` is the raw u16 cell buffer (``xsize * ysize * 2`` bytes) --
    decode with ``RamReader.tile_cell_at``. ``primary_attrs_ptr`` and
    ``secondary_attrs_ptr`` are ROM addresses pointing into the two
    tileset metatileAttributes arrays; pair them with
    ``RamReader.get_metatile_behavior`` (which reads on demand and
    caches) or read them in bulk if the caller prefers.
    """

    map_group: int
    map_num: int
    xsize: int
    ysize: int
    grid: bytes
    events: MapEventData
    primary_attrs_ptr: int
    secondary_attrs_ptr: int


class MapStore:
    """In-memory cache of CachedMapData keyed on ``(map_group, map_num)``.

    Not thread-safe -- expected to be owned by one planner instance.
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[int, int], CachedMapData] = {}

    def get(self, map_group: int, map_num: int) -> CachedMapData | None:
        """Return the cached snapshot for this map, or ``None`` if unseen."""
        return self._cache.get((map_group, map_num))

    def put(self, data: CachedMapData) -> None:
        """Overwrite any existing entry for ``(data.map_group, data.map_num)``."""
        self._cache[(data.map_group, data.map_num)] = data

    def invalidate(
        self, map_group: int | None = None, map_num: int | None = None
    ) -> None:
        """Drop one entry, or the whole cache if either arg is ``None``."""
        if map_group is None or map_num is None:
            self._cache.clear()
            return
        self._cache.pop((map_group, map_num), None)

    def __contains__(self, key: tuple[int, int]) -> bool:
        return key in self._cache

    def __len__(self) -> int:
        return len(self._cache)

    def load_current(self, reader: RamReader) -> CachedMapData:
        """Load (or re-use) the map the reader is currently on.

        The first call per map walks the ROM pointer chain; subsequent
        calls return the cached snapshot. ``reader.invalidate_map_cache``
        is NOT called here -- callers that cross a map boundary should
        ``invalidate`` this store *and* the reader's own per-map cache.
        """
        # Resolve the key from the *fresh* SaveBlock1 so the cache still
        # fires after a reload where the snapshot is valid but the
        # reader itself hasn't been asked anything yet.
        player = reader.read_player_state()
        key = (player.map_group, player.map_num)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        xsize, ysize, grid = reader.read_map_grid()
        events = reader.read_map_events()
        primary_attrs_ptr, secondary_attrs_ptr = reader.read_tileset_attrs()

        data = CachedMapData(
            map_group=player.map_group,
            map_num=player.map_num,
            xsize=xsize,
            ysize=ysize,
            grid=grid,
            events=events,
            primary_attrs_ptr=primary_attrs_ptr,
            secondary_attrs_ptr=secondary_attrs_ptr,
        )
        self._cache[key] = data
        return data


def load_current_map(
    reader: RamReader, store: MapStore | None = None
) -> CachedMapData:
    """Convenience wrapper: loads the current map via ``store`` or a fresh one.

    Passing ``store=None`` produces a one-shot cache with one entry -- the
    caller gets a complete snapshot but no reuse. Pass a shared
    ``MapStore`` to actually benefit from caching across calls.
    """
    if store is None:
        store = MapStore()
    return store.load_current(reader)
