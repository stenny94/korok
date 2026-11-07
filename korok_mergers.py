"""More BCML merge logs for Korok (ported from BCML's own mergers).

A BNP does not ship the files that BCML's mergers rebuild (ActorInfo, gamedata,
save data, area data, drop tables, shrine entrances ...).  It ships a small
diff in logs/ instead.  Here each diff is applied to the vanilla game file and
the result is written into the Korok pack.

Supported logs:
  actorinfo.yml  -> content/Actor/ActorInfo.product.sbyml
  gamedata.yml   -> content/Pack/Bootup.pack  (GameData/gamedata.ssarc)
  savedata.yml   -> content/Pack/Bootup.pack  (GameData/savedataformat.ssarc)
  areadata.yml   -> content/Pack/Bootup.pack  (Ecosystem/AreaData.sbyml)
  dstatic.yml    -> aoc/0010/Map/CDungeon/Static.smubin  (or Bootup.pack without DLC)
  drops.json     -> content/Actor/Pack/<actor>.sbactorpack  (DropTable .bdrop)

Everything runs in child processes (a hang or crash in oead can't freeze Korok)
and results are cached by the hash of the logs + the source file.
"""
import concurrent.futures
import json
import math
import os
import pathlib
import re
import zlib
from math import ceil

try:
    import oead
except ImportError:  # optional
    oead = None

import korok_merge as km

BYML_LOGS = {"actorinfo.yml", "gamedata.yml", "savedata.yml", "areadata.yml", "dstatic.yml",
             "residents.yml", "mainstatic.yml", "quests.yml", "map.yml"}
JSON_LOGS = {"drops.json"}
MAINSTATIC_REL = "Map/MainField/Static.smubin"
RSTB_DELETE = []        # RSTB entry names the engine should drop (filled by apply_merge_logs)
RSTB_SET = {}           # {RSTB entry name: size} for every file rebuilt here (BCML sets these too)
NEW_LOGS = BYML_LOGS | JSON_LOGS

BOOTUP_REL = "content/Pack/Bootup.pack"
ACTORINFO_REL = "content/Actor/ActorInfo.product.sbyml"
DSTATIC_REL = "Map/CDungeon/Static.smubin"
STAGE_TIMEOUT = 1200


def available() -> bool:
    return oead is not None


# ------------------------------------------------------------------ helpers
def _is_map(x):
    return isinstance(x, (dict, oead.byml.Hash))


def _is_list(x):
    return isinstance(x, (list, oead.byml.Array))


def dict_merge(dct, merge_dct, overwrite_lists=False, shallow=False):
    """BCML util.dict_merge."""
    for k in list(merge_dct.keys()):
        if shallow:
            dct[k] = merge_dct[k]
        elif k in dct and _is_map(dct[k]) and _is_map(merge_dct[k]):
            dict_merge(dct[k], merge_dct[k], overwrite_lists=overwrite_lists)
        elif k in dct and _is_list(dct[k]) and _is_list(merge_dct[k]):
            if overwrite_lists:
                dct[k] = merge_dct[k]
            else:
                dct[k].extend(merge_dct[k])
        else:
            dct[k] = merge_dct[k]


def _unyaz(data: bytes) -> bytes:
    return bytes(oead.yaz0.decompress(data)) if data[:4] == b"Yaz0" else bytes(data)


def _read_byml_text(path) -> "oead.byml.Hash":
    return oead.byml.from_text(pathlib.Path(path).read_text(encoding="utf-8"))


def _pack_file(pack_path, inner):
    """Raw (decompressed-pack) bytes of `inner` in a SARC/pack on disk."""
    sarc = oead.Sarc(_unyaz(pathlib.Path(pack_path).read_bytes()))
    f = sarc.get_file(inner)
    if f is None:
        raise FileNotFoundError(f"{inner} not found in {pathlib.Path(pack_path).name}")
    return bytes(f.data)


def _write_atomic(path: pathlib.Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()           # may be a hard link into staging
    path.write_bytes(data)


# ================================================================ actorinfo
def merge_actorinfo(stock_bytes: bytes, diffs: list) -> bytes:
    stock = oead.byml.from_binary(_unyaz(stock_bytes))
    base = {}
    for actor in stock["Actors"]:
        base[zlib.crc32(str(actor["name"]).encode("utf-8")) & 0xFFFFFFFF] = actor
    all_diffs = oead.byml.Hash()
    inst_sizes = {}
    for diff in diffs:
        for h, a in diff.items():
            if _is_map(a) and "instSize" in a:
                v = int(a["instSize"].v if hasattr(a["instSize"], "v") else a["instSize"])
                if v > inst_sizes.get(h, 0):
                    inst_sizes[h] = v
        dict_merge(all_diffs, diff, overwrite_lists=True)
    for h, v in inst_sizes.items():
        all_diffs[h]["instSize"] = oead.S32(v)
    for h, actor in all_diffs.items():
        key = int(h)
        cur = base.get(key)
        if cur is not None and _is_map(cur) and _is_map(actor):
            dict_merge(cur, actor, overwrite_lists=True)     # arrays replaced
        else:
            base[key] = actor
    hashes = oead.byml.Array()
    actors = oead.byml.Array()
    for key in sorted(base):
        hashes.append(oead.S32(key) if key < 2 ** 31 else oead.U32(key))
        actors.append(base[key])
    out = oead.byml.Hash({"Actors": actors, "Hashes": hashes})
    return bytes(oead.yaz0.compress(bytes(oead.byml.to_binary(out, big_endian=True))))


def _w_actorinfo(conn, log_paths, src, out):
    try:
        diffs = [_read_byml_text(p) for p in log_paths]
        data = merge_actorinfo(pathlib.Path(src).read_bytes(), diffs)
        pathlib.Path(out).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(out).write_bytes(data)
        conn.send(("ok", sum(len(d) for d in diffs), len(data)))
    except Exception as e:
        conn.send(("err", f"{type(e).__name__}: {e}", 0))
    finally:
        conn.close()


# ================================================================ gamedata
def _consolidate_gamedata(sarc) -> "oead.byml.Hash":
    data = oead.byml.Hash()
    for f in sarc.get_files():
        d = oead.byml.from_binary(bytes(f.data))
        for k, v in d.items():
            if k in data:
                data[k].extend(v)
            else:
                data[k] = v
    return data


def merge_gamedata(stock_ssarc: bytes, diffs: list) -> bytes:
    """Returns the new (uncompressed) gamedata SARC."""
    stock = _consolidate_gamedata(oead.Sarc(_unyaz(stock_ssarc)))
    modded = oead.byml.Hash()
    for d in diffs:
        dict_merge(modded, d, overwrite_lists=True)
    merged = {}
    for data_type, entries in stock.items():
        h = oead.byml.Hash()
        for entry in entries:
            h[entry["DataName"]] = entry
        merged[data_type] = h
    for data_type in [t for t in merged if t in modded]:
        diff = modded[data_type]
        dict_merge(merged[data_type], diff["add"], shallow=True)
        for entry in diff["del"]:
            if entry in merged[data_type]:
                del merged[data_type][entry]
    writer = oead.SarcWriter(endian=oead.Endianness.Big)
    for data_type, entries in merged.items():
        values = [v for _, v in entries.items()]
        n = ceil(len(values) / 4096)
        for i in range(n):
            chunk = oead.byml.Array(values[i * 4096:(i + 1) * 4096])
            writer.files[f"/{data_type}_{i}.bgdata"] = bytes(
                oead.byml.to_binary(oead.byml.Hash({data_type: chunk}), big_endian=True))
    return bytes(writer.write()[1])


# ================================================================ savedata
def merge_savedata(stock_ssarc: bytes, diffs: list) -> bytes:
    savedata = oead.Sarc(_unyaz(stock_ssarc))
    all_files = sorted(savedata.get_files(), key=lambda f: f.name)
    files, tail = all_files[0:-2], all_files[-2:]
    all_add, all_del, seen = [], [], set()
    for diff in reversed(diffs):                       # later mods win
        for entry in diff["add"]:
            hv = entry["HashValue"].v
            if hv not in seen:
                all_add.append(entry)
            seen.add(hv)
        for item in diff["del"]:
            v = item.v if hasattr(item, "v") else item
            if v not in all_del:
                all_del.append(v)
    del_ids = set(all_del)
    entries = {}
    for f in files:
        for e in oead.byml.from_binary(bytes(f.data))["file_list"][1]:
            entries[e["HashValue"].v] = e
    for e in all_add:
        entries[e["HashValue"].v] = e
    merged = sorted((e for hv, e in entries.items() if hv not in del_ids),
                    key=lambda e: e["HashValue"].v)
    writer = oead.SarcWriter(endian=oead.Endianness.Big)
    n = ceil(len(merged) / 8192)
    for i in range(n):
        chunk = merged[i * 8192:(i + 1) * 8192]
        data = oead.byml.Hash({
            "file_list": oead.byml.Array([
                oead.byml.Hash({
                    "IsCommon": False, "IsCommonAtSameAccount": False,
                    "IsSaveSecureCode": True, "file_name": "game_data.sav"}),
                oead.byml.Array(chunk)]),
            "save_info": oead.byml.Array([
                oead.byml.Hash({
                    "directory_num": oead.S32(8), "is_build_machine": True,
                    "revision": oead.S32(18203)})]),
        })
        writer.files[f"/saveformat_{i}.bgsvdata"] = bytes(oead.byml.to_binary(data, big_endian=True))
    writer.files[f"/saveformat_{n}.bgsvdata"] = bytes(tail[0].data)
    writer.files[f"/saveformat_{n + 1}.bgsvdata"] = bytes(tail[1].data)
    return bytes(writer.write()[1])


# ================================================================ areadata
def merge_areadata(stock_sbyml: bytes, diffs: list) -> bytes:
    areas = oead.byml.from_binary(_unyaz(stock_sbyml))
    base = oead.byml.Hash({str(a["AreaNumber"].v): a for a in areas})
    modded = oead.byml.Hash()
    for d in diffs:
        dict_merge(modded, d, overwrite_lists=True)
    dict_merge(base, modded, overwrite_lists=True)
    arr = oead.byml.Array([v for _, v in sorted(base.items(), key=lambda kv: int(kv[0]))])
    return bytes(oead.yaz0.compress(bytes(oead.byml.to_binary(arr, big_endian=True))))


# ================================================================ dungeon static
def merge_dstatic(stock_smubin: bytes, diffs: list) -> bytes:
    static = oead.byml.from_binary(_unyaz(stock_smubin))
    consolidated = {}
    for d in diffs:
        consolidated.update(dict(d.items()))
    pos = static["StartPos"]
    names = [f"{pos[i]['Map']}___{pos[i]['PosName']}" for i in range(len(pos))]
    for spawn_name, diff in consolidated.items():
        if "___" not in spawn_name:
            spawn_name = f"{spawn_name}___Entrance_1"
        if spawn_name not in names:
            pos.append(diff)
        else:
            tgt = pos[names.index(spawn_name)]
            for k, v in diff.items():
                tgt[k] = v
    return bytes(oead.yaz0.compress(bytes(oead.byml.to_binary(static, big_endian=True))))



# ================================================================ residents
def merge_residents(stock_bytes: bytes, diffs: list) -> bytes:
    """ResidentActors.byml (NOT compressed in Bootup.pack)."""
    raw = _unyaz(stock_bytes)
    stock = oead.byml.from_binary(raw)
    residents = oead.byml.Hash({str(a["name"]): a for a in stock})
    cons = oead.byml.Hash()
    for d in diffs:
        dict_merge(cons, d, overwrite_lists=True)
    dict_merge(residents, cons, overwrite_lists=True)
    arr = oead.byml.Array([v for _, v in residents.items() if "remove" not in v])
    return bytes(oead.byml.to_binary(arr, big_endian=True))


# ================================================================ main field static
def _key_from_coords(x, y, z) -> str:
    return str(math.ceil(x)) + str(math.ceil(y)) + str(math.ceil(z))


def _static_id(item) -> str:
    name = ""
    for k, v in item.items():
        if "name" in k.lower():
            name = v
            break
    t = item["Translate"]
    return _key_from_coords(t["X"].v, t["Y"].v, t["Z"].v) + str(name)


def merge_mainstatic(stock_smubin: bytes, diffs: list) -> bytes:
    stock = oead.byml.from_binary(_unyaz(stock_smubin))
    cons = oead.byml.Hash()
    for d in diffs:
        dict_merge(cons, d, overwrite_lists=True)
    merged = oead.byml.Hash()
    for cat in stock:
        if cat in cons:
            items = {_static_id(i): i for i in stock[cat]}
            dict_merge(items, cons[cat])
            merged[cat] = oead.byml.Array([i for _, i in items.items() if "remove" not in i])
        else:
            merged[cat] = stock[cat]
    return bytes(oead.yaz0.compress(bytes(oead.byml.to_binary(merged, big_endian=True))))


def _w_mainstatic(conn, log_paths, src, out):
    try:
        diffs = [_read_byml_text(p) for p in log_paths]
        data = merge_mainstatic(pathlib.Path(src).read_bytes(), diffs)
        pathlib.Path(out).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(out).write_bytes(data)
        conn.send(("ok", len(diffs), len(data)))
    except Exception as e:
        conn.send(("err", f"{type(e).__name__}: {e}", 0))
    finally:
        conn.close()


# ================================================================ quests
QUEST_INNER = "Quest/QuestProduct.sbquestpack"


def _consolidate_quests(diffs: list):
    cons = {"add": [], "mod": {}, "del": []}
    added = set()
    for diff in reversed(diffs):
        for add in diff.get("add", []):
            if str(add["Name"]) not in added:
                cons["add"].append(add)
                added.add(str(add["Name"]))
        for name, mod in diff.get("mod", {}).items():
            cons["mod"][name] = mod
        for d in diff.get("del", []):
            if str(d) not in cons["del"]:
                cons["del"].append(str(d))
    return cons


def merge_quests(stock_bytes: bytes, diffs: list) -> bytes:
    """stock_bytes: decompressed or compressed QuestProduct byml. Returns plain BYML bytes."""
    quests = oead.byml.from_binary(_unyaz(stock_bytes))
    stock_names = [str(q["Name"]) for q in quests]
    cons = _consolidate_quests(diffs)
    adds = list(cons["add"])
    for name, mod in cons["mod"].items():
        if name in stock_names:
            quests[stock_names.index(name)] = mod
        else:
            adds.append(mod)
    for d in reversed(cons["del"]):
        if d in stock_names:
            idx = stock_names.index(d)
            # names index refers to the original list, remove by name from current list
            for j, q in enumerate(quests):
                if str(q["Name"]) == d:
                    quests.pop(j)
                    break
    seen = set()
    for add in adds:
        nm = str(add["Name"])
        if nm in seen:
            continue
        idx = len(quests)
        if "prev_quest" in add:
            prev = str(add["prev_quest"])
            if prev == "--index_zero":
                idx = 0
            else:
                names = [str(q["Name"]) for q in quests]
                if prev in names:
                    idx = names.index(prev) + 1
            del add["prev_quest"]
        quests.insert(idx, add)
        seen.add(nm)
    return bytes(oead.byml.to_binary(quests, big_endian=True))


def _w_quests(conn, log_paths, src_pack, out_pack):
    try:
        src_bytes = pathlib.Path(src_pack).read_bytes()
        wrapped = src_bytes[:4] == b"Yaz0"
        raw = _unyaz(src_bytes)
        pack = oead.Sarc(raw)
        f = pack.get_file(QUEST_INNER)
        if f is None:
            raise FileNotFoundError(f"{QUEST_INNER} not in {pathlib.Path(src_pack).name}")
        diffs = [_read_byml_text(p) for p in log_paths]
        new = merge_quests(bytes(f.data), diffs)
        writer = km._new_writer(pack, raw)
        writer.files[QUEST_INNER] = bytes(oead.yaz0.compress(new))
        out = bytes(writer.write()[1])
        if wrapped:
            out = bytes(oead.yaz0.compress(out))
        pathlib.Path(out_pack).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(out_pack).write_bytes(out)
        conn.send(("ok", f"{len(diffs)} log(s)", len(out)))
    except Exception as e:
        conn.send(("err", f"{type(e).__name__}: {e}", 0))
    finally:
        conn.close()


# ================================================================ shop data
SHOP_KEYS = ["ItemName", "ItemNum", "ItemAdjustPrice", "ItemLookGetFlg", "ItemAmount"]


def _is_string_param(p) -> bool:
    return p.type() in (oead.aamp.Parameter.Type.String32, oead.aamp.Parameter.Type.String64,
                        oead.aamp.Parameter.Type.String256, oead.aamp.Parameter.Type.StringRef)


def _make_shopdata(pio):
    A = oead.aamp
    name_table = A.get_default_name_table()
    shopdata = A.ParameterList()
    tables = [str(t.v) for _, t in pio.objects["Header"].params.items() if _is_string_param(t)]
    if not tables:
        raise ValueError("a shop file is invalid: has no tables")
    shopdata.objects["TableNames"] = A.ParameterObject()
    for table in tables:
        table_plist = A.ParameterList()
        table_hash = zlib.crc32(table.encode())
        tobj = pio.objects[table_hash]
        items = {str(p.v): k.hash for k, p in tobj.params.items() if _is_string_param(p)}
        total = len(tobj.params)
        for item in items:
            no = int(name_table.get_name(items[item], total, table_hash).replace("ItemName", ""))
            item_obj = A.ParameterObject()
            for key in SHOP_KEYS:
                item_obj.params[key] = tobj.params[f"{key}{no:03d}"]
            table_plist.objects[item] = item_obj
        if table_plist.objects:
            shopdata.objects["TableNames"].params[table] = A.Parameter(oead.FixedSafeString64(table))
            shopdata.lists[table_hash] = table_plist
    return shopdata


def _subtract_plists(plist, other):
    for key in other.lists.keys():
        if key in plist.lists:
            for key2 in other.lists[key].objects.keys():
                if key2 in plist.lists[key].objects:
                    del plist.lists[key].objects[key2]
            if not plist.lists[key].lists and not plist.lists[key].objects:
                del plist.lists[key]


def _merge_plists(plist, other):
    for key, sub in other.lists.items():
        if key in plist.lists:
            _merge_plists(plist.lists[key], sub)
        else:
            plist.lists[key] = sub
    for key, obj in other.objects.items():
        if key in plist.objects:
            for param, value in obj.params.items():
                plist.objects[key].params[param] = value
        else:
            plist.objects[key] = obj


def merge_shopdata(pio, plist):
    """pio: the shop file as ParameterIO; plist: the diff list (Additions/Removals)."""
    A = oead.aamp
    shopdata = _make_shopdata(pio)
    if "Removals" in plist.lists:
        _subtract_plists(shopdata, plist.lists["Removals"])
    if "Additions" in plist.lists:
        _merge_plists(shopdata, plist.lists["Additions"])
    bshop = A.ParameterIO()
    bshop.type = "xml"
    tables = [str(t.v) for _, t in shopdata.objects["TableNames"].params.items()]
    bshop.objects["Header"] = A.ParameterObject()
    bshop.objects["Header"].params["TableNum"] = A.Parameter(oead.S32(len(tables)))
    for i, table in enumerate(tables, 1):
        table_hash = zlib.crc32(table.encode())
        bshop.objects["Header"].params[f"Table{i:02d}"] = A.Parameter(oead.FixedSafeString64(table))
        tobj = A.ParameterObject()
        items = [it for _, it in shopdata.lists[table_hash].objects.items()]
        tobj.params["ColumnNum"] = A.Parameter(oead.S32(len(items)))
        for j, item in enumerate(items, 1):
            tobj.params[f"ItemSort{j:03d}"] = A.Parameter(oead.S32(j - 1))
            for key in SHOP_KEYS:
                tobj.params[f"{key}{j:03d}"] = item.params[key]
        if tobj.params:
            bshop.objects[table_hash] = tobj
    return bshop


# ================================================================ map units
def _unit_split(name: str):
    parts = name.split("_")
    if len(parts) != 2 or parts[1] not in ("Static", "Dynamic"):
        raise ValueError(f"not a map unit name: {name}")
    return parts[0], parts[1]


def _consolidate_maps(diffs: list) -> dict:
    """diffs: list of Hash {unit: {Objs, Rails}}; returns {unit: {Objs:{add,mod,del}, Rails:{...}}}."""
    per = {}
    for d in diffs:
        for unit, ch in d.items():
            if "Objs" not in ch and "Rails" not in ch:      # legacy: the value IS the Objs diff
                ch = oead.byml.Hash({"Objs": ch, "Rails": oead.byml.Hash()})
            per.setdefault(unit, []).append(ch)
    out = {}
    for unit, mods in per.items():
        c = {}
        for section in ("Objs", "Rails"):
            dels = []
            seen = set()
            for m in mods:
                if section in m and "del" in m[section]:
                    for h in m[section]["del"]:
                        v = _num(h) & 0xFFFFFFFF
                        if v not in seen:
                            seen.add(v)
                            dels.append(v)
            mod = {}
            for m in mods:
                if section in m and "mod" in m[section]:
                    for hid, actor in m[section]["mod"].items():
                        mod[hid] = actor
            adds, added = [], set()
            for m in reversed(mods):
                if section in m and "add" in m[section]:
                    for actor in m[section]["add"]:
                        hid = _hid(actor)
                        if hid not in added:
                            added.add(hid)
                            adds.append(actor)
            c[section] = {"add": adds, "mod": mod, "del": dels}
        out[unit] = c
    return out


def _num(v) -> int:
    try:
        return int(v.v)
    except Exception:
        return int(v)


def _hid(e) -> int:
    return _num(e["HashId"]) & 0xFFFFFFFF


def _merge_entries(diff: dict, entries):
    entries = oead.byml.Array(list(entries))
    stock_hashes = [_hid(e) for e in entries]
    stock_set = set(stock_hashes)
    orphans = []
    for hid, entry in diff["mod"].items():
        h = int(hid)
        if h in stock_set:
            entries[stock_hashes.index(h)] = entry
        else:
            orphans.append(entry)
    to_del = sorted({stock_hashes.index(h) for h in diff["del"] if h in stock_set}, reverse=True)
    for i in to_del:
        entries.pop(i)
    for e in list(diff["add"]) + orphans:
        if _hid(e) not in stock_set:
            entries.append(e)
    return oead.byml.Array(sorted(list(entries), key=_hid))


def merge_map_unit(stock_smubin: bytes, diff: dict) -> bytes:
    m = oead.byml.from_binary(_unyaz(stock_smubin))
    if "Objs" in m and diff.get("Objs"):
        m["Objs"] = _merge_entries(diff["Objs"], m["Objs"])
    if "Rails" in m and len(m["Rails"]) and diff.get("Rails"):
        m["Rails"] = _merge_entries(diff["Rails"], m["Rails"])
    return bytes(oead.yaz0.compress(bytes(oead.byml.to_binary(m, big_endian=True))))


MAP_REL_DIR = "Map/MainField"
AOC_PACK_REL = "aoc/Pack/AocMainField.pack"


def _where(e, step: str = "") -> str:
    import traceback
    try:
        fr = traceback.extract_tb(e.__traceback__)[-1]
        return (f"{type(e).__name__}: {e} [{step + ', ' if step else ''}"
                f"line {fr.lineno}: {(fr.line or '').strip()[:90]}]")
    except Exception:
        return f"{type(e).__name__}: {e}"


def _w_maps(conn, log_paths, units, src_pack, dyn_srcs, out_pack, out_dir):
    """units: list of unit names to merge. src_pack: vanilla AocMainField.pack (static maps).
    dyn_srcs: {unit: path of vanilla *_Dynamic.smubin}. Writes the pack to out_pack and
    dynamic maps to out_dir/<Section>/<unit>.smubin.  A unit that fails is skipped and reported.
    Sends ("ok", {"msg": str, "sizes": {unit: uncompressed size}}, pack size)."""
    step = "start"
    try:
        step = "read logs"
        diffs = [_read_byml_text(p) for p in log_paths]
        step = "consolidate"
        cons = _consolidate_maps(diffs)
        failed = []
        sizes = {}
        n_dyn = 0
        for unit in units:
            sec, kind = _unit_split(unit)
            if unit not in cons or kind != "Dynamic":
                continue
            src = dyn_srcs.get(unit)
            if not src:
                continue
            try:
                step = f"{unit} read"
                raw0 = pathlib.Path(src).read_bytes()
                step = f"{unit} merge"
                data = merge_map_unit(raw0, cons[unit])
                step = f"{unit} write"
                dest = pathlib.Path(out_dir) / sec / f"{unit}.smubin"
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                sizes[unit] = _uncompressed_len(data)
                n_dyn += 1
            except Exception as e:
                failed.append(f"{unit}: {_where(e, step)}")
        stat_units = [u for u in units if u in cons and u.endswith("_Static")]
        out_size = 0
        n_stat = 0
        if stat_units:
            step = "open AocMainField.pack"
            src_bytes = pathlib.Path(src_pack).read_bytes()
            wrapped = src_bytes[:4] == b"Yaz0"
            raw = _unyaz(src_bytes)
            pack = oead.Sarc(raw)
            writer = km._new_writer(pack, raw)
            for unit in stat_units:
                sec, _ = _unit_split(unit)
                inner = f"{MAP_REL_DIR}/{sec}/{unit}.smubin"
                f = pack.get_file(inner)
                if f is None:
                    failed.append(f"{unit}: not in AocMainField.pack")
                    continue
                try:
                    step = f"{unit} merge"
                    data = merge_map_unit(bytes(f.data), cons[unit])
                    writer.files[inner] = data
                    sizes[unit] = _uncompressed_len(data)
                    n_stat += 1
                except Exception as e:
                    failed.append(f"{unit}: {_where(e, step)}")
            step = "write pack"
            out = bytes(writer.write()[1])
            if wrapped:
                out = bytes(oead.yaz0.compress(out))
            pathlib.Path(out_pack).parent.mkdir(parents=True, exist_ok=True)
            pathlib.Path(out_pack).write_bytes(out)
            out_size = len(out)
        msg = f"{n_stat} static + {n_dyn} dynamic"
        if failed:
            msg += f"; {len(failed)} FAILED: " + " | ".join(failed[:4])
        conn.send(("ok", {"msg": msg, "sizes": sizes}, out_size))
    except Exception as e:
        conn.send(("err", _where(e, step), 0))
    finally:
        conn.close()


# ------------------------------------------------------------ RSTB sizes
_SARC_LIKE = {"sarc", "pack", "bactorpack", "bmodelsh", "beventpack", "stera", "stats"}


def _uncompressed_len(data: bytes) -> int:
    if data[:4] == b"Yaz0":
        return int(oead.yaz0.get_header(bytes(data[:16])).uncompressed_size)
    return len(data)


def _rstb_name(path: str) -> str:
    """'GameData/gamedata.ssarc' -> 'GameData/gamedata.sarc' (RSTB names have no Yaz0 'one')."""
    base, dot, ext = path.rpartition(".")
    if ext.startswith("s") and ext not in ("sarc",):
        ext = ext[1:]
    return f"{base}.{ext}"


def _rstb_size(uncompressed: int, ext: str) -> int:
    """BCML/rstb crate formula for the Wii U (types with a plain factory entry)."""
    size = (int(uncompressed) + 31) & -32
    size += 0xE4 + (0x3C if ext in _SARC_LIKE else 0x20)
    return size


def _note_size(path: str, data_len_uncompressed: int, name_prefix: str = ""):
    name = name_prefix + _rstb_name(path)
    ext = name.rpartition(".")[2]
    RSTB_SET[name] = _rstb_size(data_len_uncompressed, ext)


def _note_file(path: str, file_path, name_prefix: str = ""):
    try:
        with open(file_path, "rb") as f:
            head = f.read(16)
        _note_size(path, _uncompressed_len(head) if head[:4] == b"Yaz0" else os.path.getsize(file_path),
                   name_prefix)
    except Exception as e:
        km._log(f"rstb size for {path}: {e}")


def _note_inner(pack_path, inners, prefix: str = ""):
    """Record sizes of files inside a pack (the pack is opened once)."""
    try:
        raw = _unyaz(pathlib.Path(pack_path).read_bytes())
        sarc = oead.Sarc(raw)
        for inner in inners:
            f = sarc.get_file(inner)
            if f is not None:
                d = bytes(f.data)
                _note_size(inner, _uncompressed_len(d), prefix)
    except Exception as e:
        km._log(f"rstb sizes from {pack_path}: {e}")


# ------------------------------------------------------------ bootup worker
def _w_bootup(conn, jobs, src_pack, out_pack, dstatic_src):
    """jobs: {'gamedata': [paths], 'savedata': [...], 'areadata': [...], 'dstatic': [...]}.
    dstatic_src: path of a DLC Static.smubin, '' to edit the copy inside Bootup.pack."""
    try:
        src_bytes = pathlib.Path(src_pack).read_bytes()
        wrapped = src_bytes[:4] == b"Yaz0"
        raw = _unyaz(src_bytes)
        pack = oead.Sarc(raw)
        writer = km._new_writer(pack, raw)
        done = []

        def stock(inner):
            f = pack.get_file(inner)
            if f is None:
                raise FileNotFoundError(f"{inner} not found in {pathlib.Path(src_pack).name}")
            return bytes(f.data)

        if jobs.get("gamedata"):
            diffs = [_read_byml_text(p) for p in jobs["gamedata"]]
            new = merge_gamedata(stock("GameData/gamedata.ssarc"), diffs)
            writer.files["GameData/gamedata.ssarc"] = bytes(oead.yaz0.compress(new))
            done.append("gamedata")
        if jobs.get("savedata"):
            diffs = [_read_byml_text(p) for p in jobs["savedata"]]
            new = merge_savedata(stock("GameData/savedataformat.ssarc"), diffs)
            writer.files["GameData/savedataformat.ssarc"] = bytes(oead.yaz0.compress(new))
            done.append("savedata")
        if jobs.get("areadata"):
            diffs = [_read_byml_text(p) for p in jobs["areadata"]]
            writer.files["Ecosystem/AreaData.sbyml"] = merge_areadata(stock("Ecosystem/AreaData.sbyml"), diffs)
            done.append("areadata")
        if jobs.get("residents"):
            diffs = [_read_byml_text(p) for p in jobs["residents"]]
            writer.files["Actor/ResidentActors.byml"] = merge_residents(stock("Actor/ResidentActors.byml"), diffs)
            done.append("residents")
        if jobs.get("mainstatic"):
            diffs = [_read_byml_text(p) for p in jobs["mainstatic"]]
            writer.files[MAINSTATIC_REL] = merge_mainstatic(stock(MAINSTATIC_REL), diffs)
            done.append("mainstatic")
        if jobs.get("dstatic") and not dstatic_src:
            diffs = [_read_byml_text(p) for p in jobs["dstatic"]]
            writer.files[DSTATIC_REL] = merge_dstatic(stock(DSTATIC_REL), diffs)
            done.append("dstatic")
        out = bytes(writer.write()[1])
        if wrapped:
            out = bytes(oead.yaz0.compress(out))
        pathlib.Path(out_pack).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(out_pack).write_bytes(out)
        conn.send(("ok", ",".join(done), len(out)))
    except Exception as e:
        conn.send(("err", f"{type(e).__name__}: {e}", 0))
    finally:
        conn.close()


def _w_dstatic(conn, log_paths, src, out):
    try:
        diffs = [_read_byml_text(p) for p in log_paths]
        data = merge_dstatic(pathlib.Path(src).read_bytes(), diffs)
        pathlib.Path(out).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(out).write_bytes(data)
        conn.send(("ok", len(diffs), len(data)))
    except Exception as e:
        conn.send(("err", f"{type(e).__name__}: {e}", 0))
    finally:
        conn.close()


# ================================================================ drops
def _drop_to_dict(pio) -> dict:
    out = {}
    header = pio.objects["Header"]
    for pname, table in header.params.items():
        if str(pname) == "TableNum" or str(table.v) not in pio.objects:
            continue
        # (Table names are plain strings; ParameterIO objects are looked up by name hash)
        tname = str(table.v)
        obj = pio.objects[tname]
        items = {}
        nparams = len(obj.params)
        for i in range(1, int((nparams - 5) / 2) + 1):
            nk, pk = f"ItemName{i:02}", f"ItemProbability{i:02}"
            if nk in obj.params:
                items[str(obj.params[nk].v)] = float(obj.params[pk].v)
        out[tname] = {
            "repeat_num_min": int(obj.params["RepeatNumMin"].v),
            "repeat_num_max": int(obj.params["RepeatNumMax"].v),
            "approach_type": int(obj.params["ApproachType"].v),
            "occurrence_speed_type": int(obj.params["OccurrenceSpeedType"].v),
            "items": items,
        }
    return out


def _dict_to_drop(drop_dict: dict):
    P = oead.aamp.Parameter
    pio = oead.aamp.ParameterIO()
    pio.type = "xml"
    header = oead.aamp.ParameterObject()
    header.params["TableNum"] = P(len(drop_dict))
    for i, table in enumerate(drop_dict):
        header.params[f"Table{i + 1:02}"] = P(oead.FixedSafeString64(table))
    pio.objects["Header"] = header
    for table, c in drop_dict.items():
        o = oead.aamp.ParameterObject()
        o.params["RepeatNumMin"] = P(int(c["repeat_num_min"]))
        o.params["RepeatNumMax"] = P(int(c["repeat_num_max"]))
        o.params["ApproachType"] = P(int(c["approach_type"]))
        o.params["OccurrenceSpeedType"] = P(int(c["occurrence_speed_type"]))
        o.params["ColumnNum"] = P(len(c["items"]))
        for idx, (item, prob) in enumerate(c["items"].items()):
            o.params[f"ItemName{idx + 1:02}"] = P(oead.FixedSafeString64(item))
            o.params[f"ItemProbability{idx + 1:02}"] = P(float(prob))
        pio.objects[table] = o
    return pio


def merge_drop_table(ref_bytes, drop_table: dict) -> bytes:
    """ref_bytes: the stock .bdrop (or None).  Returns the new .bdrop."""
    if ref_bytes:
        try:
            ref = _drop_to_dict(oead.aamp.ParameterIO.from_binary(ref_bytes))
            for table in list(ref):
                if table not in drop_table:
                    del ref[table]
                else:
                    for item in list(ref[table]["items"]):
                        if item not in drop_table[table]["items"]:
                            del ref[table]["items"][item]
            dict_merge(ref, drop_table)
            drop_table = ref
        except Exception:
            pass
    return bytes(_dict_to_drop(drop_table).to_binary())


def _w_drops(conn, items, out_dir):
    """items: [(actor, inner_bdrop_path, table_json, src_pack_path, out_pack_path)]"""
    try:
        n = 0
        for actor, inner, table, src, out in items:
            raw = _unyaz(pathlib.Path(src).read_bytes())
            sarc = oead.Sarc(raw)
            f = sarc.get_file(inner)
            ref = bytes(f.data) if f is not None else None
            new = merge_drop_table(ref, table)
            w = km._new_writer(sarc, raw)
            w.files[inner] = new
            data = bytes(oead.yaz0.compress(bytes(w.write()[1])))
            outp = pathlib.Path(out)
            outp.parent.mkdir(parents=True, exist_ok=True)
            if outp.exists():
                outp.unlink()
            outp.write_bytes(data)
            n += 1
        conn.send(("ok", n, 0))
    except Exception as e:
        conn.send(("err", f"{type(e).__name__}: {e}", 0))
    finally:
        conn.close()


# ================================================================ driver
def _logs_by_name(log_dirs):
    """{log name: [paths in mod order]} for the logs this module handles."""
    out = {}
    for d in log_dirs:
        for name in sorted(NEW_LOGS):
            p = pathlib.Path(d) / name
            if p.is_file():
                out.setdefault(name, []).append(p)
    return out


def _stage(label, worker, args, key_parts, out_path, problems, report):
    """Cache check -> child process -> install into the pack. Returns True on success."""
    key = km._cache_key(label, km.CACHE_VERSION, *key_parts)
    if km._cache_get(key, out_path):
        km._log(f"{label}: cached")
        return True
    tmp = out_path.with_name(out_path.name + ".korok_tmp")
    status, info, size = km._run_proc(worker, args(str(tmp)), STAGE_TIMEOUT, None)
    if status == "ok":
        if out_path.exists():
            out_path.unlink()
        os.replace(tmp, out_path)
        km._cache_put(key, out_path)
        km._log(f"{label}: ok ({info}) {size} bytes")
        return True
    km._log(f"{label}: FAILED {info}")
    problems.append(f"{label}: {info}")
    try:
        tmp.unlink()
    except OSError:
        pass
    return False


def apply_merge_logs(log_dirs, pack_dir: pathlib.Path, vanilla_roots, progress=None):
    """Returns (report lines, problems)."""
    if oead is None:
        return [], []
    logs = _logs_by_name(log_dirs)
    if not logs:
        return [], []
    report, problems = [], []
    h = km.file_hash

    def note(msg):
        if progress:
            progress(0.0, msg)

    # ---- actorinfo
    if "actorinfo.yml" in logs:
        note("Merging actor info...")
        src = km._find_vanilla(ACTORINFO_REL, vanilla_roots)
        out = pack_dir / ACTORINFO_REL
        if src is None:
            problems.append("actorinfo: vanilla ActorInfo.product.sbyml not found")
        else:
            ps = logs["actorinfo.yml"]
            if _stage("actorinfo", _w_actorinfo,
                      lambda tmp: ([str(p) for p in ps], str(src), tmp),
                      [h(p) for p in ps] + [h(src)], out, problems, report):
                report.append(f"Actor info merged ({len(ps)} log(s)).")
                _note_file("Actor/ActorInfo.product.sbyml", out)

    # ---- Bootup.pack: gamedata / savedata / areadata / dstatic (no DLC)
    del RSTB_DELETE[:]
    RSTB_SET.clear()
    boot = {k.split(".")[0]: v for k, v in logs.items()
            if k in ("gamedata.yml", "savedata.yml", "areadata.yml", "dstatic.yml", "residents.yml")}
    # main field Static.smubin lives in the DLC when there is one, else in Bootup.pack
    dlc_main_static = None
    if "mainstatic.yml" in logs:
        for root in vanilla_roots.get("aoc", []):
            p = pathlib.Path(root) / MAINSTATIC_REL
            if p.exists():
                dlc_main_static = p
                break
        if dlc_main_static is None:
            boot["mainstatic"] = logs["mainstatic.yml"]
    dlc_static = None
    if "dstatic" in boot:
        for root in vanilla_roots.get("aoc", []):
            p = pathlib.Path(root) / DSTATIC_REL
            if p.exists():
                dlc_static = p
                break
    boot_jobs = {k: v for k, v in boot.items() if not (k == "dstatic" and dlc_static)}
    if boot_jobs:
        note("Merging game data / save data / area data...")
        out = pack_dir / BOOTUP_REL
        src = out if out.exists() else km._find_vanilla(BOOTUP_REL, vanilla_roots)
        if src is None:
            problems.append("gamedata/savedata/areadata: vanilla Bootup.pack not found")
        else:
            jobs = {k: [str(p) for p in v] for k, v in boot_jobs.items()}
            parts = [k for k in sorted(boot_jobs)] + [h(p) for k in sorted(boot_jobs) for p in boot_jobs[k]] + [h(src)]
            if _stage("bootup-data", _w_bootup,
                      lambda tmp: (jobs, str(src), tmp, ""), parts, out, problems, report):
                report.append("Merged into Bootup.pack: " + ", ".join(sorted(boot_jobs)) + ".")
                inner = {"gamedata": "GameData/gamedata.ssarc", "savedata": "GameData/savedataformat.ssarc",
                         "areadata": "Ecosystem/AreaData.sbyml", "residents": "Actor/ResidentActors.byml",
                         "mainstatic": MAINSTATIC_REL, "dstatic": DSTATIC_REL}
                _note_inner(out, [inner[k] for k in boot_jobs if k in inner])

    # ---- shrine entrances in the DLC
    if "dstatic" in boot and dlc_static is not None:
        note("Merging shrine entrances...")
        out = pack_dir / "aoc" / "0010" / DSTATIC_REL
        ps = boot["dstatic"]
        if _stage("dstatic", _w_dstatic,
                  lambda tmp: ([str(p) for p in ps], str(dlc_static), tmp),
                  [h(p) for p in ps] + [h(dlc_static)], out, problems, report):
            report.append("Shrine entrance coordinates merged.")
            _note_file(DSTATIC_REL, out, "Aoc/0010/")

    # ---- DLC main field static
    if dlc_main_static is not None:
        note("Merging main field static objects...")
        out = pack_dir / "aoc" / "0010" / MAINSTATIC_REL
        ps = logs["mainstatic.yml"]
        if _stage("mainstatic", _w_mainstatic,
                  lambda tmp: ([str(p) for p in ps], str(dlc_main_static), tmp),
                  [h(p) for p in ps] + [h(dlc_main_static)], out, problems, report):
            report.append("Main field static objects merged.")
            _note_file(MAINSTATIC_REL, out, "Aoc/0010/")

    # ---- quests (TitleBG.pack)
    if "quests.yml" in logs:
        note("Merging quests...")
        rel = "content/Pack/TitleBG.pack"
        out = pack_dir / rel
        src = out if out.exists() else km._find_vanilla(rel, vanilla_roots)
        if src is None:
            problems.append("quests: vanilla TitleBG.pack not found")
        else:
            ps = logs["quests.yml"]
            if _stage("quests", _w_quests,
                      lambda tmp: ([str(p) for p in ps], str(src), tmp),
                      [h(p) for p in ps] + [h(src)], out, problems, report):
                report.append(f"Quests merged ({len(ps)} log(s)).")
                _note_inner(out, [QUEST_INNER])

    # ---- map units
    if "map.yml" in logs:
        _apply_maps(logs["map.yml"], pack_dir, vanilla_roots, problems, report, note)

    # ---- drop tables
    if "drops.json" in logs:
        n_ok = _apply_drops(logs["drops.json"], pack_dir, vanilla_roots, problems, progress)
        if n_ok:
            report.append(f"Drop tables merged for {n_ok} actor(s).")
    return report, problems


def _apply_maps(log_paths, pack_dir, vanilla_roots, problems, report, note):
    h = km.file_hash
    units = []
    for lp in log_paths:
        try:
            d = _read_byml_text(lp)
            for u in d.keys():
                if u not in units:
                    units.append(u)
        except Exception as e:
            problems.append(f"map.yml: could not read ({e})")
    if not units:
        return
    src_pack = pack_dir / "aoc" / "0010" / "Pack" / "AocMainField.pack"
    if not src_pack.exists():
        src_pack = km._find_vanilla(AOC_PACK_REL, vanilla_roots)
    if src_pack is None:
        problems.append("map.yml: the DLC (AocMainField.pack) was not found; map changes skipped")
        return
    dyn_srcs = {}
    for u in units:
        try:
            sec, kind = _unit_split(u)
        except ValueError:
            problems.append(f"map.yml: unknown unit {u}")
            continue
        if kind == "Dynamic":
            p = km._find_vanilla(f"aoc/{MAP_REL_DIR}/{sec}/{u}.smubin", vanilla_roots)
            if p is None:
                problems.append(f"map.yml: vanilla {u}.smubin not found")
            else:
                dyn_srcs[u] = str(p)
    note("Merging map units...")
    out_pack = pack_dir / "aoc" / "0010" / "Pack" / "AocMainField.pack"
    out_dir = pack_dir / "aoc" / "0010" / MAP_REL_DIR
    # the output pack is always rebuilt from the vanilla one
    tmp_pack = out_pack.with_name(out_pack.name + ".korok_tmp")
    status, info, size = km._run_proc(
        _w_maps, ([str(p) for p in log_paths], units, str(src_pack), dyn_srcs, str(tmp_pack), str(out_dir)),
        STAGE_TIMEOUT, None)
    if status != "ok":
        problems.append(f"maps: {info}")
        km._log(f"maps: FAILED {info}")
        try:
            tmp_pack.unlink()
        except OSError:
            pass
        return
    if tmp_pack.exists():
        if out_pack.exists():
            out_pack.unlink()
        os.replace(tmp_pack, out_pack)
    msg = info["msg"]
    km._log(f"maps: ok ({msg})")
    if "FAILED" in msg:
        problems.append(f"maps: {msg}")
    report.append(f"Map units merged ({msg.split(';')[0]}).")
    for u, n in info["sizes"].items():
        sec, _ = _unit_split(u)
        RSTB_SET[f"Aoc/0010/{MAP_REL_DIR}/{sec}/{u}.mubin"] = _rstb_size(n, "mubin")


def _apply_drops(log_paths, pack_dir, vanilla_roots, problems, progress):
    # consolidated {file: {table: contents}}; later mods win per table
    cons = {}
    for lp in log_paths:
        try:
            data = json.loads(pathlib.Path(lp).read_text(encoding="utf-8"))
        except Exception as e:
            problems.append(f"drops.json: could not read ({e})")
            continue
        for file, tables in data.items():
            cons.setdefault(file, {}).update(tables)
    jobs = []     # (actor, inner, table, src, out)
    for file, tables in cons.items():
        if "//" not in file:
            continue
        m = re.search(r"Actor/Pack/(.+)\.sbactorpack", file.replace("\\", "/"))
        if not m:
            problems.append(f"drops: no actor name in {file}")
            continue
        actor = m.group(1)
        inner = file.replace("\\", "/").split("//", 1)[1]
        out = pack_dir / "content" / "Actor" / "Pack" / f"{actor}.sbactorpack"
        src = out if out.exists() else km._find_vanilla(f"content/Actor/Pack/{actor}.sbactorpack", vanilla_roots)
        if src is None:
            problems.append(f"drops: {actor}.sbactorpack not found (skipped)")
            continue
        jobs.append((actor, inner, tables, src, out))
    if not jobs:
        return 0
    done = 0
    chunk = 25
    batches = [jobs[i:i + chunk] for i in range(0, len(jobs), chunk)]

    def run(batch):
        tmp_items = [(a, i, t, str(s), str(o) + ".korok_tmp") for a, i, t, s, o in batch]
        status, info, _ = km._run_proc(_w_drops, (tmp_items, ""), STAGE_TIMEOUT, None)
        if status != "ok":
            for it in tmp_items:
                try:
                    pathlib.Path(it[4]).unlink()
                except OSError:
                    pass
            return 0, f"drops batch failed: {info}"
        for a, i, t, s, o in batch:
            tmp = pathlib.Path(str(o) + ".korok_tmp")
            if tmp.exists():
                if o.exists():
                    o.unlink()
                os.replace(tmp, o)
        return len(batch), None

    with concurrent.futures.ThreadPoolExecutor(max_workers=km._worker_count()) as pool:
        for fut in concurrent.futures.as_completed([pool.submit(run, b) for b in batches]):
            n, err = fut.result()
            done += n
            if err:
                problems.append(err)
                km._log(err)
            if progress:
                progress(done / len(jobs), f"drop tables {done}/{len(jobs)}")
    km._log(f"drops: {done}/{len(jobs)} actors merged")
    return done
