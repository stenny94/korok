"""BotW's resource size table (RSTB), and UKMM's way of updating it.

The game reserves memory for each resource using this table, so every file a
mod changes needs a large enough entry. When a deploy rebuilds files, UKMM
works out a size for each one (rstb::calc, by GingerAvalanche/NiceneNerd) and
the table entry is raised to it; files whose size can't be worked out lose
their entry, which makes the game fall back to its own estimate.

Wii U format (big-endian):
  "RSTB", u32 crc_count, u32 name_count,
  crc_count  * (u32 crc32(name), u32 size)      sorted by crc
  name_count * (char[128] name,  u32 size)      for crc collisions

Port of rstb-rust's calc module and UKMM's deploy step (GPL-3.0).
"""
import pathlib
import struct
import zlib

import oead

RSTB_PATH = "System/Resource/ResourceSizeTable.product.srsizetable"


def crc_of(name: str) -> int:
    return zlib.crc32(name.encode("utf-8")) & 0xFFFFFFFF


class SizeTable:
    def __init__(self, raw: bytes):
        if raw[:4] == b"Yaz0":
            raw = bytes(oead.yaz0.decompress(raw))
        if raw[:4] != b"RSTB":
            raise ValueError("Not an RSTB file")
        crc_n, name_n = struct.unpack(">II", raw[4:12])
        off = 12
        self.crc = dict(struct.iter_unpack(">II", raw[off:off + 8 * crc_n]))
        off += 8 * crc_n
        self.names = {}
        for _ in range(name_n):
            name = raw[off:off + 128].split(b"\0", 1)[0].decode("utf-8", "replace")
            self.names[name] = struct.unpack_from(">I", raw, off + 128)[0]
            off += 132

    def get(self, name: str):
        if name in self.names:
            return self.names[name]
        return self.crc.get(crc_of(name))

    def set(self, name: str, size: int):
        if name in self.names:
            self.names[name] = size
        else:
            self.crc[crc_of(name)] = size

    def remove(self, name: str):
        if name in self.names:
            del self.names[name]
        else:
            self.crc.pop(crc_of(name), None)

    def to_binary(self) -> bytes:
        out = bytearray(b"RSTB")
        out += struct.pack(">II", len(self.crc), len(self.names))
        for crc in sorted(self.crc):
            out += struct.pack(">II", crc, self.crc[crc])
        for name, size in self.names.items():
            out += name.encode("utf-8")[:128].ljust(128, b"\0") + struct.pack(">I", size)
        return bytes(out)

    def apply(self, updates: dict):
        """Deployer::apply_rstb: raise entries (never lower them); None removes the entry."""
        for canon, size in updates.items():
            if size is None:
                self.remove(canon)
            else:
                cur = self.get(canon)
                if cur is None or cur < size:
                    self.set(canon, size)


# ------------------------------------------------------------------ rstb::calc (Wii U)
PARSE_CONST = 0xE4
EXCLUDE_EXTS = {"pack", "bgdata", "txt", "bgsvdata", "yml", "msbt", "bat", "ini", "png", "bfstm", "py", "sh"}
EXCLUDE_NAMES = {"ActorInfo.product.byml"}

_STRIP_S = {"sarc": "sarc", "Tex.sbfres": "Tex.bfres", "Tex1.sbfres": "Tex1.bfres", "Tex2.sbfres": "Tex2.bfres",
            "product.sbyml": "product.byml", "hght.sstera": "hght.stera", "mate.sstera": "mate.stera",
            "grass.extm.sstera": "grass.extm.stera", "water.extm.sstera": "water.extm.stera"}
_CANON_TO_FACTORY = {"product.byml": "byml", "hght.stera": "stera", "mate.stera": "stera",
                     "grass.extm.stera": "stera", "water.extm.stera": "stera"}

# Wii U: extension -> (factory, (size, alignment, parse size or None when it must be parsed))
_FACTORY_EXTS = {
    "sarc": "Sarc", "pack": "Sarc", "bactorpack": "Sarc", "bmodelsh": "Sarc", "stats": "Sarc", "stera": "Sarc",
    "Tex.bfres": "Tex", "Tex1.bfres": "Tex", "Tex2.bfres": "Tex", "bactcapt": "ActorCapture",
    "baiprog": "AIProgram", "baischedule": "AISchedule", "baniminfo": "AnimInfo", "bars": "AudioResource",
    "bas": "AnimSeq", "baslist": "AnimSeqList", "bassetting": "AnimSeqSetting", "batcl": "AttentionClient",
    "batcllist": "AttentionClientList", "batpl": "Batpl", "bnfprl": "Batpl", "bawareness": "Awareness",
    "bbonectrl": "BoneControl", "bcamanim": "CamAnim", "bchemical": "Chemical", "bdemo": "Demo",
    "bdmgparam": "DamageParams", "bdrop": "DropTable", "beventpack": "EventPack", "bfevfl": "EventFlow",
    "bfevtm": "Timeline", "bfres": "CafeResource", "bgdata": "GameData", "bgparamlist": "GeneralParamList",
    "bgsvdata": "SaveData", "bitemico": "ItemIcon", "blifecondition": "LifeCondition", "blod": "Lod",
    "bmaptex": "MapTexture", "bmodellist": "ModelList", "bmscdef": "MusicDefinition", "bphysics": "Physics",
    "bphyssb": "PhysicsSupportBone", "bplacement": "Placement", "brecipe": "Recipe", "brgbw": "RagdollBlendWeight",
    "brgconfig": "RagdollConfig", "brgconfiglist": "RagdollConfigList", "brgcon": "RagdollControl",
    "bshop": "ShopTable", "bstftex": "MiscTexture", "bmapopen": "MiscTexture", "breviewtex": "MiscTexture",
    "bumii": "UMii", "bxml": "ActorLink", "byaml": "Byaml", "byml": "BinaryYAML", "esetlist": "EffectSettingList",
    "hkcl": "HavokCloth", "hknm2": "HavokNavMesh", "hkrb": "HavokRigidBody", "hkrg": "HavokRagdoll",
    "hksc": "HavokStaticCompound", "hks": "Lua", "lua": "Lua", "hktmrb": "HavokTmrb", "jpg": "Jpeg",
    "mubin": "Map",
}
# size, alignment, parse size. Tex (Tex1/Tex2 texture files) is aligned to 0x2000: rstb-rust has
# 0x4 there, but the game's own table holds exactly file size + 0x2000 for these (checked on
# Armor_046.Tex1/Tex2), and entries that small leave textures garbled in game.
# (int = simple, None = complex: needs parsing, which korok
# doesn't port, so it is estimated instead as UKMM does when parsing gives nothing)
_FACTORY_INFO = {
    "Sarc": (0x3C, 0x80, 0), "EventPack": (0x3C, 0x80, 0), "Tex": (0x20, 0x2000, 0),
    "CafeResource": (0x13C, 0x1000, None), "CamAnim": (0x2C, 0x2000, None), "Batpl": (0x24, 0x4, 0),
    "Placement": (0x14, 0x4, 0), "Lua": (0x14, 0x4, 0), "ActorCapture": (0x3B4, 0x4, 0),
    "ItemIcon": (0xD0, 0x2000, 0), "Jpeg": (0x174, 0x2000, 0), "MapTexture": (0xD0, 0x2000, 0),
    "MiscTexture": (0xD0, 0x2000, 0), "GameData": (0xCC, 0x4, 0), "SaveData": (0x14, 0x4, 0),
    "HavokNavMesh": (0x28, 0x4, ("complex", 0x174)), "MusicDefinition": (0x1FC, 0x4, None),
    "AudioResource": (0x84, 0x80, None), "ActorLink": (0, 0x4, None), "GeneralParamList": (0, 0x4, None),
    "ModelList": (0, 0x4, None), "AnimSeqList": (0, 0x4, None), "AIProgram": (0, 0x4, None),
    "Physics": (0, 0x4, None), "Chemical": (0, 0x4, None), "AnimSeq": (0x2D0, 0x4, None),
    "AttentionClientList": (0x2E4, 0x4, None), "AttentionClient": (0x344, 0x4, None),
    "AISchedule": (0x244, 0x4, 0x18), "DamageParams": (0x9F0, 0x4, 0x40C), "RagdollConfigList": (0, 0x4, None),
    "RagdollConfig": (0x2ACC, 0x4, 0x10), "RagdollBlendWeight": (0x248, 0x4, None),
    "Awareness": (0x70C, 0x4, 0x10), "DropTable": (0, 0x4, None), "ShopTable": (0, 0x4, None),
    "Recipe": (0, 0x4, None), "Lod": (0x2CC, 0x4, 0x10), "BoneControl": (0x564, 0x4, None),
    "LifeCondition": (0, 0x4, None), "UMii": (0x244, 0x4, 0x18), "AnimInfo": (0, 0x4, None),
    "Byaml": (0x14, 0x4, 0), "BinaryYAML": (0x20, 0x4, 0x3C), "AnimSeqSetting": (0x1D8, 0x4, None),
    "HavokRigidBody": (0x14, 0x4, 0x8), "HavokRagdoll": (0x14, 0x4, 0x8), "PhysicsSupportBone": (0, 0x4, None),
    "HavokCloth": (0xB8, 0x4, None), "HavokStaticCompound": (0xE8, 0x4, ("complex", 0x73E4)),
    "HavokTmrb": (0x28, 0x4, None), "RagdollControl": (0x28, 0x4, None), "EffectSettingList": (0x20, 0x4000, 0),
    "Demo": (0x6CC, 0x4, 0x10), "EventFlow": (0x24, 0x4, 0x38), "Timeline": (0x24, 0x4, 0x38),
    "Map": (0x20, 0x4, 0x1C), "Base": (0x20, 0x4, 0),
}


def _round32(n):
    return (n + 31) & -32


def _round64(n):
    return (n + 63) & -64


def _align(n, a):
    return (n + (a - 1)) & -a


def _factory(name: str):
    base = name.rpartition("/")[2] if "/" in name else name
    full = name
    dot = full.find(".")
    if dot < 0:
        return None
    raw_ext = full[dot + 1:]
    ext = _STRIP_S.get(raw_ext, raw_ext[1:] if raw_ext.startswith("s") else raw_ext)
    ext = _CANON_TO_FACTORY.get(ext, ext)
    del base
    return _FACTORY_EXTS.get(ext, "Base")


def _f32(x):
    return struct.unpack("<f", struct.pack("<f", x))[0]


def _estimate_bfres(filesize):
    for limit, m in ((500, 7.0), (750, 5.0), (1250, 4.0), (2000, 3.5), (400_000, 2.25), (600_000, 2.1),
                     (1_000_000, 1.95), (1_500_000, 1.85), (3_000_000, 1.66)):
        if filesize < limit:
            return int(_f32(filesize * m))
    return int(_f32(filesize * 1.45))


_AAMP_MULT = {
    "baiprog": ((380, 7.0), (400, 6.0), (450, 5.5), (600, 5.0), (1000, 4.0), (1750, 3.5), (None, 3.0)),
    "bas": ((100, 20.0), (200, 12.5), (300, 10.0), (600, 8.0), (1500, 6.0), (2000, 5.5), (15000, 5.0), (None, 4.5)),
    "baslist": ((100, 15.0), (200, 10.0), (300, 8.0), (500, 6.0), (800, 5.0), (4000, 4.0), (None, 3.5)),
    "bdrop": ((200, 8.5), (250, 7.0), (350, 6.0), (450, 5.25), (850, 4.5), (None, 4.0)),
    "bgparamlist": ((100, 20.0), (150, 12.0), (250, 10.0), (350, 8.0), (450, 7.0), (None, 6.0)),
    "brecipe": ((100, 12.5), (160, 8.5), (200, 7.5), (215, 7.0), (None, 6.5)),
    "bshop": ((200, 7.25), (400, 6.0), (500, 5.0), (None, 4.05)),
    "bxml": ((350, 6.0), (450, 5.0), (550, 4.5), (650, 4.0), (800, 3.5), (None, 3.0)),
}


def _estimate_aamp(filesize, name):
    size = _f32(filesize * 1.05)
    ext = name.rpartition(".")[2]
    if ext == "bas":
        size = _f32(size * 1.05)
    if ext == "bassetting":
        size = _f32(float(_round32(int(size)) + PARSE_CONST + 0x1D8) + _f32(size * 2.75))
    elif ext == "bdmgparam":
        size = _f32(_f32(_f32(_f32(-0.0018 * size) + 6.6273) * size) + 500.0)
    elif ext == "bphysics":
        size = _f32(float(_round32(int(size)) + 0x4E + 0x324) * max(4.0 * float(int(size / 1388.0)), 3.0))
    else:
        table = _AAMP_MULT.get(ext)
        if table is None:
            return None
        n = int(size)
        for limit, m in table:
            if limit is None or n < limit:
                size = _f32(size * m)
                break
    return int(size)


def _calc(filesize, name, estimate, data=None):
    if "." not in name:
        return None
    rounded = _round64(filesize) if data is not None else _round32(filesize)
    fac = _factory(name)
    size, alignment, parse = _FACTORY_INFO[fac]
    if isinstance(parse, tuple):           # parse size known without parsing
        return rounded + _align(PARSE_CONST + size + parse[1], alignment) if data is not None else (
            _estimate(filesize, name, fac, rounded) if estimate else None)
    if parse is not None:
        if fac in ("EffectSettingList", "Map"):
            return _round32(filesize) + _align(PARSE_CONST + size + parse, alignment)
        return rounded + _align(PARSE_CONST + size + parse, alignment) + (0xE0 if fac == "EventPack" else 0)
    if data is not None and fac in CLASS_SIZE:
        ps = parse_size(fac, data)
        if ps is not None:
            return rounded + _align(PARSE_CONST + CLASS_SIZE[fac] + ps, alignment)
    if not estimate:
        return None
    if data is not None and fac == "AnimInfo":
        return _estimate_aamp(filesize, name)   # parsing failed: rstb::calc falls back to the AAMP guess
    return _estimate(filesize, name, fac, rounded)


def _estimate(filesize, name, fac, rounded):
    if fac == "AnimInfo":
        r = _round32(filesize)
        return int(_f32(r * (1.5 if filesize > 36864 else 4.0))) + PARSE_CONST + 0x24C
    if fac == "CafeResource":
        return _estimate_bfres(filesize)
    return _estimate_aamp(filesize, name)


def estimate_from_bytes(data: bytes, name: str):
    """rstb::calc::estimate_from_slice_and_name (Wii U)."""
    if len(data) < 8:
        return None
    filesize = struct.unpack(">I", data[4:8])[0] if data[:4] == b"Yaz0" else len(data)
    return _calc(filesize, name, True, data)


def calc_from_size(filesize: int, name: str):
    """rstb::calc::calc_from_size_and_name (Wii U)."""
    return _calc(filesize, name, False)


def can_rstb(canon: str) -> bool:
    base = canon.rpartition("/")[2]
    ext = base.rpartition(".")[2] if "." in base else ""
    return ext not in EXCLUDE_EXTS and base not in EXCLUDE_NAMES


def load_vanilla(dump, update_dirs=()):
    """The game's own table (update first)."""
    data = dump.get_bytes(RSTB_PATH) if dump is not None else None
    if data is None:
        for d in update_dirs:
            p = pathlib.Path(d) / RSTB_PATH
            if p.is_file():
                data = p.read_bytes()
                break
    return SizeTable(data) if data is not None else None


def write(table: SizeTable, out_content_dir: pathlib.Path):
    p = pathlib.Path(out_content_dir) / RSTB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(bytes(oead.yaz0.compress(table.to_binary())))
    return p


# ------------------------------------------------------------------ exact sizes (cpp_memsizes)
# Wii U sizes of the game's C++ classes (rstb-rust cpp_classes tests)
ITER = 0x10
P_BOOL = P_INT = P_UINT = P_F32 = 0x10
P_VEC3 = 0x18
P_SAFESTRING = 0x14
CLASS_SIZE = {"ActorLink": 0x4A8, "GeneralParamList": 0x248, "ModelList": 0x500, "AnimSeqList": 0x2F4,
              "AIProgram": 0x30C, "Physics": 0x324, "Chemical": 0x2CC, "DropTable": 0x27C, "ShopTable": 0x27C,
              "Recipe": 0x27C, "RagdollConfigList": 0x2D4, "LifeCondition": 0x35C, "AnimInfo": 0x24C,
              "PhysicsSupportBone": 0x384}
GPARAM_OBJS = {
    "AirWall": 0x34, "AnimalFollowOffset": 0x38, "AnimalUnit": 0x134, "Armor": 0xA8, "ArmorEffect": 0x74,
    "ArmorHead": 0x5C, "ArmorUpper": 0x80, "Arrow": 0x80, "Attack": 0xCC, "AttackInterval": 0x80,
    "AutoGen": 0x58, "Beam": 0x30, "BindActor": 0x44, "BindBone": 0x64, "Bow": 0x2DC, "Bullet": 0x40,
    "Camera": 0xD0, "ChemicalType": 0x48, "ClothReaction": 0x150, "CookSpice": 0x70, "CureItem": 0x64,
    "EatTarget": 0x98, "Enemy": 0x148, "EnemyLevel": 0xE0, "EnemyRace": 0x31C, "EnemyShown": 0x60,
    "Event": 0xC0, "ExtendedEntity": 0x40, "Fish": 0x70, "GelEnemy": 0x130, "General": 0xA8,
    "GiantArmor": 0x48, "GiantArmorSlot": 0x110, "Global": 0x5F0, "Golem": 0x94, "GolemIK": 0x1B0,
    "Grab": 0x110, "Guardian": 0xB4, "GuardianMini": 0x94, "GuardianMiniWeapon": 0x84, "Horse": 0xEC,
    "HorseCreator": 0x48, "HorseObject": 0x40, "HorseRider": 0x184, "HorseTargetedInfo": 0x50,
    "HorseUnit": 0x60, "Insect": 0x30, "Item": 0x94, "LargeSword": 0x228, "Liftable": 0x18C,
    "LumberjackTree": 0x90, "MasterSword": 0xA8, "MonsterShop": 0x40, "Motorcycle": 0x280, "Nest": 0x34,
    "Npc": 0x11C, "NpcEquipment": 0x240, "PictureBook": 0x50, "Player": 0xAC0, "Prey": 0x70, "Rod": 0x114,
    "Rope": 0xC0, "Rupee": 0x30, "Sandworm": 0x1CC, "SeriesArmor": 0x44, "ShiekerStone": 0xA8,
    "Shield": 0x1B4, "SmallSword": 0x228, "Spear": 0x288, "StalEnemy": 0x48, "Swarm": 0x54, "System": 0x44,
    "Traveler": 0x18A8, "WeaponCommon": 0x318, "WeaponOption": 0xB0, "WeaponThrow": 0x68, "WizzRobe": 0xB8,
    "WolfLink": 0x440, "Zora": 0x70}

AIDEF_LOADER = None     # () -> bytes of Actor/AIDef/AIDef_Game.product.byml (set by the deploy)
_AIDEF = None


def _aidef():
    global _AIDEF
    if _AIDEF is None and AIDEF_LOADER is not None:
        data = AIDEF_LOADER()
        _AIDEF = oead.byml.from_binary(data) if data is not None else False
    return _AIDEF or None


def _N(name):
    return oead.aamp.Name(name)


def _o(plist, name):
    return plist.objects.get(_N(name)) if plist is not None else None


def _l(plist, name):
    return plist.lists.get(_N(name)) if plist is not None else None


def _p(obj, name):
    p = obj.params.get(_N(name)) if obj is not None else None
    return None if p is None else p.v


def _int(obj, name):
    v = _p(obj, name)
    if v is None:
        raise KeyError(name)
    return int(v)


def _n(x):
    return len(list(x.items()))


def _table_list(pio, table_size, item_size, header_overhead=0x20):
    total = 0
    header = _o(pio, "Header")
    if header is None:
        return total
    total += header_overhead
    num = _p(header, "TableNum")
    if num is None:
        return total
    num = int(num)
    if num > 0:
        total += ITER + num * table_size
    for i in range(num):
        name = _p(header, f"Table{i + 1:02d}")
        if name is None:
            raise KeyError("table name")
        table = _o(pio, str(name))
        if table is not None:
            items = _int(table, "ColumnNum")
            if items > 0:
                total += ITER + items * item_size
    return total


def _ps_bxml(pio):
    total = 0x34
    tags = _o(pio, "Tags")
    if tags is not None:
        n = _n(tags.params)
        if n > 0:
            total += ITER
        total += 4 * n
    return total


def _ps_baslist(pio):
    total = 0x28
    asd = _l(pio, "ASDefines")
    if asd is not None:
        n = _n(asd.objects)
        if n > 0:
            total += ITER + n * 0x48
            cfd = _l(pio, "CFDefines")
            if cfd is not None:
                nc = _n(cfd.lists)
                if nc > 0:
                    total += ITER + nc * 0xA4
                    for i in range(nc):
                        d = _l(cfd, f"CFDefine_{i}")
                        if d is None:
                            raise KeyError("CFDefine")
                        posts = _l(d, "CFPosts")
                        if posts is not None and _n(posts.objects) > 0:
                            total += ITER + _n(posts.objects) * 0x50
                        exc = _o(d, "CFExcepts")
                        if exc is not None and _n(exc.params) > 0:
                            total += ITER + _n(exc.params) * 0x14
    add = _l(pio, "AddReses")
    if add is not None and _n(add.objects) > 0:
        total += ITER + _n(add.objects) * 0x54
    return total


def _ps_bchemical(pio):
    total = 0
    root = _l(pio, "chemical_root")
    hdr = _o(root, "chemical_header")
    if hdr is not None:
        total += 0x10
        n = _p(hdr, "res_shape_num")
        if n is not None and int(n) > 0:
            total += ITER + int(n) * 0x240 + ITER + int(n) * 0x10C
    return total


def _ps_blife(pio):
    total = 0x40
    for key in ("InvalidWeathers", "InvalidTimes", "DeleteWeathers", "DeleteTimes"):
        o = _o(pio, key)
        if o is not None:
            total += ITER + _n(o.params) * P_SAFESTRING
    return total


def _ps_bmodellist(pio):
    total = 0x1C
    md = _l(pio, "ModelData")
    if md is not None:
        n = _n(md.lists)
        if n > 0:
            total += ITER + n * 0x80
            for i in range(n):
                unit = _l(_l(md, f"ModelData_{i}"), "Unit")
                if unit is not None:
                    nu = min(_n(unit.objects), 8)
                    if nu > 0:
                        total += ITER + nu * 0x44
    at = _l(pio, "AnmTarget")
    if at is not None:
        n = min(_n(at.lists), 8)
        if n > 0:
            total += ITER + n * 0x9C
            for i in range(n):
                part = _l(_l(at, f"AnmTarget_{i}"), "Partial")
                if part is not None and _n(part.objects) > 0:
                    total += ITER + _n(part.objects) * 0x50
    return total


def _ps_brgconfiglist(pio):
    total = 0x8 + ITER + 0x34 * 10
    bl = _l(pio, "BodyParamList")
    if bl is None:
        raise KeyError("BodyParamList")
    if _n(bl.objects) > 0:
        total += ITER + 0x50 * _n(bl.objects)
    return total


def _ps_bgparamlist(pio):
    total = 0xEC + 0x4F * 4
    for name, size in GPARAM_OBJS.items():
        if _o(pio, name) is not None:
            total += ITER + size
    return total


def _ps_bphyssb(pio):
    total = 0x18
    hdr = _o(pio, "support_bone_header")
    if hdr is not None:
        for key, size in (("bone_num", 0x74), ("connection_linear_num", 0x40), ("connection_curve_num", 0xA0),
                          ("output_single_num", 0x3C), ("output_double_num", 0x5C), ("main_bone_num", 0xBC),
                          ("support_bone_num", 0x11C)):
            n = _int(hdr, key)
            if n > 0:
                total += ITER + n * size
    return total


def _ps_bphysics(pio):
    total = 0x120
    ps = _l(pio, "ParamSet")
    hdr = _o(ps, "ParamSetHeader")
    if hdr is None:
        raise KeyError("ParamSetHeader")

    def shapes(holder, num):
        t = 0
        if num > 0:
            t += ITER + num * 0x1C4
            for k in range(num):
                sp = _o(holder, f"ShapeParam_{k}")
                if sp is None:
                    raise KeyError("ShapeParam")
                nv = int(_p(sp, "vertex_num") or 0)
                if nv > 0:
                    t += ITER + nv * P_VEC3
        return t

    nsets = _int(hdr, "use_rigid_body_set_num")
    if nsets > 0:
        total += ITER + nsets * 0x98
        rbs = _l(ps, "RigidBodySet")
        for i in range(nsets):
            s = _l(rbs, f"RigidBodySet_{i}")
            nb = _int(_o(s, "RigidBodySetHeader"), "num")
            if nb > 0:
                total += ITER + nb * 0x338
                for j in range(nb):
                    rb = _l(s, f"RigidBody_{j}")
                    if rb is None:
                        raise KeyError("RigidBody")
                    total += shapes(rb, int(_p(_o(rb, "RigidBodyParam"), "shape_num") or 0))
    if _p(hdr, "use_character_controller"):
        total += 0x300
        cc = _l(ps, "CharacterController")
        nf = _int(_o(cc, "CharacterControllerParam"), "form_num")
        if nf > 0:
            total += ITER + nf * 0x90
            for i in range(nf):
                form = _l(cc, f"Form_{i}")
                if form is None:
                    raise KeyError("Form")
                total += shapes(form, _int(_o(form, "FormHeader"), "shape_num"))
    if _p(hdr, "use_contact_info"):
        total += 0x70
        rh = _o(_l(ps, "RigidContactInfo"), "RigidContactInfoHeader")
        n = _int(rh, "contact_point_info_num")
        if n > 0:
            total += ITER + n * 0x9C
        n = _int(rh, "collision_info_num")
        if n > 0:
            total += ITER + n * 0x8C
    if _p(hdr, "use_support_bone"):
        total += 0x30
    if _p(hdr, "use_ragdoll"):
        total += 0x84
    if _p(hdr, "use_cloth"):
        total += 0x10C
        n = _int(_o(_l(ps, "Cloth"), "ClothHeader"), "cloth_num")
        if n > 0:
            total += ITER + n * 0xC4
    n = _int(hdr, "use_edge_rigid_body_num")
    if n > 0:
        total += 0x2C + ITER + n * 0x58
    return total


_AI_PARAM_SIZES = {"Bool": P_BOOL, "UInt": P_UINT, "Int": P_INT, "Float": P_F32, "String": P_SAFESTRING,
                   "Vec3": P_VEC3}


def _ps_baiprog(pio):
    aidef = _aidef()
    if aidef is None:
        raise KeyError("AIDef")
    total = 0x100

    def defparams(class_name, type_, lst):
        nonlocal total
        sinst = _o(lst, "SInst")
        if sinst is None:
            return
        cdef = aidef[f"{type_}s"][class_name]
        n = _n(sinst.params)
        if n > 0:
            total += n * 4
            for prm in (cdef.get("StaticInstParams") or []):
                if _p(sinst, str(prm["Name"])) is not None:
                    total += _AI_PARAM_SIZES.get(str(prm["Type"]), 0)

    def class_of(child):
        return str(_p(_o(child, "Def"), "ClassName"))

    for type_name in ("AI", "Action"):
        lst = _l(pio, type_name)
        n = _n(lst.lists)
        if n > 0:
            total += n * 0x6C
            for i in range(n):
                child = _l(lst, f"{type_name}_{i}")
                ci = _o(child, "ChildIdx")
                if ci is not None:
                    total += _n(ci.params) * 2
                bi = _o(child, "BehaviorIdx")
                if bi is not None:
                    total += _n(bi.params) * 1
                defparams(class_of(child), type_name, child)
    for type_name, def_size in (("Behavior", 0x54), ("Query", 0x50)):
        lst = _l(pio, type_name)
        n = _n(lst.lists)
        if n > 0:
            total += n * def_size
            for i in range(n):
                child = _l(lst, f"{type_name}_{i}")
                defparams(class_of(child), type_name, child)
    o = _o(pio, "DemoAIActionIdx")
    if o is not None:
        total += _n(o.params) * 2
    o = _o(pio, "DemoBehaviorIdx")
    if o is not None:
        total += _n(o.params)
    return total


def _ps_baniminfo(data):
    b = oead.byml.from_binary(data)
    total = 0xAC
    num_entries = len(b)
    if num_entries < 1:
        return total
    num_anims = num_entries - 1
    allocate = False
    sb = b.get("_sword_blur")
    if sb is not None:
        num = len(sb) - 1
        if num_entries > 1:
            allocate = True
    else:
        num = 0
        num_anims = num_entries
        allocate = True
    if allocate:
        total += num_anims * 0x14
    if num > 0:
        total += 0xC

        def matrices(v):
            if isinstance(v, oead.byml.Hash):
                return len(v["mtx"])
            if isinstance(v, oead.byml.Array):
                return sum(len(x["mtx"]) for x in v)
            return 0
        sub = ITER
        for k, v in sb.items():
            if k == "double_attack_anm_num":
                continue
            iters = 2 if isinstance(v, oead.byml.Array) else (1 if isinstance(v, oead.byml.Hash) else 0)
            sub += ITER * iters * 2 + 0x1C + matrices(v) * 48 * 2
        total += sub
    return total


_PARSERS = {"ActorLink": _ps_bxml, "AnimSeqList": _ps_baslist, "Chemical": _ps_bchemical,
            "LifeCondition": _ps_blife, "ModelList": _ps_bmodellist, "RagdollConfigList": _ps_brgconfiglist,
            "GeneralParamList": _ps_bgparamlist, "PhysicsSupportBone": _ps_bphyssb, "Physics": _ps_bphysics,
            "AIProgram": _ps_baiprog,
            "DropTable": lambda pio: _table_list(pio, 0x88, 0x24),
            "Recipe": lambda pio: _table_list(pio, 0x48, 0x24),
            "ShopTable": lambda pio: _table_list(pio, 0x48, 0x64)}


def parse_size(fac, data):
    """cpp_memsizes::*::parse_size: what the game allocates while parsing, or None."""
    try:
        if fac == "AnimInfo":
            return _ps_baniminfo(unyaz_(data))
        fn = _PARSERS.get(fac)
        if fn is None:
            return None
        return fn(oead.aamp.ParameterIO.from_binary(unyaz_(data)))
    except Exception:
        return None


def unyaz_(data):
    return bytes(oead.yaz0.decompress(data)) if data[:4] == b"Yaz0" else bytes(data)
