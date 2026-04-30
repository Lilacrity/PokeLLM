"""Structured accessors over a memory backend for Pokemon FireRed.

The RamReader does not care how bytes get off the emulator; it takes any
object that implements ``read_bytes(addr, length) -> bytes``. That keeps
mgba-http, a Lua bridge, a prerecorded snapshot file, or a unit-test fake
interchangeable from the reader's point of view.

Design notes:
    *   Every read that resolves through a SaveBlock pointer refreshes the
        pointer fresh. The game's anti-tamper DMA relocator moves the save
        blocks around, so a cached pointer is a time-bomb. The overhead is
        one extra 4-byte read per query.
    *   Struct reads are chunked -- the object-event array is read as a
        single 576-byte blob, not 16 separate slot reads -- because round
        trips to the emulator dominate cost.
    *   Accessor names keep the pret struct vocabulary so cross-referencing
        the decomp stays straightforward.
    *   Species/move name tables are cached at init time. They live in ROM
        and never change, so one bulk read at startup is correct.
"""

from __future__ import annotations

import dataclasses
import struct
from collections.abc import Iterable
from typing import Protocol

from . import constants as C


# -- FireRed custom charset -> unicode ---------------------------------------
# Built from pret/include/characters.h.  0xFF = EOS, 0xFE = newline,
# 0xFD = placeholder (next byte is a command), 0xFC = ext ctrl code
# (next byte is a sub-command; some sub-commands consume a third byte).

_CHARMAP: dict[int, str] = {
    0x00: " ",
    0x01: "\u00c0", 0x02: "\u00c1", 0x03: "\u00c2", 0x04: "\u00c7",
    0x05: "\u00c8", 0x06: "\u00c9", 0x07: "\u00ca", 0x08: "\u00cb",
    0x09: "\u00cc", 0x0B: "\u00ce", 0x0C: "\u00cf",
    0x0D: "\u00d2", 0x0E: "\u00d3", 0x0F: "\u00d4",
    0x10: "\u0152", 0x11: "\u00d9", 0x12: "\u00da", 0x13: "\u00db",
    0x14: "\u00d1", 0x15: "\u00df",
    0x16: "\u00e0", 0x17: "\u00e1", 0x19: "\u00e7",
    0x1A: "\u00e8", 0x1B: "\u00e9", 0x1C: "\u00ea", 0x1D: "\u00eb",
    0x1E: "\u00ec", 0x20: "\u00ee", 0x21: "\u00ef",
    0x22: "\u00f2", 0x23: "\u00f3", 0x24: "\u00f4",
    0x25: "\u0153", 0x26: "\u00f9", 0x27: "\u00fa", 0x28: "\u00fb",
    0x29: "\u00f1",
    0x2D: "&", 0x2E: "+",
    0x35: "=",
    0x5A: "\u00cd",  # I_ACUTE
    0x5B: "%", 0x5C: "(", 0x5D: ")",
    0x68: "\u00e2",  # a_CIRCUMFLEX
    0x6F: "\u00ed",  # i_ACUTE
    0xAB: "!", 0xAC: "?", 0xAD: ".", 0xAE: "-",
    0xAF: "\u2022",  # bullet
    0xB0: "\u2026",  # ellipsis
    0xB1: "\u201c", 0xB2: "\u201d",  # double quotes
    0xB3: "\u2018", 0xB4: "\u2019",  # single quotes
    0xB5: "\u2642", 0xB6: "\u2640",  # male/female
    0xB7: "\u00a5",  # currency
    0xB8: ",", 0xB9: "\u00d7", 0xBA: "/",
    0xF0: ":",
    0xF1: "\u00c4", 0xF2: "\u00d6", 0xF3: "\u00dc",
    0xF4: "\u00e4", 0xF5: "\u00f6", 0xF6: "\u00fc",
}
# 0-9
for _i, _ch in enumerate("0123456789", 0xA1):
    _CHARMAP[_i] = _ch
# A-Z
for _i, _ch in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ", 0xBB):
    _CHARMAP[_i] = _ch
# a-z
for _i, _ch in enumerate("abcdefghijklmnopqrstuvwxyz", 0xD5):
    _CHARMAP[_i] = _ch

# Number of extra bytes consumed by each EXT_CTRL_CODE sub-command (0xFC xx).
# Most consume exactly 1 argument byte; a few consume 0 or 2.
_EXT_CTRL_ARGS: dict[int, int] = {
    0x01: 1, 0x02: 1, 0x03: 1, 0x04: 3, 0x05: 1, 0x06: 1,
    0x07: 0, 0x08: 1, 0x09: 0, 0x0A: 0, 0x0B: 2, 0x0C: 1,
    0x0D: 1, 0x0E: 1, 0x0F: 0, 0x10: 2, 0x11: 1, 0x12: 1,
    0x13: 1, 0x14: 1, 0x15: 0, 0x16: 0, 0x17: 0, 0x18: 0,
}


def _decode_game_text(raw: bytes) -> str:
    """Decode FireRed's custom charset into a Python string.

    Stops at 0xFF (EOS) or end of buffer. Control codes (0xFC, 0xFD) are
    skipped along with their argument bytes — they encode colours / pauses
    / variable placeholders that aren't meaningful as text.
    """
    out: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        b = raw[i]
        if b == 0xFF:  # EOS
            break
        if b == 0xFE:  # newline
            out.append("\n")
            i += 1
            continue
        if b == 0xFA or b == 0xFB:  # prompt_scroll / prompt_clear
            i += 1
            continue
        if b == 0xFD:  # placeholder -- skip the command byte
            i += 2
            continue
        if b == 0xFC:  # ext ctrl code -- skip sub-command + its args
            if i + 1 < n:
                sub = raw[i + 1]
                skip = _EXT_CTRL_ARGS.get(sub, 1)
                i += 2 + skip
            else:
                i += 2
            continue
        ch = _CHARMAP.get(b)
        if ch is not None:
            out.append(ch)
        i += 1
    return "".join(out)


class MemoryBackend(Protocol):
    """The only interface the reader requires.

    Implementations must return exactly ``length`` bytes starting at
    ``addr`` or raise. Implementations may do their own batching / caching.
    """

    def read_bytes(self, addr: int, length: int) -> bytes: ...


# -----------------------------------------------------------------------------
# Classification helpers -- used by read_map_events / read_visible_interactables
# to turn raw byte tags into stable string kinds the planner can switch on.
# Kept at module level so tests can exercise them directly.
# -----------------------------------------------------------------------------


def _classify_bg_event(kind_raw: int) -> str:
    """Map a BgEvent.kind byte to a stable string tag."""
    if kind_raw in C.SIGN_BG_EVENT_KINDS:
        return "sign"
    if kind_raw == C.BG_EVENT_KIND_HIDDEN_ITEM:
        return "hidden_item"
    if kind_raw == C.BG_EVENT_KIND_SECRET_BASE:
        return "secret_base"
    return f"raw({kind_raw})"


# ObjectEvent graphics IDs that FRLG uses for HM-obstacle / treasure sprites.
# These live in the overworld as ordinary ObjectEvents rather than as a
# metatile behaviour, so the graphics_id is the canonical tell.
_HM_OBSTACLE_GRAPHICS: dict[int, tuple[str, str]] = {
    C.OBJ_EVENT_GFX_ITEM_BALL:        ("item_ball",     "Item Ball"),
    C.OBJ_EVENT_GFX_CUT_TREE:         ("cut_tree",      "Cuttable Tree"),
    C.OBJ_EVENT_GFX_ROCK_SMASH_ROCK:  ("smash_rock",    "Smashable Rock"),
    C.OBJ_EVENT_GFX_PUSHABLE_BOULDER: ("push_boulder",  "Strength Boulder"),
}


def _classify_object_event(graphics_id: int, trainer_type: int) -> tuple[str, str]:
    """Classify an ObjectEvent slot by graphics_id / trainer_type.

    Returns ``(kind, label)``. The HM-obstacle / item-ball sprites take
    priority over trainer classification: a Cuttable Tree graphic with a
    non-zero trainer_type (which does happen in a couple of spots) is
    still a tree.
    """
    obstacle = _HM_OBSTACLE_GRAPHICS.get(graphics_id)
    if obstacle is not None:
        kind, label = obstacle
        return kind, label
    if trainer_type != 0:
        return "trainer", "Trainer"
    return "npc", "NPC"


# Metatile behaviours that the player can "press A against" from an adjacent
# tile. These aren't ObjectEvents -- they're baked into the tileset, so they
# only surface when you scan the metatile grid. The canonical FRLG examples
# are the PokeCenter healing counter (0x80) and the in-room PC (0x83) --
# both the user's main motivation for adding this pass.
_INTERACTABLE_TILE_BEHAVIORS: dict[int, tuple[str, str]] = {
    int(C.MetatileBehavior.COUNTER):             ("counter",   "Counter (heal / shop)"),
    int(C.MetatileBehavior.BOOKSHELF):           ("bookshelf", "Bookshelf"),
    int(C.MetatileBehavior.POKEMART_SHELF):      ("pokemart_shelf", "PokeMart Shelf"),
    int(C.MetatileBehavior.PC):                  ("pc",        "PC"),
    int(C.MetatileBehavior.SIGNPOST):            ("signpost",  "Signpost"),
    int(C.MetatileBehavior.REGION_MAP):          ("region_map", "Region Map"),
    int(C.MetatileBehavior.TELEVISION):          ("television", "Television"),
    # Building signs (POKEMON_CENTER_SIGN / POKEMART_SIGN): functionally
    # identical to any other readable sign -- you press A and a dialogue
    # fires. We classify them under the generic ``sign`` kind so they
    # share the renderer's "sign" treatment (cyan dot, no special base
    # colour). The descriptive label still makes it clear which building
    # the sign belongs to.
    int(C.MetatileBehavior.POKEMON_CENTER_SIGN): ("sign",      "Pokemon Center Sign"),
    int(C.MetatileBehavior.POKEMART_SIGN):       ("sign",      "PokeMart Sign"),
}


def _classify_tile_behavior(behavior: int) -> tuple[str, str] | None:
    """Return (kind, label) for an A-pressable tile behaviour, or None."""
    return _INTERACTABLE_TILE_BEHAVIORS.get(behavior)


def _cluster_warps_4adjacent(warps: list) -> list[list]:
    """Split warps into 4-adjacent (Manhattan distance 1) clusters.

    Two warps to the same destination map that sit next to each other
    are the same physical doorway and collapse to one Interactable;
    two warps to the same destination map that sit far apart (e.g. two
    different staircases both leading to F2) stay as separate
    interactables. Standard flood-fill on the 4-neighbour graph.
    """
    clusters: list[list] = []
    remaining = list(warps)
    while remaining:
        seed = remaining.pop()
        cluster = [seed]
        coords_in_cluster: set[tuple[int, int]] = {(seed.x, seed.y)}
        while True:
            grew = False
            for w in list(remaining):
                if any(
                    abs(w.x - cx) + abs(w.y - cy) == 1
                    for cx, cy in coords_in_cluster
                ):
                    cluster.append(w)
                    coords_in_cluster.add((w.x, w.y))
                    remaining.remove(w)
                    grew = True
            if not grew:
                break
        clusters.append(cluster)
    return clusters


# -----------------------------------------------------------------------------
# Dataclasses for typed returns.
# -----------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class PlayerState:
    """Overworld position plus the map the player is in.

    ``x`` / ``y`` are world-grid tile coordinates (no MAP_OFFSET applied --
    these match what scripts / decomp code refers to as SaveBlock1.pos).
    """

    x: int
    y: int
    map_group: int
    map_num: int
    map_layout_id: int
    party_count: int
    facing: C.Facing  # pulled from the player's slot in gObjectEvents


@dataclasses.dataclass(frozen=True)
class ObjectEventSlot:
    """One 36-byte slot of the gObjectEvents array.

    Raw-ish representation -- the caller decides how to interpret
    graphics_id / movement_type. The ``active`` flag is broken out because
    it is the single most common branch (skip inactive slots).
    """

    slot: int
    active: bool
    is_player: bool
    invisible: bool
    off_screen: bool
    graphics_id: int
    movement_type: int
    trainer_type: int
    local_id: int
    map_num: int
    map_group: int
    x: int
    y: int
    previous_x: int
    previous_y: int
    facing: C.Facing
    current_metatile_behavior: int  # raw int -- callers can coerce to MetatileBehavior


@dataclasses.dataclass(frozen=True)
class PartyMember:
    """The minimum-viable view of a party slot.

    Fields here are the *unencrypted* tail of struct Pokemon. Species, moves,
    PP, IVs, etc. live inside the encrypted substruct block at +0x20 and
    require the personality/otId XOR scheme -- we'll add that when a caller
    actually needs it, not before.
    """

    slot: int
    personality: int
    ot_id: int
    nickname: str
    level: int
    status: int
    status_text: str
    hp: int
    max_hp: int
    attack: int
    defense: int
    speed: int
    sp_attack: int
    sp_defense: int
    is_egg: bool


@dataclasses.dataclass(frozen=True)
class BattlePokemonState:
    """View of one battler slot from ``gBattleMons``."""

    species_id: int
    species_name: str
    nickname: str
    level: int
    hp: int
    max_hp: int
    attack: int
    defense: int
    speed: int
    sp_attack: int
    sp_defense: int
    moves: tuple[int, ...]
    move_names: tuple[str, ...]
    pp: tuple[int, ...]
    status_text: str
    raw_status1: int


@dataclasses.dataclass(frozen=True)
class BattleState:
    """Snapshot of the current battle, or ``active=False`` outside battle."""

    active: bool
    battlers_count: int
    player: BattlePokemonState | None
    enemy: BattlePokemonState | None
    player_party_index: int
    enemy_party_index: int
    player_party_alive: int
    enemy_party_alive: int
    outcome: int
    outcome_text: str
    weather: int
    weather_text: str
    current_move: int
    current_move_name: str
    # Battle-type classification -- the bits the agent planner branches on.
    is_wild: bool
    is_trainer: bool
    is_double: bool
    is_catchable: bool        # wild and not blocked by Safari / tutorial / etc.
    battle_type_flags: int    # raw gBattleTypeFlags, for anything else
    trainer_opponent_id: int  # gTrainerBattleOpponent_A, 0 outside trainer fights


@dataclasses.dataclass(frozen=True)
class WarpInfo:
    """One entry from the current map's WarpEvent array (ROM data)."""

    x: int                    # tile coord, SaveBlock1 space (no border offset)
    y: int
    dest_map_group: int
    dest_map_num: int
    dest_warp_id: int


@dataclasses.dataclass(frozen=True)
class BgEventInfo:
    """One entry from the current map's BgEvent array (ROM data)."""

    x: int                    # tile coord, SaveBlock1 space
    y: int
    kind: str                 # "sign" | "hidden_item" | "secret_base" | f"raw({n})"
    kind_raw: int
    elevation: int
    # union payload: ROM script pointer for signs, packed hidden-item u32 for
    # hidden items. Left raw for callers who need it.
    data: int


@dataclasses.dataclass(frozen=True)
class MapEventData:
    """Static map-event tables read once per map visit (walked from gMapHeader)."""

    warps: list[WarpInfo]
    bg_events: list[BgEventInfo]
    object_event_count: int   # count of ObjectEventTemplates the map defines


@dataclasses.dataclass(frozen=True)
class MapConnection:
    """One entry from the current map's MapConnections table (ROM data).

    Map connections are seamless edge-to-edge links between maps --
    e.g. walking off the east edge of Route 1 puts you in Viridian City
    without any warp event ever firing. The engine keeps both maps'
    grids loaded and translates coordinates across the shared edge.

    Fields are reproduced straight from the ``struct MapConnection``:

    * ``direction``: one of ``C.CONNECTION_{SOUTH,NORTH,WEST,EAST,DIVE,EMERGE}``.
    * ``offset``: signed shift of the connected map's origin along the
      shared axis (tiles). Only meaningful for the four cardinal
      connections; DIVE/EMERGE ignore it.
    * ``dest_map_group`` / ``dest_map_num``: the connected map's key.
    """

    direction: int
    offset: int
    dest_map_group: int
    dest_map_num: int


@dataclasses.dataclass(frozen=True)
class TileInfo:
    """Decoded metatile cell in the player's visible window.

    ``x`` / ``y`` are SaveBlock1 / logical coords. ``behavior`` is the
    9-bit metatile behavior byte (compare to ``MetatileBehavior``).
    Surfaced for probing -- the planner normally goes through
    ``Interactable`` entries instead.
    """

    x: int
    y: int
    metatile_id: int
    behavior: int
    collision: int
    elevation: int


@dataclasses.dataclass(frozen=True)
class Interactable:
    """A thing the agent can interact with in the visible screen area.

    Coordinates are in SaveBlock1 / logical tile space -- the same frame as
    ``PlayerState.x/y``. The ``kind`` field is the stable machine-readable
    tag; ``label`` is a short human string that the planner / LLM layer
    can show.
    """

    x: int
    y: int
    kind: str
    label: str
    details: dict  # kind-specific extras (graphics_id, dest map, script ptr, ...)


# -----------------------------------------------------------------------------
# RamReader
# -----------------------------------------------------------------------------


class RamReader:
    """Typed accessors over a MemoryBackend.

    Species and move name lookup tables are read from ROM once on first
    access and cached for the lifetime of the reader. Everything else is
    read fresh on every call.
    """

    def __init__(self, backend: MemoryBackend) -> None:
        self._backend = backend
        self._species_names: dict[int, str] | None = None
        self._move_names: dict[int, str] | None = None
        # --- Per-map caches. Invalidated on map change via _check_map_cache.
        # The agent tick loop only pays the per-map HTTP cost once per visit.
        self._cached_map_key: tuple[int, int] | None = None
        self._cached_grid: tuple[int, int, bytes] | None = None
        self._cached_map_events: MapEventData | None = None
        self._cached_map_connections: list[MapConnection] | None = None
        self._cached_tileset_ptrs: tuple[int, int] | None = None
        # --- Behavior cache, keyed by metatile_id, valid only while the
        # active tilesets stay the same. Metatile attribute *bytes* are ROM
        # constants, but the id->behavior mapping is per-tileset: id 0x015
        # might be POND_WATER on a route's tileset and a plain wood plank
        # on a PokeCenter's. ``_check_map_cache`` watches the tileset
        # attribute base pointers and wipes this cache whenever they change,
        # so a stale "water" entry from the previous map can't poison the
        # next map's tiles.
        self._behavior_cache: dict[int, int] = {}
        # Snapshot of the tileset bases ``_behavior_cache`` was populated
        # against. Used by ``_check_map_cache`` to detect tileset changes
        # without doing a fresh ROM read every tick.
        self._behavior_cache_tilesets: tuple[int, int] | None = None

    # --- Primitive reads ----------------------------------------------------

    def read_u8(self, addr: int) -> int:
        return self._backend.read_bytes(addr, 1)[0]

    def read_u16(self, addr: int) -> int:
        return struct.unpack_from("<H", self._backend.read_bytes(addr, 2))[0]

    def read_u32(self, addr: int) -> int:
        return struct.unpack_from("<I", self._backend.read_bytes(addr, 4))[0]

    def read_s8(self, addr: int) -> int:
        return struct.unpack_from("<b", self._backend.read_bytes(addr, 1))[0]

    def read_s16(self, addr: int) -> int:
        return struct.unpack_from("<h", self._backend.read_bytes(addr, 2))[0]

    def read_s32(self, addr: int) -> int:
        return struct.unpack_from("<i", self._backend.read_bytes(addr, 4))[0]

    def read_bytes(self, addr: int, length: int) -> bytes:
        return self._backend.read_bytes(addr, length)

    # --- Text decoding & name lookups ---------------------------------------

    @staticmethod
    def decode_text(raw: bytes) -> str:
        """Public wrapper around the FireRed charset decoder."""
        return _decode_game_text(raw)

    def _ensure_species_names(self) -> dict[int, str]:
        if self._species_names is None:
            blob = self._backend.read_bytes(
                C.ADDR_GSPECIES_NAMES,
                C.NUM_SPECIES * C.SPECIES_NAME_LENGTH,
            )
            table: dict[int, str] = {}
            for sid in range(C.NUM_SPECIES):
                off = sid * C.SPECIES_NAME_LENGTH
                raw = blob[off : off + C.SPECIES_NAME_LENGTH]
                table[sid] = _decode_game_text(raw)
            self._species_names = table
        return self._species_names

    def _ensure_move_names(self) -> dict[int, str]:
        if self._move_names is None:
            blob = self._backend.read_bytes(
                C.ADDR_GMOVE_NAMES,
                C.MOVES_COUNT * C.MOVE_NAME_LENGTH,
            )
            table: dict[int, str] = {}
            for mid in range(C.MOVES_COUNT):
                off = mid * C.MOVE_NAME_LENGTH
                raw = blob[off : off + C.MOVE_NAME_LENGTH]
                table[mid] = _decode_game_text(raw)
            self._move_names = table
        return self._move_names

    def species_name(self, species_id: int) -> str:
        """e.g. 4 -> 'CHARMANDER', 0 -> '?????'."""
        return self._ensure_species_names().get(species_id, f"#{species_id}")

    def move_name(self, move_id: int) -> str:
        """e.g. 10 -> 'SCRATCH', 0 -> '-'."""
        if move_id == 0:
            return "-"
        return self._ensure_move_names().get(move_id, f"move#{move_id}")

    # --- SaveBlock pointer resolution --------------------------------------

    def _save_block1_base(self) -> int:
        ptr = self.read_u32(C.ADDR_SAVEBLOCK1_PTR)
        if not C.is_plausible_ewram_ptr(ptr):
            raise RuntimeError(
                f"gSaveBlock1Ptr does not look like an EWRAM address: {ptr:#010x}. "
                "The game may not have initialised yet, or the ROM revision differs."
            )
        return ptr

    def _save_block2_base(self) -> int:
        ptr = self.read_u32(C.ADDR_SAVEBLOCK2_PTR)
        if not C.is_plausible_ewram_ptr(ptr):
            raise RuntimeError(
                f"gSaveBlock2Ptr does not look like an EWRAM address: {ptr:#010x}."
            )
        return ptr

    # --- Player / SaveBlock1 ------------------------------------------------

    def read_player_state(self) -> PlayerState:
        """Pull position, current map, and party count in one pass.

        Most of this comes from a small slice of SaveBlock1. Facing comes
        from the player's slot in gObjectEvents (SaveBlock1 does not store
        facing outside of object events). Party count comes from the *live*
        gPlayerParty rather than SaveBlock1's persisted counter, which only
        updates on save.
        """
        base = self._save_block1_base()
        # SaveBlock1 positional / map fields live in the first 0x34 bytes.
        # The party-count byte at +0x34 is stale outside of saves -- skip it.
        blob = self._backend.read_bytes(base, 0x34)
        x, y = struct.unpack_from("<hh", blob, C.SAVEBLOCK1_POS_X)
        map_group = blob[C.SAVEBLOCK1_LOCATION_MAP_GROUP]
        map_num = blob[C.SAVEBLOCK1_LOCATION_MAP_NUM]
        map_layout_id = struct.unpack_from("<H", blob, C.SAVEBLOCK1_MAP_LAYOUT_ID)[0]

        # read_player_state is called every tick; it's the single point where
        # the reader reliably knows the current map, so it's the right place
        # to drop per-map caches when the player crosses a boundary.
        self._check_map_cache(map_group, map_num)

        facing = self._player_facing_from_object_events()
        party_count = self._live_party_count()

        return PlayerState(
            x=x,
            y=y,
            map_group=map_group,
            map_num=map_num,
            map_layout_id=map_layout_id,
            party_count=party_count,
            facing=facing,
        )

    def _live_party_count(self) -> int:
        """Count gPlayerParty slots up to the first empty one.

        A slot is "empty" when its BoxPokemon flags byte (offset 0x13) is
        zero -- real Pokemon have at least the ``hasSpecies`` bit set. We
        read only the six flag bytes, one per slot, rather than the full
        600-byte party.
        """
        count = 0
        for i in range(C.PARTY_SIZE):
            flags_addr = (
                C.ADDR_GPLAYER_PARTY
                + i * C.POKEMON_SIZE
                + C.POKEMON_OFFSET_BOX_FLAGS
            )
            if self.read_u8(flags_addr) == 0:
                break
            count += 1
        return count

    def _player_facing_from_object_events(self) -> C.Facing:
        direction_byte_addr = (
            C.ADDR_GOBJECT_EVENTS + 0 * C.OBJECT_EVENT_SIZE + C.OE_OFFSET_DIRECTION_BYTE
        )
        dir_byte = self.read_u8(direction_byte_addr)
        facing_val = dir_byte & 0x0F
        try:
            return C.Facing(facing_val)
        except ValueError:
            return C.Facing.NONE

    # --- Object events ------------------------------------------------------

    def read_object_events(self, include_inactive: bool = False) -> list[ObjectEventSlot]:
        """Return all 16 object-event slots (the player is always slot 0).

        With ``include_inactive=False`` (default), empty slots are filtered
        out -- which saves the caller a branch and matches what the
        pathfinder / event detector actually care about.
        """
        blob = self._backend.read_bytes(
            C.ADDR_GOBJECT_EVENTS, C.OBJECT_EVENTS_COUNT * C.OBJECT_EVENT_SIZE
        )
        out: list[ObjectEventSlot] = []
        for i in range(C.OBJECT_EVENTS_COUNT):
            base = i * C.OBJECT_EVENT_SIZE
            flags0 = blob[base + C.OE_OFFSET_FLAGS_BYTE_0]
            active = bool(flags0 & C.OE_FLAG0_ACTIVE)
            if not active and not include_inactive:
                continue
            flags1 = blob[base + C.OE_OFFSET_FLAGS_BYTE_1]
            flags2 = blob[base + C.OE_OFFSET_FLAGS_BYTE_2]
            dir_byte = blob[base + C.OE_OFFSET_DIRECTION_BYTE]
            cx, cy = struct.unpack_from("<hh", blob, base + C.OE_OFFSET_CURRENT_X)
            px, py = struct.unpack_from("<hh", blob, base + C.OE_OFFSET_PREVIOUS_X)
            try:
                facing = C.Facing(dir_byte & 0x0F)
            except ValueError:
                facing = C.Facing.NONE
            out.append(
                ObjectEventSlot(
                    slot=i,
                    active=active,
                    is_player=bool(flags2 & C.OE_FLAG2_IS_PLAYER),
                    invisible=bool(flags1 & C.OE_FLAG1_INVISIBLE),
                    off_screen=bool(flags1 & C.OE_FLAG1_OFF_SCREEN),
                    graphics_id=blob[base + C.OE_OFFSET_GRAPHICS_ID],
                    movement_type=blob[base + C.OE_OFFSET_MOVEMENT_TYPE],
                    trainer_type=blob[base + C.OE_OFFSET_TRAINER_TYPE],
                    local_id=blob[base + C.OE_OFFSET_LOCAL_ID],
                    map_num=blob[base + C.OE_OFFSET_MAP_NUM],
                    map_group=blob[base + C.OE_OFFSET_MAP_GROUP],
                    x=cx - C.MAP_OFFSET,
                    y=cy - C.MAP_OFFSET,
                    previous_x=px,
                    previous_y=py,
                    facing=facing,
                    current_metatile_behavior=blob[
                        base + C.OE_OFFSET_CURRENT_METATILE_BEHAVIOR
                    ],
                )
            )
        return out

    # --- Party --------------------------------------------------------------

    def read_party(self) -> list[PartyMember]:
        """Return the live party, read from gPlayerParty.

        Walks consecutive slots in ``gPlayerParty`` (0x02024284) and stops
        at the first slot whose BoxPokemon flags byte is zero -- that is
        the "end of party" sentinel the game uses in place of a dedicated
        live party-count byte (SaveBlock1's counter is only accurate
        immediately after a save). A 600-byte bulk read covers all six
        slots in one backend round trip; we bail out of parsing early.
        """
        blob = self._backend.read_bytes(
            C.ADDR_GPLAYER_PARTY, C.PARTY_SIZE * C.POKEMON_SIZE
        )
        out: list[PartyMember] = []
        for i in range(C.PARTY_SIZE):
            off = i * C.POKEMON_SIZE
            box_flags = blob[off + C.POKEMON_OFFSET_BOX_FLAGS]
            if box_flags == 0:
                break
            personality = struct.unpack_from("<I", blob, off + C.POKEMON_OFFSET_PERSONALITY)[0]
            ot_id = struct.unpack_from("<I", blob, off + C.POKEMON_OFFSET_OT_ID)[0]
            nickname_raw = blob[off + C.POKEMON_OFFSET_NICKNAME : off + C.POKEMON_OFFSET_NICKNAME + 10]
            nickname = _decode_game_text(nickname_raw)
            status = struct.unpack_from("<I", blob, off + C.POKEMON_OFFSET_STATUS)[0]
            level = blob[off + C.POKEMON_OFFSET_LEVEL]
            hp = struct.unpack_from("<H", blob, off + C.POKEMON_OFFSET_HP)[0]
            max_hp = struct.unpack_from("<H", blob, off + C.POKEMON_OFFSET_MAX_HP)[0]
            attack = struct.unpack_from("<H", blob, off + C.POKEMON_OFFSET_ATTACK)[0]
            defense = struct.unpack_from("<H", blob, off + C.POKEMON_OFFSET_DEFENSE)[0]
            speed = struct.unpack_from("<H", blob, off + C.POKEMON_OFFSET_SPEED)[0]
            sp_attack = struct.unpack_from("<H", blob, off + C.POKEMON_OFFSET_SP_ATTACK)[0]
            sp_defense = struct.unpack_from("<H", blob, off + C.POKEMON_OFFSET_SP_DEFENSE)[0]
            out.append(
                PartyMember(
                    slot=i,
                    personality=personality,
                    ot_id=ot_id,
                    nickname=nickname,
                    level=level,
                    status=status,
                    status_text=C.decode_status1(status),
                    hp=hp,
                    max_hp=max_hp,
                    attack=attack,
                    defense=defense,
                    speed=speed,
                    sp_attack=sp_attack,
                    sp_defense=sp_defense,
                    is_egg=bool(box_flags & (1 << 2)),
                )
            )
        return out

    # --- OT ID check -------------------------------------------------------

    def is_own_pokemon(self, party_member: PartyMember) -> bool:
        """True if this party member was caught by the current player."""
        sb2 = self._save_block2_base()
        player_tid = self.read_u32(sb2 + C.SAVEBLOCK2_PLAYER_TRAINER_ID)
        return party_member.ot_id == player_tid

    # --- Game mode ----------------------------------------------------------

    def read_game_mode(self) -> C.GameMode:
        """Coarse classification via a handful of RAM tells.

        The sole battle indicator is ``gMain.inBattle`` (offset 0x439
        bit 1). The engine sets it TRUE on battle entry and FALSE on exit.
        ``gBattleTypeFlags`` is *not* used here -- it classifies the battle
        type (wild, trainer, double, ...) but is never explicitly zeroed
        when battle ends, so it would stick as a false positive.

        Outside of battle, ``sLockFieldControls`` (the static bool8 read by
        ``ArePlayerFieldControlsLocked()``) is the canonical "is player
        input currently locked" flag -- set by dialogue, scripts, warps,
        battle setup, etc. This matches the check the overworld callback
        itself uses, so it mirrors in-game movement-locked semantics.
        ``gPlayerAvatar.preventStep`` is *not* used here: it only tracks
        mid-field-effect animations (surfing entry, teleport, etc.) and
        does not toggle for ordinary dialogue.
        """
        try:
            flags_byte = self.read_u8(C.ADDR_GMAIN + C.GMAIN_OFFSET_FLAGS_BYTE)
        except Exception:
            flags_byte = 0

        if flags_byte & C.GMAIN_FLAG_IN_BATTLE:
            return C.GameMode.BATTLE

        try:
            if self.read_u8(C.ADDR_SLOCK_FIELD_CONTROLS):
                return C.GameMode.DIALOGUE
        except Exception:
            pass

        try:
            self._save_block1_base()
            return C.GameMode.OVERWORLD
        except Exception:
            return C.GameMode.UNKNOWN

    # --- Battle state -------------------------------------------------------

    def _parse_battle_pokemon(self, blob: bytes, offset: int) -> BattlePokemonState:
        """Parse one BattlePokemon struct from a bulk read of gBattleMons."""
        species = struct.unpack_from("<H", blob, offset + C.BPOKE_OFFSET_SPECIES)[0]
        attack = struct.unpack_from("<H", blob, offset + C.BPOKE_OFFSET_ATTACK)[0]
        defense = struct.unpack_from("<H", blob, offset + C.BPOKE_OFFSET_DEFENSE)[0]
        speed = struct.unpack_from("<H", blob, offset + C.BPOKE_OFFSET_SPEED)[0]
        sp_attack = struct.unpack_from("<H", blob, offset + C.BPOKE_OFFSET_SP_ATTACK)[0]
        sp_defense = struct.unpack_from("<H", blob, offset + C.BPOKE_OFFSET_SP_DEFENSE)[0]
        moves = tuple(
            struct.unpack_from("<H", blob, offset + C.BPOKE_OFFSET_MOVES + j * 2)[0]
            for j in range(C.MAX_MON_MOVES)
        )
        pp = tuple(blob[offset + C.BPOKE_OFFSET_PP + j] for j in range(C.MAX_MON_MOVES))
        hp = struct.unpack_from("<H", blob, offset + C.BPOKE_OFFSET_HP)[0]
        level = blob[offset + C.BPOKE_OFFSET_LEVEL]
        max_hp = struct.unpack_from("<H", blob, offset + C.BPOKE_OFFSET_MAX_HP)[0]
        status1 = struct.unpack_from("<I", blob, offset + C.BPOKE_OFFSET_STATUS1)[0]
        nick_raw = blob[offset + C.BPOKE_OFFSET_NICKNAME : offset + C.BPOKE_OFFSET_NICKNAME + C.SPECIES_NAME_LENGTH]
        return BattlePokemonState(
            species_id=species,
            species_name=self.species_name(species),
            nickname=_decode_game_text(nick_raw),
            level=level,
            hp=hp,
            max_hp=max_hp,
            attack=attack,
            defense=defense,
            speed=speed,
            sp_attack=sp_attack,
            sp_defense=sp_defense,
            moves=moves,
            move_names=tuple(self.move_name(m) for m in moves),
            pp=pp,
            status_text=C.decode_status1(status1),
            raw_status1=status1,
        )

    def _count_alive(self, party_addr: int) -> int:
        """Count party members with hasSpecies set AND hp > 0."""
        blob = self._backend.read_bytes(party_addr, C.PARTY_SIZE * C.POKEMON_SIZE)
        alive = 0
        for i in range(C.PARTY_SIZE):
            off = i * C.POKEMON_SIZE
            if blob[off + C.POKEMON_OFFSET_BOX_FLAGS] == 0:
                break
            hp = struct.unpack_from("<H", blob, off + C.POKEMON_OFFSET_HP)[0]
            if hp > 0:
                alive += 1
        return alive

    def read_battle_state(self) -> BattleState:
        """Read the current battle state from gBattleMons and friends.

        Returns ``BattleState(active=False, ...)`` with zeroed fields
        when not in battle. All battle-specific EWRAM is garbage outside
        of battle, so we gate on ``gMain.inBattle``.
        """
        try:
            flags_byte = self.read_u8(C.ADDR_GMAIN + C.GMAIN_OFFSET_FLAGS_BYTE)
        except Exception:
            flags_byte = 0

        if not (flags_byte & C.GMAIN_FLAG_IN_BATTLE):
            return BattleState(
                active=False, battlers_count=0,
                player=None, enemy=None,
                player_party_index=0, enemy_party_index=0,
                player_party_alive=0, enemy_party_alive=0,
                outcome=0, outcome_text="n/a",
                weather=0, weather_text="none",
                current_move=0, current_move_name="-",
                is_wild=False, is_trainer=False, is_double=False,
                is_catchable=False,
                battle_type_flags=0, trainer_opponent_id=0,
            )

        battlers_count = self.read_u8(C.ADDR_GBATTLERS_COUNT)
        party_indexes = self._backend.read_bytes(C.ADDR_GBATTLER_PARTY_INDEXES, 4)

        # Bulk-read all battler slots.
        mons_blob = self._backend.read_bytes(
            C.ADDR_GBATTLE_MONS, C.MAX_BATTLERS_COUNT * C.BATTLE_POKEMON_SIZE
        )
        player = self._parse_battle_pokemon(mons_blob, 0 * C.BATTLE_POKEMON_SIZE)
        enemy = self._parse_battle_pokemon(mons_blob, 1 * C.BATTLE_POKEMON_SIZE)

        outcome_raw = self.read_u8(C.ADDR_GBATTLE_OUTCOME)
        try:
            outcome_text = C.BattleOutcome(outcome_raw).name.lower()
        except ValueError:
            outcome_text = f"unknown({outcome_raw})"

        weather = self.read_u16(C.ADDR_GBATTLE_WEATHER)
        current_move = self.read_u16(C.ADDR_GCURRENT_MOVE)

        # Trainer vs wild classification.
        battle_type_flags = self.read_u32(C.ADDR_GBATTLE_TYPE_FLAGS)
        is_trainer = bool(battle_type_flags & C.BATTLE_TYPE_TRAINER)
        is_double = bool(battle_type_flags & C.BATTLE_TYPE_DOUBLE)
        # "wild" is the absence of the trainer bit; the flags word is never
        # zero during an active fight (BATTLE_TYPE_IS_MASTER is always set in
        # non-link battles), so we don't need to guard on flags != 0.
        is_wild = not is_trainer
        is_catchable = is_wild and not (
            battle_type_flags & C.BATTLE_TYPE_UNCATCHABLE_MASK
        )
        trainer_opponent_id = (
            self.read_u16(C.ADDR_GTRAINER_BATTLE_OPPONENT_A) if is_trainer else 0
        )

        return BattleState(
            active=True,
            battlers_count=battlers_count,
            player=player,
            enemy=enemy,
            player_party_index=party_indexes[0],
            enemy_party_index=party_indexes[1],
            player_party_alive=self._count_alive(C.ADDR_GPLAYER_PARTY),
            enemy_party_alive=self._count_alive(C.ADDR_GENEMY_PARTY),
            outcome=outcome_raw,
            outcome_text=outcome_text,
            weather=weather,
            weather_text=C.decode_weather(weather),
            current_move=current_move,
            current_move_name=self.move_name(current_move),
            is_wild=is_wild,
            is_trainer=is_trainer,
            is_double=is_double,
            is_catchable=is_catchable,
            battle_type_flags=battle_type_flags,
            trainer_opponent_id=trainer_opponent_id,
        )

    # --- Dialogue / text ----------------------------------------------------

    def read_dialogue_text(self) -> str | None:
        """Read current message box text. Returns None if no text is displayed."""
        raw = self._backend.read_bytes(C.ADDR_GSTRING_VAR4, 256)
        if raw[0] == 0xFF or raw[0] == 0x00:
            return None
        return _decode_game_text(raw)

    def is_movement_locked(self) -> bool:
        """True when dialogue, cutscene, or script prevents player movement.

        Reads ``sLockFieldControls`` -- the static bool8 behind
        ``ArePlayerFieldControlsLocked()`` in pret/src/script.c. This is the
        exact flag the overworld callback checks before accepting input.
        """
        return self.read_u8(C.ADDR_SLOCK_FIELD_CONTROLS) != 0

    def is_field_message_visible(self) -> bool:
        """True when a field dialogue box is currently drawn.

        Reads ``sMessageBoxType`` (pret/src/field_message_box.c). Unlike
        ``is_movement_locked``, this only fires while text is actually on
        screen -- not during non-dialogue script steps or cutscene beats.
        """
        return self.read_u8(C.ADDR_SMESSAGE_BOX_TYPE) != C.FIELD_MESSAGE_BOX_HIDDEN

    def read_last_talked_npc(self) -> int:
        """Local ID of the last NPC the player interacted with."""
        return self.read_u16(C.ADDR_GSPECIALVAR_LAST_TALKED)

    # --- Tiles --------------------------------------------------------------

    def read_map_dimensions(self) -> tuple[int, int]:
        """Width/height of the live map grid (inclusive of the 7-tile border).

        Pulls from VMap -- the BackupMapLayout at ADDR_VMAP -- which the
        game itself consults. Width is ``mapLayout->width + MAP_OFFSET_W``
        and height is ``mapLayout->height + MAP_OFFSET_H``.
        """
        xsize = self.read_s32(C.ADDR_VMAP + 0x00)
        ysize = self.read_s32(C.ADDR_VMAP + 0x04)
        return xsize, ysize

    def read_map_grid(self) -> tuple[int, int, bytes]:
        """Return ``(xsize, ysize, raw_bytes)`` for the current map grid.

        ``raw_bytes`` is ``xsize * ysize * 2`` bytes of u16 cells. Each u16
        is ``[elevation:4][collision:2][metatileId:10]``. Decoding a
        *behaviour* from a metatile id additionally requires walking the
        tileset's metatileAttributes table in ROM, which we leave to a
        later layer.

        The underlying buffer is ``gBackupMapData`` whose address we take
        via VMap->map -- that keeps the reader correct even if the game
        relocates the backing buffer.
        """
        xsize, ysize = self.read_map_dimensions()
        map_ptr = self.read_u32(C.ADDR_VMAP + 0x08)
        if not (C.is_plausible_ewram_ptr(map_ptr) or C.is_plausible_iwram_ptr(map_ptr)):
            raise RuntimeError(
                f"VMap.map pointer is not in EWRAM/IWRAM: {map_ptr:#010x}."
            )
        size = xsize * ysize * 2
        raw = self._backend.read_bytes(map_ptr, size)
        return xsize, ysize, raw

    def tile_cell_at(
        self, grid: bytes, xsize: int, x: int, y: int
    ) -> tuple[int, int, int]:
        """Decode a single grid cell from a ``read_map_grid`` result.

        ``x`` / ``y`` are *raw grid* coordinates (include the 7-tile
        border). Use ``player_state.x + MAP_OFFSET`` and
        ``player_state.y + MAP_OFFSET`` if you have a SaveBlock1-relative
        position.

        Returns ``(metatile_id, collision, elevation)``.
        """
        idx = (y * xsize + x) * 2
        cell = grid[idx] | (grid[idx + 1] << 8)
        metatile_id = cell & C.MAPGRID_METATILE_ID_MASK
        collision = (cell & C.MAPGRID_COLLISION_MASK) >> C.MAPGRID_COLLISION_SHIFT
        elevation = (cell & C.MAPGRID_ELEVATION_MASK) >> C.MAPGRID_ELEVATION_SHIFT
        return metatile_id, collision, elevation

    # --- Map events (warps / signs / hidden items) --------------------------

    def _map_key(self) -> tuple[int, int]:
        """(map_group, map_num) for the active map -- per-map cache key."""
        sb1 = self._save_block1_base()
        return (
            self.read_u8(sb1 + C.SAVEBLOCK1_LOCATION_MAP_GROUP),
            self.read_u8(sb1 + C.SAVEBLOCK1_LOCATION_MAP_NUM),
        )

    def _map_header_ptr(self) -> int:
        """Pointer to the MapHeader's mapLayout / events fields (gMapHeader itself).

        gMapHeader is a struct, not a pointer, so the "pointer" returned
        here is just its address. Kept as a helper so the calling code
        reads the same regardless of whether the engine later swaps gMapHeader
        for a pointer-to-struct in a refactor.
        """
        return C.ADDR_GMAP_HEADER

    def _read_rom_ptr(self, addr: int) -> int:
        """Read a 4-byte pointer and sanity-check it lives in ROM."""
        ptr = self.read_u32(addr)
        if not C.is_plausible_rom_ptr(ptr):
            raise RuntimeError(
                f"pointer at {addr:#010x} is not a ROM address: {ptr:#010x}"
            )
        return ptr

    def read_map_events(self) -> MapEventData:
        """Follow gMapHeader -> events -> {warps, bg events} and return them.

        Reads a bulk slab for each array rather than one struct at a time
        -- map events are cold ROM data, and mgba-http latency dominates
        per-request cost. Coordinate space is SaveBlock1 / logical tiles
        (the same space the player x/y lives in) -- confirmed by how
        WarpSetToLastHealLocation stores the raw s16 x/y straight from the
        struct into SaveBlock1.pos without adding MAP_OFFSET.
        """
        events_ptr = self._read_rom_ptr(
            self._map_header_ptr() + C.MAP_HEADER_OFFSET_EVENTS
        )
        hdr = self._backend.read_bytes(events_ptr, 0x14)
        oe_count = hdr[C.MAP_EVENTS_OFFSET_OBJECT_EVENT_COUNT]
        warp_count = hdr[C.MAP_EVENTS_OFFSET_WARP_COUNT]
        bg_count = hdr[C.MAP_EVENTS_OFFSET_BG_EVENT_COUNT]

        warps_ptr = struct.unpack_from(
            "<I", hdr, C.MAP_EVENTS_OFFSET_WARPS_PTR
        )[0]
        bg_ptr = struct.unpack_from(
            "<I", hdr, C.MAP_EVENTS_OFFSET_BG_EVENTS_PTR
        )[0]

        warps: list[WarpInfo] = []
        if warp_count and C.is_plausible_rom_ptr(warps_ptr):
            blob = self._backend.read_bytes(warps_ptr, warp_count * C.WARP_EVENT_SIZE)
            for i in range(warp_count):
                off = i * C.WARP_EVENT_SIZE
                x = struct.unpack_from("<h", blob, off + C.WARP_EVENT_OFFSET_X)[0]
                y = struct.unpack_from("<h", blob, off + C.WARP_EVENT_OFFSET_Y)[0]
                warps.append(
                    WarpInfo(
                        x=x,
                        y=y,
                        dest_map_group=blob[off + C.WARP_EVENT_OFFSET_MAP_GROUP],
                        dest_map_num=blob[off + C.WARP_EVENT_OFFSET_MAP_NUM],
                        dest_warp_id=blob[off + C.WARP_EVENT_OFFSET_WARP_ID],
                    )
                )

        bg_events: list[BgEventInfo] = []
        if bg_count and C.is_plausible_rom_ptr(bg_ptr):
            blob = self._backend.read_bytes(bg_ptr, bg_count * C.BG_EVENT_SIZE)
            for i in range(bg_count):
                off = i * C.BG_EVENT_SIZE
                x = struct.unpack_from("<H", blob, off + C.BG_EVENT_OFFSET_X)[0]
                y = struct.unpack_from("<H", blob, off + C.BG_EVENT_OFFSET_Y)[0]
                elevation = blob[off + C.BG_EVENT_OFFSET_ELEVATION]
                kind_raw = blob[off + C.BG_EVENT_OFFSET_KIND]
                union = struct.unpack_from("<I", blob, off + C.BG_EVENT_OFFSET_UNION)[0]
                bg_events.append(
                    BgEventInfo(
                        x=x,
                        y=y,
                        kind=_classify_bg_event(kind_raw),
                        kind_raw=kind_raw,
                        elevation=elevation,
                        data=union,
                    )
                )

        return MapEventData(
            warps=warps,
            bg_events=bg_events,
            object_event_count=oe_count,
        )

    def read_map_connections(self) -> list[MapConnection]:
        """Follow gMapHeader -> connections and return the raw table.

        FRLG uses this for seamless map-to-map transitions: walking off
        the edge of Route 1 puts the player into Viridian City with no
        warp event in play. The agent needs to see these edges so maps
        don't look like dead ends, and cross-map pathfinding has to
        know about them when planning multi-map routes.

        Bulk-reads the whole array in one HTTP call -- the table is
        always small (0-4 entries in practice). Returns an empty list
        for maps with no connections or a null connections pointer,
        both of which are common for indoor maps.
        """
        connections_ptr = struct.unpack_from(
            "<I",
            self._backend.read_bytes(
                self._map_header_ptr() + C.MAP_HEADER_OFFSET_CONNECTIONS, 4
            ),
        )[0]
        if connections_ptr == 0 or not C.is_plausible_rom_ptr(connections_ptr):
            return []

        hdr = self._backend.read_bytes(connections_ptr, 0x08)
        count = struct.unpack_from("<i", hdr, C.MAP_CONNECTIONS_OFFSET_COUNT)[0]
        conns_ptr = struct.unpack_from(
            "<I", hdr, C.MAP_CONNECTIONS_OFFSET_CONNS_PTR
        )[0]
        if count <= 0 or not C.is_plausible_rom_ptr(conns_ptr):
            return []

        blob = self._backend.read_bytes(
            conns_ptr, count * C.MAP_CONNECTION_SIZE
        )
        out: list[MapConnection] = []
        for i in range(count):
            off = i * C.MAP_CONNECTION_SIZE
            direction = blob[off + C.MAP_CONNECTION_OFFSET_DIRECTION]
            offset = struct.unpack_from(
                "<i", blob, off + C.MAP_CONNECTION_OFFSET_OFFSET
            )[0]
            dest_group = blob[off + C.MAP_CONNECTION_OFFSET_MAP_GROUP]
            dest_num = blob[off + C.MAP_CONNECTION_OFFSET_MAP_NUM]
            out.append(
                MapConnection(
                    direction=direction,
                    offset=offset,
                    dest_map_group=dest_group,
                    dest_map_num=dest_num,
                )
            )
        return out

    # --- Tileset attributes / metatile behaviour ----------------------------

    def read_tileset_attrs(self) -> tuple[int, int]:
        """Return (primary_attrs_ptr, secondary_attrs_ptr) for the current map.

        Both pointers address ROM u32 arrays indexed by (local) metatile
        id. Cached per map visit via ``_cached_tileset_ptrs``; the cache
        is cleared by ``_check_map_cache`` when the player crosses a map
        boundary.

        Side-effect: if these bases differ from ``_behavior_cache_tilesets``
        (the tilesets the current behavior cache was populated against),
        the behavior cache is wiped first. The cache is keyed on
        ``metatile_id`` alone but its values are per-tileset, so a stale
        entry from a route's tileset cannot be allowed to leak into a
        PokeCenter's id->behavior decode -- that's exactly what made
        non-water tiles render as water in the map viewer.
        """
        if self._cached_tileset_ptrs is not None:
            return self._cached_tileset_ptrs

        map_layout_ptr = self._read_rom_ptr(
            self._map_header_ptr() + C.MAP_HEADER_OFFSET_MAP_LAYOUT
        )
        primary_tileset_ptr = self._read_rom_ptr(
            map_layout_ptr + C.MAP_LAYOUT_OFFSET_PRIMARY_TILESET
        )
        secondary_tileset_ptr = self._read_rom_ptr(
            map_layout_ptr + C.MAP_LAYOUT_OFFSET_SECONDARY_TILESET
        )
        primary_attrs = self._read_rom_ptr(
            primary_tileset_ptr + C.TILESET_OFFSET_METATILE_ATTRS
        )
        secondary_attrs = self._read_rom_ptr(
            secondary_tileset_ptr + C.TILESET_OFFSET_METATILE_ATTRS
        )
        new_ptrs = (primary_attrs, secondary_attrs)

        if self._behavior_cache_tilesets != new_ptrs:
            # Either tileset (or both) just changed underneath us. Drop
            # everything keyed on the old tilesets before any caller can
            # observe a stale entry.
            self._behavior_cache.clear()
            self._behavior_cache_tilesets = new_ptrs

        self._cached_tileset_ptrs = new_ptrs
        return self._cached_tileset_ptrs

    def get_metatile_behavior(self, metatile_id: int) -> int:
        """Look up the 9-bit MetatileBehavior for a metatile id on this map.

        Metatile ids 0x000-0x1FF index the primary tileset's attribute array;
        0x200+ index the secondary tileset's (with 0x200 subtracted). Returns
        the raw integer; compare against C.MetatileBehavior members.

        Cached in ``_behavior_cache``. That cache is permanent across
        map transitions since it holds ROM data; per-map prefetch on
        entry overwrites entries for ids actually used by the new map.
        """
        cached = self._behavior_cache.get(metatile_id)
        if cached is not None:
            return cached

        primary_attrs, secondary_attrs = self.read_tileset_attrs()
        if metatile_id < C.NUM_METATILES_IN_PRIMARY:
            attr_addr = primary_attrs + metatile_id * 4
        else:
            attr_addr = secondary_attrs + (metatile_id - C.NUM_METATILES_IN_PRIMARY) * 4
        attr = self.read_u32(attr_addr)
        behavior = (attr & C.METATILE_ATTR_BEHAVIOR_MASK) >> C.METATILE_ATTR_BEHAVIOR_SHIFT
        self._behavior_cache[metatile_id] = behavior
        return behavior

    def prefetch_metatile_behaviors(self, metatile_ids: Iterable[int]) -> None:
        """Bulk-read behaviors for a specific set of metatile ids.

        ``get_metatile_behavior`` on its own issues one ROM read per
        unique metatile -- fine for a handful, death for a 15x10
        window where mgba-http latency dominates. We collect the
        unique ids requested, split by tileset, and issue *one* bulk
        read per tileset covering exactly the [min_id, max_id] range.

        Called at map entry from ``read_map_grid_cached`` with every
        metatile id the map actually uses, so post-entry behavior
        lookups are all cache hits. Ids already in ``_behavior_cache``
        are skipped.
        """
        primary_needed: list[int] = []
        secondary_needed: list[int] = []
        for mid in metatile_ids:
            if mid in self._behavior_cache:
                continue
            if mid < C.NUM_METATILES_IN_PRIMARY:
                primary_needed.append(mid)
            else:
                secondary_needed.append(mid)
        if not primary_needed and not secondary_needed:
            return

        primary_attrs, secondary_attrs = self.read_tileset_attrs()
        mask = C.METATILE_ATTR_BEHAVIOR_MASK
        shift = C.METATILE_ATTR_BEHAVIOR_SHIFT

        def _ingest(base_addr: int, local_ids: list[int], id_offset: int) -> None:
            if not local_ids:
                return
            lo = min(local_ids)
            hi = max(local_ids)
            blob = self._backend.read_bytes(base_addr + lo * 4, (hi - lo + 1) * 4)
            for i in range(lo, hi + 1):
                attr = int.from_bytes(blob[(i - lo) * 4 : (i - lo) * 4 + 4], "little")
                self._behavior_cache[id_offset + i] = (attr & mask) >> shift

        _ingest(primary_attrs, primary_needed, 0)
        secondary_local = [mid - C.NUM_METATILES_IN_PRIMARY for mid in secondary_needed]
        _ingest(secondary_attrs, secondary_local, C.NUM_METATILES_IN_PRIMARY)

    def _check_map_cache(self, map_group: int, map_num: int) -> None:
        """Invalidate per-map caches if the player has crossed a boundary.

        Called at the top of ``read_player_state`` (the one call every
        tick that unambiguously knows the current map). Clears the
        per-map caches -- grid, events, tileset pointers -- and then
        eagerly re-reads the tileset attribute bases. ``read_tileset_attrs``
        owns behavior-cache invalidation: if the new map shares both
        tilesets with the previous one (very common -- every PokeCenter
        uses the same interior tileset, every outdoor town shares the
        regional tileset) the behavior cache survives and we save a
        full re-prefetch; if either tileset changed, ``read_tileset_attrs``
        wipes the behavior cache so no stale id->behavior entry can leak
        across the boundary. Pre-overworld / mid-transition the read can
        fail, in which case we conservatively drop the cache.
        """
        new_key = (map_group, map_num)
        if self._cached_map_key == new_key:
            return
        self._cached_map_key = new_key
        self._cached_grid = None
        self._cached_map_events = None
        self._cached_map_connections = None
        self._cached_tileset_ptrs = None

        try:
            self.read_tileset_attrs()
        except Exception:  # noqa: BLE001
            self._behavior_cache.clear()
            self._behavior_cache_tilesets = None

    def invalidate_map_cache(self) -> None:
        """Manual reset of every cache the reader owns.

        ``_check_map_cache`` handles routine invalidation on map
        change. This method is the hammer for cases where a caller
        knows ROM has been reloaded or wants a fully cold reader --
        it additionally wipes ``_behavior_cache``, which the automatic
        path keeps.
        """
        self._cached_map_key = None
        self._cached_grid = None
        self._cached_map_events = None
        self._cached_map_connections = None
        self._cached_tileset_ptrs = None
        self._behavior_cache.clear()
        self._behavior_cache_tilesets = None

    def read_map_grid_cached(self) -> tuple[int, int, bytes]:
        """Return the current map's grid, reading once per visit.

        On first call per map this also prefetches behaviors for every
        metatile id actually used by the grid -- one or two bulk ROM
        reads -- so subsequent ``get_metatile_behavior_cached`` calls
        are pure dict lookups.
        """
        if self._cached_grid is not None:
            return self._cached_grid
        self._cached_grid = self.read_map_grid()
        xsize, ysize, grid = self._cached_grid
        # Collect every metatile id the grid actually uses, then hand
        # them to the targeted prefetch. In practice a map uses a small
        # subset of the 1024 possible ids, so the two bulk reads cover
        # a tight range rather than the full 4 KiB attribute arrays.
        mask = C.MAPGRID_METATILE_ID_MASK
        unique_ids: set[int] = set()
        for i in range(0, len(grid), 2):
            cell = grid[i] | (grid[i + 1] << 8)
            unique_ids.add(cell & mask)
        self.prefetch_metatile_behaviors(unique_ids)
        return self._cached_grid

    def read_map_events_cached(self) -> MapEventData:
        """Return the current map's event tables, reading once per visit."""
        if self._cached_map_events is None:
            self._cached_map_events = self.read_map_events()
        return self._cached_map_events

    def read_map_connections_cached(self) -> list[MapConnection]:
        """Return the current map's connection table, reading once per visit."""
        if self._cached_map_connections is None:
            self._cached_map_connections = self.read_map_connections()
        return self._cached_map_connections

    def get_metatile_behavior_cached(self, metatile_id: int) -> int:
        """Pure dict lookup once the per-map grid prefetch has run.

        Falls back to a single ROM read for ids not yet cached -- e.g.
        the very first call on a map before ``read_map_grid_cached``,
        or unit tests that bypass the grid path.
        """
        cached = self._behavior_cache.get(metatile_id)
        if cached is not None:
            return cached
        return self.get_metatile_behavior(metatile_id)

    # --- Visible tiles / interactables --------------------------------------

    def read_visible_tiles(
        self,
        player_x: int | None = None,
        player_y: int | None = None,
    ) -> list[TileInfo]:
        """Decode every cell in the 15x10 visible window with its behavior.

        One map-grid bulk read plus one prefetch bulk per used tileset --
        typically three HTTP round trips total on a cold cache. Primarily
        a probe / debug aid (compare what a tile *actually* shows for
        behavior against expectations from the ROM).
        """
        if player_x is None or player_y is None:
            st = self.read_player_state()
            player_x, player_y = st.x, st.y
        min_x, max_x = player_x - 7, player_x + 7
        min_y, max_y = player_y - 5, player_y + 5

        xsize, ysize, grid = self.read_map_grid_cached()
        logical_w = max(0, xsize - C.MAP_OFFSET_W)
        logical_h = max(0, ysize - C.MAP_OFFSET_H)
        decoded: list[tuple[int, int, int, int, int]] = []
        for ly in range(min_y, max_y + 1):
            for lx in range(min_x, max_x + 1):
                # Stay inside the logical (non-border) map. The 7-tile
                # pad around each edge is map-rendering filler and its
                # collision bits are unrelated to gameplay.
                if not (0 <= lx < logical_w and 0 <= ly < logical_h):
                    continue
                gx = lx + C.MAP_OFFSET
                gy = ly + C.MAP_OFFSET
                if not (0 <= gx < xsize and 0 <= gy < ysize):
                    continue
                mid, col, ele = self.tile_cell_at(grid, xsize, gx, gy)
                decoded.append((lx, ly, mid, col, ele))

        out: list[TileInfo] = []
        for lx, ly, mid, col, ele in decoded:
            try:
                behavior = self.get_metatile_behavior_cached(mid)
            except Exception:  # noqa: BLE001
                behavior = -1
            out.append(
                TileInfo(
                    x=lx,
                    y=ly,
                    metatile_id=mid,
                    behavior=behavior,
                    collision=col,
                    elevation=ele,
                )
            )
        return out

    def read_visible_interactables(
        self,
        player_x: int | None = None,
        player_y: int | None = None,
    ) -> list[Interactable]:
        """Enumerate every interactable thing on screen right now.

        Combines four sources, all in SaveBlock1 / logical tile space:
          * Active object events (NPCs, item balls, and the FRLG
            HM-obstacle sprites -- cuttable tree, smashable rock,
            pushable boulder -- which the engine models as ObjectEvents
            rather than metatile behaviours).
          * Map-header BgEvents (signs, hidden items).
          * Map-header WarpEvents (doors, stairs, cave mouths), collapsed
            to one representative tile per destination -- FRLG models
            every door as a 2-3 tile strip where each tile points at the
            same warp.
          * Metatile behaviours that press-A triggers, like the Pokemon
            Center healing counter (COUNTER = 0x80) and in-room PC
            (PC = 0x83). These are tileset-baked, so they only surface
            when we actually scan the grid.

        The visible window is a 15x10 tile box centred on the player --
        matching the roughly-visible region on a GBA screen. Pass
        ``player_x`` / ``player_y`` to skip the SaveBlock1 re-read when
        the caller already has them.
        """
        if player_x is None or player_y is None:
            st = self.read_player_state()
            player_x, player_y = st.x, st.y
        events = self.read_map_events_cached()

        # GBA screen renders 15 tiles wide by ~10 tiles tall, but we widen
        # the vertical sweep to 11 (5 above, 5 below) so the agent picks
        # up tall interactables -- the PokeCenter PC sits at the back wall
        # and is one tile off the top of the strict 15x10 view from the
        # entrance, which made it invisible to the agent until the player
        # walked another step north. ram_probe.print_object_events uses
        # the same window.
        min_x, max_x = player_x - 7, player_x + 7
        min_y, max_y = player_y - 5, player_y + 5

        def in_window(ix: int, iy: int) -> bool:
            return min_x <= ix <= max_x and min_y <= iy <= max_y

        out: list[Interactable] = []

        # Find the counter NPC(s) on the full active object event list,
        # NOT just the visible ones. The counter strip near the NPC has
        # many COUNTER metatiles, but only the ones the player can stand
        # facing directly are reachable -- the player can't walk onto a
        # counter tile, they press A from the floor tile in front of the
        # NPC. Counter NPCs can sit on any side of the strip (Mart
        # clerks often have the counter to their east or west, gym /
        # quiz-show staff are similar), so we collect every COUNTER tile
        # 4-adjacent to a Nurse/Clerk and treat each as interactable.
        # Multiple Nurses/Clerks per map are allowed; gather their full
        # neighbour sets.
        all_slots = self.read_object_events()
        nurse_counter_xys: set[tuple[int, int]] = set()
        clerk_counter_xys: set[tuple[int, int]] = set()
        _CARDINAL_DELTAS = ((0, -1), (0, 1), (-1, 0), (1, 0))
        for slot in all_slots:
            if slot.is_player or slot.invisible:
                continue
            if slot.graphics_id == C.OBJ_EVENT_GFX_NURSE:
                target = nurse_counter_xys
            elif slot.graphics_id == C.OBJ_EVENT_GFX_CLERK:
                target = clerk_counter_xys
            else:
                continue
            for dx, dy in _CARDINAL_DELTAS:
                target.add((slot.x + dx, slot.y + dy))

        # Object events: skip the player (slot 0 / is_player) and anything
        # off screen or invisible. gObjectEvents stores coords in
        # border-included space, but read_object_events already subtracts
        # MAP_OFFSET so slot.x/slot.y are in logical space.
        for slot in all_slots:
            if slot.is_player or slot.invisible or slot.off_screen:
                continue
            lx = slot.x
            ly = slot.y
            if not in_window(lx, ly):
                continue
            kind, label = _classify_object_event(slot.graphics_id, slot.trainer_type)
            out.append(
                Interactable(
                    x=lx,
                    y=ly,
                    kind=kind,
                    label=label,
                    details={
                        "slot": slot.slot,
                        "graphics_id": slot.graphics_id,
                        "movement_type": slot.movement_type,
                        "trainer_type": slot.trainer_type,
                        "local_id": slot.local_id,
                        "facing": slot.facing.name,
                    },
                )
            )

        for bg in events.bg_events:
            if not in_window(bg.x, bg.y):
                continue
            label = {
                "sign": "Sign",
                "hidden_item": "Hidden Item",
                "secret_base": "Secret Base Entrance",
            }.get(bg.kind, f"BgEvent({bg.kind_raw})")
            out.append(
                Interactable(
                    x=bg.x,
                    y=bg.y,
                    kind=bg.kind,
                    label=label,
                    details={
                        "kind_raw": bg.kind_raw,
                        "elevation": bg.elevation,
                        # Signs stash a ROM script pointer; hidden items stash
                        # a packed u32 with the item / flag / quantity.
                        "data": bg.data,
                    },
                )
            )

        # Warps: collapse multi-tile door strips into one Interactable per
        # destination. FRLG door mats are typically 2 tiles wide, but the
        # *same* mat often has different ``warp_id`` values per tile so
        # the player arrives back on the matching outdoor tile after
        # exiting -- keying the collapse on (dest_map, warp_id) leaks
        # through as two adjacent warp markers. Group by destination MAP
        # only and then split each map-group into 4-adjacency clusters
        # so a "two staircases on the same floor both go to F2" case
        # still surfaces as two distinct warps. Each cluster reports its
        # full tile list plus a float centroid in ``details`` so renderers
        # can place a marker between the tiles rather than biased onto
        # one of them.
        warps_by_dest_map: dict[tuple[int, int], list["WarpInfo"]] = {}
        for warp in events.warps:
            if not in_window(warp.x, warp.y):
                continue
            warps_by_dest_map.setdefault(
                (warp.dest_map_group, warp.dest_map_num), []
            ).append(warp)
        for group in warps_by_dest_map.values():
            for cluster in _cluster_warps_4adjacent(group):
                cx = sum(w.x for w in cluster) / len(cluster)
                cy = sum(w.y for w in cluster) / len(cluster)
                # Canonical tile: closest to the centroid (ties broken by
                # smallest (y, x) so the choice is deterministic).
                canon = min(
                    cluster,
                    key=lambda w: ((w.x - cx) ** 2 + (w.y - cy) ** 2, w.y, w.x),
                )
                out.append(
                    Interactable(
                        x=canon.x,
                        y=canon.y,
                        kind="warp",
                        label=f"Warp -> ({canon.dest_map_group}, {canon.dest_map_num})",
                        details={
                            "dest_map_group": canon.dest_map_group,
                            "dest_map_num": canon.dest_map_num,
                            "dest_warp_id": canon.dest_warp_id,
                            # Each tile keeps its own warp_id since
                            # different tiles in a mat can map to
                            # different arrival warps on the dest.
                            "tiles": [
                                (w.x, w.y, w.dest_warp_id) for w in cluster
                            ],
                            "centroid": (cx, cy),
                        },
                    )
                )

        # Tile-behaviour sweep: counter, PC, bookshelf, signposts, etc.
        # All three inputs (grid, behavior lookup) come from caches that
        # were populated on map entry, so this sweep is pure Python with
        # zero HTTP. Skip silently if we can't get at the grid (e.g.
        # pre-overworld).
        try:
            map_grid = self.read_map_grid_cached()
        except Exception:  # noqa: BLE001
            map_grid = None
        if map_grid is not None:
            xsize, ysize, grid = map_grid
            counter_value = int(C.MetatileBehavior.COUNTER)
            for ly in range(min_y, max_y + 1):
                for lx in range(min_x, max_x + 1):
                    gx = lx + C.MAP_OFFSET
                    gy = ly + C.MAP_OFFSET
                    if not (0 <= gx < xsize and 0 <= gy < ysize):
                        continue
                    mid, _col, _ele = self.tile_cell_at(grid, xsize, gx, gy)
                    try:
                        behavior = self.get_metatile_behavior_cached(mid)
                    except Exception:  # noqa: BLE001
                        continue
                    tile_entry = _classify_tile_behavior(behavior)
                    if tile_entry is None:
                        continue
                    kind, label = tile_entry
                    # Counter tiles (0x80): emit only the ones the player
                    # can actually press A on -- the COUNTER tiles that
                    # sit 4-adjacent to a Nurse/Clerk NPC. Decorative
                    # counter tiles further down the strip are unreachable
                    # (the NPC isn't behind them) and would just clutter
                    # the agent's interactable list.
                    if behavior == counter_value:
                        if (lx, ly) in nurse_counter_xys:
                            kind = "heal_counter"
                            label = "PokeCenter Heal Counter"
                        elif (lx, ly) in clerk_counter_xys:
                            kind = "shop_counter"
                            label = "Mart Shop Counter"
                        else:
                            continue
                    out.append(
                        Interactable(
                            x=lx,
                            y=ly,
                            kind=kind,
                            label=label,
                            details={
                                "metatile_id": mid,
                                "behavior": behavior,
                            },
                        )
                    )

        return out

    # --- SaveBlock2 odds and ends ------------------------------------------

    def read_encryption_key(self) -> int:
        """The XOR key used to obfuscate money, coins, and item quantities."""
        base = self._save_block2_base()
        return self.read_u32(base + C.SAVEBLOCK2_ENCRYPTION_KEY)

    def read_money(self) -> int:
        """Current money, after XOR-decrypting against SaveBlock2.encryptionKey."""
        sb1 = self._save_block1_base()
        raw = self.read_u32(sb1 + C.SAVEBLOCK1_MONEY)
        return (raw ^ self.read_encryption_key()) & 0xFFFFFFFF
