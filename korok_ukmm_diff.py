"""UKMM's Mergeable::diff for each resource type, on UKMM's serde shapes.

from_binary(kind, bytes) parses a game file into the same shape UKMM stores in
its packages (see korok_ukmm_formats for the reverse: merge + write), and
diff(kind, vanilla, modded) keeps only what the mod changed.

Port of uk-content (https://github.com/NiceneNerd/ukmm, GPL-3.0) by Nicene Nerd.
"""
import oead

import korok_ukmm_formats as F
from korok_ukmm_native import pio_to_cbor, pobj_to_cbor  # noqa: F401


class NoDiff(Exception):
    """This resource type has no diff port yet: the packer keeps the file whole."""


FROM = {}
DIFF = {}


def register(kind, from_binary, diff):
    FROM[kind] = from_binary
    DIFF[kind] = diff


def from_binary(kind, data):
    fn = FROM.get(kind)
    if fn is None:
        raise NoDiff(kind)
    return fn(data)


def diff(kind, base, other):
    return DIFF[kind](base, other)


def equal(kind, a, b):
    return a == b


# ------------------------------------------------------------- collections
def dset_diff(base: dict, other: dict) -> dict:
    """DeleteSet / SortedDeleteSet diff: new keys, then removed keys marked deleted."""
    out = {k: False for k in other if k not in base}
    out.update({k: True for k in base if k not in other})
    return out


def sorted_dset_diff(base: dict, other: dict) -> dict:
    return dict(sorted(dset_diff(base, other).items()))


def dmap_diff(base: dict, other: dict, sort=False) -> dict:
    """DeleteMap::diff: changed/new entries, then removed ones (deleted, old value)."""
    out = {k: [v[0], False] for k, v in other.items()
           if not v[1] and (k not in base or base[k][0] != v[0])}
    out.update({k: [v[0], True] for k, v in base.items() if k not in other})
    return dict(sorted(out.items())) if sort else out


def dmap_deep_diff(base: dict, other: dict, value_diff, default, sort=False) -> dict:
    """DeleteMap::deep_diff."""
    out = {}
    for k, v in other.items():
        if k in base:
            if base[k] != v:
                out[k] = [value_diff(base[k][0], v[0]), False]
        else:
            out[k] = [v[0], False]
    for k in base:
        if k not in other:
            out[k] = [default() if callable(default) else default, True]
    return dict(sorted(out.items())) if sort else out


def dvec_diff(base: list, other: list) -> list:
    """DeleteVec::diff: items only in other, then items only in base (deleted)."""
    b_items = [it for it, _ in base]
    o_items = [it for it, _ in other]
    return [[it, False] for it in o_items if it not in b_items] + \
           [[it, True] for it in b_items if it not in o_items]


def index_diff(base: dict, other: dict) -> dict:
    """util::simple_index_diff (BTreeMap<usize, T>)."""
    return {k: v for k, v in sorted(other.items()) if base.get(k) != v}


def opt_diff(base, other, fn):
    """other.map(|o| base.map(|b| b.diff(o)).unwrap_or(o))"""
    if other is None:
        return None
    return fn(base, other) if base is not None else other


# ------------------------------------------------------------- AAMP
def pobj_diff(base: dict, other: dict) -> dict:
    return {k: v for k, v in other.items() if k not in base or base[k] != v}


def plist_diff(base: dict, other: dict) -> dict:
    bl, bo = base.get("lists") or {}, base.get("objects") or {}
    lists = {}
    for k, v in (other.get("lists") or {}).items():
        if k not in bl:
            lists[k] = v
        elif bl[k] != v:
            lists[k] = plist_diff(bl[k], v)
    objs = {}
    for k, v in (other.get("objects") or {}).items():
        if k not in bo:
            objs[k] = v
        elif bo[k] != v:
            objs[k] = pobj_diff(bo[k], v)
    return {"lists": lists, "objects": objs}


def pio_diff(base: dict, other: dict) -> dict:
    return {"data_type": base["data_type"], "version": base["version"],
            "param_root": plist_diff(base["param_root"], other["param_root"])}


def pio_from(data):
    return pio_to_cbor(oead.aamp.ParameterIO.from_binary(data))


for _k in ("GenericAamp", "GeneralParamList", "Awareness", "DamageParam", "Lod", "UMii", "Demo",
           "WorldInfo"):
    register(_k, pio_from, pio_diff)


# ------------------------------------------------------------- BYML
def byml_to_cbor(v):
    """oead byml value -> roead Byml serde shape."""
    if v is None:
        return "Null"
    if isinstance(v, oead.byml.Hash) or isinstance(v, dict):
        return {"Map": {str(k): byml_to_cbor(x) for k, x in v.items()}}
    if isinstance(v, oead.byml.Array) or isinstance(v, list):
        return {"Array": [byml_to_cbor(x) for x in v]}
    if isinstance(v, bool):
        return {"Bool": v}
    if isinstance(v, str):
        return {"String": v}
    if isinstance(v, oead.S32) or type(v) is int:
        return {"I32": int(v)}
    if isinstance(v, oead.U32):
        return {"U32": int(v)}
    if isinstance(v, oead.F32) or type(v) is float:
        return {"Float": float(v)}
    if isinstance(v, oead.F64):
        return {"Double": float(v)}
    if isinstance(v, oead.S64):
        return {"I64": int(v)}
    if isinstance(v, oead.U64):
        return {"U64": int(v)}
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"BinaryData": bytes(v)}
    raise ValueError(f"BYML value of type {type(v).__name__}")


def byml_from(data):
    return byml_to_cbor(oead.byml.from_binary(data))


def byml_shallow_diff(base, other):
    """util::diff_byml_shallow (hashes only)."""
    if not (isinstance(base, dict) and "Map" in base and isinstance(other, dict) and "Map" in other):
        raise ValueError("Can only shallow diff BYML hashes")
    b, o = base["Map"], other["Map"]
    out = {k: v for k, v in o.items() if b.get(k) != v}
    out.update({k: "Null" for k in b if k not in o})
    return {"Map": out}


for _k in ("GenericByml", "AISchedule", "AnimationInfo", "EventInfo"):
    register(_k, byml_from, byml_shallow_diff)


# ------------------------------------------------------------- ActorLink
def actorlink_diff(b, o):
    return {"targets": pobj_diff(b["targets"], o["targets"]),
            "tags": opt_diff(b.get("tags"), o.get("tags"), dset_diff),
            "fit_tags": opt_diff(b.get("fit_tags"), o.get("fit_tags"), dset_diff)}


register("ActorLink", F.actorlink_from, actorlink_diff)


# ------------------------------------------------------------- actor parameter files
def droptable_diff(b, o):
    return {k: v for k, v in o.items() if b.get(k) != v}


def shop_diff(b, o):
    out = {}
    for name, table in o.items():
        st = b.get(name)
        if st is not None:
            if table is None:
                out[name] = None
            elif st != table:
                t = {k: v for k, v in table.items() if st.get(k) != v}
                t.update({k: {**v, "delete": True} for k, v in st.items() if k not in table})
                out[name] = t
        else:
            out[name] = table
    return out


def bonectrl_diff(b, o):
    objs = plist_diff({"objects": b["objects"]}, {"objects": o["objects"]})["objects"]
    groups = {}
    for g, bones in o["bone_groups"].items():
        if g in b["bone_groups"]:
            if b["bone_groups"][g] != bones:
                groups[g] = dset_diff(b["bone_groups"][g], bones)
        else:
            groups[g] = bones
    return {"objects": objs, "bone_groups": groups}


def rgconfiglist_diff(b, o):
    return {"common_data": pobj_diff(b["common_data"], o["common_data"]),
            "impulse_params": plist_diff(b["impulse_params"], o["impulse_params"]),
            "body_param_list": dmap_diff(b["body_param_list"], o["body_param_list"])}


def rgbw_diff(b, o):
    out = {}
    for k, ol in o.items():
        sl = b.get(k)
        if sl is not None and sl != ol:
            out[k] = dmap_diff(sl, ol)
        elif sl is None:
            out[k] = ol
    return out


def aslist_diff(b, o):
    bc, oc = b.get("cf_defines"), o.get("cf_defines")
    if bc is not None:
        cf = dmap_diff(bc, oc) if oc is not None else {}
    else:
        cf = oc
    return {"common": o.get("common") if o.get("common") != b.get("common") else None,
            "add_reses": dmap_diff(b["add_reses"], o["add_reses"]),
            "as_defines": dmap_diff(b["as_defines"], o["as_defines"]),
            "cf_defines": cf}


def chemical_diff(b, o):
    return {"unknown": o.get("unknown") if o.get("unknown") != b.get("unknown") else None,
            "body": index_diff(b["body"], o["body"])}


register("DropTable", F.droptable_from, droptable_diff)
register("Recipe", F.recipe_from, lambda b, o: o)
register("ShopData", F.shop_from, shop_diff)
register("BoneControl", F.bonectrl_from, bonectrl_diff)
register("RagdollConfigList", F.rgconfiglist_from, rgconfiglist_diff)
register("ASList", F.aslist_from, aslist_diff)
register("Chemical", F.chemical_from, chemical_diff)


def _frozen_keys(m):
    """RagdollBlendWeight keys are structs: CBOR map keys."""
    fd = _fd()
    return {(fd({"state_key": k[0], "system_key": k[1]}) if isinstance(k, tuple) else k): v
            for k, v in m.items()}


register("RagdollBlendWeight", lambda data: _frozen_keys(F.rgbw_from(data)), rgbw_diff)


def _changed(b, o, key):
    return o.get(key) if b.get(key) != o.get(key) else None


def life_diff(b, o):
    out = {}
    for field, _ in F._LIFE_SETS:
        out[field] = opt_diff(b.get(field), o.get(field), dset_diff)
    for field, _, _ in F._LIFE_ITEMS:
        out[field] = _changed(b, o, field)
    return out


def modellist_diff(b, o):
    md = {}
    for i, data in o["model_data"].items():
        v = b["model_data"].get(i)
        if v == data:
            continue
        md[i] = {"folder": data["folder"], "units": dmap_diff(v["units"], data["units"])} if v is not None \
            else data
    return {"controller_info": pobj_diff(b["controller_info"], o["controller_info"]),
            "attention": pobj_diff(b["attention"], o["attention"]),
            "model_data": md,
            "anm_target": index_diff(b["anm_target"], o["anm_target"]),
            "locators": dvec_diff(b["locators"], o["locators"])}


register("LifeCondition", F.life_from, life_diff)
register("AttClientList", F.attclientlist_from,
         lambda b, o: {"att_pos": pobj_diff(b["att_pos"], o["att_pos"]),
                       "att_clients": dmap_diff(b["att_clients"], o["att_clients"])})
register("AttClient", F.attclient_from,
         lambda b, o: {"client_params": pobj_diff(b["client_params"], o["client_params"]),
                       "checks": dvec_diff(b["checks"], o["checks"])})
register("ModelList", F.modellist_from, modellist_diff)
register("RagdollConfig", F.rgconfig_from,
         lambda b, o: {"attack_type_impulse_data": pobj_diff(b["attack_type_impulse_data"],
                                                             o["attack_type_impulse_data"]),
                       "impact_impulse_info": index_diff(b["impact_impulse_info"], o["impact_impulse_info"])})
register("ChemicalRes", F.chmres_from, lambda b, o: {k: dmap_diff(b[k], o[k]) for k in F._CHMRES})


# ------------------------------------------------------------- Physics
def _ne(b, o):
    return o if o != b else None


def _opt_struct_diff(b, o, fn):
    """Physics' Option fields: both present and different -> fn diff; equal -> None; else other."""
    if b is not None and o is not None and b != o:
        return fn(b, o)
    if b == o:
        return None
    return o


def physics_diff(b, o):
    return {
        "ragdoll": _ne(b.get("ragdoll"), o.get("ragdoll")),
        "support_bone": _ne(b.get("support_bone"), o.get("support_bone")),
        "rigid_contact_info": _opt_struct_diff(
            b.get("rigid_contact_info"), o.get("rigid_contact_info"),
            lambda x, y: {"contact_point_info": _ne(x.get("contact_point_info"), y.get("contact_point_info")),
                          "collision_info": _ne(x.get("collision_info"), y.get("collision_info"))}),
        "rigid_body_set": _opt_struct_diff(b.get("rigid_body_set"), o.get("rigid_body_set"), index_diff),
        "character_controller": _opt_struct_diff(
            b.get("character_controller"), o.get("character_controller"),
            lambda x, y: {"header": pobj_diff(x["header"], y["header"]),
                          "forms": index_diff(x["forms"], y["forms"])}),
        "cloth": _opt_struct_diff(
            b.get("cloth"), o.get("cloth"),
            lambda x, y: {"setup_file_path": _ne(x.get("setup_file_path"), y.get("setup_file_path")),
                          "subwind": pobj_diff(x["subwind"], y["subwind"]),
                          "cloths": index_diff(x["cloths"], y["cloths"])}),
        "use_system_group_handler": _ne(b.get("use_system_group_handler"), o.get("use_system_group_handler")),
    }


register("Physics", F.physics_from, physics_diff)


# ------------------------------------------------------------- AIProgram
def aientry_diff(b, o):
    def params(x, y):
        return pobj_diff(x, y)

    def behaviors(x, y):
        return {k: v for k, v in y.items() if x.get(k) != v}

    def children(x, y):
        out = {}
        for k, e in y.items():
            if k in x:
                if x[k] != e:
                    out[k] = aientry_diff(x[k], e)
            else:
                out[k] = e
        return out
    return {"category": b["category"], "def": o["def"],
            "params": opt_diff(b.get("params"), o.get("params"), params),
            "behaviors": opt_diff(b.get("behaviors"), o.get("behaviors"), behaviors),
            "children": opt_diff(b.get("children"), o.get("children"), children)}


def _entries_diff(b, o):
    out = {}
    for k, v in o.items():
        if k in b:
            if b[k] != v:
                out[k] = aientry_diff(b[k], v)
        else:
            out[k] = v
    return out


def aiprog_diff(b, o):
    return {"demos": _entries_diff(b["demos"], o["demos"]),
            "behaviors": {k: v for k, v in o["behaviors"].items() if b["behaviors"].get(k) != v},
            "queries": _entries_diff(b["queries"], o["queries"]),
            "roots": _entries_diff(b["roots"], o["roots"])}


register("AIProgram", F.aiprog_from, aiprog_diff)


# ------------------------------------------------------------- ActorInfo
def actorinfo_from(data):
    root = oead.byml.from_binary(data)
    out = {}
    for a in F.need(root.get("Actors"), "Actor info missing Actors"):
        out[F.H(str(a["name"]))] = [byml_to_cbor(a), False]
    return dict(sorted(out.items()))


register("ActorInfo", actorinfo_from,
         lambda b, o: dmap_deep_diff(b, o, byml_shallow_diff, "Null", sort=True))


# ------------------------------------------------------------- BYML data files
def bymldata_to_cbor(h, fields):
    """#[derive(BymlData)] struct from an oead Hash -> its serde shape."""
    out = {}
    for field, key, kind, optional in fields:
        if key not in h:
            if optional:
                out[field] = None
                continue
            raise ValueError(f"missing {key}")
        v = h[key]
        if kind == "i32":
            out[field] = F._i32(v)
        elif kind == "bool":
            out[field] = bool(v)
        elif kind == "str":
            out[field] = str(v)
        else:
            out[field] = byml_to_cbor(v)
    return out


class FrozenMap(dict):
    """A hashable dict, for struct keys in CBOR maps (cbor2 writes it as a plain map).
    cbor2's own frozendict class has a different name in every release, so korok uses this."""

    def __hash__(self):
        return hash(tuple(sorted(self.items())))

    def _ro(self, *a, **k):
        raise TypeError("FrozenMap is read-only")

    __setitem__ = __delitem__ = update = pop = popitem = clear = setdefault = _ro


def _fd():
    return FrozenMap


# GameDataPack
def gamedata_from(data):
    m = F.gamedata_from(data)
    return {f: {"data_type": m[f]["data_type"],
                "flags": {k: [bymldata_to_cbor(v[0], F.FLAGDATA), False] for k, v in m[f]["flags"].items()}}
            for f in F.GAMEDATA_FIELDS}


def gamedata_diff(b, o):
    return {f: {"data_type": b[f]["data_type"], "flags": dmap_diff(b[f]["flags"], o[f]["flags"])}
            for f in F.GAMEDATA_FIELDS}


register("GameDataPack", gamedata_from, gamedata_diff)


# SaveDataPack
def savedata_from(data):
    fd = _fd()
    out = {}
    for key, m in F.savedata_from(data).items():
        out[key] = {"header": dict(m["header"]),
                    "flags": {fd({"name": n, "hash": h}): False for h, n in sorted(m["flags"].items())}}
    return out


_SAVE_DEFAULT = {"header": {"is_common": False, "is_common_at_same_account": False,
                            "is_save_secure_code": False, "file_name": ""}, "flags": {}}


def savedata_diff(b, o):
    out = {}
    for key in F.SAVE_FILES:
        x, y = b.get(key) or _SAVE_DEFAULT, o.get(key) or _SAVE_DEFAULT
        bh = {k["hash"]: k for k in x["flags"]}
        oh = {k["hash"]: k for k in y["flags"]}
        flags = {oh[h]: False for h in oh if h not in bh}
        flags.update({bh[h]: True for h in bh if h not in oh})
        out[key] = {"header": x["header"], "flags": dict(sorted(flags.items(), key=lambda kv: kv[0]["hash"]))}
    return out


register("SaveDataPack", savedata_from, savedata_diff)


# MapUnit
def _hash_entries(arr):
    return dict(sorted((int(o["HashId"]) & 0xFFFFFFFF, [byml_to_cbor(o), False]) for o in arr))


def mapunit_from(data):
    root = oead.byml.from_binary(data)

    def f(key):
        return float(root[key]) if key in root else None
    return {"pos_x": f("LocationPosX"), "pos_z": f("LocationPosZ"), "size": f("LocationSize"),
            "objects": _hash_entries(F.need(root.get("Objs"), "Map unit missing objs")),
            "rails": _hash_entries(F.need(root.get("Rails"), "Map unit missing rails"))}


register("MapUnit", mapunit_from,
         lambda b, o: {"pos_x": o["pos_x"], "pos_z": o["pos_z"], "size": o["size"],
                       "objects": dmap_diff(b["objects"], o["objects"], sort=True),
                       "rails": dmap_diff(b["rails"], o["rails"], sort=True)})


# QuestProduct
register("QuestProduct",
         lambda data: {str(q["Name"]): [byml_to_cbor(q), False] for q in oead.byml.from_binary(data)},
         dmap_diff)


# ResidentActors
def residents_from(data):
    out = {}
    for a in oead.byml.from_binary(data):
        out[str(a["name"])] = [{"only_res": bool(F.need(a.get("only_res"), "missing only_res")),
                                "scale": byml_to_cbor(a["scale"]) if "scale" in a else None}, False]
    return dict(sorted(out.items()))


register("ResidentActors", residents_from, lambda b, o: dmap_diff(b, o, sort=True))


# ResidentEvents
def resevents_from(data):
    out = {}
    for e in oead.byml.from_binary(data):
        out.setdefault(str(e["entry"]), [{}, False])[0][str(e["file"])] = False
    return out


register("ResidentEvents", resevents_from, dmap_diff)


# AreaData
def areadata_from(data):
    return dict(sorted((int(a["AreaNumber"]), [byml_to_cbor(a), False]) for a in oead.byml.from_binary(data)))


register("AreaData", areadata_from, lambda b, o: dmap_deep_diff(b, o, byml_shallow_diff, "Null", sort=True))


# Tips
register("Tips",
         lambda data: dict(sorted((str(t["MessageId"]), [bymldata_to_cbor(t, F.TIPDATA), False])
                                  for t in oead.byml.from_binary(data))),
         lambda b, o: dmap_diff(b, o, sort=True))


# BarslistInfo
register("BarslistInfo",
         lambda data: dict(sorted((str(k), [{str(x): False for x in v if isinstance(x, str)}, False])
                                  for k, v in oead.byml.from_binary(data).items())),
         lambda b, o: dmap_deep_diff(b, o, dset_diff, dict, sort=True))


# LayoutArchive / FontArchive: the archive's files, whole
def _archive_from(data):
    return {f.name: bytes(f.data) for f in oead.Sarc(data).get_files()}


for _k in ("LayoutArchive", "FontArchive"):
    register(_k, _archive_from, lambda b, o: {n: d for n, d in o.items() if b.get(n) != d})


# ------------------------------------------------------------- MessagePack (game text)
def _msyt_json(doc) -> str:
    import json
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":"))


def messagepack_from(data):
    return {k: _msyt_json(v) for k, v in F.msbt_sarc_to_msyt(data).items()}


def messagepack_diff(b, o):
    import json
    out = {}
    for f, text in o.items():
        if f in b:
            if b[f] != text:
                x, y = json.loads(b[f]), json.loads(text)
                be = x.get("entries") or {}
                entries = {k: v for k, v in (y.get("entries") or {}).items() if be.get(k) != v}
                if entries:
                    d = {k: v for k, v in x.items() if k != "entries"}
                    d["entries"] = entries
                    out[f] = _msyt_json(d)
        else:
            out[f] = text
    return out


register("MessagePack", messagepack_from, messagepack_diff)


# ------------------------------------------------------------- LevelSensor
def levelsensor_from(data):
    m = F.levelsensor_from(data)
    lst_ = lambda series: {k: [list(v[0]), v[1]] for k, v in series.items()}  # noqa: E731
    return {"enemy": {k: [lst_(v[0]), v[1]] for k, v in m["enemy"].items()},
            "flag": lst_(m["flag"]), "setting": lst_(m["setting"]),
            "weapon": m["weapon"]}


register("LevelSensor", levelsensor_from,
         lambda b, o: {"enemy": dmap_deep_diff(b["enemy"], o["enemy"], dmap_diff, dict),
                       "flag": dmap_diff(b["flag"], o["flag"]),
                       "setting": dmap_diff(b["setting"], o["setting"]),
                       "weapon": dmap_deep_diff(b["weapon"], o["weapon"], dmap_diff, dict)})


# ------------------------------------------------------------- ShopGameDataInfo
def shopgd_from(data):
    root = oead.byml.from_binary(data)
    out = {}
    for field, key in (("area_info", "ShopAreaInfo"), ("sold_out_info", "SoldOutInfo")):
        sec = F.need(root.get(key), f"Shop game data info missing {key}")
        out[field] = dict(sorted((int(h) & 0xFFFFFFFF, [byml_to_cbor(v), False])
                                 for h, v in zip(sec["Hashes"], sec["Values"])))
    return out


register("ShopGameDataInfo", shopgd_from,
         lambda b, o: {f: dmap_diff(b[f], o[f], sort=True) for f in ("area_info", "sold_out_info")})


# ------------------------------------------------------------- CookData
def cookdata_from(data):
    h = oead.byml.from_binary(data)
    return {"recipes": F._dv([F._recipe_from(r, False) for r in h["Recipes"]]),
            "single_recipes": F._dv([F._recipe_from(r, True) for r in h["SingleRecipes"]]),
            "system": F._sys_from(h["System"])}


def cookdata_diff(b, o):
    sb, so = b["system"], o["system"]
    system = {f: so[f] for f, _, _ in F._SYS_SCALARS}
    for f in ("cei", "nmmr", "nmssr"):
        system[f] = dvec_diff(sb[f], so[f])
    return {"recipes": dvec_diff(b["recipes"], o["recipes"]),
            "single_recipes": dvec_diff(b["single_recipes"], o["single_recipes"]),
            "system": system}


register("CookData", cookdata_from, cookdata_diff)


# ------------------------------------------------------------- Static (Map/CDungeon/Static.smubin)
def static_from(data):
    root = oead.byml.from_binary(data)
    start = {}
    for e in root.get("StartPos") or []:
        if "PosName" not in e:
            continue
        start.setdefault(str(e["Map"]), [{}, False])[0][str(e["PosName"])] = [
            {"rotate": byml_to_cbor(e["Rotate"]), "translate": byml_to_cbor(e["Translate"]),
             "player_state": str(e["PlayerState"]) if "PlayerState" in e else None}, False]
    general = {k: [[byml_to_cbor(it), False] for it in v] for k, v in sorted(root.items()) if k != "StartPos"}
    return {"general": general, "start_pos": start}


def static_diff(b, o):
    general = {}
    for k, entries in o["general"].items():
        if k in b["general"]:
            if b["general"][k] != entries:
                general[k] = dvec_diff(b["general"][k], entries)
        else:
            general[k] = entries
    return {"general": general,
            "start_pos": dmap_deep_diff(b["start_pos"], o["start_pos"], dmap_diff, dict)}


register("Static", static_from, static_diff)
