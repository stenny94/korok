"""Turns an unpacked mod folder (content/, aoc/0010/, options/) into a UKMM mod
package: a port of UKMM's ModPacker (uk-mod/src/pack.rs).

Every changed game file is stored under its canonical name as zstd + CBOR of
UKMM's ResourceData:

  Binary      the whole file (models, textures, sounds, ...)
  Sarc        the archive's file list, as a diff against vanilla
  Mergeable   the parsed resource, as a diff against vanilla (or in full when new)

korok stores every mod this way, whatever it was imported from (UKMM package,
BCML BNP, graphic pack), so mods are merged together the way UKMM merges them.

Port of UKMM (https://github.com/NiceneNerd/ukmm, GPL-3.0) by Nicene Nerd.
"""
import pathlib
import zipfile

import cbor2
import oead
import yaml
import zstandard

import korok_ukmm_diff as D
from korok_ukmm_native import DICT_PATH, Dump, canonicalize, is_mergeable_sarc, unyaz

LANG_CODES = ("USen", "EUen", "USfr", "USes", "EUde", "EUes", "EUfr", "EUit", "EUnl", "EUru",
              "CNzh", "JPja", "KRko", "TWzh")


class PackError(RuntimeError):
    pass


# ------------------------------------------------------------- ResourceData::from_binary
def _stem(name):
    base = name.rpartition("/")[2]
    return base.rpartition(".")[0] if "." in base else base


def _ext(name):
    base = name.rpartition("/")[2]
    return base.rpartition(".")[2] if "." in base else ""


def _ends_with_components(name, suffix):
    """Path::ends_with on the path with its extension removed."""
    base = name.rpartition(".")[0] if "." in name.rpartition("/")[2] else name
    parts, want = base.split("/"), suffix.split("/")
    return parts[-len(want):] == want


def _ext_is(e):
    return lambda n: _ext(n) == e


def _stem_is(s):
    return lambda n: _stem(n) == s


# MergeableResource::from_binary, in UKMM's order
KIND_RULES = [
    ("ActorInfo", _stem_is("ActorInfo.product")),
    ("ActorLink", lambda n: "ActorLink" in n and n.endswith("bxml")),
    ("AIProgram", _ext_is("baiprog")),
    ("AISchedule", _ext_is("baischedule")),
    ("AnimationInfo", _ext_is("baniminfo")),
    ("AnimSeq", _ext_is("bas")),
    ("AreaData", _stem_is("AreaData")),
    ("ASList", _ext_is("baslist")),
    ("AttClient", _ext_is("batcl")),
    ("AttClientList", _ext_is("batcllist")),
    ("Awareness", _ext_is("bawareness")),
    ("BarslistInfo", _stem_is("BarslistInfo")),
    ("BoneControl", _ext_is("bbonectrl")),
    ("Chemical", _ext_is("bchemical")),
    ("ChemicalRes", lambda n: n.rpartition("/")[2] == "system.bchmres"),
    ("CookData", _stem_is("CookData")),
    ("DamageParam", _ext_is("bdmgparam")),
    ("Demo", _ext_is("bdemo")),
    ("DropTable", _ext_is("bdrop")),
    ("EventInfo", _stem_is("EventInfo.product")),
    ("FontArchive", lambda n: _ext(n).endswith("bfarc")),
    ("GameDataPack", _stem_is("gamedata")),
    ("GeneralParamList", _ext_is("bgparamlist")),
    ("LayoutArchive", lambda n: _ext(n).endswith("blarc")),
    ("LazyTraverseList", _stem_is("LazyTraverseList")),
    ("LevelSensor", _stem_is("LevelSensor")),
    ("LifeCondition", _ext_is("blifecondition")),
    ("Location", _stem_is("Location")),
    ("Lod", _ext_is("blod")),
    ("MainStatic", lambda n: _ends_with_components(n, "MainField/Static")),
    ("MapUnit", lambda n: (lambda b: (b.startswith("CDungeon") or "Dynamic" in b or "_Static" in b)
                           and b.endswith("mubin"))(n.rpartition("/")[2])),
    ("MessagePack", lambda n: _stem(n).startswith("Msg_")),
    ("ModelList", _ext_is("bmodellist")),
    ("Physics", _ext_is("bphysics")),
    ("QuestProduct", _stem_is("QuestProduct")),
    ("RagdollBlendWeight", _ext_is("brgbw")),
    ("RagdollConfig", _ext_is("brgconfig")),
    ("RagdollConfigList", _ext_is("brgconfiglist")),
    ("Recipe", _ext_is("brecipe")),
    ("ResidentActors", lambda n: n.rpartition("/")[2] == "ResidentActors.byml"),
    ("ResidentEvents", _stem_is("ResidentEvent")),
    ("SaveDataPack", _stem_is("savedataformat")),
    ("ShopData", _ext_is("bshop")),
    ("ShopGameDataInfo", _stem_is("ShopGameDataInfo")),
    ("Static", lambda n: _ends_with_components(n, "CDungeon/Static")),
    ("StatusEffectList", _stem_is("StatusEffectList")),
    ("Tips", lambda n: (lambda b: b.startswith("Tips") and b.endswith("byml"))(n.rpartition("/")[2])),
    ("UMii", _ext_is("bumii")),
    ("WorldInfo", _ext_is("bwinfo")),
]


def mergeable_kind(name, data):
    for kind, match in KIND_RULES:
        if match(name):
            return kind
    if len(data) > 4 and data[:4] == b"AAMP":
        return "GenericAamp"
    if len(data) > 4 and data[:2] in (b"BY", b"YB") and \
            (int.from_bytes(data[2:4], "big") < 8 or int.from_bytes(data[2:4], "little") < 8):
        return "GenericByml"
    return None


def resource_from_binary(name, data):
    """ResourceData::from_binary -> ('Binary', bytes) | ('Mergeable', kind, model) | ('Sarc', map).
    A mergeable file that can't be parsed (or whose type has no diff yet) is kept whole,
    as UKMM's BinaryOverride."""
    if _stem(name) == "Dummy" or len(data) < 0x10:
        return ("Binary", data)
    kind = mergeable_kind(name, data)
    if kind is not None:
        try:
            return ("Mergeable", kind, D.from_binary(kind, data))
        except D.NoDiff:
            return ("Mergeable", "BinaryOverride", [data, f"{kind} is not merged by korok yet"])
        except Exception as e:  # noqa: BLE001 - UKMM keeps unparsable files as-is too
            return ("Mergeable", "BinaryOverride", [data, f"{type(e).__name__}: {e}"])
    if is_mergeable_sarc(name, data):
        s = oead.Sarc(data)
        return ("Sarc", {"alignment": s.guess_min_alignment(),
                         "files": {f.name: False for f in sorted(s.get_files(), key=lambda f: f.name)}})
    return ("Binary", data)


def to_cbor(res):
    if res[0] == "Binary":
        return {"Binary": bytes(res[1])}
    if res[0] == "Sarc":
        return {"Sarc": res[1]}
    return {"Mergeable": {res[1]: res[2]}}


# ------------------------------------------------------------- vanilla / master lookup
class Masters:
    """The ResourceReaders a mod is diffed against: the game, then (for options)
    the mod's own base files. Lookups try the last one first."""

    def __init__(self, dump: Dump):
        self.readers = [dump]
        self._cache = {}

    def push_mod_root(self, root: pathlib.Path):
        self.readers.append(Dump([root / "content"], [root / "content"], [root / "aoc" / "0010"]))
        self._cache.clear()

    def get(self, name):
        canon = canonicalize(name)
        if canon in self._cache:
            return self._cache[canon]
        res = None
        for r in reversed(self.readers):
            data = r.get_bytes(name)
            if data is None and canon.startswith("Message/Msg_"):
                lang = _stem(canon)[4:8]
                for code in _reader_langs(r):
                    if code[2:] == lang[2:]:
                        data = r.get_bytes(f"Message/Msg_{code}.product.ssarc")
                        if data is not None:
                            break
            if data is not None:
                res = resource_from_binary(canon, data)
                break
        if len(self._cache) > 256:
            self._cache.clear()
        self._cache[canon] = res
        return res


def _reader_langs(reader):
    return [c for c in LANG_CODES if reader.get_bytes(f"Pack/Bootup_{c}.pack") is not None]


# ------------------------------------------------------------- the packer
class ModPacker:
    def __init__(self, source, dest, meta: dict, dump: Dump, progress=None, log=None):
        self.source = pathlib.Path(source)
        self.dest = pathlib.Path(dest)
        self.meta = meta
        self.masters = Masters(dump)
        self.progress = progress or (lambda f, msg: None)
        self.log = log or (lambda m: None)
        self.current_root = self.source
        self.built = set()
        self.kept_whole = []          # (canon, reason) stored as BinaryOverride
        self._zip = None
        self._cctx = zstandard.ZstdCompressor(
            level=8, dict_data=zstandard.ZstdCompressionDict(DICT_PATH.read_bytes()))

    # -- writing
    def _prefix(self):
        rel = self.current_root.relative_to(self.source).as_posix()
        return "" if rel == "." else rel + "/"

    def write_resource(self, canon, res):
        entry = self._prefix() + canon
        if entry in self._written:
            return
        self._written.add(entry)
        data = cbor2.dumps(to_cbor(res))
        self._zip.writestr(entry, self._cctx.compress(data))
        self.built.add(canon)
        if res[0] == "Mergeable" and res[1] == "BinaryOverride":
            self.kept_whole.append((canon, res[2][1]))

    # -- collect_resources / process_resource / process_sarc
    def process_resource(self, name, canon, res, in_new_sarc):
        if canon in self.built:
            return
        if canon.startswith("Pack/Bootup_"):
            return   # always holds a single text archive; texts are handled as MessagePack
        ref = self.masters.get(name)
        if res[0] == "Mergeable" and ref is not None and ref[0] == "Mergeable":
            if res[1] == ref[1] and res[1] != "BinaryOverride":
                if D.equal(res[1], ref[2], res[2]):
                    return
                res = ("Mergeable", res[1], D.diff(res[1], ref[2], res[2]))
            elif res[1] != "BinaryOverride" and ref[1] == "BinaryOverride":
                pass   # (BinaryOverride, anything) -> anything
            elif ref[1] != res[1] and res[1] != "BinaryOverride":
                raise PackError(f"Can't diff {canon}: {ref[1]} vs {res[1]}")
        elif res[0] == "Sarc" and ref is not None and ref[0] == "Sarc":
            if ref[1]["files"] == res[1]["files"] and not in_new_sarc:
                return
            res = ("Sarc", {"alignment": ref[1]["alignment"],
                            "files": D.sorted_dset_diff(ref[1]["files"], res[1]["files"])})
        elif res[0] == "Binary" and ref is not None and ref[0] == "Binary" and ref[1] == res[1]:
            return
        self.write_resource(canon, res)

    def _is_modded(self, name, canon, data):
        """StockHashTable::is_file_modded(canon, data, true): new files count as modded."""
        for r in reversed(self.masters.readers[:1]):
            v = r.get_bytes(name)
            if v is None:
                return True
            return v != data
        return True

    def process_sarc(self, sarc, path, is_new_sarc, is_aoc):
        for f in sarc.get_files():
            raw = bytes(f.data)
            if not raw:
                continue
            name = f"Aoc/0010/{f.name}" if is_aoc else f.name
            canon = canonicalize(name)
            data = unyaz(raw)
            if not self._is_modded(name, canon, data) and not is_new_sarc:
                continue
            res = resource_from_binary(name, data)
            self.process_resource(name, canon, res, is_new_sarc)
            if is_mergeable_sarc(canon, data):
                self.process_sarc(oead.Sarc(data), name, is_new_sarc, is_aoc)

    def collect_resources(self, root: pathlib.Path, done, total):
        files = []
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            name = p.relative_to(self.current_root).as_posix()
            canon = canonicalize(name)
            data = unyaz(p.read_bytes())
            done[0] += 1
            self.progress(done[0] / max(total, 1), canon)
            if p.name == "AocMainField.pack" and not data:
                self.write_resource("Aoc/0010/Pack/AocMainField.pack", ("Sarc", {"alignment": 0, "files": {}}))
                files.append("Pack/AocMainField.pack")
                continue
            if name.endswith("sizetable") or len(data) < 4:
                continue
            if not self._is_modded(name, canon, data):
                continue
            res = resource_from_binary(name, data)
            is_mergeable = res[0] == "Mergeable"
            self.process_resource(name, canon, res, False)
            if not is_mergeable and is_mergeable_sarc(canon, data):
                self.process_sarc(oead.Sarc(data), name, self.masters.readers[0].get_bytes(name) is None,
                                  canon.startswith("Aoc"))
            files.append(p.relative_to(root).as_posix())
        return sorted(set(files))

    def pack_root(self, root: pathlib.Path, done, total):
        self.built = set()
        content = root / "content"
        aoc = root / "aoc" / "0010"
        content_files = self.collect_resources(content, done, total) if content.is_dir() else []
        aoc_files = self.collect_resources(aoc, done, total) if aoc.is_dir() else []
        if any("Map/MainField" in f for f in aoc_files) and "Pack/AocMainField.pack" not in aoc_files:
            self.write_resource("Aoc/0010/Pack/AocMainField.pack", ("Sarc", {"alignment": 0, "files": {}}))
            aoc_files.append("Pack/AocMainField.pack")
            aoc_files.sort()
        man = yaml.safe_dump({"content": content_files, "aoc": aoc_files}, sort_keys=False)
        self._zip.writestr(self._prefix() + "manifest.yml", man)

    def option_roots(self):
        roots = []
        for g in self.meta.get("option_groups") or []:
            for o in g.get("options") or []:
                p = self.source / "options" / str(o.get("path"))
                if p not in roots:
                    roots.append(p)
        return roots

    def pack(self):
        roots = [self.source] + (self.option_roots() if (self.source / "options").is_dir() else [])
        total = sum(1 for r in roots for d in ("content", "aoc") if (r / d).is_dir()
                    for p in (r / d).rglob("*") if p.is_file())
        done = [0]
        self.dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.dest.with_suffix(".part")
        self._written = set()
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
            self._zip = z
            self.pack_root(self.source, done, total)
            if len(roots) > 1:
                self.masters.push_mod_root(self.source)
                for r in roots[1:]:
                    self.current_root = r
                    if r.is_dir():
                        self.pack_root(r, done, total)
                self.current_root = self.source
            for stem in ("thumb", "thumbnail", "preview"):
                hit = next((self.source / f"{stem}.{e}" for e in ("jpg", "jpeg", "png", "svg")
                            if (self.source / f"{stem}.{e}").is_file()), None)
                if hit:
                    z.write(hit, f"thumb{hit.suffix}")
                    break
            z.writestr("meta.yml", yaml.safe_dump(self.meta, sort_keys=False, allow_unicode=True))
        tmp.replace(self.dest)
        return self.dest


def pack_folder(source, dest, meta, dump, progress=None, log=None):
    p = ModPacker(source, dest, meta, dump, progress, log)
    p.pack()
    return p
