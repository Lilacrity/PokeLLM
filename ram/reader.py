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

        Outside of battle, ``gPlayerAvatar.preventStep`` is checked to
        detect dialogue / script / cutscene states.
        """
        try:
            flags_byte = self.read_u8(C.ADDR_GMAIN + C.GMAIN_OFFSET_FLAGS_BYTE)
        except Exception:
            flags_byte = 0

        if flags_byte & C.GMAIN_FLAG_IN_BATTLE:
            return C.GameMode.BATTLE

        # Dialogue / script / cutscene detection via preventStep.
        try:
            if self.read_u8(C.ADDR_GPLAYER_AVATAR_PREVENT_STEP):
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

        Reads ``gPlayerAvatar.preventStep`` (bool8 at PlayerAvatar+0x06).
        """
        return self.read_u8(C.ADDR_GPLAYER_AVATAR_PREVENT_STEP) != 0

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
