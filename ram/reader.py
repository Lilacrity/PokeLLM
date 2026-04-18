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
"""

from __future__ import annotations

import dataclasses
import struct
from typing import Protocol

from . import constants as C


class MemoryBackend(Protocol):
    """The only interface the reader requires.

    Implementations must return exactly ``length`` bytes starting at
    ``addr`` or raise. Implementations may do their own batching / caching.
    """

    def read_bytes(self, addr: int, length: int) -> bytes: ...


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
    level: int
    status: int
    hp: int
    max_hp: int
    attack: int
    defense: int
    speed: int
    sp_attack: int
    sp_defense: int
    is_egg: bool


# -----------------------------------------------------------------------------
# RamReader
# -----------------------------------------------------------------------------


class RamReader:
    """Typed accessors over a MemoryBackend. Stateless aside from the backend."""

    def __init__(self, backend: MemoryBackend) -> None:
        self._backend = backend

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
                    x=cx - C.MAP_OFFSET, # subtract map offset to keep coordinates inline with playerstate
                    y=cy - C.MAP_OFFSET, # subtract map offset to keep coordinates inline with playerstate
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
            box_flags = blob[off + C.POKEMON_OFFSET_BOX_FLAGS]
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
                    level=level,
                    status=status,
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

    # --- Game mode ----------------------------------------------------------

    def read_game_mode(self) -> C.GameMode:
        """Coarse classification via a handful of RAM tells.

        The sole battle indicator is ``gMain.inBattle`` (offset 0x439
        bit 1). The engine sets it TRUE on battle entry and FALSE on exit.
        ``gBattleTypeFlags`` is *not* used here -- it classifies the battle
        type (wild, trainer, double, …) but is never explicitly zeroed
        when battle ends, so it would stick as a false positive.

        Dialogue / menu detection is deliberately left as UNKNOWN until the
        specific callback addresses for this ROM revision are identified --
        better to return UNKNOWN than to guess wrong and mislead the event
        detector.
        """
        try:
            flags_byte = self.read_u8(C.ADDR_GMAIN + C.GMAIN_OFFSET_FLAGS_BYTE)
        except Exception:
            flags_byte = 0

        if flags_byte & C.GMAIN_FLAG_IN_BATTLE:
            return C.GameMode.BATTLE

        # If we successfully read SaveBlock1, assume overworld until we wire
        # up dialogue/menu detection.
        try:
            self._save_block1_base()
            return C.GameMode.OVERWORLD
        except Exception:
            return C.GameMode.UNKNOWN

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
