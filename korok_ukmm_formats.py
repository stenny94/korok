"""UKMM mergeable resource types: vanilla file + the mod's version -> game file.

Each builder gets the mod's data for one resource (UKMM's serde form, decoded
from CBOR), the vanilla file (decompressed bytes, or None for a new file), and
returns the uncompressed game file. Ported from uk-content
(https://github.com/NiceneNerd/ukmm, GPL-3.0); see korok_ukmm_native.py.
"""
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


# ------------------------------------------------------------- game text
def build_text_pack(conv, file):
    """Pack/Bootup_<lang>.pack: UKMM rebuilds it from the MessagePack resource."""
    raise UnsupportedResource("MessagePack", file)


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
