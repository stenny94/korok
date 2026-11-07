"""UKMM mergeable resource types: vanilla file + the mod's version -> game file.

Each builder gets the mod's data for one resource (UKMM's serde form, decoded
from CBOR), the vanilla file (decompressed bytes, or None for a new file), and
returns the uncompressed game file. Ported from uk-content
(https://github.com/NiceneNerd/ukmm, GPL-3.0); see korok_ukmm_native.py.
"""
import pathlib

import oead

from korok_ukmm_native import (UnsupportedResource, byml_from_cbor, byml_to_binary,
                               merge_byml_shallow, merge_plist, pio_from_cbor)

BUILDERS = {}


def builder(*kinds):
    def deco(fn):
        for k in kinds:
            BUILDERS[k] = fn
        return fn
    return deco


def build(kind, data, vanilla, canon) -> bytes:
    fn = BUILDERS.get(kind)
    if fn is None:
        raise UnsupportedResource(kind, canon)
    return fn(data, vanilla, canon)


# ------------------------------------------------------------- whole-file override
@builder("BinaryOverride")
def _binary_override(data, vanilla, canon):
    # serde tuple (Vec<u8>, String): the raw file + why UKMM couldn't parse it
    return bytes(data[0])


# ------------------------------------------------------------- AAMP wrappers
# impl_simple_aamp! types and GenericAamp: ParameterIO + util::merge_plist
@builder("GenericAamp", "GeneralParamList", "Awareness", "DamageParam", "Lod", "UMii",
         "Demo", "WorldInfo")
def _simple_aamp(data, vanilla, canon):
    if vanilla is None:
        return bytes(pio_from_cbor(data).to_binary())
    pio = oead.aamp.ParameterIO.from_binary(vanilla)
    merge_plist(pio, data.get("param_root") or {})
    return bytes(pio.to_binary())


# ------------------------------------------------------------- BYML wrappers
# impl_simple_byml! types and GenericByml: shallow map merge
@builder("GenericByml", "AISchedule", "AnimationInfo", "EventInfo")
def _simple_byml(data, vanilla, canon):
    diff = byml_from_cbor(data)
    if vanilla is None:
        return byml_to_binary(diff)
    base = oead.byml.from_binary(vanilla)
    if isinstance(base, oead.byml.Hash) and isinstance(diff, oead.byml.Hash):
        return byml_to_binary(merge_byml_shallow(base, diff))
    if diff is None:
        return byml_to_binary(base)
    # UKMM only shallow-merges hashes; anything else in a diff is a full replacement
    return byml_to_binary(diff)


# ------------------------------------------------------------- game text (MessagePack)
def _msyt_run(args):
    import korok_merge
    exe = korok_merge.msyt_path()
    if exe is None:
        raise UnsupportedResource("MessagePack (needs: pip install pymsyt)", "")
    korok_merge._run_msyt(exe, args)


def _game_languages(dump):
    import re
    from korok_ukmm_native import _filemap
    langs = []
    for k in _filemap():
        m = re.fullmatch(r"Pack/Bootup_([A-Za-z]{4})\.pack", k)
        if m and m.group(1) not in langs and dump.get_bytes(k) is not None:
            langs.append(m.group(1))
    return langs


def _merge_msyt(base: dict, diff: dict) -> dict:
    """Msyt::merge: entries of the diff replace/extend the base's; group_count follows,
    the other MSBT header fields (flattened into the document) stay the base's."""
    entries = dict(base.get("entries") or {})
    entries.update(diff.get("entries") or {})
    out = {k: v for k, v in base.items() if k != "entries"}
    out["group_count"] = len(entries)
    out["entries"] = entries
    return out


def build_text_packs(conv):
    """unpack_texts for every language in the game: vanilla Msg_<lang> + the mod's
    MessagePack (same language, else same short code, else any) -> Bootup_<lang>.pack."""
    import json
    import shutil
    import tempfile
    import yaml
    from korok_ukmm_native import canonicalize, compress_if, unyaz
    modded = {}
    for f in conv.pkg.content_files:
        if f.startswith("Pack/Bootup_") and f.endswith(".pack"):
            lang = f[len("Pack/Bootup_"):-len(".pack")]
            res = conv.pkg.get(canonicalize(f"Message/Msg_{lang}.product.ssarc"))
            if res is not None and "Mergeable" in res and "MessagePack" in res["Mergeable"]:
                modded[lang] = res["Mergeable"]["MessagePack"]
    if not modded:
        return {}
    out = {}
    for lang in _game_languages(conv.dump):
        src = next((l for l in modded if l == lang), None) or \
            next((l for l in modded if l[-2:] == lang[-2:]), None) or next(iter(modded))
        diff = {name: json.loads(text) for name, text in modded[src].items()}
        boot = conv.dump.get_bytes(f"Pack/Bootup_{lang}.pack")
        msg_name = f"Message/Msg_{lang}.product.ssarc"
        vanilla = oead.Sarc(boot).get_file(msg_name) if boot is not None else None
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="korok_ukmm_text_"))
        try:
            in_dir, yml_dir, new_dir, msb_dir = (tmp / d for d in ("in", "yml", "new", "out"))
            for d in (in_dir, yml_dir, new_dir, msb_dir):
                d.mkdir()
            base_files = {}
            if vanilla is not None:
                vs = oead.Sarc(unyaz(bytes(vanilla.data)))
                for f in vs.get_files():
                    if f.name.endswith(".msbt"):
                        p = in_dir / f.name
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_bytes(bytes(f.data))
                        base_files[f.name[:-5]] = None
                if base_files:
                    _msyt_run(["export", "-d", "-o", str(yml_dir), str(in_dir)])
            names = sorted(set(base_files) | set(diff))
            for name in names:
                if name in diff and name in base_files:
                    base = yaml.safe_load((yml_dir / f"{name}.msyt").read_text(encoding="utf-8")) or {}
                    doc = _merge_msyt(base, diff[name])
                elif name in diff:
                    doc = diff[name]
                else:
                    doc = None
                if doc is not None:
                    t = new_dir / f"{name}.msyt"
                    t.parent.mkdir(parents=True, exist_ok=True)
                    t.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            if any(new_dir.rglob("*.msyt")):
                _msyt_run(["create", "-d", "-B", "-p", "wiiu", "-o", str(msb_dir), str(new_dir)])
            w = oead.SarcWriter(endian=oead.Endianness.Big)
            for name in names:
                built = msb_dir / f"{name}.msbt"
                w.files[f"{name}.msbt"] = built.read_bytes() if built.exists() else (in_dir / f"{name}.msbt").read_bytes()
            msg = compress_if(bytes(w.write()[1]), msg_name)
            pw = oead.SarcWriter(endian=oead.Endianness.Big)
            pw.files[msg_name] = msg
            out[f"Pack/Bootup_{lang}.pack"] = bytes(pw.write()[1])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return out


# ------------------------------------------------------------- helpers
from korok_ukmm_native import (dmap_deep_merge, dmap_merge, dset_merge, dvec_merge,  # noqa: E402
                               merge_pobj_cbor, merge_plist_cbor, param_str, pobj_from_cbor,
                               pobj_to_cbor, plist_from_cbor, plist_to_cbor, pio_to_cbor,
                               sorted_dset_merge)

A = oead.aamp
P = A.Parameter


def H(name) -> int:
    """AAMP name hash (crc32)."""
    import zlib
    return zlib.crc32(name.encode("utf-8")) & 0xFFFFFFFF


def new_pio(version=0, data_type="xml"):
    pio = A.ParameterIO()
    pio.type = data_type
    pio.version = version
    return pio


def obj(pio_or_list, name):
    k = A.Name(name) if isinstance(name, str) else A.Name(int(name))
    return pio_or_list.objects.get(k)


def lst(pio_or_list, name):
    k = A.Name(name) if isinstance(name, str) else A.Name(int(name))
    return pio_or_list.lists.get(k)


def set_obj(target, name, cbor_obj: dict):
    k = A.Name(name) if isinstance(name, str) else A.Name(int(name))
    target.objects[k] = pobj_from_cbor(cbor_obj)


def opt_merge(base, diff, fn):
    """Option<T> merge used all over uk-content: both -> fn, else whichever exists."""
    if base is not None and diff is not None:
        return fn(base, diff)
    return base if base is not None else diff


def typed(kind, from_binary, merge, to_binary):
    """Registers a typed resource: model = from_binary(vanilla) (serde shape),
    merged with the mod's data, then written with to_binary."""
    def _build(data, vanilla, canon):
        model = data if vanilla is None else merge(from_binary(vanilla), data)
        return to_binary(model)
    BUILDERS[kind] = _build


# ------------------------------------------------------------- ActorLink (.bxml)
FIT_TAGS = 1115720914


def _tags_from(o):
    if o is None:
        return None
    out = {}
    for _, v in o.params.items():
        if v.type() in (A.Parameter.Type.String32, A.Parameter.Type.String64,
                        A.Parameter.Type.String256, A.Parameter.Type.StringRef):
            out[str(v.v)] = False
    return out


def actorlink_from(data):
    pio = A.ParameterIO.from_binary(data)
    t = obj(pio, "LinkTarget")
    if t is None:
        raise ValueError("Actor link missing link targets")
    return {"targets": pobj_to_cbor(t), "tags": _tags_from(obj(pio, "Tags")),
            "fit_tags": _tags_from(obj(pio, FIT_TAGS))}


def actorlink_merge(b, d):
    return {"targets": merge_pobj_cbor(b["targets"], d["targets"]),
            "tags": b["tags"] if b.get("tags") is not None and d.get("tags") is None
            else (dset_merge(b["tags"], d["tags"]) if b.get("tags") is not None else d.get("tags")),
            "fit_tags": b["fit_tags"] if b.get("fit_tags") is not None and d.get("fit_tags") is None
            else (dset_merge(b["fit_tags"], d["fit_tags"]) if b.get("fit_tags") is not None
                  else d.get("fit_tags"))}


def _tag_obj(tags: dict) -> dict:
    return {H(f"Tag{i}"): {"StringRef": t} for i, t in enumerate(k for k, dl in tags.items() if not dl)}


def actorlink_to(m):
    pio = new_pio()
    set_obj(pio, "LinkTarget", m["targets"])
    if m.get("tags") is not None:
        set_obj(pio, "Tags", _tag_obj(m["tags"]))
    if m.get("fit_tags") is not None:
        set_obj(pio, FIT_TAGS, _tag_obj(m["fit_tags"]))
    return bytes(pio.to_binary())


typed("ActorLink", actorlink_from, actorlink_merge, actorlink_to)


# ------------------------------------------------------------- shared pieces
_STR_TYPES = None


def is_str_param(p) -> bool:
    global _STR_TYPES
    if _STR_TYPES is None:
        T = A.Parameter.Type
        _STR_TYPES = (T.String32, T.String64, T.String256, T.StringRef)
    return p.type() in _STR_TYPES


def pstr(o, name):
    """as_safe_string of a parameter in an oead object, or None."""
    p = o.params.get(A.Name(name)) if o is not None else None
    if p is None or not is_str_param(p):
        return None
    return str(p.v)


def need(x, msg):
    if x is None:
        raise ValueError(msg)
    return x


def index_merge(base: dict, diff: dict) -> dict:
    """util::simple_index_merge on BTreeMap<usize, T>."""
    out = dict(base)
    out.update({int(k): v for k, v in diff.items()})
    return dict(sorted(out.items()))


def ilist(d: dict) -> dict:
    """BTreeMap<usize, T> from CBOR: int keys, sorted."""
    return dict(sorted((int(k), v) for k, v in d.items()))


def add_list(target, name, pl):
    target.lists[A.Name(name) if isinstance(name, str) else A.Name(int(name))] = pl


def add_obj(target, name, o):
    target.objects[A.Name(name) if isinstance(name, str) else A.Name(int(name))] = o


# ------------------------------------------------------------- ModelList (.bmodellist)
def _modeldata_from(pl):
    folder = need(pstr(obj(pl, "Base"), "Folder"), "Model data missing Folder")
    units = {}
    u = lst(pl, "Unit")
    if u is not None:
        for _, o in u.objects.items():
            units[need(pstr(o, "UnitName"), "unit name")] = [need(pstr(o, "BindBone"), "bind bone"), False]
    return {"folder": folder, "units": units}


def modellist_from(data):
    pio = A.ParameterIO.from_binary(data)
    locs = []
    i = 0
    while obj(pio, f"Locator_{i}") is not None:
        locs.append([pobj_to_cbor(obj(pio, f"Locator_{i}")), False])
        i += 1
    return {
        "controller_info": pobj_to_cbor(need(obj(pio, "ControllerInfo"), "no ControllerInfo")),
        "attention": pobj_to_cbor(need(obj(pio, "Attention"), "no Attention")),
        "model_data": {i: _modeldata_from(l) for i, (_, l) in
                       enumerate(need(lst(pio, "ModelData"), "no ModelData").lists.items())},
        "anm_target": {i: plist_to_cbor(l) for i, (_, l) in
                       enumerate(need(lst(pio, "AnmTarget"), "no AnmTarget").lists.items())},
        "locators": locs,
    }


def modellist_merge(b, d):
    md = ilist(b["model_data"])
    for i, v in ilist(d["model_data"]).items():
        if i in md:
            md[i] = {"folder": v["folder"], "units": dmap_merge(md[i]["units"], v["units"])}
        else:
            md[i] = v
    return {
        "controller_info": merge_pobj_cbor(b["controller_info"], d["controller_info"]),
        "attention": merge_pobj_cbor(b["attention"], d["attention"]),
        "model_data": dict(sorted(md.items())),
        "anm_target": index_merge(ilist(b["anm_target"]), d["anm_target"]),
        "locators": dvec_merge(b["locators"], d["locators"]),
    }


def modellist_to(m):
    pio = new_pio()
    set_obj(pio, "ControllerInfo", m["controller_info"])
    set_obj(pio, "Attention", m["attention"])
    mdl = A.ParameterList()
    for i, data in ilist(m["model_data"]).items():
        pl = A.ParameterList()
        base = A.ParameterObject()
        base.params["Folder"] = P(oead.FixedSafeString64(data["folder"]))
        add_obj(pl, "Base", base)
        units = [(k, v[0]) for k, v in data["units"].items() if not v[1]]
        if units:
            ul = A.ParameterList()
            for j, (name, bone) in enumerate(units):
                o = A.ParameterObject()
                o.params["UnitName"] = P(oead.FixedSafeString64(name))
                o.params["BindBone"] = P(oead.FixedSafeString64(bone))
                add_obj(ul, f"Unit_{j}", o)
            add_list(pl, "Unit", ul)
        add_list(mdl, f"ModelData_{i}", pl)
    add_list(pio, "ModelData", mdl)
    anm = A.ParameterList()
    for i, t in ilist(m["anm_target"]).items():
        add_list(anm, f"AnmTarget_{i}", plist_from_cbor(t))
    add_list(pio, "AnmTarget", anm)
    for i, o in enumerate(x for x, dl in m["locators"] if not dl):
        set_obj(pio, f"Locator_{i}", o)
    return bytes(pio.to_binary())


typed("ModelList", modellist_from, modellist_merge, modellist_to)


# ------------------------------------------------------------- DropTable (.bdrop)
def droptable_from(data):
    pio = A.ParameterIO.from_binary(data)
    header = need(obj(pio, "Header"), "Drop table missing header")
    out = {}
    for _, p in header.params.items():
        if is_str_param(p):
            t = obj(pio, str(p.v))
            if t is not None:
                out[str(p.v)] = pobj_to_cbor(t)
    return out


def droptable_merge(b, d):
    out = dict(b)
    out.update(d)
    return out


def droptable_to(m):
    pio = new_pio()
    hdr = A.ParameterObject()
    hdr.params["TableNum"] = P(oead.S32(len(m)))
    for i, name in enumerate(m, 1):
        hdr.params[f"Table{i:02d}"] = P(oead.FixedSafeString64(name))
    add_obj(pio, "Header", hdr)
    for name, table in m.items():
        set_obj(pio, name, table)
    return bytes(pio.to_binary())


typed("DropTable", droptable_from, droptable_merge, droptable_to)


# ------------------------------------------------------------- Recipe (.brecipe)
def _pint(o, name):
    p = o.params.get(A.Name(name))
    return None if p is None else int(p.v)


def recipe_from(data):
    pio = A.ParameterIO.from_binary(data)
    header = need(obj(pio, "Header"), "Recipe missing header")
    count = need(_pint(header, "TableNum"), "Recipe header missing table count")
    names = [need(pstr(header, f"Table{i:02d}"), "Recipe header missing table name")
             for i in range(1, count + 1)]
    out = {}
    for name in names:
        table = need(obj(pio, name), f"Recipe missing table {name}")
        cols = need(_pint(table, "ColumnNum"), "Recipe table missing column count")

        def process(n):
            for pad in (2, 3):
                try:
                    items = {}
                    for i in range(1, n + 1):
                        nm = need(pstr(table, f"ItemName{i:0{pad}d}"), "item name")
                        items[nm] = [need(_pint(table, f"ItemNum{i:0{pad}d}"), "item count"), False]
                    return items
                except ValueError:
                    continue
            raise ValueError("Recipe items unreadable")
        try:
            items = process(cols)
        except ValueError:
            items = process((len(table.params) - 1) // 2)
        out[name] = [items, False]
    return out


def recipe_merge(b, d):
    return d                                   # Recipe::merge is a full replacement


def recipe_to(m):
    pio = new_pio()
    tables = [(k, v[0]) for k, v in m.items() if not v[1]]
    hdr = A.ParameterObject()
    hdr.params["TableNum"] = P(oead.S32(len(tables)))
    for i, (name, _) in enumerate(tables, 1):
        hdr.params[f"Table{i:02d}"] = P(oead.FixedSafeString64(name))
    add_obj(pio, "Header", hdr)
    for name, table in tables:
        items = [(k, v[0]) for k, v in table.items() if not v[1]]
        o = A.ParameterObject()
        o.params["ColumnNum"] = P(oead.S32(len(items)))
        for i, (it, cnt) in enumerate(((k, c) for k, c in items if c > 0), 1):
            o.params[f"ItemName{i:02d}"] = P(oead.FixedSafeString64(it))
            o.params[f"ItemNum{i:02d}"] = P(oead.S32(cnt))
        add_obj(pio, name, o)
    return bytes(pio.to_binary())


typed("Recipe", recipe_from, recipe_merge, recipe_to)


# ------------------------------------------------------------- #[derive(ParamData)] structs
def _to_param(kind, v):
    if kind == "s64":
        return P(oead.FixedSafeString64(str(v)))
    if kind == "s32":
        return P(oead.FixedSafeString32(str(v)))
    if kind == "s256":
        return P(oead.FixedSafeString256(str(v)))
    if kind == "str":
        return P(str(v))
    if kind == "bool":
        return P(bool(v))
    if kind == "f32":
        return P(oead.F32(float(v)))
    if kind == "i32":
        return P(oead.S32(int(v)))
    if kind == "u32":
        return P(oead.U32(int(v)))
    if kind == "vec3":
        x = oead.Vector3f()
        x.x, x.y, x.z = float(v["x"]), float(v["y"]), float(v["z"])
        return P(x)
    if kind == "param":           # already a serde Parameter
        from korok_ukmm_native import param_from_cbor
        return param_from_cbor(v)
    raise ValueError(kind)


def _from_param(kind, p):
    if kind in ("s64", "s32", "s256", "str"):
        if not is_str_param(p):
            raise ValueError("expected a string parameter")
        return str(p.v)
    if kind == "bool":
        return bool(p.v)
    if kind == "f32":
        return float(p.v)
    if kind in ("i32", "u32"):
        return int(p.v)
    if kind == "vec3":
        return {"x": p.v.x, "y": p.v.y, "z": p.v.z}
    if kind == "param":
        from korok_ukmm_native import param_to_cbor
        return param_to_cbor(p)
    raise ValueError(kind)


def paramdata_from(o, fields):
    """fields: [(serde field, AAMP name, kind, optional)]"""
    out = {}
    for field, name, kind, optional in fields:
        p = o.params.get(A.Name(name))
        if p is None:
            if not optional:
                raise ValueError(f"missing {name}")
            out[field] = None
        else:
            out[field] = _from_param(kind, p)
    return out


def paramdata_to(d, fields):
    o = A.ParameterObject()
    for field, name, kind, optional in fields:
        v = d.get(field)
        if v is None and optional:
            continue
        o.params[A.Name(name)] = _to_param(kind, v)
    return o


# ------------------------------------------------------------- ASList (.baslist)
ADDRES = [("anim", "Anim", "s64", False), ("retarget_model", "RetargetModel", "s64", True),
          ("retarget_nocorrect", "RetargetNoCorrect", "bool", True)]


def aslist_from(data):
    pio = A.ParameterIO.from_binary(data)
    add = {}
    for _, o in need(lst(pio, "AddReses"), "AS list missing add reses").objects.items():
        r = paramdata_from(o, ADDRES)
        add[r["anim"]] = [r, False]
    defs = {}
    for _, o in need(lst(pio, "ASDefines"), "AS list missing AS defines").objects.items():
        defs[need(pstr(o, "Name"), "AS define name")] = [need(pstr(o, "Filename"), "AS define filename"), False]
    cf = None
    cfl = lst(pio, "CFDefines")
    if cfl is not None:
        cf = {}
        for _, l in cfl.lists.items():
            cf[need(pstr(obj(l, "CFPre"), "Name"), "CFPre name")] = [plist_to_cbor(l), False]
    c = obj(pio, "Common")
    return {"common": pobj_to_cbor(c) if c is not None else None, "add_reses": add,
            "as_defines": defs, "cf_defines": cf}


def aslist_merge(b, d):
    return {"common": d.get("common") if d.get("common") is not None else b.get("common"),
            "add_reses": dmap_merge(b["add_reses"], d["add_reses"]),
            "as_defines": dmap_merge(b["as_defines"], d["as_defines"]),
            "cf_defines": (dmap_merge(b["cf_defines"], d["cf_defines"]) if b.get("cf_defines") is not None
                           else d["cf_defines"]) if d.get("cf_defines") is not None else b.get("cf_defines")}


def aslist_to(m):
    pio = new_pio()
    if m.get("common") is not None:
        set_obj(pio, "Common", m["common"])
    add = A.ParameterList()
    for i, r in enumerate(v[0] for v in m["add_reses"].values() if not v[1]):
        add_obj(add, f"AddRes_{i}", paramdata_to(r, ADDRES))
    add_list(pio, "AddReses", add)
    defs = A.ParameterList()
    for i, (n, f) in enumerate((k, v[0]) for k, v in m["as_defines"].items() if not v[1]):
        o = A.ParameterObject()
        o.params["Name"] = P(oead.FixedSafeString64(n))
        o.params["Filename"] = P(oead.FixedSafeString64(f))
        add_obj(defs, f"ASDefine_{i}", o)
    add_list(pio, "ASDefines", defs)
    if m.get("cf_defines") is not None:
        cf = A.ParameterList()
        for i, l in enumerate(v[0] for v in m["cf_defines"].values() if not v[1]):
            add_list(cf, f"CFDefine_{i}", plist_from_cbor(l))
        add_list(pio, "CFDefines", cf)
    return bytes(pio.to_binary())


typed("ASList", aslist_from, aslist_merge, aslist_to)


# ------------------------------------------------------------- Chemical (.bchemical)
CHEM_UNKNOWN = 3635073347


def chemical_from(data):
    pio = A.ParameterIO.from_binary(data)
    root = need(lst(pio, "chemical_root"), "Chemical missing chemical_root")
    hdr = need(obj(root, "chemical_header"), "Chemical missing chemical_header")
    n = need(_pint(hdr, "res_shape_num"), "Chemical missing shape count")
    body = need(lst(root, "chemical_body"), "Chemical missing chemical_body")
    unk = hdr.params.get(A.Name(CHEM_UNKNOWN))
    return {"unknown": int(unk.v) if unk is not None else None,
            "body": {i: {"rigid_c": pobj_to_cbor(need(obj(body, f"rigid_c_{i:02d}"), "rigid_c")),
                         "shape": pobj_to_cbor(need(obj(body, f"shape_{i:02d}"), "shape"))}
                     for i in range(n)}}


def chemical_merge(b, d):
    return {"unknown": d["unknown"] if d.get("unknown") is not None else b.get("unknown"),
            "body": index_merge(ilist(b["body"]), d["body"])}


def chemical_to(m):
    pio = new_pio()
    root = A.ParameterList()
    hdr = A.ParameterObject()
    body = ilist(m["body"])
    hdr.params["res_shape_num"] = P(oead.U32(len(body)))
    if m.get("unknown") is not None:
        hdr.params[A.Name(CHEM_UNKNOWN)] = P(oead.U32(int(m["unknown"])))
    add_obj(root, "chemical_header", hdr)
    bl = A.ParameterList()
    for i, b in body.items():
        set_obj(bl, f"shape_{i:02d}", b["shape"])
        set_obj(bl, f"rigid_c_{i:02d}", b["rigid_c"])
    add_list(root, "chemical_body", bl)
    add_list(pio, "chemical_root", root)
    return bytes(pio.to_binary())


typed("Chemical", chemical_from, chemical_merge, chemical_to)


# ------------------------------------------------------------- AttClient (.batcl)
def attclient_from(data):
    pio = A.ParameterIO.from_binary(data)
    return {"client_params": pobj_to_cbor(need(obj(pio, "AttClientParams"), "Attention client missing params")),
            "checks": [[plist_to_cbor(l), False] for _, l in pio.lists.items()]}


def attclient_merge(b, d):
    return {"client_params": merge_pobj_cbor(b["client_params"], d["client_params"]),
            "checks": dvec_merge(b["checks"], d["checks"])}


def attclient_to(m):
    pio = new_pio()
    set_obj(pio, "AttClientParams", m["client_params"])
    for i, l in enumerate(x for x, dl in m["checks"] if not dl):
        add_list(pio, f"Check_{i}", plist_from_cbor(l))
    return bytes(pio.to_binary())


typed("AttClient", attclient_from, attclient_merge, attclient_to)


# ------------------------------------------------------------- AttClientList (.batcllist)
def attclientlist_from(data):
    pio = A.ParameterIO.from_binary(data)
    cl = {}
    for _, o in need(lst(pio, "AttClients"), "missing AttClients").objects.items():
        cl[need(pstr(o, "Name"), "client name")] = [need(pstr(o, "FileName"), "client filename"), False]
    return {"att_pos": pobj_to_cbor(need(obj(pio, "AttPos"), "missing AttPos")), "att_clients": cl}


def attclientlist_merge(b, d):
    return {"att_pos": merge_pobj_cbor(b["att_pos"], d["att_pos"]),
            "att_clients": dmap_merge(b["att_clients"], d["att_clients"])}


def attclientlist_to(m):
    pio = new_pio()
    set_obj(pio, "AttPos", m["att_pos"])
    cl = A.ParameterList()
    for i, (n, f) in enumerate((k, v[0]) for k, v in m["att_clients"].items() if not v[1]):
        o = A.ParameterObject()
        o.params["Name"] = P(oead.FixedSafeString64(n))
        o.params["FileName"] = P(oead.FixedSafeString64(f))
        add_obj(cl, f"AttClient_{i}", o)
    add_list(pio, "AttClients", cl)
    return bytes(pio.to_binary())


typed("AttClientList", attclientlist_from, attclientlist_merge, attclientlist_to)


# ------------------------------------------------------------- RagdollConfig (.brgconfig)
def rgconfig_from(data):
    pio = A.ParameterIO.from_binary(data)
    root = need(lst(pio, "ConfigRoot"), "Ragdoll config missing config root list")
    return {"attack_type_impulse_data": pobj_to_cbor(need(obj(root, "AttackTypeImpulseData"), "no impulse data")),
            "impact_impulse_info": {i: plist_to_cbor(l) for i, (_, l) in enumerate(root.lists.items())}}


def rgconfig_merge(b, d):
    return {"attack_type_impulse_data": merge_pobj_cbor(b["attack_type_impulse_data"], d["attack_type_impulse_data"]),
            "impact_impulse_info": index_merge(ilist(b["impact_impulse_info"]), d["impact_impulse_info"])}


def rgconfig_to(m):
    pio = new_pio()
    root = A.ParameterList()
    set_obj(root, "AttackTypeImpulseData", m["attack_type_impulse_data"])
    for i, l in ilist(m["impact_impulse_info"]).items():
        add_list(root, f"ImpactImpulseInfo{i + 1:02d}", plist_from_cbor(l))
    add_list(pio, "ConfigRoot", root)
    return bytes(pio.to_binary())


typed("RagdollConfig", rgconfig_from, rgconfig_merge, rgconfig_to)


# ------------------------------------------------------------- RagdollConfigList (.brgconfiglist)
BODYPARAM = [("name", "RigidName", "s64", False), ("friction_scale", "FrictionScale", "f32", False),
             ("buoyancy_scale", "BuoyancyScale", "f32", False)]


def rgconfiglist_from(data):
    pio = A.ParameterIO.from_binary(data)
    bodies = {}
    for _, o in need(lst(pio, "BodyParamList"), "missing body param list").objects.items():
        bodies[need(pstr(o, "RigidName"), "body param name")] = [paramdata_from(o, BODYPARAM), False]
    return {"common_data": pobj_to_cbor(need(obj(pio, "CommonData"), "missing common data")),
            "impulse_params": plist_to_cbor(need(lst(pio, "ImpulseParamList"), "missing impulse params")),
            "body_param_list": bodies}


def rgconfiglist_merge(b, d):
    return {"common_data": merge_pobj_cbor(b["common_data"], d["common_data"]),
            "impulse_params": merge_plist_cbor(b["impulse_params"], d["impulse_params"]),
            "body_param_list": dmap_merge(b["body_param_list"], d["body_param_list"])}


def rgconfiglist_to(m):
    pio = new_pio()
    set_obj(pio, "CommonData", m["common_data"])
    add_list(pio, "ImpulseParamList", plist_from_cbor(m["impulse_params"]))
    bl = A.ParameterList()
    for i, bp in enumerate(v[0] for v in m["body_param_list"].values() if not v[1]):
        add_obj(bl, f"BodyParam_{i}", paramdata_to(bp, BODYPARAM))
    add_list(pio, "BodyParamList", bl)
    return bytes(pio.to_binary())


typed("RagdollConfigList", rgconfiglist_from, rgconfiglist_merge, rgconfiglist_to)


# ------------------------------------------------------------- RagdollBlendWeight (.brgbw)
RGBW_KEY = [("state_key", "StateKey", "s32", False), ("system_key", "SystemKey", "s32", False)]


def _rgbw_key(k):
    return (str(k["state_key"]), str(k["system_key"]))


def rgbw_from(data):
    pio = A.ParameterIO.from_binary(data)
    out = {}
    for _, l in pio.lists.items():
        key = paramdata_from(need(obj(l, "Setting"), "blend weight state missing header"), RGBW_KEY)
        weights = {}
        for _, o in need(lst(l, "InputWeightList"), "missing input weight list").objects.items():
            weights[need(pstr(o, "RigidName"), "rigid name")] = [float(o.params[A.Name("BlendRate")].v), False]
        out[_rgbw_key(key)] = weights
    return out


def _rgbw_norm(m):
    return {(_rgbw_key(k) if not isinstance(k, tuple) else k): v for k, v in m.items()}


def rgbw_merge(b, d):
    # UKMM iterates a HashSet here, so its state order is arbitrary; keep the base order
    b, d = _rgbw_norm(b), _rgbw_norm(d)
    out = {}
    for k in list(dict.fromkeys(list(b) + list(d))):
        out[k] = dmap_merge(b[k], d[k]) if k in b and k in d else (d.get(k) if k in d else b[k])
    return out


def rgbw_to(m):
    pio = new_pio()
    for i, (key, state) in enumerate(_rgbw_norm(m).items(), 1):
        pl = A.ParameterList()
        add_obj(pl, "Setting", paramdata_to({"state_key": key[0], "system_key": key[1]}, RGBW_KEY))
        wl = A.ParameterList()
        for j, (name, rate) in enumerate(((k, v[0]) for k, v in state.items() if not v[1]), 1):
            o = A.ParameterObject()
            o.params["RigidName"] = P(oead.FixedSafeString32(name))
            o.params["BlendRate"] = P(oead.F32(float(rate)))
            add_obj(wl, f"InputWeight_{j}", o)
        add_list(pl, "InputWeightList", wl)
        add_list(pio, f"State_{i}", pl)
    return bytes(pio.to_binary())


typed("RagdollBlendWeight", rgbw_from, rgbw_merge, rgbw_to)


# ------------------------------------------------------------- BoneControl (.bbonectrl)
def bonectrl_from(data):
    pio = A.ParameterIO.from_binary(data)
    groups = {}
    for _, l in need(lst(pio, "BoneGroups"), "Bone control missing BoneGroups").lists.items():
        name = need(pstr(obj(l, "Param"), "GroupName"), "bone group name")
        bones = need(obj(l, "Bones"), "bone list")
        groups[name] = {str(p.v): False for _, p in bones.params.items() if is_str_param(p)}
    return {"objects": {k: v for k, v in plist_to_cbor(pio)["objects"].items()}, "bone_groups": groups}


def bonectrl_merge(b, d):
    objs = merge_plist_cbor({"objects": b["objects"]}, {"objects": d["objects"]})["objects"]
    groups = {}
    # UKMM iterates a HashSet here (arbitrary order); keep the base order
    for g in list(dict.fromkeys(list(b["bone_groups"]) + list(d["bone_groups"]))):
        if g in d["bone_groups"]:
            groups[g] = (dset_merge(b["bone_groups"][g], d["bone_groups"][g]) if g in b["bone_groups"]
                         else d["bone_groups"][g])
        else:
            groups[g] = b["bone_groups"][g]
    return {"objects": objs, "bone_groups": groups}


def bonectrl_to(m):
    pio = new_pio()
    for k, o in m["objects"].items():
        set_obj(pio, k, o)
    bg = A.ParameterList()
    for i, (g, bones) in enumerate(m["bone_groups"].items()):
        pl = A.ParameterList()
        po = A.ParameterObject()
        po.params["GroupName"] = P(oead.FixedSafeString64(g))
        add_obj(pl, "Param", po)
        bo = A.ParameterObject()
        for j, bone in enumerate(k for k, dl in bones.items() if not dl):
            bo.params[f"Bone_{j}"] = P(oead.FixedSafeString64(bone))
        add_obj(pl, "Bones", bo)
        add_list(bg, f"BoneGroup_{i}", pl)
    add_list(pio, "BoneGroups", bg)
    return bytes(pio.to_binary())


typed("BoneControl", bonectrl_from, bonectrl_merge, bonectrl_to)


# ------------------------------------------------------------- LifeCondition (.blifecondition)
_LIFE_SETS = [("invalid_weathers", "InvalidWeathers"), ("invalid_times", "InvalidTimes"),
              ("delete_weathers", "DeleteWeathers"), ("delete_times", "DeleteTimes")]
_LIFE_ITEMS = [("display_dist", "DisplayDistance", "f32"),
               ("auto_disp_dist_algo", "AutoDisplayDistanceAlgorithm", "str"),
               ("y_limit_algo", "YLimitAlgorithm", "str")]


def life_from(data):
    pio = A.ParameterIO.from_binary(data)
    out = {}
    for field, name in _LIFE_SETS:
        o = obj(pio, name)
        out[field] = None if o is None else {str(p.v): False for _, p in o.params.items()}
    for field, name, kind in _LIFE_ITEMS:
        o = need(obj(pio, name), f"Life condition missing {name}")
        p = need(o.params.get(A.Name("Item")), f"{name} missing item")
        out[field] = _from_param(kind, p)
    return out


def life_merge(b, d):
    out = {}
    for field, _ in _LIFE_SETS:
        out[field] = opt_merge(b.get(field), d.get(field), dset_merge)
    for field, _, _ in _LIFE_ITEMS:
        out[field] = d.get(field) if d.get(field) is not None else b.get(field)
    return out


def life_to(m):
    pio = new_pio()

    def items(name, values):
        o = A.ParameterObject()
        for i, v in enumerate((k for k, dl in values.items() if not dl), 1):
            o.params[f"Item{i:03d}"] = P(oead.FixedSafeString64(v))
        add_obj(pio, name, o)
    if m.get("invalid_weathers") is not None:
        items("InvalidWeathers", m["invalid_weathers"])
    if m.get("invalid_times") is not None:
        items("InvalidTimes", m["invalid_times"])
    for field, name, kind in _LIFE_ITEMS:
        if m.get(field) is not None:
            o = A.ParameterObject()
            o.params["Item"] = _to_param(kind, m[field])
            add_obj(pio, name, o)
    if m.get("delete_weathers") is not None:
        items("DeleteWeathers", m["delete_weathers"])
    if m.get("delete_times") is not None:
        items("DeleteTimes", m["delete_times"])
    return bytes(pio.to_binary())


typed("LifeCondition", life_from, life_merge, life_to)


# ------------------------------------------------------------- ShopData (.bshop)
_SHOP_FIELDS = [("sort", "ItemSort", "i32"), ("num", "ItemNum", "i32"), ("adjust_price", "ItemAdjustPrice", "i32"),
                ("look_get_flag", "ItemLookGetFlg", "bool"), ("amount", "ItemAmount", "i32")]


def shop_from(data):
    pio = A.ParameterIO.from_binary(data)
    header = need(obj(pio, "Header"), "Shop data missing header")
    out = {}
    for _, p in header.params.items():
        if not is_str_param(p):
            continue
        tname = str(p.v)
        t = need(obj(pio, tname), f"Table {tname} in shop data missing")
        cols = need(_pint(t, "ColumnNum"), "Shop data table missing column count")

        def process(n):
            items = {}
            for i in range(1, n + 1):
                nm = need(pstr(t, f"ItemName{i:03d}"), "Shop table missing item name")
                item = {}
                for field, key, kind in _SHOP_FIELDS:
                    item[field] = _from_param(kind, need(t.params.get(A.Name(f"{key}{i:03d}")), key))
                item["delete"] = False
                items[nm] = item
            return items
        try:
            out[tname] = process(cols)
        except ValueError:
            out[tname] = process((len(t.params) - 1) // 6)
    return out


def shop_merge(b, d):
    out = {}
    for name, table in b.items():
        if table is not None:
            dt = d.get(name)
            if dt is not None:
                merged = dict(table)
                merged.update(dt)
                out[name] = {k: v for k, v in merged.items() if not v.get("delete")}
            else:
                # UKMM's ShopData::merge drops a base table the diff doesn't carry, but the
                # header still lists it; diffs leave out unchanged tables, so keep it instead
                out[name] = table
        else:
            out[name] = d.get(name)
    for name, table in d.items():
        if name not in b:
            out[name] = table
    return out


def shop_to(m):
    pio = new_pio()
    hdr = A.ParameterObject()
    hdr.params["TableNum"] = P(oead.S32(len(m)))
    for i, name in enumerate(m, 1):
        hdr.params[f"Table{i:02d}"] = P(oead.FixedSafeString64(name))
    add_obj(pio, "Header", hdr)
    for name, table in m.items():
        if table is None:
            continue
        items = [(k, v) for k, v in table.items() if not v.get("delete")]
        o = A.ParameterObject()
        o.params["ColumnNum"] = P(oead.S32(len(items)))
        for i, (it, data) in enumerate(items, 1):
            o.params[f"ItemSort{i:03d}"] = P(oead.S32(int(data["sort"])))
            o.params[f"ItemName{i:03d}"] = P(oead.FixedSafeString64(it))
            o.params[f"ItemNum{i:03d}"] = P(oead.S32(int(data["num"])))
            o.params[f"ItemAdjustPrice{i:03d}"] = P(oead.S32(int(data["adjust_price"])))
            o.params[f"ItemLookGetFlg{i:03d}"] = P(bool(data["look_get_flag"]))
            o.params[f"ItemAmount{i:03d}"] = P(oead.S32(int(data["amount"])))
        add_obj(pio, name, o)
    return bytes(pio.to_binary())


typed("ShopData", shop_from, shop_merge, shop_to)


# ------------------------------------------------------------- Physics (.bphysics)
CONTACT_ITEM = [("name", "name", "s32", False), ("info_type", "type", "s32", False), ("num", "num", "i32", True)]
CONTACT_HDR = 3387849585
CHAR_HDR = 2311816730
PHYS_HDR = 1258832850


def _pbool(o, name):
    p = need(o.params.get(A.Name(name)), f"Physics header missing {name}")
    return bool(p.v)


def _contact_from(l):
    hdr = need(l.objects.get(A.Name(CONTACT_HDR)), "contact info missing header")
    pn = need(_pint(hdr, "contact_point_info_num"), "contact point count")
    cn = need(_pint(hdr, "collision_info_num"), "collision count")
    return {"contact_point_info": [paramdata_from(need(obj(l, f"ContactPointInfo_{i}"), "contact entry"), CONTACT_ITEM)
                                   for i in range(pn)],
            "collision_info": [paramdata_from(need(obj(l, f"CollisionInfo_{i}"), "collision entry"), CONTACT_ITEM)
                               for i in range(cn)]}


def _contact_to(c):
    l = A.ParameterList()
    if c.get("contact_point_info") is not None and c.get("collision_info") is not None:
        hdr = A.ParameterObject()
        hdr.params["contact_point_info_num"] = P(oead.S32(len(c["contact_point_info"])))
        hdr.params["collision_info_num"] = P(oead.S32(len(c["collision_info"])))
        add_obj(l, CONTACT_HDR, hdr)
        for i, it in enumerate(c["contact_point_info"]):
            add_obj(l, f"ContactPointInfo_{i}", paramdata_to(it, CONTACT_ITEM))
        for i, it in enumerate(c["collision_info"]):
            add_obj(l, f"CollisionInfo_{i}", paramdata_to(it, CONTACT_ITEM))
    return l


def _contact_merge(b, d):
    return {"contact_point_info": d.get("contact_point_info") if d.get("contact_point_info") is not None
            else b.get("contact_point_info"),
            "collision_info": d.get("collision_info") if d.get("collision_info") is not None
            else b.get("collision_info")}


def _cloth_from(l):
    hdr = need(obj(l, "ClothHeader"), "Physics missing cloth header")
    n = need(_pint(hdr, "cloth_num"), "cloth count")
    return {"setup_file_path": need(pstr(hdr, "cloth_setup_file_path"), "cloth setup path"),
            "subwind": pobj_to_cbor(need(obj(l, "ClothSubWind"), "cloth subwind")),
            "cloths": {i: pobj_to_cbor(need(obj(l, f"Cloth_{i}"), f"Cloth_{i}")) for i in range(n)}}


def _cloth_to(c):
    l = A.ParameterList()
    hdr = A.ParameterObject()
    hdr.params["cloth_setup_file_path"] = P(oead.FixedSafeString256(c.get("setup_file_path") or ""))
    cloths = ilist(c["cloths"])
    hdr.params["cloth_num"] = P(oead.S32(len(cloths)))
    add_obj(l, "ClothHeader", hdr)
    set_obj(l, "ClothSubWind", c["subwind"])
    for i, o in cloths.items():
        set_obj(l, f"Cloth_{i}", o)
    return l


def physics_from(data):
    pio = A.ParameterIO.from_binary(data)
    ps = need(lst(pio, "ParamSet"), "Physics missing ParamSet")
    hdr = need(ps.objects.get(A.Name(PHYS_HDR)), "Physics missing header")
    out = {"ragdoll": None, "support_bone": None, "rigid_contact_info": None, "rigid_body_set": None,
           "character_controller": None, "cloth": None}
    if _pbool(hdr, "use_ragdoll"):
        out["ragdoll"] = need(pstr(obj(ps, "Ragdoll"), "ragdoll_setup_file_path"), "ragdoll path")
    if _pbool(hdr, "use_support_bone"):
        out["support_bone"] = need(pstr(obj(ps, "SupportBone"), "support_bone_setup_file_path"), "support bone path")
    if _pbool(hdr, "use_contact_info"):
        out["rigid_contact_info"] = _contact_from(need(lst(ps, "RigidContactInfo"), "rigid contact info"))
    n = need(_pint(hdr, "use_rigid_body_set_num"), "use_rigid_body_set_num")
    if n > 0:
        rbs = need(lst(ps, "RigidBodySet"), "rigid body set list")
        out["rigid_body_set"] = {i: plist_to_cbor(need(lst(rbs, f"RigidBodySet_{i}"), "rigid body set entry"))
                                 for i in range(n)}
    if _pbool(hdr, "use_character_controller"):
        cc = need(lst(ps, "CharacterController"), "character controller")
        out["character_controller"] = {
            "header": pobj_to_cbor(need(cc.objects.get(A.Name(CHAR_HDR)), "controller header")),
            "forms": {i: plist_to_cbor(f) for i, (_, f) in enumerate(cc.lists.items())}}
    if _pbool(hdr, "use_cloth"):
        out["cloth"] = _cloth_from(need(lst(ps, "Cloth"), "cloth section"))
    out["use_system_group_handler"] = _pbool(hdr, "use_system_group_handler")
    return out


def _first(d, b, key):
    return d.get(key) if d.get(key) is not None else b.get(key)


def physics_merge(b, d):
    return {
        "ragdoll": _first(d, b, "ragdoll"),
        "support_bone": _first(d, b, "support_bone"),
        "rigid_contact_info": opt_merge(b.get("rigid_contact_info"), d.get("rigid_contact_info"), _contact_merge),
        "rigid_body_set": opt_merge(b.get("rigid_body_set"), d.get("rigid_body_set"),
                                    lambda x, y: index_merge(ilist(x), y)),
        "character_controller": opt_merge(
            b.get("character_controller"), d.get("character_controller"),
            lambda x, y: {"header": merge_pobj_cbor(x["header"], y["header"]),
                          "forms": index_merge(ilist(x["forms"]), y["forms"])}),
        "cloth": opt_merge(b.get("cloth"), d.get("cloth"),
                           lambda x, y: {"setup_file_path": _first(y, x, "setup_file_path"),
                                         "subwind": merge_pobj_cbor(x["subwind"], y["subwind"]),
                                         "cloths": index_merge(ilist(x["cloths"]), y["cloths"])}),
        "use_system_group_handler": _first(d, b, "use_system_group_handler"),
    }


def physics_to(m):
    pio = new_pio()
    ps = A.ParameterList()
    hdr = A.ParameterObject()
    rbs = ilist(m["rigid_body_set"]) if m.get("rigid_body_set") is not None else None
    hdr.params["use_rigid_body_set_num"] = P(oead.S32(len(rbs) if rbs is not None else 0))
    hdr.params["use_ragdoll"] = P(m.get("ragdoll") is not None)
    hdr.params["use_cloth"] = P(m.get("cloth") is not None)
    hdr.params["use_support_bone"] = P(m.get("support_bone") is not None)
    hdr.params["use_character_controller"] = P(m.get("character_controller") is not None)
    hdr.params["use_contact_info"] = P(m.get("rigid_contact_info") is not None)
    hdr.params["use_edge_rigid_body_num"] = P(oead.S32(0))
    hdr.params["use_system_group_handler"] = P(bool(m.get("use_system_group_handler") or False))
    add_obj(ps, PHYS_HDR, hdr)
    for name, key, field in (("Ragdoll", "ragdoll_setup_file_path", "ragdoll"),
                             ("SupportBone", "support_bone_setup_file_path", "support_bone")):
        if m.get(field) is not None:
            o = A.ParameterObject()
            o.params[key] = P(oead.FixedSafeString256(m[field]))
            add_obj(ps, name, o)
    if m.get("rigid_contact_info") is not None:
        add_list(ps, "RigidContactInfo", _contact_to(m["rigid_contact_info"]))
    if rbs is not None:
        l = A.ParameterList()
        for i, x in rbs.items():
            add_list(l, f"RigidBodySet_{i}", plist_from_cbor(x))
        add_list(ps, "RigidBodySet", l)
    if m.get("character_controller") is not None:
        cc = m["character_controller"]
        l = A.ParameterList()
        set_obj(l, CHAR_HDR, cc["header"])
        for i, f in ilist(cc["forms"]).items():
            add_list(l, f"Form_{i}", plist_from_cbor(f))
        add_list(ps, "CharacterController", l)
    if m.get("cloth") is not None:
        add_list(ps, "Cloth", _cloth_to(m["cloth"]))
    add_list(pio, "ParamSet", ps)
    return bytes(pio.to_binary())


typed("Physics", physics_from, physics_merge, physics_to)


# ------------------------------------------------------------- AIProgram (.baiprog)
AIDEF = [("name", "Name", "str", True), ("class_name", "ClassName", "s32", False),
         ("group_name", "GroupName", "str", True)]


def aiprog_from(data):
    pio = A.ParameterIO.from_binary(data)
    demos = need(obj(pio, "DemoAIActionIdx"), "AI program missing DemoAIActionIdx")
    ais = need(lst(pio, "AI"), "AI program missing AI list")
    actions = need(lst(pio, "Action"), "AI program missing Action list")
    behaviors = need(lst(pio, "Behavior"), "AI program missing Behavior list")
    queries = need(lst(pio, "Query"), "AI program missing Query list")
    ai_l = [l for _, l in ais.lists.items()]
    act_l = [l for _, l in actions.lists.items()]
    beh_l = [l for _, l in behaviors.lists.items()]
    q_l = [l for _, l in queries.lists.items()]
    act_off = len(ai_l)
    beh_off = act_off + len(act_l)
    q_off = beh_off + len(beh_l)

    def locate(index):
        if index >= q_off:
            return q_l[index - q_off], "Query"
        if index >= beh_off:
            return beh_l[index - beh_off], "Behavior"
        if index >= act_off:
            return act_l[index - act_off], "Action"
        return ai_l[index], "AI"

    def entry(l, category, depth=0):
        if depth > 64:
            raise ValueError("AI program tree too deep")
        d = paramdata_from(need(obj(l, "Def"), "AI entry missing Def object"), AIDEF)
        si = obj(l, "SInst")
        bi = obj(l, "BehaviorIdx")
        ci = obj(l, "ChildIdx")
        children = None
        if ci is not None:
            children = {}
            for k, v in ci.params.items():
                cl, cat = locate(int(v.v))
                children[k.hash] = entry(cl, cat, depth + 1)
        return {"category": category, "def": d,
                "params": pobj_to_cbor(si) if si is not None else None,
                "behaviors": {k.hash: int(v.v) for k, v in bi.params.items()} if bi is not None else None,
                "children": children}

    out_demos = {}
    for k, v in demos.params.items():
        l, cat = locate(int(v.v))
        out_demos[k.hash] = entry(l, cat)
    child_idx = set()
    for l in ai_l:
        ci = obj(l, "ChildIdx")
        if ci is not None:
            child_idx.update(int(v.v) for _, v in ci.params.items())
    roots = {}
    for i, (l, cat) in enumerate([(l, "AI") for l in ai_l] + [(l, "Action") for l in act_l]):
        if i in child_idx:
            continue
        e = entry(l, cat)
        roots[need(e["def"].get("name"), "AI entry def missing name")] = e
    return {"demos": out_demos,
            "behaviors": {i: entry(l, "Behavior") for i, l in enumerate(beh_l)},
            "queries": {i: entry(l, "Query") for i, l in enumerate(q_l)},
            "roots": roots}


def aientry_merge(b, d):
    out = {"category": b["category"], "def": d["def"]}
    out["params"] = opt_merge(b.get("params"), d.get("params"), merge_pobj_cbor)
    out["behaviors"] = opt_merge(b.get("behaviors"), d.get("behaviors"), lambda x, y: {**x, **y})
    out["children"] = opt_merge(b.get("children"), d.get("children"), _entries_merge)
    return out


def _entries_merge(b, d):
    out = {}
    for k in list(dict.fromkeys(list(b) + list(d))):
        out[k] = aientry_merge(b[k], d[k]) if k in b and k in d else (b[k] if k in b else d[k])
    return out


def aiprog_merge(b, d):
    # UKMM collects these keys through HashSets (arbitrary order); keep base order
    return {"demos": _entries_merge(b["demos"], d["demos"]),
            "behaviors": dict(sorted(_entries_merge(ilist(b["behaviors"]), ilist(d["behaviors"])).items())),
            "queries": dict(sorted(_entries_merge(ilist(b["queries"]), ilist(d["queries"])).items())),
            "roots": _entries_merge(b["roots"], d["roots"])}


def _def_key(d):
    return (d.get("name"), d["class_name"], d.get("group_name"))


def aiprog_to(m):
    def count_ais(e, checked):
        k = _def_key(e["def"])
        own = 0 if k in checked else 1
        checked.add(k)
        if e["category"] != "AI":
            return 0
        return own + sum(count_ais(c, checked) for c in (e.get("children") or {}).values())

    checked = set()
    action_offset = sum(count_ais(r, checked) for r in m["roots"].values())
    ais, actions, behaviors, queries = [], [], [], []
    finished = {}

    def to_list(e):
        k = _def_key(e["def"])
        cat = e["category"]
        if cat in ("AI", "Action") and k in finished:
            return finished[k]
        pl = A.ParameterList()
        add_obj(pl, "Def", paramdata_to(e["def"], AIDEF))
        if e.get("children") is not None:
            add_obj(pl, "ChildIdx", A.ParameterObject())
        if e.get("behaviors") is not None:
            bo = A.ParameterObject()
            for h, idx in e["behaviors"].items():
                bo.params[A.Name(int(h))] = P(oead.S32(int(idx)))
            add_obj(pl, "BehaviorIdx", bo)
        if e.get("params") is not None:
            set_obj(pl, "SInst", e["params"])
        if cat == "AI":
            index = len(ais)
            ais.append(pl)
            if e.get("children") is not None:
                kids = sorted(e["children"].items(),
                              key=lambda kv: not str(kv[1]["def"].get("name") or "").startswith("Demo_"))
                co = A.ParameterObject()
                for h, child in kids:
                    co.params[A.Name(int(h))] = P(oead.S32(to_list(child)))
                pl.objects[A.Name("ChildIdx")] = co
            finished[k] = index
            return index
        if cat == "Action":
            index = action_offset + len(actions)
            actions.append(pl)
            finished[k] = index
            return index
        (behaviors if cat == "Behavior" else queries).append(pl)
        return 0

    for e in ilist(m["behaviors"]).values():
        to_list(e)
    for e in ilist(m["queries"]).values():
        to_list(e)
    for e in m["roots"].values():
        to_list(e)
    demo_items = [(int(h), to_list(e)) for h, e in m["demos"].items()]
    nt = A.get_default_name_table()

    def demo_name(h):
        try:
            return nt.get_name(h, 0, H("DemoAIActionIdx"))
        except Exception:
            return None
    demo_items.sort(key=lambda hv: (demo_name(hv[0]) is not None, demo_name(hv[0]) or ""))
    pio = new_pio()
    do = A.ParameterObject()
    for h, idx in demo_items:
        do.params[A.Name(h)] = P(oead.S32(idx))
    add_obj(pio, "DemoAIActionIdx", do)
    for name, items, prefix in (("AI", ais, "AI"), ("Action", actions, "Action"),
                                ("Behavior", behaviors, "Behavior"), ("Query", queries, "Query")):
        l = A.ParameterList()
        for i, pl in enumerate(items):
            add_list(l, f"{prefix}_{i}", pl)
        add_list(pio, name, l)
    return bytes(pio.to_binary())


typed("AIProgram", aiprog_from, aiprog_merge, aiprog_to)


# ------------------------------------------------------------- BYML-based resources
from korok_ukmm_native import byml_from_cbor as _bc  # noqa: E402


def _byml_shallow(b, d):
    """Byml as Mergeable: shallow merge of hashes (oead values)."""
    if isinstance(b, oead.byml.Hash) and isinstance(d, oead.byml.Hash):
        return merge_byml_shallow(b, d)
    if d is None and isinstance(b, oead.byml.Hash):
        return b
    raise ValueError("Can only shallow merge BYML hashes")


def deep_merge(base: dict, diff: dict, merge_value, sort=False) -> dict:
    """DeleteMap::deep_merge where values are already Python/oead objects."""
    keys = list(dict.fromkeys([k for k, v in base.items() if not v[1]] + list(diff)))
    out = {}
    for k in keys:
        b, d = base.get(k), diff.get(k)
        if b is not None and d is not None:
            out[k] = [merge_value(b[0], d[0]), bool(d[1])]
        else:
            out[k] = list(d if d is not None else b)
    out = {k: v for k, v in out.items() if not v[1]}
    return dict(sorted(out.items())) if sort else out


def _byml_dmap(d: dict, key=lambda k: k) -> dict:
    """CBOR {key: [Byml, deleted]} -> {key: [oead value, deleted]}"""
    return {key(k): [_bc(v[0]), bool(v[1])] for k, v in d.items()}


def _hash_byml(h: int):
    return oead.U32(h) if h > 0x7FFFFFFF else oead.S32(h)


# ------------------------------------------------------------- ActorInfo.product.sbyml
def actorinfo_build(data, vanilla, canon):
    diff = _byml_dmap(data, int)
    if vanilla is None:
        model = {k: v for k, v in sorted(diff.items()) if not v[1]}
    else:
        root = oead.byml.from_binary(vanilla)
        base = {}
        for a in need(root.get("Actors"), "Actor info missing Actors"):
            base[H(str(a["name"]))] = [a, False]
        model = deep_merge(dict(sorted(base.items())), diff, _byml_shallow, sort=True)
    actors = oead.byml.Array([v[0] for v in model.values()])
    hashes = oead.byml.Array([_hash_byml(k) for k in model])
    return byml_to_binary(oead.byml.Hash({"Actors": actors, "Hashes": hashes}))


BUILDERS["ActorInfo"] = actorinfo_build


# ------------------------------------------------------------- GameDataPack (GameData/gamedata.ssarc)
GAMEDATA_FIELDS = ["bool_array_data", "bool_data", "f32_array_data", "f32_data", "revival_bool_data",
                   "revival_s32_data", "s32_array_data", "s32_data", "string32_data", "string64_array_data",
                   "string64_data", "string256_array_data", "string256_data", "vector2f_array_data",
                   "vector2f_data", "vector3f_array_data", "vector3f_data", "vector4f_data"]
# (serde field, BYML key, kind, optional) - #[derive(BymlData)] FlagData
FLAGDATA = [("category", "Category", "i32", True), ("data_name", "DataName", "str", False),
            ("delete_rev", "DeleteRev", "i32", False), ("hash_value", "HashValue", "i32", False),
            ("init_value", "InitValue", "byml", False), ("is_event_associated", "IsEventAssociated", "bool", False),
            ("is_one_trigger", "IsOneTrigger", "bool", False), ("is_program_readable", "IsProgramReadable", "bool", False),
            ("is_program_writable", "IsProgramWritable", "bool", False), ("is_save", "IsSave", "bool", False),
            ("max_value", "MaxValue", "byml", False), ("min_value", "MinValue", "byml", False),
            ("reset_type", "ResetType", "i32", False)]
GAMEDATA_STAGES = ("MainField", "AocField", "CDungeon", "MainFieldDungeon")


def _i32(v) -> int:
    v = int(v)
    return v - (1 << 32) if v >= (1 << 31) else v


def bymldata_from_byml(h, fields):
    """#[derive(BymlData)] TryFrom<&Byml>: an oead Hash -> normalized oead Hash."""
    out = oead.byml.Hash()
    for _, key, kind, optional in fields:
        if key not in h:
            if optional:
                continue
            raise ValueError(f"missing {key}")
        v = h[key]
        if kind == "i32":
            v = oead.S32(_i32(v))
        elif kind == "bool":
            v = bool(v)
        elif kind == "str":
            v = str(v)
        out[key] = v
    return out


def bymldata_from_cbor(d, fields):
    """A serde struct with BymlData fields -> oead Hash."""
    out = oead.byml.Hash()
    for field, key, kind, optional in fields:
        v = d.get(field)
        if v is None and optional:
            continue
        if kind == "i32":
            v = oead.S32(_i32(v))
        elif kind == "bool":
            v = bool(v)
        elif kind == "str":
            v = str(v)
        elif kind == "byml":
            v = _bc(v)
        out[key] = v
    return out


def _gd_extract(sarc, field):
    data_type = "string_data" if field == "string32_data" else field.replace("revival_", "", 1)
    flags = {}
    i = 0
    while True:
        f = sarc.get_file(f"/{field}_{i}.bgdata") or sarc.get_file(f"{field}_{i}.bgdata")
        if f is None:
            break
        h = oead.byml.from_binary(bytes(f.data))
        for item in (h.get(data_type) or []):
            flags[str(item["DataName"])] = [bymldata_from_byml(item, FLAGDATA), False]
        i += 1
    return {"data_type": data_type, "flags": flags}


def gamedata_from(data):
    sarc = oead.Sarc(data)
    names = [f.name.lstrip("/") for f in sarc.get_files()]
    if any(n.startswith("revival") for n in names):
        return {f: _gd_extract(sarc, f) for f in GAMEDATA_FIELDS}
    # BCML-style pack (no revival_ files): sort bool/s32 flags into the revival tables
    out = {f: _gd_extract(sarc, f) for f in GAMEDATA_FIELDS}
    rb, b, rs, s, s32 = {}, {}, {}, {}, {}
    for f in sarc.get_files():
        n = f.name.lstrip("/")
        h = oead.byml.from_binary(bytes(f.data))
        for prefix, key, plain, revival in (("bool_data", "bool_data", b, rb), ("s32_data", "s32_data", s, rs),
                                            ("string_data", "string_data", s32, None)):
            if n.startswith(prefix):
                for item in (h.get(key) or []):
                    name = str(item["DataName"])
                    flag = [bymldata_from_byml(item, FLAGDATA), False]
                    first = name.split("_")[0]
                    if revival is not None and first in GAMEDATA_STAGES and \
                            (prefix != "bool_data" or "HiddenKorok" not in name):
                        revival[name] = flag
                    else:
                        plain[name] = flag
    out["bool_data"] = {"data_type": "bool_data", "flags": b}
    out["revival_bool_data"] = {"data_type": "bool_data", "flags": rb}
    out["s32_data"] = {"data_type": "s32_data", "flags": s}
    out["revival_s32_data"] = {"data_type": "s32_data", "flags": rs}
    out["string32_data"] = {"data_type": "string_data", "flags": s32}
    return out


def _gd_diff_norm(d):
    return {"data_type": d["data_type"],
            "flags": {k: [bymldata_from_cbor(v[0], FLAGDATA) if isinstance(v[0], dict) else v[0], bool(v[1])]
                      for k, v in d["flags"].items()}}


def gamedata_build(data, vanilla, canon):
    diff = {f: _gd_diff_norm(data[f]) for f in GAMEDATA_FIELDS if f in data}
    if vanilla is None:
        model = {f: {"data_type": v["data_type"], "flags": {k: x for k, x in v["flags"].items() if not x[1]}}
                 for f, v in diff.items()}
    else:
        base = gamedata_from(vanilla)
        model = {}
        for f in GAMEDATA_FIELDS:
            b = base[f]
            if f in diff:
                model[f] = {"data_type": b["data_type"], "flags": dmap_merge(b["flags"], diff[f]["flags"])}
            else:
                model[f] = b
    w = oead.SarcWriter(endian=oead.Endianness.Big, mode=oead.SarcWriter.Mode.Legacy)
    w.set_min_alignment(4)
    for f in GAMEDATA_FIELDS:
        if f not in model:
            continue
        flags = [v[0] for v in model[f]["flags"].values() if not v[1]]
        for i in range((len(flags) + 4095) // 4096):
            chunk = oead.byml.Array(flags[i * 4096:(i + 1) * 4096])
            w.files[f"/{f}_{i}.bgdata"] = byml_to_binary(oead.byml.Hash({model[f]["data_type"]: chunk}))
    return bytes(w.write()[1])


BUILDERS["GameDataPack"] = gamedata_build


# ------------------------------------------------------------- SaveDataPack (GameData/savedataformat.ssarc)
SAVE_HEADER = [("is_common", "IsCommon", "bool", False),
               ("is_common_at_same_account", "IsCommonAtSameAccount", "bool", False),
               ("is_save_secure_code", "IsSaveSecureCode", "bool", False),
               ("file_name", "file_name", "str", False)]
SAVE_FILES = ("game_data.sav", "caption.sav", "option.sav")


def _sv_flags_merge(base: dict, diff: dict) -> dict:
    """SortedDeleteSet<Flag> merge; flags compare by hash, base keeps its name."""
    keys = dict(base)
    dels = {}
    for h, (name, d) in diff.items():
        if h not in keys:
            keys[h] = name
        dels[h] = d
    return {h: keys[h] for h in sorted(keys) if not dels.get(h, False)}


def savedata_from(data):
    sarc = oead.Sarc(data)
    out = {}
    for f in sarc.get_files():
        if not f.name.endswith(".bgsvdata"):
            continue
        fl = oead.byml.from_binary(bytes(f.data))["file_list"]
        hdr = {k: (bool(fl[0][n]) if kind == "bool" else str(fl[0][n])) for k, n, kind, _ in SAVE_HEADER}
        flags = {_i32(x["HashValue"]): str(x["DataName"]) for x in fl[1]}
        name = hdr["file_name"]
        if name in out:
            out[name]["flags"] = _sv_flags_merge(out[name]["flags"], {h: (n, False) for h, n in flags.items()})
        else:
            out[name] = {"header": hdr, "flags": dict(sorted(flags.items()))}
    return out


def _sv_diff_flags(cbor_flags) -> dict:
    """CBOR SortedDeleteSet<Flag>: {(map name/hash): deleted} -> {hash: (name, deleted)}"""
    out = {}
    for k, d in cbor_flags.items():
        out[_i32(k["hash"])] = (str(k["name"]), bool(d))
    return out


def savedata_build(data, vanilla, canon):
    base = savedata_from(vanilla) if vanilla is not None else {}
    model = {}
    for key in SAVE_FILES:
        b = base.get(key) or {"header": None, "flags": {}}
        d = data.get(key) or {"header": None, "flags": {}}
        hdr = b["header"] or d.get("header")
        flags = _sv_flags_merge(b["flags"], _sv_diff_flags(d.get("flags") or {}))
        if hdr is not None:
            model[key] = {"header": hdr, "flags": flags}
    w = oead.SarcWriter(endian=oead.Endianness.Big)
    w.set_min_alignment(4)
    count = 0
    for key in SAVE_FILES:
        m = model.get(key)
        if m is None:
            continue
        flags = list(m["flags"].items())
        n = (len(flags) + 8191) // 8192
        for i in range(n):
            arr = oead.byml.Array([oead.byml.Hash({"DataName": nm, "HashValue": oead.S32(h)})
                                   for h, nm in flags[i * 8192:(i + 1) * 8192]])
            doc = oead.byml.Hash({
                "file_list": oead.byml.Array([bymldata_from_cbor(m["header"], SAVE_HEADER), arr]),
                "save_info": oead.byml.Array([oead.byml.Hash({"directory_num": oead.S32(8),
                                                              "is_build_machine": True,
                                                              "revision": oead.S32(18203)})])})
            w.files[f"/saveformat_{count + i}.bgsvdata"] = byml_to_binary(doc)
        count += n
    return bytes(w.write()[1])


BUILDERS["SaveDataPack"] = savedata_build


# ------------------------------------------------------------- MapUnit (*_Static/_Dynamic/CDungeon .smubin)
def _u32(v) -> int:
    return int(v) & 0xFFFFFFFF


def _unit_entries(arr):
    return {_u32(o["HashId"]): [o, False] for o in arr}


def mapunit_build(data, vanilla, canon):
    d_objs = _byml_dmap(data.get("objects") or {}, _u32)
    d_rails = _byml_dmap(data.get("rails") or {}, _u32)
    if vanilla is None:
        objs = {k: v for k, v in sorted(d_objs.items()) if not v[1]}
        rails = {k: v for k, v in sorted(d_rails.items()) if not v[1]}
    else:
        root = oead.byml.from_binary(vanilla)
        objs = dmap_merge(_unit_entries(need(root.get("Objs"), "Map unit missing objs")), d_objs, sort=True)
        rails = dmap_merge(_unit_entries(need(root.get("Rails"), "Map unit missing rails")), d_rails, sort=True)
    out = oead.byml.Hash()
    out["Objs"] = oead.byml.Array([v[0] for v in objs.values()])
    out["Rails"] = oead.byml.Array([v[0] for v in rails.values()])
    # MapUnit::merge takes the diff's location values as they are (None drops them)
    for key, field in (("LocationPosX", "pos_x"), ("LocationPosZ", "pos_z"), ("LocationSize", "size")):
        if data.get(field) is not None:
            out[key] = oead.F32(float(data[field]))
    return byml_to_binary(out)


BUILDERS["MapUnit"] = mapunit_build


# ------------------------------------------------------------- QuestProduct (Quest/QuestProduct.sbquestpack)
def quest_build(data, vanilla, canon):
    diff = _byml_dmap(data, str)
    if vanilla is None:
        model = {k: v for k, v in diff.items() if not v[1]}
    else:
        base = {str(q["Name"]): [q, False] for q in oead.byml.from_binary(vanilla)}
        model = dmap_merge(base, diff)
    return byml_to_binary(oead.byml.Array([v[0] for v in model.values()]))


BUILDERS["QuestProduct"] = quest_build


# ------------------------------------------------------------- ResidentActors (Actor/ResidentActors.byml)
def residents_build(data, vanilla, canon):
    diff = {str(k): [{"only_res": bool(v[0]["only_res"]),
                      "scale": _bc(v[0]["scale"]) if v[0].get("scale") is not None else None}, bool(v[1])]
            for k, v in data.items()}
    if vanilla is None:
        model = {k: v for k, v in sorted(diff.items()) if not v[1]}
    else:
        base = {}
        for a in oead.byml.from_binary(vanilla):
            base[str(a["name"])] = [{"only_res": bool(need(a.get("only_res"), "missing only_res")),
                                     "scale": a.get("scale")}, False]
        model = dmap_merge(dict(sorted(base.items())), diff, sort=True)
    arr = oead.byml.Array()
    for name, (d, _) in model.items():
        h = oead.byml.Hash({"name": name, "only_res": d["only_res"]})
        if d.get("scale") is not None:
            h["scale"] = d["scale"]
        arr.append(h)
    return byml_to_binary(arr)


BUILDERS["ResidentActors"] = residents_build


# ------------------------------------------------------------- ResidentEvents (Event/ResidentEvent.byml)
def resevents_build(data, vanilla, canon):
    diff = {str(k): [dict(v[0]), bool(v[1])] for k, v in data.items()}
    if vanilla is None:
        model = {k: v for k, v in diff.items() if not v[1]}
    else:
        base = {}
        for e in oead.byml.from_binary(vanilla):
            base.setdefault(str(e["entry"]), [{}, False])[0][str(e["file"])] = False
        model = dmap_merge(base, diff)
    arr = oead.byml.Array()
    for entry, (files, _) in model.items():
        for f, dl in files.items():
            if not dl:
                arr.append(oead.byml.Hash({"entry": entry, "file": f}))
    return byml_to_binary(arr)


BUILDERS["ResidentEvents"] = resevents_build


# ------------------------------------------------------------- AreaData (Ecosystem/AreaData.sbyml)
def areadata_build(data, vanilla, canon):
    diff = _byml_dmap(data, int)
    if vanilla is None:
        model = {k: v for k, v in sorted(diff.items()) if not v[1]}
    else:
        base = {int(a["AreaNumber"].v if hasattr(a["AreaNumber"], "v") else a["AreaNumber"]): [a, False]
                for a in oead.byml.from_binary(vanilla)}
        model = deep_merge(dict(sorted(base.items())), diff, _byml_shallow, sort=True)
    return byml_to_binary(oead.byml.Array([v[0] for v in model.values()]))


BUILDERS["AreaData"] = areadata_build


# ------------------------------------------------------------- Static (Map/CDungeon/Static.smubin)
def _btext(b) -> str:
    return oead.byml.to_text(b) if isinstance(b, (oead.byml.Hash, oead.byml.Array)) else repr(b)


def _dvec_merge_byml(base: list, diff: list) -> list:
    """DeleteVec<Byml>::merge, comparing BYML values by content."""
    deleted = {_btext(it) for it, d in diff if d}
    items = [it for it, d in base if not d and _btext(it) not in deleted]
    keys = [_btext(it) for it in items]
    for idx, it in enumerate(x for x, d in diff if not d):
        if _btext(it) not in keys:
            items.insert(idx, it)
            keys.insert(idx, _btext(it))
    return [[it, False] for it in items]


def static_build(data, vanilla, canon):
    d_general = {str(k): [[_bc(it), bool(dl)] for it, dl in v] for k, v in (data.get("general") or {}).items()}
    d_start = {}
    for m, (entries, dl) in (data.get("start_pos") or {}).items():
        d_start[str(m)] = [{str(p): [{"rotate": _bc(e[0]["rotate"]), "translate": _bc(e[0]["translate"]),
                                      "player_state": e[0].get("player_state")}, bool(e[1])]
                            for p, e in entries.items()}, bool(dl)]
    if vanilla is None:
        general = {k: v for k, v in sorted(d_general.items())}
        start = {k: v for k, v in d_start.items() if not v[1]}
    else:
        root = oead.byml.from_binary(vanilla)
        base_start = {}
        for e in root.get("StartPos") or []:
            if "PosName" not in e:
                continue
            base_start.setdefault(str(e["Map"]), [{}, False])[0][str(e["PosName"])] = [
                {"rotate": e["Rotate"], "translate": e["Translate"],
                 "player_state": str(e["PlayerState"]) if "PlayerState" in e else None}, False]
        base_general = {k: [[it, False] for it in v] for k, v in sorted(root.items()) if k != "StartPos"}
        # Static::merge keeps only the base's general keys
        general = {k: (_dvec_merge_byml(v, d_general[k]) if k in d_general else v) for k, v in base_general.items()}
        start = deep_merge(base_start, d_start, lambda b, d: dmap_merge(b, d))
    out = oead.byml.Hash()
    sp = oead.byml.Array()
    for m, (entries, _) in start.items():
        for pos, (e, dl) in entries.items():
            if dl:
                continue
            h = oead.byml.Hash({"Map": m, "PosName": pos, "Rotate": e["rotate"], "Translate": e["translate"]})
            if e.get("player_state") is not None:
                h["PlayerState"] = e["player_state"]
            sp.append(h)
    out["StartPos"] = sp
    for k, v in general.items():
        out[k] = oead.byml.Array([it for it, dl in v if not dl])
    return byml_to_binary(out)


BUILDERS["Static"] = static_build
