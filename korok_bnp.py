"""BCML mod packages (.bnp) -> plain mod folders, the way UKMM converts them
(uk-manager/src/bnp.rs and bnp/*.rs).

A BNP holds the mod's changed files plus logs/ describing merged changes
against vanilla (gamedata.yml, deepmerge.aamp, texts.json, ...), and its
packs (.pack, .sbactorpack, ...) are "stripped": only modified files are left
inside. The converter applies every log to the vanilla file it belongs to,
writes the result into the mod folder, and fills stripped packs back up with
the vanilla files they left out. The folder is then packaged like any other
mod (korok_pack), so it ends up as diffs UKMM-style merging understands.

Port of UKMM (https://github.com/NiceneNerd/ukmm, GPL-3.0) by Nicene Nerd.
"""
import json
import pathlib
import zlib

import oead
import yaml

import korok_ukmm_diff as D
import korok_ukmm_formats as F
from korok_ukmm_native import Dump, compress_if, unyaz

A = oead.aamp
P = A.Parameter
BCML_SARC_EXTS = {"sarc", "pack", "bactorpack", "bmodelsh", "stats", "ssarc", "sbactorpack", "sbmodelsh",
                  "sstats", "sblarc", "blarc"}
BCML_SPECIAL = ("gamedata", "savedataformat", "tera_resource.Nin_NX_NVN", "Dungeon", "Bootup_", "AocMainField")
GAMEDATA_STAGES = ("MainField", "AocField", "CDungeon", "MainFieldDungeon")


class BnpError(RuntimeError):
    pass


def _is_sarc(data: bytes) -> bool:
    return len(data) > 0x20 and (data[:4] == b"SARC" or (data[:4] == b"Yaz0" and data[0x11:0x15] == b"SARC"))


def _is_stripped_sarc(name: str, data: bytes) -> bool:
    ext = name.rpartition(".")[2] if "." in name.rpartition("/")[2] else ""
    return _is_sarc(data) and ext in BCML_SARC_EXTS


def _files(sarc_bytes: bytes) -> dict:
    """SARC -> {name: bytes} (copies, so nothing borrows a freed buffer)."""
    s = oead.Sarc(unyaz(sarc_bytes))
    return {f.name: bytes(f.data) for f in s.get_files()}


class SarcFiles:
    """A SARC being edited: ordered files plus how to write it back."""

    def __init__(self, files=None, legacy=False, alignment=None):
        self.files = dict(files or {})
        self.legacy = legacy
        self.alignment = alignment

    @classmethod
    def from_bytes(cls, data: bytes):
        raw = unyaz(data)
        s = oead.Sarc(raw)
        files = {f.name: bytes(f.data) for f in s.get_files()}
        return cls(files, alignment=s.guess_min_alignment())

    def write(self) -> bytes:
        mode = oead.SarcWriter.Mode.Legacy if self.legacy else oead.SarcWriter.Mode.New
        w = oead.SarcWriter(endian=oead.Endianness.Big, mode=mode)
        if self.alignment:
            try:
                w.set_min_alignment(self.alignment)
            except Exception:
                pass
        for k, v in self.files.items():
            w.files[k] = v
        return bytes(w.write()[1])


def _names_in(plist, out):
    """Every string in an AAMP list (to name hashed keys, like roead's name table)."""
    for _, o in plist.objects.items():
        for _, p in o.params.items():
            if F.is_str_param(p):
                s = str(p.v)
                out[zlib.crc32(s.encode())] = s
    for _, l in plist.lists.items():
        _names_in(l, out)
    return out


def _name_of(key, table):
    h = key.hash if hasattr(key, "hash") else int(key)
    return table.get(h)


def parse_aamp_diff(header: str, pio) -> dict:
    """bnp::parse_aamp_diff: {root: {'sarc': {...}} | {'aamp': plist}} from a BCML aamp log."""
    table = F.obj(pio, header)
    if table is None:
        raise BnpError("Deepmerge log missing file table")
    acc = {}
    for _, p in table.params.items():
        if not F.is_str_param(p):
            continue
        file = str(p.v)
        parts = file.split("//")
        root = acc.setdefault(parts[0], {"sarc": {}})["sarc"] if len(parts) > 1 else None
        if len(parts) == 3:
            parent = root.setdefault(parts[1], {"sarc": {}})["sarc"]
        elif len(parts) == 2:
            parent = root
        else:
            parent = acc
        plist = pio.lists.get(A.Name(file))
        if plist is None:
            raise BnpError("Missing entry in deepmerge log")
        parent[parts[-1]] = {"aamp": plist}
    return acc


def _pio_with_root(plist):
    pio = A.ParameterIO()
    pio.type = "xml"
    pio.version = 0
    for k, o in plist.objects.items():
        pio.objects[k] = o
    for k, l in plist.lists.items():
        pio.lists[k] = l
    return pio


def _merge_plist_oead(base, diff):
    """util::merge_plist with an oead diff list, in place."""
    for k, o in diff.objects.items():
        cur = base.objects.get(k)
        if cur is None:
            base.objects[k] = o
        else:
            for pk, pv in o.params.items():
                cur.params[pk] = pv
    for k, l in diff.lists.items():
        cur = base.lists.get(k)
        if cur is None:
            base.lists[k] = l
        else:
            _merge_plist_oead(cur, l)


class BnpConverter:
    def __init__(self, root: pathlib.Path, dump: Dump, lang: str = "USen", log=None):
        self.path = pathlib.Path(root)
        self.current_root = self.path
        self.dump = dump
        self.lang = lang
        self.log = log or (lambda m: None)
        self.packs = set()
        self.parent_packs = set()
        self.root_maps = {}
        self.opt_maps = {}
        self._opt_cache = {}

    # ---------------------------------------------------------- vanilla / master data
    @staticmethod
    def trim_prefixes(file: str) -> str:
        f = file.replace("\\", "/")
        if f.startswith("content"):
            f = f[len("content"):]
        return f.lstrip("/")

    def _mod_file(self, rel: str, aoc=False):
        p = self.path / ("aoc/0010" if aoc else "content") / rel
        if p.is_file():
            if p not in self._opt_cache:
                self._opt_cache[p] = p.read_bytes()
            return self._opt_cache[p]
        return None

    def _need(self, data, what):
        if data is None:
            raise BnpError(f"{what} not found in your game files")
        return data

    def get_master_bytes(self, path: str) -> bytes:
        if self.current_root != self.path:
            data = self._mod_file(path)
            if data is not None:
                return unyaz(data)
        return self._need(self.dump.get_bytes(path), path)

    def get_master_aoc_bytes(self, path: str) -> bytes:
        if self.current_root != self.path:
            data = self._mod_file(f"Aoc/0010/{path}", aoc=True) or self._mod_file(path, aoc=True)
            if data is not None:
                return unyaz(data)
        return self._need(self.dump.get_bytes(f"Aoc/0010/{path}"), f"Aoc/0010/{path}")

    def _dump_nested(self, path: str):
        parts = path.split("//")
        data = self.dump.get_bytes(parts[0])
        for part in parts[1:]:
            if data is None:
                return None
            data = _files(data).get(part)
        return unyaz(data) if data is not None else None

    def get_from_master_sarc(self, path: str) -> bytes:
        if self.current_root != self.path:
            parts = path.split("//")
            data = self._mod_file(parts[0])
            if data is not None:
                for part in parts[1:]:
                    data = _files(data).get(part)
                    if data is None:
                        raise BnpError(f"Could not get nested file at {path}")
                return unyaz(data)
        return self._need(self._dump_nested(path), path)

    # ---------------------------------------------------------- packs
    def _inflate(self, sarc: SarcFiles, stripped: dict, base: dict):
        for name, data in base.items():
            if name not in stripped:
                sarc.files.setdefault(name, data)
            elif _is_stripped_sarc(name, data):
                try:
                    nested = SarcFiles.from_bytes(data)
                    if name.endswith("arc"):
                        nested.legacy = True
                    nested.files.update(_files(stripped[name]))
                    sarc.files[name] = compress_if(nested.write(), name)
                except Exception:
                    pass

    def open_or_create_sarc(self, dest: pathlib.Path, root_path: str) -> SarcFiles:
        base = self.dump.get_bytes(root_path)
        if not dest.exists():
            if base is None:
                raise BnpError(f"Failed to get base game SARC at {root_path}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(base)
            return SarcFiles.from_bytes(base)
        self.packs.discard(dest)
        try:
            raw = dest.read_bytes()
            stripped = _files(raw)
            sarc = SarcFiles(stripped)
            try:
                sarc.alignment = oead.Sarc(unyaz(raw)).guess_min_alignment()
            except Exception:
                pass
        except Exception:
            if dest.name.startswith(BCML_SPECIAL) and base is not None:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(base)
                return SarcFiles.from_bytes(base)
            raise
        parent = None
        for cand in (self.path / "content" / root_path, self.path / "aoc/0010" / root_path):
            if cand in self.parent_packs and cand.is_file():
                parent = cand
                break
        if parent is not None:
            try:
                self._inflate(sarc, stripped, _files(parent.read_bytes()))
            except Exception:
                pass
        if base is not None:
            try:
                self._inflate(sarc, stripped, _files(base))
            except Exception:
                pass
        return sarc

    def inject_into_sarc(self, nest_path: str, data: bytes, dlc: bool):
        parts = nest_path.split("//")
        if len(parts) < 2:
            raise BnpError(f"Bad nested path: {nest_path}")
        base_path = self.current_root / ("aoc/0010" if dlc else "content") / parts[0]
        sarc = self.open_or_create_sarc(base_path, parts[0])
        nested = None
        if len(parts) == 3:
            if parts[1] not in sarc.files:
                raise BnpError("Missing nested SARC")
            nested = SarcFiles.from_bytes(sarc.files[parts[1]])
        parent = nested or sarc
        parent.files[parts[-1]] = compress_if(data, parts[-1])
        if nested is not None:
            sarc.files[parts[1]] = compress_if(nested.write(), parts[1])
        base_path.parent.mkdir(parents=True, exist_ok=True)
        base_path.write_bytes(compress_if(sarc.write(), base_path.name))

    # ---------------------------------------------------------- logs
    def _yaml(self, name):
        p = self.current_root / "logs" / name
        if not p.exists():
            return None
        return oead.byml.from_text(p.read_text(encoding="utf-8"))

    def handle_actorinfo(self):
        diff = self._yaml("actorinfo.yml")
        if diff is None:
            return
        data = {int(h): [D.byml_to_cbor(a), False] for h, a in diff.items()}
        out = F.BUILDERS["ActorInfo"](data, self.get_master_bytes("Actor/ActorInfo.product.sbyml"), "")
        dest = self.current_root / "content/Actor/ActorInfo.product.sbyml"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(oead.yaz0.compress(out))

    def handle_areadata(self):
        diff = self._yaml("areadata.yml")
        if diff is None:
            return
        data = {int(k): [D.byml_to_cbor(v), False] for k, v in diff.items()}
        path = "Pack/Bootup.pack//Ecosystem/AreaData.sbyml"
        out = F.BUILDERS["AreaData"](data, self.get_from_master_sarc(path), "")
        self.inject_into_sarc(path, out, False)

    def _aamp_log(self, name, header, nested_fn, root_fn):
        p = self.current_root / "logs" / name
        if not p.exists():
            return
        raw = p.read_bytes()
        pio = A.ParameterIO.from_binary(raw) if raw[:4] == b"AAMP" else A.ParameterIO.from_text(raw.decode("utf-8"))
        for root, contents in parse_aamp_diff(header, pio).items():
            base_path = self.current_root / root
            base_path.parent.mkdir(parents=True, exist_ok=True)
            if "sarc" in contents:
                sarc = self.open_or_create_sarc(base_path, self.trim_prefixes(root))
                for nest_root, sub in contents["sarc"].items():
                    self._diff_entry(sarc, nest_root, sub, nested_fn)
                base_path.write_bytes(compress_if(sarc.write(), root))
            else:
                data = root_fn(self.get_master_bytes(self.trim_prefixes(root)), contents["aamp"], root)
                base_path.write_bytes(compress_if(data, root))

    def _diff_entry(self, sarc: SarcFiles, nest_root, contents, on_aamp):
        nested = sarc.files.get(nest_root)
        if "sarc" in contents:
            if nested is None:
                raise BnpError(f"SARC missing file at {nest_root}")
            nest = SarcFiles.from_bytes(nested)
            for name, sub in contents["sarc"].items():
                self._diff_entry(nest, name, sub, on_aamp)
            sarc.files[nest_root] = compress_if(nest.write(), nest_root)
        else:
            data = on_aamp(unyaz(nested) if nested is not None else None, contents["aamp"], nest_root)
            sarc.files[nest_root] = compress_if(data, nest_root)

    @staticmethod
    def _deepmerge(base, plist, name):
        pio = A.ParameterIO.from_binary(base) if base else A.ParameterIO()
        _merge_plist_oead(pio, plist)
        return bytes(pio.to_binary())

    @staticmethod
    def _aslist_merge(base, plist, name):
        if base is None:
            raise BnpError(f"SARC missing file at {name}")
        try:
            b = F.aslist_from(base)
            d = F.aslist_from(bytes(_pio_with_root(plist).to_binary()))
        except Exception:
            return BnpConverter._deepmerge(base, plist, name)
        return F.aslist_to(F.aslist_merge(b, d))

    @staticmethod
    def _shop_merge(base, plist, name):
        if base is None:
            raise BnpError(f"SARC missing file at {name}")
        data = F.shop_from(base)
        names = _names_in(plist, {})
        for tname, table in data.items():
            names[zlib.crc32(tname.encode())] = tname
            for item in (table or {}):
                names[zlib.crc32(item.encode())] = item
        adds = plist.lists.get(A.Name("Additions"))
        rems = plist.lists.get(A.Name("Removals"))
        if adds is None or rems is None:
            raise BnpError("Shop diff missing Additions/Removals")
        for tkey, table in adds.lists.items():
            tname = _name_of(tkey, names)
            if tname is None:
                raise BnpError("Bad shop diff")
            base_t = data.get(tname)
            if base_t is None:
                base_t = data[tname] = {}
            for i, (okey, params) in enumerate(table.objects.items()):
                iname = _name_of(okey, names) or F.pstr(params, "ItemName")
                if iname is None:
                    raise BnpError("Bad shop diff")

                def g(key):
                    p = params.params.get(A.Name(key))
                    return None if p is None else p.v
                num, adjust, look, amount = g("ItemNum"), g("ItemAdjustPrice"), g("ItemLookGetFlg"), g("ItemAmount")
                cur = base_t.get(iname)
                if cur is not None:
                    cur["sort"] = i
                    if num is not None:
                        cur["num"] = int(num)
                    if adjust is not None:
                        cur["adjust_price"] = int(adjust)
                    if look is not None:
                        cur["look_get_flag"] = bool(look)
                    if amount is not None:
                        cur["amount"] = int(amount)
                else:
                    if None in (num, adjust, look, amount):
                        raise BnpError("Shop diff item missing data")
                    base_t[iname] = {"sort": i, "num": int(num), "adjust_price": int(adjust),
                                     "look_get_flag": bool(look), "amount": int(amount), "delete": False}
        for tkey, table in rems.lists.items():
            tname = _name_of(tkey, names)
            base_t = data.get(tname) if tname else None
            if base_t is None:
                continue
            for okey, _ in table.objects.items():
                iname = _name_of(okey, names)
                if iname in base_t:
                    base_t[iname]["delete"] = True
        return F.shop_to(data)

    def handle_aslist(self):
        self._aamp_log("aslist.aamp", "FileTable", self._aslist_merge, self._aslist_merge)

    def handle_deepmerge(self):
        self._aamp_log("deepmerge.aamp", "FileTable", self._deepmerge, self._deepmerge)

    def handle_shops(self):
        self._aamp_log("shop.aamp", "Filenames", self._shop_merge, self._deepmerge)

    def handle_drops(self):
        p = self.current_root / "logs/drops.json"
        if not p.exists():
            return
        text = p.read_text(encoding="utf-8")
        do_refs = "UNDERRIDE_CONST" in text
        drops = json.loads(text)
        for path, tables in drops.items():
            ref = None
            if do_refs:
                try:
                    raw = self.dump.get_bytes(path.split("//")[-1])
                    ref = A.ParameterIO.from_binary(raw) if raw is not None else None
                except Exception:
                    ref = None
            pio = F.new_pio()
            hdr = A.ParameterObject()
            hdr.params["TableNum"] = P(oead.S32(len(tables)))
            for i, name in enumerate(tables, 1):
                hdr.params[f"Table{i:02d}"] = P(oead.FixedSafeString64(name))
            F.add_obj(pio, "Header", hdr)
            for tname, table in tables.items():
                o = A.ParameterObject()
                for key, field in (("RepeatNumMin", "repeat_num_min"), ("RepeatNumMax", "repeat_num_max"),
                                   ("ApproachType", "approach_type"), ("OccurrenceSpeedType", "occurrence_speed_type")):
                    o.params[key] = P(oead.S32(int(table[field])))
                items = table.get("items") or {}
                o.params["ColumnNum"] = P(oead.S32(len(items)))
                for i, (item, prob) in enumerate(items.items(), 1):
                    if isinstance(prob, str):
                        prob = self._underride(ref, tname, item)
                        if prob is None:
                            continue
                    o.params[f"ItemName{i:02d}"] = P(oead.FixedSafeString64(item))
                    o.params[f"ItemProbability{i:02d}"] = P(oead.F32(float(prob)))
                F.add_obj(pio, tname, o)
            self.inject_into_sarc(self.trim_prefixes(path), bytes(pio.to_binary()), path.startswith("aoc"))

    @staticmethod
    def _underride(ref, table, item):
        if ref is None:
            return None
        o = F.obj(ref, table)
        if o is None:
            return None
        vals = list(o.params.items())
        for i, (_, v) in enumerate(vals):
            if F.is_str_param(v) and str(v.v) == item and i + 1 < len(vals):
                try:
                    return float(vals[i + 1][1].v)
                except Exception:
                    return None
        return None

    def handle_dungeon_static(self):
        diff = self._yaml("dstatic.yml")
        if diff is None:
            return
        base = oead.byml.from_binary(self.get_master_aoc_bytes("Map/CDungeon/Static.smubin"))
        entries = {}
        for e in F.need(base.get("StartPos"), "No StartPos"):
            entries[f"{e['Map']}___{e['PosName']}"] = e
        for k, v in diff.items():
            entries[k] = v
        out = oead.byml.Hash({"StartPos": oead.byml.Array(list(entries.values()))})
        dest = self.current_root / "aoc/0010/Map/CDungeon/Static.smubin"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(oead.yaz0.compress(F.byml_to_binary(out)))

    def handle_effects(self):
        diff = self._yaml("effects.yml")
        if diff is None:
            return
        path = "Pack/Bootup.pack//Ecosystem/StatusEffectList.sbyml"
        base = oead.byml.from_binary(self._need(self._dump_nested(path), path))
        m = oead.byml.Hash()
        for k, v in base[0].items():
            m[k] = v
        for k, v in diff.items():
            m[k] = v
        self.inject_into_sarc(path, F.byml_to_binary(oead.byml.Array([m])), False)

    def handle_events(self):
        diff = self._yaml("eventinfo.yml")
        if diff is None:
            return
        path = "Pack/Bootup.pack//Event/EventInfo.product.sbyml"
        out = F.BUILDERS["EventInfo"](D.byml_to_cbor(diff), self.get_from_master_sarc(path), "")
        self.inject_into_sarc(path, out, False)

    def handle_gamedata(self):
        diff = self._yaml("gamedata.yml")
        if diff is None:
            return
        path = "Pack/Bootup.pack//GameData/gamedata.ssarc"
        base = F.gamedata_from(self.get_from_master_sarc(path))

        def flag_from(name, flag, revival=False):
            try:
                return F.bymldata_from_byml(flag, F.FLAGDATA)
            except Exception:
                h = oead.byml.Hash()
                for k, v in flag.items():
                    h[k] = v
                h["DataName"] = name
                if revival:
                    h["DeleteRev"] = oead.S32(-1)
                return F.bymldata_from_byml(h, F.FLAGDATA)

        simple = [(f, "string_data" if f == "string32_data" else f) for f in
                  ("bool_array_data", "f32_array_data", "f32_data", "s32_array_data", "string64_array_data",
                   "string64_data", "string256_array_data", "string256_data", "vector2f_array_data",
                   "vector2f_data", "vector3f_array_data", "vector3f_data", "vector4f_data", "string32_data")]
        for field, key in simple:
            d = diff.get(key)
            if not isinstance(d, oead.byml.Hash):
                continue
            flags = base[field]["flags"]
            for name, flag in (d.get("add") or {}).items():
                try:
                    flags[name] = [flag_from(name, flag), False]
                except Exception:
                    pass
            for name in (d.get("del") or []):
                flags.pop(str(name), None)
        for field, rfield in (("bool_data", "revival_bool_data"), ("s32_data", "revival_s32_data")):
            d = diff.get(field)
            if not isinstance(d, oead.byml.Hash):
                continue
            for name, flag in (d.get("add") or {}).items():
                f = flag_from(name, flag, revival=True)
                target = rfield if name.split("_")[0] in GAMEDATA_STAGES and "HiddenKorok" not in name else field
                base[target]["flags"][str(f["DataName"])] = [f, False]
            for name in (d.get("del") or []):
                base[field]["flags"].pop(str(name), None)
                base[rfield]["flags"].pop(str(name), None)
        self.inject_into_sarc(path, F.BUILDERS["GameDataPack"](base, None, ""), False)

    def handle_savedata(self):
        diff = self._yaml("savedata.yml")
        if diff is None:
            return
        path = "Pack/Bootup.pack//GameData/savedataformat.ssarc"
        base = F.savedata_from(self.get_from_master_sarc(path))
        data = base.get("game_data.sav")
        if data is not None:
            for flag in (diff.get("add") or []):
                try:
                    data["flags"][F._i32(flag["HashValue"])] = str(flag["DataName"])
                except Exception:
                    pass
            for h in (diff.get("del") or []):
                data["flags"].pop(F._i32(h), None)
        fd = D._fd()
        cbor = {k: {"header": v["header"],
                    "flags": {fd({"name": n, "hash": h}): False for h, n in sorted(v["flags"].items())}}
                for k, v in base.items()}
        self.inject_into_sarc(path, F.BUILDERS["SaveDataPack"](cbor, None, ""), False)

    def handle_mainfield_static(self):
        diff = self._yaml("mainstatic.yml")
        if diff is None:
            return
        base = oead.byml.from_binary(self.get_master_aoc_bytes("Map/MainField/Static.smubin"))
        cats = {}
        for cat, entries in base.items():
            cats[cat] = {_ms_id(e): e for e in entries}
        for cat, entries in diff.items():
            if cat not in cats:
                raise BnpError("Base mainfield static missing category")
            for k, v in entries.items():
                cats[cat][k] = v
        out = oead.byml.Hash({cat: oead.byml.Array(list(e.values())) for cat, e in cats.items()})
        dest = self.current_root / "aoc/0010/Map/MainField/Static.smubin"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(oead.yaz0.compress(F.byml_to_binary(out)))

    def handle_quests(self):
        diff = self._yaml("quests.yml")
        if diff is None:
            return
        path = "Pack/TitleBG.pack//Quest/QuestProduct.sbquestpack"
        quests = list(oead.byml.from_binary(self._need(self._dump_nested(path), path)))
        index = {str(q["Name"]): i for i, q in enumerate(quests) if "Name" in q}
        for name, quest in (diff.get("mod") or {}).items():
            if name in index:
                quests[index[name]] = quest
            else:
                quests.append(quest)
        dels = {str(d) for d in (diff.get("del") or [])}
        quests = [q for q in quests if str(q.get("Name", "")) not in dels]
        quests += list(diff.get("add") or [])
        self.inject_into_sarc(path, oead.yaz0.compress(F.byml_to_binary(oead.byml.Array(quests))), False)

    def handle_residents(self):
        diff = self._yaml("residents.yml")
        if diff is None:
            return
        path = "Pack/Bootup.pack//Actor/ResidentActors.byml"
        model = D.residents_from(self.get_from_master_sarc(path))
        for name, d in diff.items():
            try:
                model[str(name)] = [{"only_res": bool(d["only_res"]),
                                     "scale": D.byml_to_cbor(d["scale"]) if "scale" in d else None}, False]
            except Exception:
                pass
        self.inject_into_sarc(path, F.BUILDERS["ResidentActors"](model, None, ""), False)

    def handle_texts(self):
        p = self.current_root / "logs/texts.json"
        if not p.exists():
            return
        diff = json.loads(p.read_text(encoding="utf-8"))
        if not diff:
            return
        lang = self.lang if self.lang in diff else next(
            (l for l in diff if l[2:] == self.lang[2:]), None) or next(iter(diff))
        diff = diff[lang]
        path = f"Pack/Bootup_{self.lang}.pack//Message/Msg_{self.lang}.product.ssarc"
        texts = F.msbt_sarc_to_msyt(self.get_from_master_sarc(path))
        for file, entries in diff.items():
            key = file[:-5] if file.endswith(".msyt") else file
            doc = texts.get(key)
            if doc is None:
                doc = texts[key] = {"group_count": len(entries),
                                    "atr1_unknown": 0 if "EventFlowMsg" in file else 4,
                                    "entries": {}}
            doc.setdefault("entries", {}).update(entries)
        msg = F.msyt_to_msbt_sarc(texts)
        out = self.current_root / "content" / f"Pack/Bootup_{self.lang}.pack"
        out.parent.mkdir(parents=True, exist_ok=True)
        s = SarcFiles({f"Message/Msg_{self.lang}.product.ssarc": oead.yaz0.compress(msg)})
        out.write_bytes(s.write())

    # ---------------------------------------------------------- maps
    def _maps(self):
        return self.root_maps if self.current_root == self.path else self.opt_maps

    def _has_aoc_dump(self):
        return self.dump.get_bytes("Aoc/0010/Pack/AocMainField.pack") is not None

    def set_up_temp_map_state(self):
        diff = self._yaml("map.yml")
        if diff is None:
            return
        is_root = self.current_root == self.path
        maps = self._maps()
        has_aoc = self._has_aoc_dump()
        static_pack = None
        for key in diff:
            section, _, load_type = str(key).partition("_")
            canon = f"Map/MainField/{section}/{section}_{load_type}.smubin"
            if not is_root and canon in self.root_maps:
                maps[canon] = self.root_maps[canon]
                continue
            if load_type == "Dynamic":
                data = self.dump.get_bytes(f"Aoc/0010/{canon}" if has_aoc else canon)
            elif load_type == "Static":
                if static_pack is None:
                    pack = self.dump.get_bytes("Aoc/0010/Pack/AocMainField.pack" if has_aoc else "Pack/TitleBG.pack")
                    static_pack = _files(self._need(pack, "Map archive"))
                data = static_pack.get(canon)
            else:
                raise BnpError(f"Invalid hash: {key}")
            if data is None:
                raise BnpError(f"Could not find map file {canon}")
            maps[canon] = data

    def handle_maps(self):
        diff = self._yaml("map.yml")
        if diff is None:
            return
        maps = self._maps()
        for section, d in diff.items():
            square = str(section).partition("_")[0]
            canon = f"Map/MainField/{square}/{section}.smubin"
            base = oead.byml.from_binary(unyaz(maps[canon]))
            _merge_map(base, d)
            maps[canon] = oead.yaz0.compress(F.byml_to_binary(base))

    def clear_temp_map_state(self):
        if not (self.current_root / "logs/map.yml").exists():
            return
        has_aoc = self._has_aoc_dump()
        dest = self.current_root / ("aoc/0010/Pack/AocMainField.pack" if has_aoc else "content/Pack/TitleBG.pack")
        dump_pack = _files(self.get_master_aoc_bytes("Pack/AocMainField.pack") if has_aoc
                           else self.get_master_bytes("Pack/TitleBG.pack"))
        merged = None
        if dest.is_file():
            try:
                merged = SarcFiles(_files(dest.read_bytes()))
                for k, v in dump_pack.items():
                    merged.files.setdefault(k, v)
            except Exception:
                merged = None
        if merged is None:
            merged = SarcFiles(dump_pack)
        prefix = self.current_root / ("aoc/0010" if has_aoc else "content")
        maps = self._maps()
        for canon, data in maps.items():
            if canon.endswith("_Static.smubin"):
                merged.files[canon] = data
            else:
                out = prefix / canon
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(data)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(merged.write())
        maps.clear()

    # ---------------------------------------------------------- driver
    def convert_root(self):
        packs_path = self.current_root / "logs/packs.json"
        if packs_path.exists():
            is_root = self.current_root.parent.name != "options"
            log = json.loads(packs_path.read_text(encoding="utf-8"))
            for p in log.values():
                p = str(p).replace("\\", "/")
                if p.startswith("Pack/Bootup_") and len(p) == 21:
                    continue
                pack = self.current_root / p
                if is_root:
                    self.parent_packs.add(pack)
                self.packs.add(pack)
        steps = [("actor info", self.handle_actorinfo), ("AS list", self.handle_aslist),
                 ("areadata", self.handle_areadata), ("deepmerge", self.handle_deepmerge),
                 ("drops", self.handle_drops), ("dungeon static", self.handle_dungeon_static),
                 ("eventinfo", self.handle_events), ("gamedata", self.handle_gamedata),
                 ("mainfield static", self.handle_mainfield_static), ("maps", self.handle_maps),
                 ("quests", self.handle_quests), ("residents", self.handle_residents),
                 ("savedata", self.handle_savedata), ("shops", self.handle_shops),
                 ("status effect", self.handle_effects), ("texts", self.handle_texts)]
        for what, fn in steps:
            try:
                fn()
            except Exception as e:
                raise BnpError(f"Failed to process {what} log: {type(e).__name__}: {e}") from e
        packs, self.packs = set(self.packs), set()
        for file in sorted(packs):
            if file.name == "AocMainField.pack" and file.exists() and file.stat().st_size == 0:
                continue
            rel = file.relative_to(self.current_root).as_posix()
            sarc = self.open_or_create_sarc(file, self.trim_prefixes(rel))
            file.write_bytes(compress_if(sarc.write(), file.name))

    def convert(self):
        root = self.current_root
        self.set_up_temp_map_state()
        self.convert_root()
        opt_dir = root / "options"
        if opt_dir.is_dir():
            for option in sorted(p for p in opt_dir.iterdir() if p.is_dir()):
                self.log(f"Processing BNP logs for option {option.name}")
                self.current_root = option
                self.set_up_temp_map_state()
                self.convert_root()
                self.clear_temp_map_state()
        self.current_root = root
        self.clear_temp_map_state()
        return root


def _ms_id(entry) -> str:
    """bnp::mainstatic::get_id: translation (x100000, Rust f32 formatting) + the first name-ish string."""
    t = entry["Translate"]
    key = "".join(F.rust_f32(F._f32mul(float(t[c]), 100000.0)) for c in ("X", "Y", "Z"))
    name = next((str(v) for k, v in entry.items() if "name" in str(k).lower() and isinstance(v, str)), "")
    return key + name


def _merge_map(base, diff):
    def merge_section(arr, d):
        hashes = {}
        for i, o in enumerate(arr):
            if isinstance(o, oead.byml.Hash) and "HashId" in o:
                hashes[int(o["HashId"]) & 0xFFFFFFFF] = i
        for h, entry in (d.get("mod") or {}).items():
            i = hashes.get(int(h) & 0xFFFFFFFF)
            if i is not None:
                arr[i] = entry
        dels = d.get("del")
        if dels is not None:
            dset = {int(x) & 0xFFFFFFFF for x in dels}
            keep = [o for o in arr if isinstance(o, oead.byml.Hash) and "HashId" in o
                    and (int(o["HashId"]) & 0xFFFFFFFF) not in dset]
            arr = oead.byml.Array(keep)
            hashes = {h: i for h, i in hashes.items() if h not in dset}
        for o in (d.get("add") or []):
            if isinstance(o, oead.byml.Hash) and "HashId" in o and (int(o["HashId"]) & 0xFFFFFFFF) not in hashes:
                arr.append(o)
        return arr

    for key in ("Objs", "Rails"):
        d = diff.get(key)
        if isinstance(d, oead.byml.Hash) and isinstance(base.get(key), oead.byml.Array):
            base[key] = merge_section(base[key], d)


# ------------------------------------------------------------- BNP metadata
def meta_from_info(info: dict) -> dict:
    """ModPacker::parse_info: BCML info.json -> UKMM meta (with option groups)."""
    opts = info.get("options") or {}
    groups = []
    multi = opts.get("multi") or []
    if multi:
        groups.append({"type": "Multiple", "name": "Optional Components",
                       "description": "Select one or more of these",
                       "defaults": [str(o.get("folder")) for o in multi if o.get("default")],
                       "options": [{"name": str(o.get("name", "")), "description": str(o.get("desc", "")),
                                    "path": str(o.get("folder", "")), "requires": []} for o in multi],
                       "required": False})
    for g in opts.get("single") or []:
        req = g.get("required")
        required = (req if isinstance(req, bool) else (req in ("", "false"))) if req is not None else False
        groups.append({"type": "Exclusive", "name": str(g.get("name", "")), "description": str(g.get("desc", "")),
                       "default": None,
                       "options": [{"name": str(o.get("name", "")), "description": str(o.get("desc", "")),
                                    "path": str(o.get("folder", "")), "requires": []}
                                   for o in g.get("options") or []],
                       "required": bool(required)})
    return {"name": str(info.get("name") or "Unnamed mod"), "description": str(info.get("desc") or ""),
            "version": str(info.get("version") or "1.0.0"), "category": "Other", "author": "",
            "masters": {}, "option_groups": groups, "platform": "WiiU", "url": None}


def meta_from_rules(rules_txt: str, fallback_name: str) -> dict:
    """ModPacker::parse_rules: a Cemu graphic pack's rules.txt [Definition]."""
    name, desc = fallback_name, ""
    section = None
    for line in rules_txt.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if section == "definition" and "=" in line:
            k, _, v = line.partition("=")
            k, v = k.strip().lower(), v.strip().strip('"')
            if k == "name" and v:
                name = v
            elif k == "description":
                desc = v
    return {"name": name, "description": desc, "version": "0.1.0", "category": "Other", "author": "",
            "masters": {}, "option_groups": [], "platform": "WiiU", "url": None}


def convert_bnp_folder(root: pathlib.Path, dump: Dump, lang="USen", log=None) -> dict:
    """Converts an extracted BNP in place. Returns its UKMM meta."""
    root = pathlib.Path(root)
    if (root / "rules.txt").exists() and not (root / "info.json").exists():
        raise BnpError("This is a BCML 2.x mod package, which Korok can't convert. "
                       "Open it in BCML 3 once to upgrade it, or use a newer release of the mod.")
    BnpConverter(root, dump, lang, log).convert()
    info = json.loads((root / "info.json").read_text(encoding="utf-8-sig"))
    return meta_from_info(info)


def load_yaml(text):
    return yaml.safe_load(text)
