"""Structured RAM reader for Pokemon FireRed (US v1.1 / BPRE1).

Public surface:
    MemoryBackend        -- protocol for raw memory access
    RamReader            -- typed accessors built on top of a backend
    MgbaHttpBackend      -- concrete backend that talks to a running mGBA
                            via mgba-http (http://localhost:5000 by default)
    MgbaHttpError        -- raised on transport / response failures
    PlayerState          -- overworld position / facing / map
    ObjectEventSlot      -- one slot of the runtime object-event array
    PartyMember          -- a single overworld party Pokemon
    BattlePokemonState   -- one battler slot from gBattleMons
    BattleState          -- full battle snapshot
    WarpInfo             -- one warp destination from a map's event table
    BgEventInfo          -- one sign / hidden item from a map's event table
    MapEventData         -- bundled warps / BG events for a map
    Interactable         -- classified on-screen thing the agent can act on
    CachedMapData        -- MapStore snapshot of a single map
    MapStore             -- in-memory cache of CachedMapData
    load_current_map     -- one-liner that loads the reader's current map
    GameMode             -- coarse game state (overworld / battle / ...)
    BattleOutcome        -- win / loss / fled / caught / ...

Target ROM: Pokemon FireRed (US), revision 1.1 (BPRE1) -- the single
ROM this project targets. SaveBlock1 / SaveBlock2 are reached via
pointers and are version-stable; ROM-symbol addresses are BPRE1.
"""

from .constants import (
    GameMode,
    Facing,
    MetatileBehavior,
    BattleOutcome,
    decode_status1,
    decode_weather,
)
from .map_memory import (
    ExploredTile,
    MapEdgeConnection,
    MapMemory,
    SeenInteractable,
    WarpConnection,
    WorldMemory,
    compute_visible_tiles,
)
from .map_store import CachedMapData, MapStore, load_current_map
from .mgba_http import MgbaHttpBackend, MgbaHttpError
from .reader import (
    MemoryBackend,
    RamReader,
    PlayerState,
    ObjectEventSlot,
    PartyMember,
    BattlePokemonState,
    BattleState,
    WarpInfo,
    BgEventInfo,
    MapConnection,
    MapEventData,
    Interactable,
    TileInfo,
)

__all__ = [
    "MemoryBackend",
    "RamReader",
    "MgbaHttpBackend",
    "MgbaHttpError",
    "PlayerState",
    "ObjectEventSlot",
    "PartyMember",
    "BattlePokemonState",
    "BattleState",
    "WarpInfo",
    "BgEventInfo",
    "MapEventData",
    "MapConnection",
    "Interactable",
    "TileInfo",
    "CachedMapData",
    "MapStore",
    "load_current_map",
    "ExploredTile",
    "SeenInteractable",
    "WarpConnection",
    "MapEdgeConnection",
    "MapMemory",
    "WorldMemory",
    "compute_visible_tiles",
    "GameMode",
    "Facing",
    "MetatileBehavior",
    "BattleOutcome",
    "decode_status1",
    "decode_weather",
]
