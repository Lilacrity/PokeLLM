"""Structured RAM reader for Pokemon FireRed (US v1.0).

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
    GameMode             -- coarse game state (overworld / battle / ...)
    BattleOutcome        -- win / loss / fled / caught / ...

Target ROM: Pokemon FireRed (US), revision 1.0 (BPRE0). Other revisions
(rev 1.1, LeafGreen, non-US) shift the fixed EWRAM/IWRAM addresses; see
constants.py. SaveBlock1 / SaveBlock2 are reached via pointers and are
version-stable.
"""

from .constants import (
    GameMode,
    Facing,
    MetatileBehavior,
    BattleOutcome,
    decode_status1,
    decode_weather,
)
from .mgba_http import MgbaHttpBackend, MgbaHttpError
from .reader import (
    MemoryBackend,
    RamReader,
    PlayerState,
    ObjectEventSlot,
    PartyMember,
    BattlePokemonState,
    BattleState,
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
    "GameMode",
    "Facing",
    "MetatileBehavior",
    "BattleOutcome",
    "decode_status1",
    "decode_weather",
]
