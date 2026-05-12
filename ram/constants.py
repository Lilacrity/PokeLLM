"""Named RAM constants for Pokemon FireRed (US, rev 1.1 / BPRE1).

Two classes of value live here:

*   **Pointer / fixed-address anchors** (ADDR_*). Absolute GBA memory
    addresses. Pointer anchors (SaveBlock1, SaveBlock2) are version-stable
    because they live in the fixed scratch region at 0x03005008 /
    0x0300500C. ROM symbols (name tables, etc.) are BPRE1-specific: the
    pret/pokefirered map in ./pret is a BPRE0 build, so ROM addresses
    past the v1.1 shift point are taken from the map plus +0x70.
    EWRAM/IWRAM anchors (gObjectEvents, gMain, gBackupMapData,
    gBattleMons, ...) are identical between BPRE0 and BPRE1.

*   **Struct offsets** (OFFSET_*). Byte offsets inside a known struct.
    These come straight from the ``/*0xNN*/`` layout comments in the
    decomp headers; they are source-of-truth and do not depend on ROM
    revision.

Every value is ``int`` so Python-side arithmetic stays explicit. No
runtime initialisation, no side effects -- this file is only ever
``import``-read.
"""

from __future__ import annotations

import enum

# -----------------------------------------------------------------------------
# Sizes / constants from constants/*.h and the decomp headers.
# -----------------------------------------------------------------------------

# pret/include/config.h / constants/global.h
PARTY_SIZE = 6
OBJECT_EVENTS_COUNT = 16  # FRLG; RSE uses a different value
MAP_OFFSET = 7            # invisible border tiles on each side of the map grid
# pret/include/fieldmap.h: MAP_OFFSET_W = MAP_OFFSET * 2 + 1 = 15,
# MAP_OFFSET_H = MAP_OFFSET * 2 = 14. The extra column on W reflects the
# asymmetry fieldmap.c bakes into the backup buffer: VMap.Xsize gets
# width + MAP_OFFSET_W, Ysize gets height + MAP_OFFSET_H.
MAP_OFFSET_W = MAP_OFFSET * 2 + 1
MAP_OFFSET_H = MAP_OFFSET * 2

POKEMON_SIZE = 0x64          # sizeof(struct Pokemon)
BOX_POKEMON_SIZE = 0x50      # sizeof(struct BoxPokemon)
OBJECT_EVENT_SIZE = 0x24     # sizeof(struct ObjectEvent) -- see global.fieldmap.h
BATTLE_POKEMON_SIZE = 0x58   # sizeof(struct BattlePokemon) -- see pokemon.h
MAX_BATTLERS_COUNT = 4       # singles=2, doubles=4
MAX_MON_MOVES = 4

SPECIES_NAME_LENGTH = 11     # POKEMON_NAME_LENGTH + 1 (null terminated in ROM)
MOVE_NAME_LENGTH = 13        # MOVE_NAME_LENGTH + 1
NUM_SPECIES = 412            # constants/species.h: SPECIES_EGG
MOVES_COUNT = 355            # constants/moves.h

# -----------------------------------------------------------------------------
# Pointer anchors. These are the fixed IWRAM slots that hold pointers to the
# dynamically-placed save blocks. Reads *must* be done fresh every frame --
# the DMA anti-tamper relocator moves SaveBlock1/2 around between boots.
# -----------------------------------------------------------------------------

# pret/include/global.h:    extern struct SaveBlock1* gSaveBlock1Ptr
# pret/include/global.h:    extern struct SaveBlock2* gSaveBlock2Ptr
ADDR_SAVEBLOCK1_PTR = 0x03005008
ADDR_SAVEBLOCK2_PTR = 0x0300500C

# -----------------------------------------------------------------------------
# Fixed-address globals (FireRed US v1.0). All addresses verified against the
# compiled decomp's linker map (pret/pokefirered.map).
# -----------------------------------------------------------------------------

# struct Main gMain -- holds callbacks, key state, and inBattle flag.
# pret/include/main.h describes the layout.
ADDR_GMAIN = 0x030030F0  # pokefirered.map: gMain = 0x030030f0

# struct ObjectEvent gObjectEvents[OBJECT_EVENTS_COUNT]
# 16 * 0x24 = 576 bytes. One bulk read covers the whole array.
ADDR_GOBJECT_EVENTS = 0x02036E38  # VERIFIED

# struct PlayerAvatar gPlayerAvatar -- flags, running state, gender, etc.
# Useful for detecting surf/bike and for disambiguating the player slot.
ADDR_GPLAYER_AVATAR = 0x02037078  # pokefirered.map: gPlayerAvatar = 0x02037078

# struct MapHeader gMapHeader -- current map header, reloaded on transition.
# Contains music, mapLayoutId, weather, etc.
ADDR_GMAP_HEADER = 0x02036DFC  # pokefirered.map: gMapHeader = 0x02036dfc

# gBackupMapData: u16 tile grid buffer (metatile_id | collision | elevation).
# Row stride is VMap.Xsize == mapLayout->width + MAP_OFFSET_W. To read a
# *behaviour* you still need the tileset's metatileAttributes table (on ROM),
# so in practice RamReader.read_tile_behavior pulls the whole machinery from
# gMapHeader.mapLayout.
ADDR_GBACKUP_MAP_DATA = 0x02031DFC  # pokefirered.map: gBackupMapData = 0x02031dfc

# struct BackupMapLayout VMap -- holds Xsize, Ysize, and map pointer. Useful
# if you want to confirm the grid dimensions live instead of deriving them
# from MapHeader.
ADDR_VMAP = 0x03005040  # pokefirered.map: VMap = 0x03005040

# gPlayerParty -- the *live* player party, 6 * struct Pokemon (100 bytes each).
# This is what actually changes during play as the team gains XP, takes damage,
# etc. SaveBlock1.playerParty (at SB1 + 0x0038) holds only the last-saved copy
# and does not update between saves; live reads should use this address.
#
# Verified by the user against the Data Crystal FireRed RAM map:
#   0x02024284  Pokemon 1
#   0x020242E8  Pokemon 2  (+0x64)
#   0x0202434C  Pokemon 3
#   0x020243B0  Pokemon 4
#   0x02024414  Pokemon 5
#   0x02024478  Pokemon 6
ADDR_GPLAYER_PARTY = 0x02024284

# -- Battle globals (only valid while gMain.inBattle is set) ----------------

ADDR_GBATTLE_TYPE_FLAGS = 0x02022B4C      # u32; pokefirered.map
ADDR_GACTIVE_BATTLER = 0x02023BC4         # u8; which battler slot is acting
ADDR_GBATTLERS_COUNT = 0x02023BCC         # u8; 2 singles, 4 doubles
ADDR_GBATTLER_PARTY_INDEXES = 0x02023BCE  # u8[4]; battler slot -> party index
ADDR_GBATTLE_MONS = 0x02023BE4            # struct BattlePokemon[4]; pokefirered.map
ADDR_GCURRENT_MOVE = 0x02023D4A           # u16; move ID currently executing
ADDR_GBATTLE_MOVE_DAMAGE = 0x02023D50     # s32; damage from current move
ADDR_GBATTLER_ATTACKER = 0x02023D6B       # u8; battler slot of attacker
ADDR_GBATTLER_TARGET = 0x02023D6C         # u8; battler slot of target
ADDR_GMOVE_RESULT_FLAGS = 0x02023DCC      # u32; miss/effective/etc bitmask
ADDR_GBATTLE_OUTCOME = 0x02023E8A         # u8; BattleOutcome enum
ADDR_GBATTLE_WEATHER = 0x02023F1C         # u16; weather bitmask

# u16; opponent-A trainer ID lives in its own EWRAM slot (0 for wild battles).
# The full trainer record (name, class, party) is in ROM at gTrainers; we only
# need the ID here for "is this a trainer fight" bookkeeping.
ADDR_GTRAINER_BATTLE_OPPONENT_A = 0x020386AE

# struct BattleResults in IWRAM -- populated on battle exit (mon that KO'd us,
# turns taken, etc). Address retained for a later phase; not read yet.
ADDR_GBATTLE_RESULTS = 0x03004F90

# -- Party globals -----------------------------------------------------------

ADDR_GPLAYER_PARTY_COUNT = 0x02024029     # u8; pokefirered.map
ADDR_GENEMY_PARTY = 0x0202402C            # struct Pokemon[6]; pokefirered.map

# -- Text / dialogue ---------------------------------------------------------

ADDR_GSTRING_VAR4 = 0x02021D18            # message box text buffer (game charset)
ADDR_GDISPLAYED_STRING_BATTLE = 0x0202298C  # battle-specific displayed text
ADDR_GSPECIALVAR_LAST_TALKED = 0x020370D2 # u16; local_id of last NPC talked to

# sMessageBoxType -- pret/src/field_message_box.c static, the single byte of
# field_message_box.o's ewram_data in pokefirered.map (0x0203709C, size 0x1).
# 0 == FIELD_MESSAGE_BOX_HIDDEN, 1 == NORMAL, 2 == AUTO_SCROLL.
ADDR_SMESSAGE_BOX_TYPE = 0x0203709C

# sLockFieldControls -- pret/src/script.c static bool8. Set TRUE whenever
# LockPlayerFieldControls() is called (dialogue, scripts, cutscenes, battle
# setup, warps); read back by ArePlayerFieldControlsLocked() which the
# overworld callback uses to decide whether to accept player input.
#
# Not a named symbol in the linker map; its address is computed from the
# .bss block for script.o (0x03000EA8, total 0xFA bytes). Declaration order
# in script.c lines 29-39 gives the exact layout (verified: sum of member
# sizes matches 0xFA):
#   +0x00 sGlobalScriptContextStatus  (u8)
#   +0x04 sUnusedVariable1            (u32)
#   +0x08 sGlobalScriptContext        (struct, 0x74)
#   +0x7C sUnusedVariable2            (u32)
#   +0x80 sImmediateScriptContext     (struct, 0x74)
#   +0xF4 sLockFieldControls          (bool8) <--
#   +0xF5 .. 0xF9  six more u8 statics
ADDR_SLOCK_FIELD_CONTROLS = 0x03000F9C

# gPlayerAvatar.preventStep -- TRUE during field effect animations (surf,
# teleport, warp transitions). NOT a dialogue indicator; use
# ADDR_SLOCK_FIELD_CONTROLS for that.
ADDR_GPLAYER_AVATAR_PREVENT_STEP = ADDR_GPLAYER_AVATAR + 0x06

FIELD_MESSAGE_BOX_HIDDEN = 0
FIELD_MESSAGE_BOX_NORMAL = 1
FIELD_MESSAGE_BOX_AUTO_SCROLL = 2

# -- ROM name tables (read-only) --------------------------------------------
#
# Addresses are for the FireRed US v1.1 retail ROM (BPRE1). v1.1 inserts
# ~0x70 bytes of code ahead of the name tables versus v1.0 (BPRE0), so
# every ROM symbol past that shift moves by +0x70. pokefirered.map in
# ./pret is the v1.0 build, so the map's gSpeciesNames/gMoveNames are at
# 0x08245EE0 / 0x08247094; the addresses below add the +0x70 offset.
# Verified empirically by reading move #10 and seeing the end of POUND
# followed by the start of KARATE_CHOP at the v1.0 offset -- consistent
# with a v1.1 ROM being read through v1.0 addresses.
ADDR_GSPECIES_NAMES = 0x08245F50  # NUM_SPECIES * SPECIES_NAME_LENGTH bytes
ADDR_GMOVE_NAMES = 0x08247104     # MOVES_COUNT * MOVE_NAME_LENGTH bytes

# -----------------------------------------------------------------------------
# Offsets into struct SaveBlock1 (pret/include/global.h). All offsets come
# straight from the layout comments in that header.
# -----------------------------------------------------------------------------

# struct Coords16 pos
SAVEBLOCK1_POS_X = 0x0000  # s16
SAVEBLOCK1_POS_Y = 0x0002  # s16

# struct WarpData location { s8 mapGroup; s8 mapNum; s8 warpId; s16 x, y; }
# sizeof(WarpData) == 8 -- there's one byte of padding between warpId and x.
SAVEBLOCK1_LOCATION_MAP_GROUP = 0x0004  # s8
SAVEBLOCK1_LOCATION_MAP_NUM = 0x0005    # s8
SAVEBLOCK1_LOCATION_WARP_ID = 0x0006    # s8
SAVEBLOCK1_LOCATION_X = 0x0008          # s16
SAVEBLOCK1_LOCATION_Y = 0x000A          # s16

# Subsequent WarpData slots (continueGameWarp, dynamicWarp, lastHealLocation,
# escapeWarp) each occupy 8 bytes starting at 0x000C, 0x0014, 0x001C, 0x0024.

SAVEBLOCK1_SAVED_MUSIC = 0x002C       # u16
SAVEBLOCK1_WEATHER = 0x002E           # u8
SAVEBLOCK1_MAP_LAYOUT_ID = 0x0032     # u16
SAVEBLOCK1_PLAYER_PARTY_COUNT = 0x0034  # u8, persisted -- not the live count
SAVEBLOCK1_PLAYER_PARTY = 0x0038      # struct Pokemon[PARTY_SIZE], persisted
# For the *live* party, read from ADDR_GPLAYER_PARTY instead -- SaveBlock1
# holds only the last-saved snapshot. There is no known live party-count
# address; derive the count by walking gPlayerParty slots until BoxPokemon
# flags byte is zero (see RamReader.read_party).
SAVEBLOCK1_MONEY = 0x0290             # u32, XOR-encrypted with SaveBlock2.encryptionKey
SAVEBLOCK1_COINS = 0x0294             # u16
SAVEBLOCK1_REGISTERED_ITEM = 0x0296   # u16
# Bag pockets are 4-byte (u16 itemId, u16 quantity) slots.
SAVEBLOCK1_PC_ITEMS = 0x0298
SAVEBLOCK1_BAG_ITEMS = 0x0310
SAVEBLOCK1_BAG_KEY_ITEMS = 0x03B8
SAVEBLOCK1_BAG_POKE_BALLS = 0x0430
SAVEBLOCK1_BAG_TMHM = 0x0464
SAVEBLOCK1_BAG_BERRIES = 0x054C
SAVEBLOCK1_FLAGS = 0x0EE0             # NUM_FLAG_BYTES bytes of bit flags
SAVEBLOCK1_VARS = 0x1000              # u16[VARS_COUNT]
SAVEBLOCK1_GAME_STATS = 0x1200        # u32[NUM_GAME_STATS]

# -----------------------------------------------------------------------------
# Offsets into struct SaveBlock2 (pret/include/global.h).
# -----------------------------------------------------------------------------

SAVEBLOCK2_PLAYER_NAME = 0x0000       # u8[PLAYER_NAME_LENGTH + 1] == u8[8]
SAVEBLOCK2_PLAYER_GENDER = 0x0008     # u8 (0 = male, 1 = female)
SAVEBLOCK2_PLAYER_TRAINER_ID = 0x000A  # u8[4]
SAVEBLOCK2_PLAY_TIME_HOURS = 0x000E   # u16
SAVEBLOCK2_PLAY_TIME_MINUTES = 0x0010  # u8
SAVEBLOCK2_PLAY_TIME_SECONDS = 0x0011  # u8
SAVEBLOCK2_OPTIONS_BUTTON_MODE = 0x0013  # u8
SAVEBLOCK2_ENCRYPTION_KEY = 0x0F20    # u32 -- XOR key for money, coins, items

PLAYER_NAME_LEN = 8   # 7 chars + terminator; uses custom charset (not ASCII)

# -----------------------------------------------------------------------------
# Offsets into struct ObjectEvent (pret/include/global.fieldmap.h).
# Slot 0 is always the player.
# -----------------------------------------------------------------------------

# Bits 0-3 of the first byte are the active / movement flags. Bit 0 is the
# "active" flag -- zero for empty slots.
OE_OFFSET_FLAGS_BYTE_0 = 0x00  # u8; bit 0 == active
OE_OFFSET_FLAGS_BYTE_1 = 0x01  # u8; bit 5 == invisible, bit 7 == trackedByCamera
OE_OFFSET_FLAGS_BYTE_2 = 0x02  # u8; bit 0 == isPlayer
OE_OFFSET_SPRITE_ID = 0x04
OE_OFFSET_GRAPHICS_ID = 0x05   # u8, indexes OBJ_EVENT_GFX_*
OE_OFFSET_MOVEMENT_TYPE = 0x06  # u8
OE_OFFSET_TRAINER_TYPE = 0x07   # u8
OE_OFFSET_LOCAL_ID = 0x08       # u8
OE_OFFSET_MAP_NUM = 0x09        # u8
OE_OFFSET_MAP_GROUP = 0x0A      # u8
OE_OFFSET_ELEVATION = 0x0B      # u8, nibble 0 = current, nibble 1 = previous
OE_OFFSET_INITIAL_X = 0x0C      # s16
OE_OFFSET_INITIAL_Y = 0x0E      # s16
OE_OFFSET_CURRENT_X = 0x10      # s16
OE_OFFSET_CURRENT_Y = 0x12      # s16
OE_OFFSET_PREVIOUS_X = 0x14     # s16
OE_OFFSET_PREVIOUS_Y = 0x16     # s16
OE_OFFSET_DIRECTION_BYTE = 0x18  # u8, low nibble = facing, high nibble = movement
OE_OFFSET_RANGE_BYTE = 0x19     # u8, low nibble = rangeX, high nibble = rangeY
OE_OFFSET_FIELD_EFFECT_SPRITE_ID = 0x1A
OE_OFFSET_WARP_ARROW_SPRITE_ID = 0x1B
OE_OFFSET_MOVEMENT_ACTION_ID = 0x1C
OE_OFFSET_TRAINER_RANGE = 0x1D
OE_OFFSET_CURRENT_METATILE_BEHAVIOR = 0x1E  # u8
OE_OFFSET_PREVIOUS_METATILE_BEHAVIOR = 0x1F  # u8

# Bit masks for the flag bytes.
OE_FLAG0_ACTIVE = 1 << 0
OE_FLAG1_INVISIBLE = 1 << 5
OE_FLAG1_OFF_SCREEN = 1 << 6
OE_FLAG1_TRACKED_BY_CAMERA = 1 << 7
OE_FLAG2_IS_PLAYER = 1 << 0

# -----------------------------------------------------------------------------
# Offsets into struct Pokemon (pret/include/pokemon.h).
# The first 0x50 bytes are struct BoxPokemon -- species, moves, experience,
# EVs, IVs etc. live encrypted inside box.secure.substructs and require the
# personality*otId XOR key plus the substruct permutation table. The fields
# below this point (from 0x50) are plain unencrypted runtime stats -- which
# covers everything the minimum-viable reader needs.
# -----------------------------------------------------------------------------

POKEMON_OFFSET_PERSONALITY = 0x00    # u32 -- encryption key (lo)
POKEMON_OFFSET_OT_ID = 0x04          # u32 -- encryption key (hi)
POKEMON_OFFSET_NICKNAME = 0x08       # u8[10]
POKEMON_OFFSET_LANGUAGE = 0x12       # u8
POKEMON_OFFSET_BOX_FLAGS = 0x13      # u8; bit 1 == hasSpecies, bit 2 == isEgg
POKEMON_OFFSET_OT_NAME = 0x14        # u8[7]
POKEMON_OFFSET_MARKINGS = 0x1B       # u8
POKEMON_OFFSET_CHECKSUM = 0x1C       # u16
POKEMON_OFFSET_SUBSTRUCT_BLOCK = 0x20  # 48 encrypted bytes

POKEMON_OFFSET_STATUS = 0x50         # u32 -- sleep/poison/burn/freeze/paralysis
POKEMON_OFFSET_LEVEL = 0x54          # u8
POKEMON_OFFSET_MAIL = 0x55           # u8
POKEMON_OFFSET_HP = 0x56             # u16  (matches pokegym)
POKEMON_OFFSET_MAX_HP = 0x58         # u16  (matches pokegym)
POKEMON_OFFSET_ATTACK = 0x5A
POKEMON_OFFSET_DEFENSE = 0x5C
POKEMON_OFFSET_SPEED = 0x5E
POKEMON_OFFSET_SP_ATTACK = 0x60
POKEMON_OFFSET_SP_DEFENSE = 0x62

# -----------------------------------------------------------------------------
# Offsets into struct BattlePokemon (pret/include/pokemon.h).
# All offsets verified against the /*0xNN*/ comments in the header.
# Only valid while a battle is active.
# -----------------------------------------------------------------------------

BPOKE_OFFSET_SPECIES    = 0x00   # u16
BPOKE_OFFSET_ATTACK     = 0x02   # u16
BPOKE_OFFSET_DEFENSE    = 0x04   # u16
BPOKE_OFFSET_SPEED      = 0x06   # u16
BPOKE_OFFSET_SP_ATTACK  = 0x08   # u16
BPOKE_OFFSET_SP_DEFENSE = 0x0A   # u16
BPOKE_OFFSET_MOVES      = 0x0C   # u16[4]
BPOKE_OFFSET_IV_BLOCK   = 0x14   # u32 bitfield (5-bit IVs + isEgg + abilityNum)
BPOKE_OFFSET_STAT_STAGES = 0x18  # s8[8] (atk/def/spe/spa/spd/acc/eva/?)
BPOKE_OFFSET_ABILITY    = 0x20   # u8
BPOKE_OFFSET_TYPE1      = 0x21   # u8
BPOKE_OFFSET_TYPE2      = 0x22   # u8
BPOKE_OFFSET_PP         = 0x24   # u8[4]
BPOKE_OFFSET_HP         = 0x28   # u16
BPOKE_OFFSET_LEVEL      = 0x2A   # u8
BPOKE_OFFSET_FRIENDSHIP = 0x2B   # u8
BPOKE_OFFSET_MAX_HP     = 0x2C   # u16
BPOKE_OFFSET_ITEM       = 0x2E   # u16
BPOKE_OFFSET_NICKNAME   = 0x30   # u8[11]
BPOKE_OFFSET_PP_BONUSES = 0x3B   # u8
BPOKE_OFFSET_OT_NAME    = 0x3C   # u8[8]
BPOKE_OFFSET_EXPERIENCE = 0x44   # u32
BPOKE_OFFSET_PERSONALITY = 0x48  # u32
BPOKE_OFFSET_STATUS1    = 0x4C   # u32
BPOKE_OFFSET_STATUS2    = 0x50   # u32
BPOKE_OFFSET_OT_ID      = 0x54   # u32

# -----------------------------------------------------------------------------
# Status condition bitmasks (pret/include/constants/battle.h).
# -----------------------------------------------------------------------------

STATUS1_NONE          = 0
STATUS1_SLEEP_MASK    = 0x07    # bits 0-2: sleep turns remaining (0 = awake)
STATUS1_POISON        = 1 << 3
STATUS1_BURN          = 1 << 4
STATUS1_FREEZE        = 1 << 5
STATUS1_PARALYSIS     = 1 << 6
STATUS1_TOXIC_POISON  = 1 << 7
STATUS1_TOXIC_COUNTER = 0x0F00  # bits 8-11: toxic damage counter

# Battle weather bitmask (constants/battle.h B_WEATHER_*).
B_WEATHER_RAIN_TEMPORARY      = 1 << 0
B_WEATHER_RAIN_PERMANENT      = 1 << 2
B_WEATHER_RAIN                = B_WEATHER_RAIN_TEMPORARY | (1 << 1) | B_WEATHER_RAIN_PERMANENT
B_WEATHER_SANDSTORM_TEMPORARY = 1 << 3
B_WEATHER_SANDSTORM_PERMANENT = 1 << 4
B_WEATHER_SANDSTORM           = B_WEATHER_SANDSTORM_TEMPORARY | B_WEATHER_SANDSTORM_PERMANENT
B_WEATHER_SUN_TEMPORARY       = 1 << 5
B_WEATHER_SUN_PERMANENT       = 1 << 6
B_WEATHER_SUN                 = B_WEATHER_SUN_TEMPORARY | B_WEATHER_SUN_PERMANENT
B_WEATHER_HAIL_TEMPORARY      = 1 << 7
B_WEATHER_HAIL                = B_WEATHER_HAIL_TEMPORARY

# -----------------------------------------------------------------------------
# Offsets into struct Main (pret/include/main.h).
# -----------------------------------------------------------------------------

GMAIN_OFFSET_CALLBACK1 = 0x000       # MainCallback (u32 function pointer)
GMAIN_OFFSET_CALLBACK2 = 0x004       # MainCallback
GMAIN_OFFSET_SAVED_CALLBACK = 0x008
GMAIN_OFFSET_HELD_KEYS_RAW = 0x028   # u16
GMAIN_OFFSET_NEW_KEYS_RAW = 0x02A    # u16
GMAIN_OFFSET_HELD_KEYS = 0x02C       # u16 (with L=A remapping applied)
GMAIN_OFFSET_NEW_KEYS = 0x02E        # u16
GMAIN_OFFSET_STATE = 0x438           # u8
GMAIN_OFFSET_FLAGS_BYTE = 0x439      # u8 -- bit 0 oamLoadDisabled, bit 1 inBattle
GMAIN_FLAG_IN_BATTLE = 1 << 1


# -----------------------------------------------------------------------------
# Map grid encoding -- pret/include/global.fieldmap.h
# Each tile is a u16: [elevation:4][collision:2][metatileId:10]
# -----------------------------------------------------------------------------

MAPGRID_METATILE_ID_MASK = 0x03FF
MAPGRID_COLLISION_MASK = 0x0C00
MAPGRID_ELEVATION_MASK = 0xF000
MAPGRID_COLLISION_SHIFT = 10
MAPGRID_ELEVATION_SHIFT = 12
MAPGRID_UNDEFINED = MAPGRID_METATILE_ID_MASK

# Metatile attribute encoding (pret/src/fieldmap.c sMetatileAttrMasks).
METATILE_ATTR_BEHAVIOR_MASK = 0x000001FF  # bits 0-8
METATILE_ATTR_BEHAVIOR_SHIFT = 0
METATILE_ATTR_TERRAIN_MASK = 0x00003E00   # bits 9-13
METATILE_ATTR_TERRAIN_SHIFT = 9
METATILE_ATTR_ENCOUNTER_MASK = 0x07000000  # bits 24-26
METATILE_ATTR_ENCOUNTER_SHIFT = 24


# -----------------------------------------------------------------------------
# gBattleTypeFlags bit positions (pret/include/constants/battle.h).
# Only the bits we classify on here. Full list in the header.
# -----------------------------------------------------------------------------

BATTLE_TYPE_DOUBLE          = 1 << 0
BATTLE_TYPE_LINK            = 1 << 1
BATTLE_TYPE_IS_MASTER       = 1 << 2
BATTLE_TYPE_TRAINER         = 1 << 3   # the "is this a trainer fight" bit
BATTLE_TYPE_FIRST_BATTLE    = 1 << 4
BATTLE_TYPE_SAFARI          = 1 << 7
BATTLE_TYPE_BATTLE_TOWER    = 1 << 8
BATTLE_TYPE_OLD_MAN_TUTORIAL = 1 << 9  # FRLG Viridian Old Man "catch tutorial"
BATTLE_TYPE_ROAMER          = 1 << 10
BATTLE_TYPE_LEGENDARY       = 1 << 13
BATTLE_TYPE_GHOST           = 1 << 15  # Pokemon Tower unveiled-Marowak fights
BATTLE_TYPE_POKEDUDE        = 1 << 16  # FRLG Pokedude tutorial battle
BATTLE_TYPE_LEGENDARY_FRLG  = 1 << 18

# Battles where catching is blocked even without BATTLE_TYPE_TRAINER set.
BATTLE_TYPE_UNCATCHABLE_MASK = (
    BATTLE_TYPE_TRAINER
    | BATTLE_TYPE_LINK
    | BATTLE_TYPE_BATTLE_TOWER
    | BATTLE_TYPE_OLD_MAN_TUTORIAL
    | BATTLE_TYPE_POKEDUDE
)


# -----------------------------------------------------------------------------
# Map header / events / layout / tileset struct offsets
# (pret/include/global.fieldmap.h).
# -----------------------------------------------------------------------------

# struct MapHeader at gMapHeader (ADDR_GMAP_HEADER).
MAP_HEADER_OFFSET_MAP_LAYOUT  = 0x00  # const struct MapLayout *
MAP_HEADER_OFFSET_EVENTS      = 0x04  # const struct MapEvents *
MAP_HEADER_OFFSET_SCRIPTS     = 0x08  # const u8 *
MAP_HEADER_OFFSET_CONNECTIONS = 0x0C  # const struct MapConnections *

# struct MapConnections -- {s32 count; const struct MapConnection *}
MAP_CONNECTIONS_OFFSET_COUNT        = 0x00  # s32
MAP_CONNECTIONS_OFFSET_CONNS_PTR    = 0x04  # const struct MapConnection *

# struct MapConnection is 10 bytes of data but padded to 12 by alignment
# (the s32 offset field forces 4-byte alignment on the following u8 pair).
MAP_CONNECTION_SIZE             = 0x0C
MAP_CONNECTION_OFFSET_DIRECTION = 0x00  # u8: CONNECTION_*
MAP_CONNECTION_OFFSET_OFFSET    = 0x04  # s32 (shift along the shared edge)
MAP_CONNECTION_OFFSET_MAP_GROUP = 0x08  # u8
MAP_CONNECTION_OFFSET_MAP_NUM   = 0x09  # u8

# pret/include/constants/global.h
CONNECTION_NONE     = 0
CONNECTION_SOUTH    = 1
CONNECTION_NORTH    = 2
CONNECTION_WEST     = 3
CONNECTION_EAST     = 4
CONNECTION_DIVE     = 5
CONNECTION_EMERGE   = 6
MAP_HEADER_OFFSET_MUSIC       = 0x10  # u16
MAP_HEADER_OFFSET_LAYOUT_ID   = 0x12  # u16
MAP_HEADER_OFFSET_REGION_ID   = 0x14  # u8
MAP_HEADER_OFFSET_CAVE        = 0x15  # u8
MAP_HEADER_OFFSET_WEATHER     = 0x16  # u8
MAP_HEADER_OFFSET_MAP_TYPE    = 0x17  # u8
MAP_HEADER_OFFSET_BATTLE_TYPE = 0x1B  # u8

# struct MapLayout (pointer lives at MapHeader + MAP_HEADER_OFFSET_MAP_LAYOUT).
MAP_LAYOUT_OFFSET_WIDTH              = 0x00  # s32
MAP_LAYOUT_OFFSET_HEIGHT             = 0x04  # s32
MAP_LAYOUT_OFFSET_BORDER             = 0x08  # const u16 *
MAP_LAYOUT_OFFSET_MAP                = 0x0C  # const u16 *
MAP_LAYOUT_OFFSET_PRIMARY_TILESET    = 0x10  # const struct Tileset *
MAP_LAYOUT_OFFSET_SECONDARY_TILESET  = 0x14  # const struct Tileset *
MAP_LAYOUT_OFFSET_BORDER_WIDTH       = 0x18  # u8
MAP_LAYOUT_OFFSET_BORDER_HEIGHT      = 0x19  # u8

# struct Tileset. metatileAttributes sits at +0x14 (four u32 pointers
# precede it, not three -- the isSecondary/isCompressed bools take up the
# first 4-byte slot with padding before the `tiles` pointer).
TILESET_OFFSET_IS_COMPRESSED      = 0x00  # bool8
TILESET_OFFSET_IS_SECONDARY       = 0x01  # bool8
TILESET_OFFSET_TILES              = 0x04  # const u32 *
TILESET_OFFSET_PALETTES           = 0x08  # const u16 (*)[16]
TILESET_OFFSET_METATILES          = 0x0C  # const u16 *
TILESET_OFFSET_CALLBACK           = 0x10  # fn ptr
TILESET_OFFSET_METATILE_ATTRS     = 0x14  # const u32 *

# Metatile IDs 0x000-0x27F use the primary tileset; 0x280-0x3FF the
# secondary. FRLG raised the primary tileset count from RSE's 512 (0x200)
# to 640 (0x280) -- pret/include/fieldmap.h NUM_METATILES_IN_PRIMARY.
# Using the RSE value misroutes ids in [0x200, 0x27F] into the secondary
# attributes array and returns whatever junk happens to live at that
# offset, which is exactly what was painting random tiles in Viridian
# blue.
NUM_METATILES_IN_PRIMARY = 0x280
NUM_METATILES_IN_SECONDARY = 0x180
NUM_METATILES_TOTAL = NUM_METATILES_IN_PRIMARY + NUM_METATILES_IN_SECONDARY

# struct MapEvents -- four u8 counts followed by four pointers.
MAP_EVENTS_OFFSET_OBJECT_EVENT_COUNT = 0x00  # u8
MAP_EVENTS_OFFSET_WARP_COUNT         = 0x01  # u8
MAP_EVENTS_OFFSET_COORD_EVENT_COUNT  = 0x02  # u8
MAP_EVENTS_OFFSET_BG_EVENT_COUNT     = 0x03  # u8
MAP_EVENTS_OFFSET_OBJECT_EVENTS_PTR  = 0x04  # const struct ObjectEventTemplate *
MAP_EVENTS_OFFSET_WARPS_PTR          = 0x08  # const struct WarpEvent *
MAP_EVENTS_OFFSET_COORD_EVENTS_PTR   = 0x0C  # const struct CoordEvent *
MAP_EVENTS_OFFSET_BG_EVENTS_PTR      = 0x10  # const struct BgEvent *

# struct WarpEvent (8 bytes, no padding).
WARP_EVENT_SIZE              = 0x08
WARP_EVENT_OFFSET_X          = 0x00  # s16
WARP_EVENT_OFFSET_Y          = 0x02  # s16
WARP_EVENT_OFFSET_ELEVATION  = 0x04  # u8
WARP_EVENT_OFFSET_WARP_ID    = 0x05  # u8 (index into the destination's warp list)
WARP_EVENT_OFFSET_MAP_NUM    = 0x06  # u8
WARP_EVENT_OFFSET_MAP_GROUP  = 0x07  # u8

# struct BgEvent (12 bytes -- 2 bytes padding between kind@0x05 and union@0x08
# to align the 4-byte hiddenItem / script union).
BG_EVENT_SIZE              = 0x0C
BG_EVENT_OFFSET_X          = 0x00  # u16
BG_EVENT_OFFSET_Y          = 0x02  # u16
BG_EVENT_OFFSET_ELEVATION  = 0x04  # u8
BG_EVENT_OFFSET_KIND       = 0x05  # u8 (see BG_EVENT_KIND_* below)
BG_EVENT_OFFSET_UNION      = 0x08  # u32; script ptr OR packed hidden item

# struct CoordEvent (16 bytes; kept here even though we don't decode them --
# walking past them requires the right stride).
COORD_EVENT_SIZE = 0x10

# struct ObjectEventTemplate (static ROM-side template, distinct from the
# runtime struct ObjectEvent in gObjectEvents).
OBJECT_EVENT_TEMPLATE_SIZE = 0x18

# BgEvent.kind values (pret/include/constants/event_bg.h). FRLG only uses
# 0-4 for signs and 7 for hidden items; 8 (SECRET_BASE) is defined but
# never referenced in FRLG scripts.
BG_EVENT_KIND_PLAYER_FACING_ANY   = 0
BG_EVENT_KIND_PLAYER_FACING_NORTH = 1
BG_EVENT_KIND_PLAYER_FACING_SOUTH = 2
BG_EVENT_KIND_PLAYER_FACING_EAST  = 3
BG_EVENT_KIND_PLAYER_FACING_WEST  = 4
BG_EVENT_KIND_HIDDEN_ITEM         = 7
BG_EVENT_KIND_SECRET_BASE         = 8

SIGN_BG_EVENT_KINDS = {
    BG_EVENT_KIND_PLAYER_FACING_ANY,
    BG_EVENT_KIND_PLAYER_FACING_NORTH,
    BG_EVENT_KIND_PLAYER_FACING_SOUTH,
    BG_EVENT_KIND_PLAYER_FACING_EAST,
    BG_EVENT_KIND_PLAYER_FACING_WEST,
}

# -----------------------------------------------------------------------------
# Object-event graphics IDs for HM-obstacle / treasure sprites
# (pret/include/constants/event_objects.h). FRLG identifies cuttable trees,
# breakable rocks, strength boulders, and item balls by graphicsId rather
# than by a metatile behaviour -- they ship as regular ObjectEvents.
# -----------------------------------------------------------------------------

OBJ_EVENT_GFX_ITEM_BALL         = 92
OBJ_EVENT_GFX_CUT_TREE          = 95
OBJ_EVENT_GFX_ROCK_SMASH_ROCK   = 96
OBJ_EVENT_GFX_PUSHABLE_BOULDER  = 97

# Counter-staff sprites: Nurse Joy (PokeCenter heal) and Clerk (Mart
# shop). Previously used by the interactables pass to disambiguate
# COUNTER tiles into heal_counter / shop_counter variants. That logic
# was removed when the tile-behaviour sweep was narrowed to ledges /
# water / PC -- the NPC behind a counter now surfaces through the
# ObjectEvent pass (kind="npc") at the NPC's own tile, and the planner
# is expected to route the agent to the adjacent counter tile one step
# in the NPC's facing direction once A* is implemented. Left in place
# but commented for now; remove after the planner ships and we've
# verified nothing else looks them up.
# OBJ_EVENT_GFX_NURSE             = 64
# OBJ_EVENT_GFX_CLERK             = 68


# -----------------------------------------------------------------------------
# Enums. Name -> value pulled from the relevant constants/*.h file.
# -----------------------------------------------------------------------------


class Facing(enum.IntEnum):
    """Low nibble of ObjectEvent.facingDirection.

    Pulled from pret/include/constants/event_object_movement.h (DIR_* values).
    Values 0 (DIR_NONE) and 5-7 are reserved.
    """

    NONE = 0
    SOUTH = 1
    NORTH = 2
    WEST = 3
    EAST = 4


class GameMode(enum.Enum):
    """Coarse state categorisation from RAM tells.

    Computed by RamReader.read_game_mode. We do not try to enumerate every
    possible CB2 here -- dialogue/menu require overworld-specific tells that
    are easier to infer than to identify by callback address.
    """

    UNKNOWN = "unknown"
    OVERWORLD = "overworld"
    BATTLE = "battle"
    DIALOGUE = "dialogue"
    MENU = "menu"
    CUTSCENE = "cutscene"
    TITLE = "title"


class BattleOutcome(enum.IntEnum):
    """Values from pret/include/constants/battle.h B_OUTCOME_*."""

    ONGOING = 0
    WON = 1
    LOST = 2
    DREW = 3
    FLED = 4
    PLAYER_TELEPORTED = 5
    MON_FLED = 6
    CAUGHT = 7
    NO_SAFARI_BALLS = 8
    FORFEITED = 9
    MON_TELEPORTED = 10


class MetatileBehavior(enum.IntEnum):
    """Values from pret/include/constants/metatile_behaviors.h.

    Not exhaustive -- see the header for the full 0x00-0xA3 range. Only the
    behaviours the pathfinder / event detector need in the first pass are
    enumerated here; the rest are still readable as raw ints.
    """

    NORMAL = 0x00
    TALL_GRASS = 0x02
    CAVE = 0x08
    RUNNING_DISALLOWED = 0x0A
    INDOOR_ENCOUNTER = 0x0B
    MOUNTAIN_TOP = 0x0C

    POND_WATER = 0x10
    FAST_WATER = 0x11
    DEEP_WATER = 0x12
    WATERFALL = 0x13
    OCEAN_WATER = 0x15
    PUDDLE = 0x16
    SHALLOW_WATER = 0x17
    UNDERWATER_BLOCKED_ABOVE = 0x19
    UNUSED_WATER = 0x1A
    CYCLING_ROAD_WATER = 0x1B

    STRENGTH_BUTTON = 0x20  # switch tile pushed by a Strength boulder landing on it
    SAND = 0x21
    ICE = 0x23
    ROCK_STAIRS = 0x2A
    SAND_CAVE = 0x2B

    IMPASSABLE_EAST = 0x30
    IMPASSABLE_WEST = 0x31
    IMPASSABLE_NORTH = 0x32
    IMPASSABLE_SOUTH = 0x33
    IMPASSABLE_NORTHEAST = 0x34
    IMPASSABLE_NORTHWEST = 0x35
    IMPASSABLE_SOUTHEAST = 0x36
    IMPASSABLE_SOUTHWEST = 0x37
    JUMP_EAST = 0x38
    JUMP_WEST = 0x39
    JUMP_NORTH = 0x3A
    JUMP_SOUTH = 0x3B

    WALK_EAST = 0x40
    WALK_WEST = 0x41
    WALK_NORTH = 0x42
    WALK_SOUTH = 0x43

    EASTWARD_CURRENT = 0x50
    WESTWARD_CURRENT = 0x51
    NORTHWARD_CURRENT = 0x52
    SOUTHWARD_CURRENT = 0x53

    CAVE_DOOR = 0x60
    LADDER = 0x61
    EAST_ARROW_WARP = 0x62
    WEST_ARROW_WARP = 0x63
    NORTH_ARROW_WARP = 0x64
    SOUTH_ARROW_WARP = 0x65
    FALL_WARP = 0x66
    REGULAR_WARP = 0x67
    WARP_DOOR = 0x69
    UP_ESCALATOR = 0x6A
    DOWN_ESCALATOR = 0x6B

    COUNTER = 0x80
    BOOKSHELF = 0x81
    POKEMART_SHELF = 0x82
    PC = 0x83
    SIGNPOST = 0x84
    REGION_MAP = 0x85
    TELEVISION = 0x86
    POKEMON_CENTER_SIGN = 0x87
    POKEMART_SIGN = 0x88
    # 0x89-0xA3: the rest of FRLG's interactive-furniture behaviours.
    # Each has a dedicated MetatileBehavior_Is* check in
    # pret/src/metatile_behavior.c, which is what scripts use to fire
    # press-A dialogue for these tiles.
    CABINET = 0x89
    KITCHEN = 0x8A
    DRESSER = 0x8B
    SNACKS = 0x8C
    CABLE_CLUB_WIRELESS_MONITOR = 0x8D
    BATTLE_RECORDS = 0x8E
    QUESTIONNAIRE = 0x8F
    FOOD = 0x90
    INDIGO_PLATEAU_SIGN_1 = 0x91
    INDIGO_PLATEAU_SIGN_2 = 0x92
    BLUEPRINTS = 0x93
    PAINTING = 0x94
    POWER_PLANT_MACHINE = 0x95
    TELEPHONE = 0x96
    COMPUTER = 0x97
    ADVERTISING_POSTER = 0x98
    FOOD_SMELLS_TASTY = 0x99
    TRASH_BIN = 0x9A
    CUP = 0x9B
    BLINKING_LIGHTS = 0x9E
    NEATLY_LINED_UP_TOOLS = 0x9F
    IMPRESSIVE_MACHINE = 0xA0
    VIDEO_GAME = 0xA1
    BURGLARY = 0xA2
    TRAINER_TOWER_MONITOR = 0xA3


# Which behaviours are walkable from a given direction. This is a starting
# point; the real collision check combines MAPGRID_COLLISION bits with the
# behaviour. The pathfinder (phase 2) will own the authoritative version --
# keep this minimal for now.
LEDGE_JUMP_BEHAVIORS = {
    MetatileBehavior.JUMP_EAST,
    MetatileBehavior.JUMP_WEST,
    MetatileBehavior.JUMP_NORTH,
    MetatileBehavior.JUMP_SOUTH,
}

IMPASSABLE_BEHAVIORS = {
    MetatileBehavior.IMPASSABLE_EAST,
    MetatileBehavior.IMPASSABLE_WEST,
    MetatileBehavior.IMPASSABLE_NORTH,
    MetatileBehavior.IMPASSABLE_SOUTH,
    MetatileBehavior.IMPASSABLE_NORTHEAST,
    MetatileBehavior.IMPASSABLE_NORTHWEST,
    MetatileBehavior.IMPASSABLE_SOUTHEAST,
    MetatileBehavior.IMPASSABLE_SOUTHWEST,
}

# "Real" water -- impassable on foot, requires Surf (or is a Waterfall).
# Mirrors pret/src/metatile_behavior.c sBehaviorSurfable[]. SHALLOW_WATER
# (0x17) and PUDDLE (0x16) are NOT in here: the player walks straight
# through both, the engine just plays a splash effect (see
# MetatileBehavior_IsShallowFlowingWater / MetatileBehavior_IsPuddle).
WATER_BEHAVIORS = {
    MetatileBehavior.POND_WATER,
    MetatileBehavior.FAST_WATER,
    MetatileBehavior.DEEP_WATER,
    MetatileBehavior.WATERFALL,
    MetatileBehavior.OCEAN_WATER,
    MetatileBehavior.UNUSED_WATER,
    MetatileBehavior.CYCLING_ROAD_WATER,
    MetatileBehavior.EASTWARD_CURRENT,
    MetatileBehavior.WESTWARD_CURRENT,
    MetatileBehavior.NORTHWARD_CURRENT,
    MetatileBehavior.SOUTHWARD_CURRENT,
}

WARP_BEHAVIORS = {
    MetatileBehavior.CAVE_DOOR,
    MetatileBehavior.LADDER,
    MetatileBehavior.EAST_ARROW_WARP,
    MetatileBehavior.WEST_ARROW_WARP,
    MetatileBehavior.NORTH_ARROW_WARP,
    MetatileBehavior.SOUTH_ARROW_WARP,
    MetatileBehavior.FALL_WARP,
    MetatileBehavior.REGULAR_WARP,
    MetatileBehavior.WARP_DOOR,
    MetatileBehavior.UP_ESCALATOR,
    MetatileBehavior.DOWN_ESCALATOR,
}


# -----------------------------------------------------------------------------
# Human-readable decoders for status / weather bitmasks.
# -----------------------------------------------------------------------------


def decode_status1(status: int) -> str:
    """Decode primary status condition to a readable string."""
    if status == 0:
        return "healthy"
    parts: list[str] = []
    sleep_turns = status & STATUS1_SLEEP_MASK
    if sleep_turns:
        parts.append(f"sleep({sleep_turns})")
    if status & STATUS1_POISON:
        parts.append("poison")
    if status & STATUS1_BURN:
        parts.append("burn")
    if status & STATUS1_FREEZE:
        parts.append("freeze")
    if status & STATUS1_PARALYSIS:
        parts.append("paralysis")
    if status & STATUS1_TOXIC_POISON:
        parts.append("toxic")
    return ", ".join(parts) if parts else "healthy"


def decode_weather(weather: int) -> str:
    """Decode battle weather bitmask to a readable string."""
    if weather == 0:
        return "none"
    parts: list[str] = []
    if weather & B_WEATHER_RAIN:
        parts.append("rain")
    if weather & B_WEATHER_SANDSTORM:
        parts.append("sandstorm")
    if weather & B_WEATHER_SUN:
        parts.append("sun")
    if weather & B_WEATHER_HAIL:
        parts.append("hail")
    return ", ".join(parts) if parts else "none"


# GBA memory region bounds -- useful for sanity-checking pointer reads.
EWRAM_START = 0x02000000
EWRAM_END = 0x02040000
IWRAM_START = 0x03000000
IWRAM_END = 0x03008000
ROM_START = 0x08000000
ROM_END = 0x0A000000


def is_plausible_ewram_ptr(value: int) -> bool:
    """Cheap sanity check for a pointer that should live in EWRAM."""
    return EWRAM_START <= value < EWRAM_END


def is_plausible_iwram_ptr(value: int) -> bool:
    """Cheap sanity check for a pointer that should live in IWRAM."""
    return IWRAM_START <= value < IWRAM_END


def is_plausible_rom_ptr(value: int) -> bool:
    """Cheap sanity check for a pointer that should live in ROM."""
    return ROM_START <= value < ROM_END
