"""Reads UKMM mod packages without UKMM.

A UKMM package (.zip) holds meta.yml, manifest.yml, and one entry per changed
game resource, stored under its canonical name (e.g. 'Actor/Pack/X.bactorpack').
Each entry is zstd (with UKMM's dictionary) + CBOR of UKMM's ResourceData:

  {'Binary': bytes}           the whole file (models, textures, sounds...)
  {'Sarc': {alignment, files}} the archive's file list as a diff against vanilla
  {'Mergeable': {Type: data}}  a parsed resource; a *diff* against vanilla when
                               the game has the file, the full resource if not

To deploy, every file listed in the manifest is rebuilt like UKMM's own
ModUnpacker::build_file: vanilla resource + the mod's version, then written
back as a game file. Mergeable types are handled by korok_ukmm_formats; types
not ported yet are reported as unsupported.

Port of UKMM (https://github.com/NiceneNerd/ukmm, GPL-3.0) by Nicene Nerd.
data/ukmm_zsdic and data/ukmm_filemap_wiiu.json.zst are UKMM's own data files.
"""
import collections
import json
import pathlib
import zipfile

import yaml

try:
    import oead
except ImportError:  # optional
    oead = None
try:
    import cbor2
    import zstandard
except ImportError:  # optional
    cbor2 = zstandard = None

DATA_DIR = pathlib.Path(__file__).resolve().parent / "data"
DICT_PATH = DATA_DIR / "ukmm_zsdic"
FILEMAP_PATH = DATA_DIR / "ukmm_filemap_wiiu.json.zst"


class UnsupportedResource(Exception):
    """A resource type that has not been ported from UKMM yet."""

    def __init__(self, kind, canon):
        super().__init__(f"{kind} ({canon})")
        self.kind, self.canon = kind, canon


def available() -> bool:
    return oead is not None and cbor2 is not None and zstandard is not None and DICT_PATH.is_file()


def missing_requirements() -> list:
    out = []
    if oead is None:
        out.append("oead")
    if cbor2 is None:
        out.append("cbor2")
    if zstandard is None:
        out.append("zstandard")
    return out


# ------------------------------------------------------------- names
_CANON_PREFIXES = [
    ("Content/", ""), ("content/", ""), ("atmosphere/titles/", ""), ("atmosphere/contents/", ""),
    ("01007EF00011E000/romfs/", ""), ("01007ef00011e000/romfs/", ""),
    ("01007EF00011E001/romfs", "Aoc/0010"), ("01007EF00011E002/romfs", "Aoc/0010"),
    ("01007EF00011F001/romfs", "Aoc/0010"), ("01007EF00011F002/romfs", "Aoc/0010"),
    ("01007ef00011e001/romfs", "Aoc/0010"), ("01007ef00011e002/romfs", "Aoc/0010"),
    ("01007ef00011f001/romfs", "Aoc/0010"), ("01007ef00011f002/romfs", "Aoc/0010"),
    ("romfs/", ""), ("aoc/content", "Aoc"), ("aoc", "Aoc"),
]


def canonicalize(path: str) -> str:
    """uk_content::canonicalize."""
    canon = str(path).replace("\\", "/")
    for k, v in _CANON_PREFIXES:
        if canon.startswith(k):
            canon = v + canon[len(k):]
    return canon.replace(".s", ".")


def compress_if(data: bytes, name: str) -> bytes:
    """roead::yaz0::compress_if: Yaz0 when the extension starts with 's' (not .sarc)."""
    ext = name.rpartition("/")[2].rpartition(".")[2] if "." in name.rpartition("/")[2] else ""
    if ext.startswith("s") and ext != "sarc":
        return bytes(oead.yaz0.compress(data))
    return data


def unyaz(data: bytes) -> bytes:
    return bytes(oead.yaz0.decompress(data)) if data[:4] == b"Yaz0" else bytes(data)


_SARC_EXCLUDE_EXTS = ("genvb", "sarc", "arc")
_SARC_EXCLUDE_NAMES = ("tera_resource.Nin_NX_NVN", "tera_resource.Cafe_Cafe_GX2")


def is_mergeable_sarc(name: str, data: bytes) -> bool:
    """uk_content::resource::is_mergeable_sarc (data already decompressed)."""
    if len(data) < 0x40 or not (data[:4] == b"SARC" or data[0x11:0x15] == b"SARC"):
        return False
    base = name.rpartition("/")[2]
    if "." not in base:
        return False
    stem, _, ext = base.rpartition(".")
    ext = ext[1:] if ext.startswith("s") else ext
    return ext not in _SARC_EXCLUDE_EXTS and not any(stem.startswith(x) for x in _SARC_EXCLUDE_NAMES)


# ------------------------------------------------------------- package
class Package:
    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.zip = zipfile.ZipFile(self.path)
        self._names = {n.rstrip("/"): n for n in self.zip.namelist()}
        lower = {k.lower(): v for k, v in self._names.items()}
        self.meta = yaml.safe_load(self.zip.read(lower["meta.yml"]).decode("utf-8-sig").replace("!", "")) or {}
        man = yaml.safe_load(self.zip.read(lower["manifest.yml"]).decode("utf-8-sig")) or {}
        self.content_files = list(man.get("content") or man.get("content_files") or [])
        self.aoc_files = list(man.get("aoc") or man.get("aoc_files") or [])
        self._dctx = zstandard.ZstdDecompressor(
            dict_data=zstandard.ZstdCompressionDict(DICT_PATH.read_bytes()))
        self._cache = {}

    @property
    def name(self):
        return str(self.meta.get("name") or self.path.stem)

    @property
    def has_options(self):
        return bool(self.meta.get("option_groups"))

    def has(self, canon: str) -> bool:
        return canon in self._names

    def get(self, canon: str):
        """Decoded ResourceData for a canonical name, or None if the mod doesn't change it."""
        if canon in self._cache:
            return self._cache[canon]
        entry = self._names.get(canon)
        if entry is None:
            return None
        raw = self.zip.read(entry)
        try:
            data = self._dctx.decompress(raw, max_output_size=1 << 31)
        except zstandard.ZstdError:
            data = zstandard.ZstdDecompressor().decompressobj().decompress(raw)
        res = cbor2.loads(data)
        if len(self._cache) > 64:
            self._cache.clear()
        self._cache[canon] = res
        return res

    def close(self):
        self.zip.close()


# ------------------------------------------------------------- vanilla game files
_FILEMAP = None


def _filemap():
    global _FILEMAP
    if _FILEMAP is None:
        raw = zstandard.ZstdDecompressor().decompress(FILEMAP_PATH.read_bytes(), max_output_size=1 << 27)
        _FILEMAP = json.loads(raw)
    return _FILEMAP


class Dump:
    """uk_reader::ResourceReader over unpacked Wii U folders: every resource is
    found by its canonical name through UKMM's file map [aoc, update, base]."""

    def __init__(self, base_dirs, update_dirs, aoc_dirs):
        self.dirs = ([pathlib.Path(d) for d in aoc_dirs], [pathlib.Path(d) for d in update_dirs],
                     [pathlib.Path(d) for d in base_dirs])
        self._sarcs = collections.OrderedDict()

    @classmethod
    def from_vanilla_roots(cls, roots: dict):
        """Korok's _vanilla_roots(): content roots list update dirs (0005000e) before base
        (00050000); aoc roots end in content/0010."""
        update = [r for r in roots.get("content", []) if "0005000e" in str(r).lower()]
        base = [r for r in roots.get("content", []) if "00050000" in str(r).lower().replace("\\", "/").split("/title/")[-1][:8]]
        aoc = list(roots.get("aoc", []))
        return cls(base, update, aoc)

    def _read_loose(self, which: int, rel: str):
        for d in self.dirs[which]:
            p = d / rel
            if p.is_file():
                return p.read_bytes()
        return None

    def _sarc(self, path: str):
        s = self._sarcs.get(path)
        if s is None:
            data = self.get_bytes(path)
            if data is None:
                return None
            s = oead.Sarc(unyaz(data))
            self._sarcs[path] = s
            while len(self._sarcs) > 12:
                self._sarcs.popitem(last=False)
        else:
            self._sarcs.move_to_end(path)
        return s

    def _from_sarc(self, nest_path: str, aoc: bool):
        parts = nest_path.split("//")
        root = f"Aoc/0010/{parts[0]}" if aoc else parts[0]
        if len(parts) == 2:
            parent = self._sarc(root)
            inner = parts[1]
        else:
            outer = self._sarc(root)
            if outer is None:
                return None
            key = root + "//" + parts[1]
            parent = self._sarcs.get(key)
            if parent is None:
                f = outer.get_file(parts[1])
                if f is None:
                    return None
                parent = oead.Sarc(unyaz(bytes(f.data)))
                self._sarcs[key] = parent
            inner = parts[2]
        if parent is None:
            return None
        f = parent.get_file(inner)
        return unyaz(bytes(f.data)) if f is not None else None

    def get_bytes(self, path: str):
        """Decompressed vanilla bytes of a resource (any path form), or None."""
        canon = canonicalize(path)
        entry = _filemap().get(canon)
        if entry is None:
            return None
        for which, fp in enumerate(entry):
            if not fp:
                continue
            if "//" in fp:
                data = self._from_sarc(fp, which == 0)
            else:
                data = self._read_loose(which, fp)
            if data is not None:
                return unyaz(data)
        return None


# ------------------------------------------------------------- CBOR -> oead
def _vec(cls, d, fields):
    v = cls()
    for f in fields:
        setattr(v, f, float(d[f]))
    return v


def _curve(d):
    c = oead.Curve()
    c.a, c.b = int(d["a"]), int(d["b"])
    c.floats = [float(x) for x in d["floats"]]
    return c


def param_from_cbor(p):
    """roead aamp::Parameter (serde) -> oead.aamp.Parameter."""
    (kind, v), = p.items()
    P = oead.aamp.Parameter
    if kind == "Bool":
        return P(bool(v))
    if kind == "F32":
        return P(oead.F32(float(v)))
    if kind == "I32":
        return P(oead.S32(int(v)))
    if kind == "U32":
        return P(oead.U32(int(v)))
    if kind == "Vec2":
        return P(_vec(oead.Vector2f, v, "xy"))
    if kind == "Vec3":
        return P(_vec(oead.Vector3f, v, "xyz"))
    if kind == "Vec4":
        return P(_vec(oead.Vector4f, v, "xyzt"))
    if kind == "Color":
        return P(_vec(oead.Color4f, v, "rgba"))
    if kind == "Quat":
        return P(_vec(oead.Quatf, v, "abcd"))
    if kind == "String32":
        return P(oead.FixedSafeString32(str(v)))
    if kind == "String64":
        return P(oead.FixedSafeString64(str(v)))
    if kind == "String256":
        return P(oead.FixedSafeString256(str(v)))
    if kind == "StringRef":
        return P(str(v))
    if kind in ("Curve1", "Curve2", "Curve3", "Curve4"):
        return P([_curve(c) for c in v])
    if kind == "BufferInt":
        return P(oead.BufferInt([int(x) for x in v]))
    if kind == "BufferF32":
        return P(oead.BufferF32([float(x) for x in v]))
    if kind == "BufferU32":
        return P(oead.BufferU32([int(x) for x in v]))
    raise UnsupportedResource(f"AAMP parameter type {kind}", "")


def _name(k):
    return oead.aamp.Name(int(k)) if isinstance(k, int) else oead.aamp.Name(str(k))


def pobj_from_cbor(d):
    o = oead.aamp.ParameterObject()
    for k, v in d.items():
        o.params[_name(k)] = param_from_cbor(v)
    return o


def plist_from_cbor(d, target=None):
    pl = target if target is not None else oead.aamp.ParameterList()
    for k, v in (d.get("objects") or {}).items():
        pl.objects[_name(k)] = pobj_from_cbor(v)
    for k, v in (d.get("lists") or {}).items():
        pl.lists[_name(k)] = plist_from_cbor(v)
    return pl


def pio_from_cbor(d):
    pio = oead.aamp.ParameterIO()
    pio.type = str(d.get("data_type", "xml"))
    pio.version = int(d.get("version", 0))
    plist_from_cbor(d.get("param_root") or {}, pio)
    return pio


def merge_pobj(base, diff):
    """util::merge_pobj: params from the diff override/extend the base object."""
    for k, v in diff.items():
        base.params[_name(k)] = param_from_cbor(v)


def merge_plist(base, diff):
    """util::merge_plist, applied in place to an oead ParameterList/IO."""
    for k, v in (diff.get("objects") or {}).items():
        key = _name(k)
        cur = base.objects.get(key)
        if cur is None:
            base.objects[key] = pobj_from_cbor(v)
        else:
            merge_pobj(cur, v)
    for k, v in (diff.get("lists") or {}).items():
        key = _name(k)
        cur = base.lists.get(key)
        if cur is None:
            base.lists[key] = plist_from_cbor(v)
        else:
            merge_plist(cur, v)


def byml_from_cbor(b):
    """roead Byml (serde) -> oead byml value."""
    if b == "Null" or b is None:
        return None
    (kind, v), = b.items()
    if kind == "Map":
        h = oead.byml.Hash()
        for k, x in v.items():
            h[str(k)] = byml_from_cbor(x)
        return h
    if kind == "Array":
        return oead.byml.Array([byml_from_cbor(x) for x in v])
    if kind == "String":
        return str(v)
    if kind == "Bool":
        return bool(v)
    if kind == "I32":
        return oead.S32(int(v))
    if kind == "U32":
        return oead.U32(int(v))
    if kind == "Float":
        return oead.F32(float(v))
    if kind == "Double":
        return oead.F64(float(v))
    if kind == "I64":
        return oead.S64(int(v))
    if kind == "U64":
        return oead.U64(int(v))
    if kind == "BinaryData":
        return bytes(v)
    raise UnsupportedResource(f"BYML node type {kind}", "")


def merge_byml_shallow(base, diff):
    """util::merge_byml_shallow: top-level keys of the diff replace the base's;
    Null removes a key."""
    out = oead.byml.Hash()
    for k, v in base.items():
        out[k] = v
    for k, v in diff.items():
        out[k] = v
    for k in [k for k, v in out.items() if v is None]:
        del out[k]
    return out


def byml_to_binary(b, big_endian=True, version=2) -> bytes:
    return bytes(oead.byml.to_binary(b, big_endian=big_endian, version=version))


# ------------------------------------------------------------- building files
class Converter:
    def __init__(self, package: Package, dump: Dump, log=None):
        self.pkg = package
        self.dump = dump
        self.log = log or (lambda m: None)
        self.unsupported = collections.defaultdict(list)   # kind -> [canon]
        self.problems = []
        self.built = 0
        import korok_ukmm_formats as fmt
        self.fmt = fmt

    def build_file(self, file: str, aoc: bool) -> bytes:
        """ModUnpacker::build_file (one mod): returns the uncompressed game file."""
        filepath = f"Aoc/0010/{file}" if aoc and not file.startswith("Aoc/0010/") else file
        canon = canonicalize(filepath)
        res = self.pkg.get(canon)
        vanilla = self.dump.get_bytes(filepath)
        if res is None and vanilla is None:
            raise FileNotFoundError(f"No copy of {filepath} in the game files or the mod")

        if res is not None and "Binary" in res:
            return bytes(res["Binary"])
        if res is not None and "Mergeable" in res:
            (kind, data), = res["Mergeable"].items()
            try:
                return self.fmt.build(kind, data, vanilla, canon)
            except UnsupportedResource as e:
                self.unsupported[e.kind].append(canon)
                if vanilla is not None:
                    return vanilla            # keep the game working; the change is missing
                raise
        # SARC (changed file list, or unchanged archive whose files may be modded)
        sarc_diff = res.get("Sarc") if res is not None else None
        if sarc_diff is None and not (vanilla is not None and is_mergeable_sarc(canon, vanilla)):
            return vanilla
        return self.build_sarc(vanilla, sarc_diff, aoc, canon)

    def _touched(self, name, raw, aoc, depth=0) -> bool:
        """Does the mod change this file, or anything inside it?"""
        path = f"Aoc/0010/{name}" if aoc and not name.startswith("Aoc/0010/") else name
        canon = canonicalize(path)
        if self.pkg.has(canon):
            return True
        if depth > 4:
            return False
        try:
            data = unyaz(raw)
            if not is_mergeable_sarc(canon, data):
                return False
            return any(self._touched(f.name, bytes(f.data), aoc, depth + 1) for f in oead.Sarc(data).get_files())
        except Exception:
            return True

    def build_sarc(self, vanilla, diff, aoc, canon) -> bytes:
        if vanilla is not None:
            vs = oead.Sarc(vanilla)
            names = {f.name: False for f in vs.get_files()}
            alignment = vs.guess_min_alignment()
        else:
            names, alignment = {}, int((diff or {}).get("alignment", 4))
        for name, deleted in ((diff or {}).get("files") or {}).items():
            names[str(name)] = bool(deleted)          # SortedDeleteSet::merge
        writer = oead.SarcWriter(endian=oead.Endianness.Big)
        try:
            writer.set_min_alignment(alignment)
        except Exception:
            pass
        for name in sorted(n for n, d in names.items() if not d):
            raw = None
            if vanilla is not None:
                f = vs.get_file(name)
                raw = bytes(f.data) if f is not None else None
            if raw is not None and not self._touched(name, raw, aoc):
                writer.files[name] = raw            # untouched: keep the vanilla bytes as they are
                continue
            try:
                data = self.build_file(name, aoc)
            except FileNotFoundError as e:
                self.problems.append(f"{canon}: {e}")
                continue
            writer.files[name] = compress_if(data, name)
        return bytes(writer.write()[1])

    def convert(self, out_dir: pathlib.Path, progress=None):
        """Writes out_dir/content/... and out_dir/aoc/0010/... like a UKMM deploy."""
        out_dir = pathlib.Path(out_dir)
        jobs = [(f, False) for f in self.pkg.content_files] + [(f, True) for f in self.pkg.aoc_files]
        texts_done = False
        for i, (f, aoc) in enumerate(jobs):
            if progress:
                progress(i / max(len(jobs), 1), f)
            if f.startswith("Pack/Bootup_") and not aoc:
                # UKMM rebuilds the text packs in a separate step (unpack_texts), for every
                # language of the game at once
                if texts_done:
                    continue
                texts_done = True
                try:
                    packs = self.fmt.build_text_packs(self)
                except UnsupportedResource as e:
                    self.unsupported[e.kind].append(e.canon or f)
                    continue
                except Exception as e:
                    self.problems.append(f"game text: {type(e).__name__}: {e}")
                    continue
                for rel, data in packs.items():
                    dest = out_dir / "content" / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(data)
                    self.built += 1
                continue
            try:
                data = self.build_file(f, aoc)
            except UnsupportedResource as e:
                self.unsupported[e.kind].append(e.canon or f)
                continue
            except Exception as e:
                self.problems.append(f"{f}: {type(e).__name__}: {e}")
                continue
            dest = out_dir / ("aoc/0010" if aoc else "content") / f
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(compress_if(data, f))
            self.built += 1
        return self


# ------------------------------------------------------------- oead -> CBOR shape
def param_to_cbor(p):
    """oead.aamp.Parameter -> roead serde form ({'Type': value})."""
    T = oead.aamp.Parameter.Type
    t, v = p.type(), p.v
    if t == T.Bool:
        return {"Bool": bool(v)}
    if t == T.F32:
        return {"F32": float(v)}
    if t == T.Int:
        return {"I32": int(v)}
    if t == T.U32:
        return {"U32": int(v)}
    if t == T.Vec2:
        return {"Vec2": {"x": v.x, "y": v.y}}
    if t == T.Vec3:
        return {"Vec3": {"x": v.x, "y": v.y, "z": v.z}}
    if t == T.Vec4:
        return {"Vec4": {"x": v.x, "y": v.y, "z": v.z, "t": v.t}}
    if t == T.Color:
        return {"Color": {"r": v.r, "g": v.g, "b": v.b, "a": v.a}}
    if t == T.Quat:
        return {"Quat": {"a": v.a, "b": v.b, "c": v.c, "d": v.d}}
    if t in (T.String32, T.String64, T.String256, T.StringRef):
        return {t.name: str(v)}
    if t in (T.Curve1, T.Curve2, T.Curve3, T.Curve4):
        return {t.name: [{"a": c.a, "b": c.b, "floats": list(c.floats)} for c in v]}
    if t in (T.BufferInt, T.BufferF32, T.BufferU32):
        return {t.name: list(v)}
    raise UnsupportedResource(f"AAMP parameter type {t.name}", "")


def _hash(k) -> int:
    return k.hash if hasattr(k, "hash") else int(k)


def pobj_to_cbor(o) -> dict:
    return {_hash(k): param_to_cbor(v) for k, v in o.params.items()}


def plist_to_cbor(pl) -> dict:
    return {"lists": {_hash(k): plist_to_cbor(v) for k, v in pl.lists.items()},
            "objects": {_hash(k): pobj_to_cbor(v) for k, v in pl.objects.items()}}


def pio_to_cbor(pio) -> dict:
    return {"data_type": str(pio.type), "version": int(pio.version), "param_root": plist_to_cbor(pio)}


def param_str(p) -> str:
    """The string in a {'StringXX': s} serde parameter."""
    (_, v), = p.items()
    return str(v)


# ------------------------------------------------------------- UKMM collections (serde shapes)
# DeleteSet / SortedDeleteSet: {key: deleted}; DeleteMap / SortedDeleteMap: {key: [value, deleted]};
# DeleteVec: [[item, deleted], ...]
def dset_merge(base: dict, diff: dict) -> dict:
    """DeleteSet::merge (IndexMap order): base items the diff doesn't mention, then the diff's."""
    out = {k: False for k, d in base.items() if not d and k not in diff}
    for k, d in diff.items():
        if not d:
            out[k] = False
    return out


def sorted_dset_merge(base: dict, diff: dict) -> dict:
    """SortedDeleteSet::merge."""
    keys = {**{k: False for k in base}, **{k: bool(d) for k, d in diff.items()}}
    return {k: False for k in sorted(keys) if not keys[k]}


def dmap_merge(base: dict, diff: dict, sort=False) -> dict:
    """DeleteMap::merge / SortedDeleteMap::merge: diff values replace base values."""
    out = dict(base)
    for k, v in diff.items():
        out[k] = v
    out = {k: [v[0], False] for k, v in out.items() if not v[1]}
    return dict(sorted(out.items())) if sort else out


def dmap_deep_merge(base: dict, diff: dict, merge_value, sort=False) -> dict:
    """DeleteMap::deep_merge: values present in both are merged with merge_value."""
    keys = list(dict.fromkeys(list(base) + list(diff)))
    out = {}
    for k in keys:
        b, d = base.get(k), diff.get(k)
        if b is not None and d is not None:
            out[k] = [merge_value(b[0], d[0]), bool(d[1])]
        else:
            out[k] = list(d if d is not None else b)
    out = {k: [v[0], False] for k, v in out.items() if not v[1]}
    return dict(sorted(out.items())) if sort else out


def dvec_merge(base: list, diff: list) -> list:
    """DeleteVec::merge."""
    deleted = [it for it, d in diff if d]
    items = [it for it, d in base if not d and it not in deleted]
    for idx, it in enumerate(x for x, d in diff if not d):
        if it not in items:
            items.insert(idx, it)
    return [[it, False] for it in items]


def merge_pobj_cbor(base: dict, diff: dict) -> dict:
    out = dict(base)
    out.update(diff)
    return out


def merge_plist_cbor(base: dict, diff: dict) -> dict:
    objs = dict(base.get("objects") or {})
    for k, v in (diff.get("objects") or {}).items():
        objs[k] = merge_pobj_cbor(objs[k], v) if k in objs else v
    lists = dict(base.get("lists") or {})
    for k, v in (diff.get("lists") or {}).items():
        lists[k] = merge_plist_cbor(lists[k], v) if k in lists else v
    return {"lists": lists, "objects": objs}


# ------------------------------------------------------------- entry point for Korok
def dump_from_cemu(user_dir, title_ids=("101c9400", "101c9500", "101c9300")):
    title = pathlib.Path(user_dir) / "mlc01" / "usr" / "title"
    base = [title / "00050000" / t / "content" for t in title_ids]
    update = [title / "0005000e" / t / "content" for t in title_ids]
    aoc = [title / "0005000c" / t / "content" / "0010" for t in title_ids]
    return Dump([d for d in base if d.is_dir()], [d for d in update if d.is_dir()],
                [d for d in aoc if d.is_dir()])


def convert_package(package_path, out_dir, dump: Dump, progress=None, log=None):
    """Returns the Converter (see .built, .unsupported, .problems, .pkg)."""
    if not (dump.dirs[1] or dump.dirs[2]):
        raise FileNotFoundError("the game's files weren't found in Cemu's mlc01 folder "
                                "(base game and v208 update must be installed there)")
    pkg = Package(package_path)
    try:
        return Converter(pkg, dump, log=log).convert(pathlib.Path(out_dir), progress=progress)
    finally:
        pkg.close()
