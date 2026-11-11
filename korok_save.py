"""korok save engine: read and edit BotW `game_data.sav` files.

Design notes
------------
* The save only stores (hash, value) records, so the *names and types* come
  from the user's own game files: Pack/Bootup.pack -> GameData/gamedata.ssarc.
  Nothing is copied from third-party flag lists.
* Editing is done by PATCHING bytes in place. A record that isn't touched is
  never re-encoded, so unknown / future flags survive byte for byte.
* Layout (header size, endianness) is auto-detected by checking how many
  record ids match the schema, instead of being trusted blindly.
* Every write: size must be unchanged, backup first, atomic replace, re-read
  and verify.
"""
import binascii
import json
import os
import pathlib
import shutil
import struct
import time
import zlib

try:
    import oead
except ImportError:  # editor is disabled without oead
    oead = None

BOOTUP_REL = ("content", "Pack", "Bootup.pack")
GAMEDATA_SSARC = "GameData/gamedata.ssarc"

# words per element for each fixed-width kind
STRING_WORDS = {"string32": 8, "string64": 16, "string256": 64}
VECTOR_WORDS = {"vector2f": 2, "vector3f": 3, "vector4f": 4}
KNOWN_KINDS = {"bool", "s32", "f32", "string32", "string64", "string256",
               "vector2f", "vector3f", "vector4f"}


def available() -> bool:
    return oead is not None


def crc32_name(name: str) -> int:
    return zlib.crc32(name.encode("utf-8")) & 0xFFFFFFFF


def _kind_of(key: str):
    """'string64_array_data' -> ('string64', True)."""
    k = key
    if k.startswith("revival_"):
        k = k[len("revival_"):]
    if k.endswith("_data"):
        k = k[:-5]
    is_array = False
    if k.endswith("_array"):
        k = k[:-6]
        is_array = True
    return k, is_array


# ---------------------------------------------------------------- schema --
class FlagDef:
    __slots__ = ("name", "hash", "kind", "is_array", "init")

    def __init__(self, name, h, kind, is_array, init=None):
        self.name = name
        self.hash = h
        self.kind = kind
        self.is_array = is_array
        self.init = init


class Schema:
    def __init__(self):
        self.by_hash = {}
        self.by_name = {}

    def add(self, fd: FlagDef):
        self.by_hash[fd.hash] = fd
        self.by_name[fd.name] = fd

    def __len__(self):
        return len(self.by_hash)

    # --- loading
    @classmethod
    def from_bootup(cls, bootup_path) -> "Schema":
        if oead is None:
            raise RuntimeError("The save editor needs the 'oead' package (pip install oead).")
        data = pathlib.Path(bootup_path).read_bytes()
        outer = oead.Sarc(data)
        inner = None
        for f in outer.get_files():
            if f.name.replace("\\", "/").lstrip("/") == GAMEDATA_SSARC:
                inner = bytes(f.data)
                break
        if inner is None:
            raise RuntimeError(f"{GAMEDATA_SSARC} not found inside {bootup_path}")
        if inner[:4] == b"Yaz0":
            inner = bytes(oead.yaz0.decompress(inner))
        return cls._from_gamedata_sarc(oead.Sarc(inner))

    @classmethod
    def _from_gamedata_sarc(cls, sarc) -> "Schema":
        sch = cls()
        for f in sarc.get_files():
            if not f.name.endswith(".bgdata"):
                continue
            try:
                root = oead.byml.from_binary(bytes(f.data))
            except Exception:
                continue
            for key, flags in root.items():
                kind, is_arr = _kind_of(str(key))
                for fl in flags:
                    try:
                        name = str(fl["DataName"])
                        h = int(fl["HashValue"].v if hasattr(fl["HashValue"], "v") else fl["HashValue"]) & 0xFFFFFFFF
                    except Exception:
                        continue
                    sch.add(FlagDef(name, h, kind, is_arr or "array" in str(key)))
        return sch

    # --- caching (parsing ~10k flags is slowish)
    def to_json(self) -> str:
        return json.dumps([[f.name, f.hash, f.kind, f.is_array] for f in self.by_hash.values()])

    @classmethod
    def from_json(cls, text: str) -> "Schema":
        sch = cls()
        for n, h, k, a in json.loads(text):
            sch.add(FlagDef(n, h, k, a))
        return sch


def find_bootup(vanilla_roots: dict):
    """Locate Bootup.pack in the update/base content folders."""
    for root in vanilla_roots.get("content", []):
        p = pathlib.Path(root) / "Pack" / "Bootup.pack"
        if p.exists():
            return p
    return None


def load_schema(vanilla_roots: dict, cache_file=None) -> Schema:
    boot = find_bootup(vanilla_roots)
    if boot is None:
        raise RuntimeError("Couldn't find Pack/Bootup.pack in your Cemu game files.")
    stamp = f"{boot}|{boot.stat().st_size}|{int(boot.stat().st_mtime)}"
    if cache_file:
        try:
            c = json.loads(pathlib.Path(cache_file).read_text(encoding="utf-8"))
            if c.get("stamp") == stamp:
                return Schema.from_json(c["schema"])
        except Exception:
            pass
    sch = Schema.from_bootup(boot)
    if len(sch) == 0:
        raise RuntimeError("Bootup.pack contained no flag definitions.")
    if cache_file:
        try:
            pathlib.Path(cache_file).write_text(
                json.dumps({"stamp": stamp, "schema": sch.to_json()}), encoding="utf-8")
        except Exception:
            pass
    return sch


# ------------------------------------------------------------- save file --
class SaveError(Exception):
    pass


class SaveFile:
    """A parsed game_data.sav. `raw` is the authoritative byte buffer."""

    def __init__(self, path, raw: bytes, schema: Schema):
        self.path = pathlib.Path(path) if path else None
        self.raw = bytearray(raw)
        self.original = bytes(raw)
        self.schema = schema
        self.big = True
        self.header_len = 12
        self.runs = {}          # hash -> [record offsets]
        self.order = []         # hashes in file order
        self.match_ratio = 0.0
        self._detect_and_index()

    # --- parsing
    @classmethod
    def load(cls, path, schema: Schema) -> "SaveFile":
        return cls(path, pathlib.Path(path).read_bytes(), schema)

    def _score(self, header, big, limit=4000):
        n = len(self.raw)
        if n < header + 8 or (n - header) % 8 not in (0, 4):   # 4 = trailing terminator word
            return 0.0
        fmt = ">I" if big else "<I"
        total = (n - header) // 8
        look = min(total, limit)
        hit = 0
        by_hash = self.schema.by_hash
        for i in range(look):
            h = struct.unpack_from(fmt, self.raw, header + i * 8)[0]
            if h in by_hash:
                hit += 1
        return hit / look

    def diagnose(self) -> str:
        """Plain-text facts about why a file didn't match (for bug reports)."""
        raw, by_hash = self.raw, self.schema.by_hash
        lines = [f"file size: {len(raw)} bytes", f"schema flags: {len(by_hash)}",
                 "first 48 bytes: " + bytes(raw[:48]).hex(" ")]
        sample = list(by_hash)[:3]
        lines.append("sample schema hashes: " + ", ".join(f"{h:08x}" for h in sample))
        for big in (True, False):
            counts = {}
            fmt = ">I" if big else "<I"
            for off in range(0, len(raw) - 3, 4):
                if struct.unpack_from(fmt, raw, off)[0] in by_hash:
                    counts[off % 8] = counts.get(off % 8, 0) + 1
            lines.append(f"{'big' if big else 'little'}-endian id hits by (offset mod 8): {counts or 'none'}")
        return "\n".join(lines)

    def _detect_and_index(self):
        best = (0.0, 12, True)
        for big in (True, False):
            for header in range(0, 65, 4):
                s = self._score(header, big)
                if s > best[0] + 1e-9:
                    best = (s, header, big)
        score, self.header_len, self.big = best
        if score < 0.5:
            raise SaveError(
                "This doesn't look like a BotW game_data.sav that matches your game "
                f"data (only {score:.0%} of its records are known). Not touching it.\n\n"
                + self.diagnose())
        self.match_ratio = self._score(self.header_len, self.big, limit=10**9)
        fmt = ">I" if self.big else "<I"
        total = (len(self.raw) - self.header_len) // 8
        self.runs.clear()
        self.order.clear()
        for i in range(total):
            off = self.header_len + i * 8
            h = struct.unpack_from(fmt, self.raw, off)[0]
            if h not in self.runs:
                self.runs[h] = []
                self.order.append(h)
            self.runs[h].append(off)

    @property
    def _u(self):
        return ">I" if self.big else "<I"

    @property
    def _i(self):
        return ">i" if self.big else "<i"

    @property
    def _f(self):
        return ">f" if self.big else "<f"

    # --- element access
    def _words(self, offs):
        return [self.raw[o + 4:o + 8] for o in offs]

    def _elem_words(self, fd: FlagDef) -> int:
        if fd.kind in STRING_WORDS:
            return STRING_WORDS[fd.kind]
        if fd.kind in VECTOR_WORDS:
            return VECTOR_WORDS[fd.kind]
        return 1

    def count(self, name: str) -> int:
        fd = self.schema.by_name.get(name)
        if fd is None or fd.hash not in self.runs:
            return 0
        return len(self.runs[fd.hash]) // self._elem_words(fd)

    def has(self, name: str) -> bool:
        fd = self.schema.by_name.get(name)
        return bool(fd and fd.hash in self.runs)

    def get_all(self, name: str) -> list:
        fd = self.schema.by_name.get(name)
        if fd is None or fd.hash not in self.runs:
            raise KeyError(name)
        offs = self.runs[fd.hash]
        w = self._elem_words(fd)
        out = []
        for i in range(0, len(offs) - len(offs) % w, w):
            out.append(self._decode(fd, offs[i:i + w]))
        return out

    def get(self, name: str, index: int = 0):
        return self.get_all(name)[index]

    def _decode(self, fd, offs):
        k = fd.kind
        if k in STRING_WORDS:
            b = b"".join(bytes(self.raw[o + 4:o + 8]) for o in offs)
            b = b.split(b"\0", 1)[0]
            return b.decode("utf-8", "replace")
        if k in VECTOR_WORDS:
            return tuple(struct.unpack_from(self._f, self.raw, o + 4)[0] for o in offs)
        o = offs[0] + 4
        if k == "bool":
            return bool(struct.unpack_from(self._u, self.raw, o)[0])
        if k == "s32":
            return struct.unpack_from(self._i, self.raw, o)[0]
        if k == "f32":
            return struct.unpack_from(self._f, self.raw, o)[0]
        return struct.unpack_from(self._u, self.raw, o)[0]     # unknown: raw u32

    def set(self, name: str, value, index: int = 0):
        fd = self.schema.by_name.get(name)
        if fd is None or fd.hash not in self.runs:
            raise KeyError(f"{name} isn't in this save")
        offs = self.runs[fd.hash]
        w = self._elem_words(fd)
        if (index + 1) * w > len(offs):
            raise IndexError(f"{name}[{index}] out of range")
        mine = offs[index * w:(index + 1) * w]
        k = fd.kind
        if k in STRING_WORDS:
            b = str(value).encode("utf-8")
            width = STRING_WORDS[k] * 4
            if len(b) >= width:
                raise ValueError(f"Text too long for {name} (max {width - 1} bytes).")
            b = b + b"\0" * (width - len(b))
            for j, o in enumerate(mine):
                self.raw[o + 4:o + 8] = b[j * 4:(j + 1) * 4]
        elif k in VECTOR_WORDS:
            vals = tuple(value)
            if len(vals) != w:
                raise ValueError(f"{name} needs {w} numbers.")
            for o, v in zip(mine, vals):
                struct.pack_into(self._f, self.raw, o + 4, float(v))
        elif k == "bool":
            struct.pack_into(self._u, self.raw, mine[0] + 4, 1 if value else 0)
        elif k == "s32":
            v = int(value)
            if not -2**31 <= v < 2**31:
                raise ValueError("Number out of 32-bit range.")
            struct.pack_into(self._i, self.raw, mine[0] + 4, v)
        elif k == "f32":
            struct.pack_into(self._f, self.raw, mine[0] + 4, float(value))
        else:
            struct.pack_into(self._u, self.raw, mine[0] + 4, int(value) & 0xFFFFFFFF)

    # --- browsing
    def known_flags(self):
        """Yield (FlagDef, element_count) for every flag present in this save, file order."""
        for h in self.order:
            fd = self.schema.by_hash.get(h)
            if fd is None:
                continue
            yield fd, len(self.runs[h]) // self._elem_words(fd)

    def unknown_hash_count(self) -> int:
        return sum(1 for h in self.order if h not in self.schema.by_hash)

    def dirty(self) -> bool:
        return bytes(self.raw) != self.original

    def changed_offsets(self):
        a, b = self.original, self.raw
        return [i for i in range(len(a)) if a[i] != b[i]]

    def revert(self):
        self.raw = bytearray(self.original)

    # --- saving
    def save(self, path=None, backup_dir=None) -> pathlib.Path:
        """Verify, back up the existing file, then atomically write. Returns backup path (or None)."""
        path = pathlib.Path(path or self.path)
        if len(self.raw) != len(self.original):
            raise SaveError("Internal error: save size changed. Refusing to write.")
        # structure must be identical: same record ids at every record offset
        fmt = self._u
        for h, offs in self.runs.items():
            for o in offs:
                if struct.unpack_from(fmt, self.raw, o)[0] != h:
                    raise SaveError("Internal error: a record id changed. Refusing to write.")
        if bytes(self.raw[:self.header_len]) != self.original[:self.header_len]:
            raise SaveError("Internal error: header changed. Refusing to write.")

        backup = None
        if path.exists() and backup_dir:
            backup_dir = pathlib.Path(backup_dir)
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            backup = backup_dir / f"{path.parent.name}_{path.name}.{stamp}.bak"
            shutil.copy2(path, backup)

        tmp = path.with_name(path.name + ".korok_tmp")
        tmp.write_bytes(bytes(self.raw))
        # read back and verify before swapping in
        check = SaveFile(tmp, tmp.read_bytes(), self.schema)
        if bytes(check.raw) != bytes(self.raw):
            tmp.unlink(missing_ok=True)
            raise SaveError("Verification failed after writing. Original left untouched.")
        os.replace(tmp, path)
        self.original = bytes(self.raw)
        self.path = path
        return backup


# ------------------------------------------------- friendly core editing --
# Candidate flag names for the "easy" tab; only those present in the user's
# own game data are shown. Everything else is reachable in the flag browser.
CORE_SCALARS = [
    ("Rupees", ["CurrentRupee"], "int", 0, 999999),
    ("Max hearts (quarter-hearts, 4 = 1 heart)", ["MaxHartValue"], "int", 12, 120),
    ("Max stamina", ["MaxStamina"], "float", 0, 3000),
    ("Korok seeds", ["KorokNutsNum", "KorokSeedNum"], "int", 0, 900),
    ("Mamo / Monster parts", ["MonsterShopPoint"], "int", 0, 9999999),
    ("Playtime (seconds)", ["PlayTime"], "int", 0, 2**31 - 1),
    ("Map / location name", ["Location"], "str", 0, 0),
    ("Player position", ["PlayerPosition"], "vec", 0, 0),
]

INVENTORY_NAMES = "PorchItem"
INVENTORY_VALUE = "PorchItem_Value1"
INVENTORY_EQUIP = "PorchItem_EquipFlag"


def core_scalar_name(save: SaveFile, candidates):
    for c in candidates:
        if save.has(c):
            return c
    return None


def inventory_rows(save: SaveFile):
    """[(slot, item name, value, equipped)] for non-empty inventory slots."""
    if not save.has(INVENTORY_NAMES):
        return []
    names = save.get_all(INVENTORY_NAMES)
    vals = save.get_all(INVENTORY_VALUE) if save.has(INVENTORY_VALUE) else [0] * len(names)
    eq = save.get_all(INVENTORY_EQUIP) if save.has(INVENTORY_EQUIP) else [False] * len(names)
    rows = []
    for i, n in enumerate(names):
        if n:
            rows.append((i, n, vals[i] if i < len(vals) else 0, eq[i] if i < len(eq) else False))
    return rows


def free_inventory_slots(save: SaveFile):
    if not save.has(INVENTORY_NAMES):
        return []
    return [i for i, n in enumerate(save.get_all(INVENTORY_NAMES)) if not n]
