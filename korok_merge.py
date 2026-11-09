"""Applies BCML-style merge logs (aslist.aamp, deepmerge.aamp) to game files.

BNPs don't ship files that BCML's mergers handle; they ship a sparse diff
instead.  To use such a BNP without BCML we take the base file (from another
mod already in the pack, else the vanilla game), apply the diff, and write the
result into the pack.  Requires the optional `oead` package (pip install oead).

Diff path syntax:  content/Pack/TitleBG.pack//Actor/Pack/X.sbactorpack//Actor/ASList/Player.baslist
"""
import concurrent.futures
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import time

try:
    import oead
except ImportError:  # optional dependency
    oead = None

SUPPORTED_LOGS = {"aslist.aamp", "deepmerge.aamp", "texts.json", "eventinfo.yml",
                  "actorinfo.yml", "gamedata.yml", "savedata.yml", "areadata.yml", "dstatic.yml", "drops.json",
                  "residents.yml", "mainstatic.yml", "quests.yml", "map.yml", "shop.aamp"}


def msyt_path():
    """Path of the msyt tool bundled with the 'pymsyt' package, or None."""
    try:
        import pymsyt
        import yaml  # noqa: F401
        p = pathlib.Path(pymsyt.MSYT_PATH)
        return p if p.exists() else None
    except Exception:
        return None


def supported_logs() -> set:
    out = set()
    if oead is not None:
        out |= {"aslist.aamp", "deepmerge.aamp",
                "actorinfo.yml", "gamedata.yml", "savedata.yml", "areadata.yml", "dstatic.yml", "drops.json",
                "residents.yml", "mainstatic.yml", "quests.yml", "map.yml", "shop.aamp"}
        if msyt_path() is not None:
            out.add("texts.json")
        try:
            import yaml  # noqa: F401
            out.add("eventinfo.yml")
        except ImportError:
            pass
    return out


LOG_FILE = None  # set by the engine to a writable path


def _log(msg: str):
    if LOG_FILE:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            pass


# ------------------------------------------------------------------ result cache
CACHE_DIR = None          # set by the engine; None disables caching
_HASH_MEMO = {}


def file_hash(path) -> str:
    p = pathlib.Path(path)
    st = p.stat()
    memo_key = (str(p), st.st_size, st.st_mtime_ns)
    h = _HASH_MEMO.get(memo_key)
    if h is None:
        d = hashlib.sha1()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                d.update(chunk)
        h = _HASH_MEMO[memo_key] = d.hexdigest()
    return h


CACHE_VERSION = "archives2"   # bump when merge output changes so stale cached results are not reused


def _cache_key(*parts) -> str:
    return hashlib.sha1("\x00".join(str(x) for x in (CACHE_VERSION,) + parts).encode("utf-8")).hexdigest()


def _atomic_copy(src: pathlib.Path, dst: pathlib.Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".korok_tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def _cache_get(key: str, out_path: pathlib.Path) -> bool:
    if CACHE_DIR is None:
        return False
    entry = pathlib.Path(CACHE_DIR) / key
    if entry.is_file():
        try:
            _atomic_copy(entry, out_path)
            os.utime(entry, None)  # recently used
            return True
        except OSError:
            return False
    return False


def _cache_put(key: str, out_path: pathlib.Path):
    if CACHE_DIR is None:
        return
    try:
        _atomic_copy(out_path, pathlib.Path(CACHE_DIR) / key)
    except OSError:
        pass


def prune_cache(max_bytes: int = 2 * 1024 ** 3):
    """Drops the least recently used cache entries beyond max_bytes."""
    if CACHE_DIR is None or not pathlib.Path(CACHE_DIR).is_dir():
        return
    files = [(f.stat().st_mtime, f.stat().st_size, f) for f in pathlib.Path(CACHE_DIR).iterdir() if f.is_file()]
    total = sum(x[1] for x in files)
    for _, size, f in sorted(files, key=lambda x: x[0]):
        if total <= max_bytes:
            break
        try:
            f.unlink()
            total -= size
        except OSError:
            pass


def _worker_count() -> int:
    return max(2, min(6, (os.cpu_count() or 4) // 2))


def available() -> bool:
    return oead is not None


# ---------------------------------------------------------------- AAMP helpers
def _name(key):
    return key if isinstance(key, oead.aamp.Name) else oead.aamp.Name(key)


def _load_log(path: pathlib.Path):
    raw = path.read_bytes()
    if raw[:4] == b"Yaz0":
        raw = bytes(oead.yaz0.decompress(raw))
    return oead.aamp.ParameterIO.from_binary(raw)


def parse_diff(pio) -> dict:
    """Returns {target path string: ParameterList diff}."""
    root = pio  # ParameterIO itself is the root list
    paths = []
    for obj in root.objects.values():
        for param in obj.params.values():
            try:
                s = param.v
                s = s if isinstance(s, str) else str(s)
            except Exception:
                continue
            if "/" in s:
                paths.append(s)
    out = {}
    for p in paths:
        lst = root.lists.get(_name(p))
        if lst is not None:
            out[p] = lst
    return out


def overlay(base, diff):
    """Generic sparse overlay: params in `diff` overwrite/add to `base`."""
    for key, dobj in diff.objects.items():
        bobj = base.objects.get(key)
        if bobj is None:
            base.objects[key] = dobj
            continue
        for pk, pv in dobj.params.items():
            bobj.params[pk] = pv
    for key, dlist in diff.lists.items():
        blist = base.lists.get(key)
        if blist is None:
            base.lists[key] = dlist
        else:
            overlay(blist, dlist)


def _str(param):
    try:
        return str(param.v)
    except Exception:
        return None


def _next_key(container, prefix):
    n = len(container)
    while True:
        k = oead.aamp.Name(f"{prefix}_{n}")
        if k not in container:
            return k
        n += 1


def _find_param_obj(lst, objname, paramname):
    """Map value of obj.param -> key, for entries (objects) of a list."""
    out = {}
    for k, v in lst.items():
        out[v] = k
    return out


def merge_aslist(base, diff):
    """Merge a Player.baslist-style diff (AddReses, ASDefines, CFDefines)."""
    # AddReses: objects keyed AddRes_n with param Anim; add the missing ones.
    d_add = diff.lists.get(_name("AddReses"))
    if d_add is not None:
        b_add = base.lists.get(_name("AddReses"))
        if b_add is None:
            base.lists[_name("AddReses")] = d_add
        else:
            have = {_str(o.params.get(_name("Anim"))) for o in b_add.objects.values()
                    if o.params.get(_name("Anim")) is not None}
            for o in d_add.objects.values():
                anim = _str(o.params.get(_name("Anim")))
                if anim not in have:
                    b_add.objects[_next_key(b_add.objects, "AddRes")] = o
                    have.add(anim)

    # ASDefines: lists/objects keyed ASDefine_n with Name; match by Name.
    d_as = diff.lists.get(_name("ASDefines"))
    if d_as is not None:
        b_as = base.lists.get(_name("ASDefines"))
        if b_as is None:
            base.lists[_name("ASDefines")] = d_as
        else:
            have = {}
            for k, o in b_as.objects.items():
                nm = o.params.get(_name("Name"))
                if nm is not None:
                    have[_str(nm)] = k.hash
            for o in d_as.objects.values():
                nm = _str(o.params.get(_name("Name"))) if o.params.get(_name("Name")) is not None else None
                if nm in have:
                    b_as.objects[oead.aamp.Name(have[nm])] = o
                else:
                    b_as.objects[_next_key(b_as.objects, "ASDefine")] = o

    # CFDefines: lists keyed CFDefine_n containing CFPre (obj), CFExcepts (obj),
    # CFPosts (list of CFPost_n objects).  Match on (CFPre.Name, CFExcepts values).
    d_cf = diff.lists.get(_name("CFDefines"))
    if d_cf is not None:
        b_cf = base.lists.get(_name("CFDefines"))
        if b_cf is None:
            base.lists[_name("CFDefines")] = d_cf
            return

        def sig(cf):
            pre = cf.objects.get(_name("CFPre"))
            pname = _str(pre.params.get(_name("Name"))) if pre is not None and pre.params.get(_name("Name")) is not None else None
            exc = cf.objects.get(_name("CFExcepts"))
            ex = tuple(sorted(_str(v) for v in exc.params.values())) if exc is not None else ()
            return (pname, ex)

        have = {sig(v): k.hash for k, v in b_cf.lists.items()}
        for dcf in d_cf.lists.values():
            s = sig(dcf)
            if s not in have:
                key = _next_key(b_cf.lists, "CFDefine")
                b_cf.lists[key] = dcf
                have[s] = key.hash
                continue
            bcf = b_cf.lists.get(oead.aamp.Name(have[s]))
            d_posts = dcf.lists.get(_name("CFPosts"))
            if d_posts is None:
                continue
            b_posts = bcf.lists.get(_name("CFPosts"))
            if b_posts is None:
                bcf.lists[_name("CFPosts")] = d_posts
                continue
            have_posts = {}
            for k, o in b_posts.objects.items():
                nm = o.params.get(_name("Name"))
                if nm is not None:
                    have_posts[_str(nm)] = k.hash
            for o in d_posts.objects.values():
                nm = _str(o.params.get(_name("Name"))) if o.params.get(_name("Name")) is not None else None
                if nm in have_posts:
                    b_posts.objects[oead.aamp.Name(have_posts[nm])] = o
                else:
                    k = _next_key(b_posts.objects, "CFPost")
                    b_posts.objects[k] = o
                    have_posts[nm] = k.hash


# ----------------------------------------------------------- SARC / file tree
def _is_sarc(data: bytes) -> bool:
    return data[:4] == b"SARC"


def _edit_nested(data: bytes, edits: dict, kind: str) -> bytes:
    """edits: {inner path tuple: diff}.  `data` may be Yaz0-wrapped SARC
    (when the path has further segments) or a leaf AAMP file."""
    wrapped = data[:4] == b"Yaz0"
    raw = bytes(oead.yaz0.decompress(data)) if wrapped else data

    # Leaf: an AAMP file.
    leaf = edits.get(())
    if leaf is not None:
        pio = oead.aamp.ParameterIO.from_binary(raw)
        root = pio  # ParameterIO itself is the root list
        if kind == "aslist":
            merge_aslist(root, leaf)
        elif kind == "shop":
            import korok_mergers
            pio = korok_mergers.merge_shopdata(pio, leaf)
        else:
            overlay(root, leaf)
        raw = bytes(pio.to_binary())
    else:
        if not _is_sarc(raw):
            raise ValueError("expected a SARC archive")
        sarc = oead.Sarc(raw)
        writer = _new_writer(sarc, raw)
        groups = {}
        for chain, diff in edits.items():
            groups.setdefault(chain[0], {})[chain[1:]] = diff
        for first, sub in groups.items():
            f = sarc.get_file(first)
            if f is None:
                low = first.lower().lstrip("/")
                base = low.rsplit("/", 1)[-1]
                names = [x.name for x in sarc.get_files()]
                match = next((n for n in names if n.lower().lstrip("/") == low), None)
                if match is None:
                    similar = [n for n in names if n.lower().rsplit("/", 1)[-1] == base][:3]
                    raise FileNotFoundError(
                        f"'{first}' not found inside archive "
                        f"({len(names)} files, e.g. {names[:4]}, same-name: {similar})")
                f = sarc.get_file(match)
                first = match
            writer.files[first] = _edit_nested(bytes(f.data), sub, kind)
        _, out = writer.write()
        raw = bytes(out)
    return bytes(oead.yaz0.compress(raw)) if wrapped else raw


def _split_target(target: str):
    """'content/A.pack//B.sbactorpack//C.baslist' -> ('content/A.pack', ('B.sbactorpack','C.baslist'))"""
    parts = target.split("//")
    return parts[0], tuple(parts[1:])


# Only these types get their alignment forced. Everything else keeps oead's own
# (long-proven) alignment choices, so unrelated files are written exactly as before.
# DISABLED: forcing alignment made Linkle hang on load (bigger packs than the RSTB
# sizes allow for). Keep empty unless a specific type is proven to need it.
HAVOK_EXTS = set()


def _sarc_alignments(raw: bytes) -> dict:
    """{file extension: smallest data alignment actually used by files of that type}.
    oead's SarcWriter re-guesses alignment when it rewrites an archive and gets some
    types wrong (e.g. Havok cloth .hkcl / .bphyssb need 16+), so we feed the real
    alignments back in."""
    import struct
    out = {}
    try:
        if raw[:4] != b"SARC":
            return out
        e = ">" if raw[6:8] == b"\xfe\xff" else "<"
        doff = struct.unpack_from(e + "I", raw, 0x0C)[0]
        count = struct.unpack_from(e + "H", raw, 0x1A)[0]
        mult = struct.unpack_from(e + "I", raw, 0x1C)[0]
        by_hash = {}
        for i in range(count):
            h, _attr, start, end = struct.unpack_from(e + "IIII", raw, 0x20 + 16 * i)
            by_hash[h] = (start, end)
        for f in oead.Sarc(raw).get_files():
            h = 0
            for ch in f.name.encode("utf-8"):
                h = (h * mult + ch) & 0xFFFFFFFF
            start, end = by_hash.get(h, (0, 0))
            if end <= start or "." not in f.name:
                continue
            a = min((start & -start) if start > 0 else (doff & -doff), 0x2000)
            ext = f.name.rsplit(".", 1)[1]
            if ext not in HAVOK_EXTS:
                continue
            out[ext] = min(out.get(ext, a), a)
    except Exception:
        return {}
    return out


def _new_writer(sarc, *raws):
    """SarcWriter seeded from `sarc`, keeping the alignments seen in the given raw archives."""
    w = oead.SarcWriter.from_sarc(sarc)
    req = {}
    for raw in raws:
        for ext, a in _sarc_alignments(raw).items():
            req[ext] = max(req.get(ext, 1), a)
    for ext, a in req.items():
        if a > 4:
            w.add_alignment_requirement(ext, a)
    return w


def _looks_sarc(data: bytes) -> bool:
    if data[:4] == b"Yaz0":
        try:
            return bytes(oead.yaz0.decompress(data))[:4] == b"SARC"
        except Exception:
            return False
    return data[:4] == b"SARC"


def merge_sarc_bytes(vanilla: bytes, shipped: bytes) -> bytes:
    """Vanilla archive + the files a BNP shipped on top (shipped wins).
    Nested archives present in both are merged recursively."""
    v_wrapped = vanilla[:4] == b"Yaz0"
    vraw = bytes(oead.yaz0.decompress(vanilla)) if v_wrapped else vanilla
    sraw = bytes(oead.yaz0.decompress(shipped)) if shipped[:4] == b"Yaz0" else shipped
    if vraw[:4] != b"SARC" or sraw[:4] != b"SARC":
        return shipped
    vs, ss = oead.Sarc(vraw), oead.Sarc(sraw)
    writer = _new_writer(vs, vraw, sraw)
    for f in ss.get_files():
        data = bytes(f.data)
        vf = vs.get_file(f.name)
        if vf is not None:
            vd = bytes(vf.data)
            if _looks_sarc(vd) and _looks_sarc(data):
                data = merge_sarc_bytes(vd, data)
        writer.files[f.name] = data
    out = bytes(writer.write()[1])
    return bytes(oead.yaz0.compress(out)) if v_wrapped else out


def _merge_worker(conn, vanilla_path, shipped_path, out):
    try:
        data = merge_sarc_bytes(pathlib.Path(vanilla_path).read_bytes(),
                                pathlib.Path(shipped_path).read_bytes())
        outp = pathlib.Path(out)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_bytes(data)
        conn.send(("ok", 1, len(data)))
    except Exception as e:
        conn.send(("err", str(e), 0))
    finally:
        conn.close()


def _run_proc(target, args, timeout, label_src):
    import multiprocessing as mp
    ctx = mp.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=target, args=(child,) + tuple(args))
    proc.start()
    child.close()
    try:
        if parent.poll(timeout):
            return parent.recv()
        return ("err", f"TIMEOUT after {timeout}s", 0)
    except EOFError:
        return ("err", "worker process crashed", 0)
    finally:
        if proc.is_alive():
            proc.terminate()
        proc.join(5)


# ------------------------------------------------------------------ texts.json
def _run_msyt(exe, args):
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    r = subprocess.run([str(exe)] + args, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, creationflags=flags, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"msyt failed: {(r.stdout + r.stderr).strip()[:300]}")


def apply_texts_to_pack(texts: dict, src_pack: pathlib.Path, out_pack: pathlib.Path,
                        target_lang: str) -> int:
    """texts: {'DemoMsg/X.msyt': {label: entry}}.  Returns number of files changed."""
    import yaml
    exe = msyt_path()
    raw = src_pack.read_bytes()
    if raw[:4] == b"Yaz0":
        raw = bytes(oead.yaz0.decompress(raw))
    pack = oead.Sarc(raw)
    msg_name = next((f.name for f in pack.get_files()
                     if f.name.lower().endswith(f"msg_{target_lang.lower()}.product.ssarc")), None)
    if msg_name is None:
        raise FileNotFoundError(f"no Msg_{target_lang} archive in {src_pack.name}")
    msg_raw = bytes(pack.get_file(msg_name).data)
    msg_wrapped = msg_raw[:4] == b"Yaz0"
    msg = oead.Sarc(bytes(oead.yaz0.decompress(msg_raw)) if msg_wrapped else msg_raw)
    inner_names = {f.name for f in msg.get_files()}
    by_lower = {n.lower(): n for n in inner_names}

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="korok_texts_"))
    try:
        in_dir, yml_dir, new_dir, out_dir = (tmp / d for d in ("in", "yml", "new", "out"))
        for d in (in_dir, yml_dir, new_dir, out_dir):
            d.mkdir()
        existing, brand_new = [], []
        for msyt_rel in texts:
            msbt_rel = msyt_rel[:-5] + ".msbt" if msyt_rel.endswith(".msyt") else msyt_rel
            real = by_lower.get(msbt_rel.lower())
            if real is not None:
                dest = in_dir / real
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(bytes(msg.get_file(real).data))
                existing.append((msyt_rel, real))
            else:
                brand_new.append((msyt_rel, msbt_rel))

        if existing:
            _run_msyt(exe, ["export", "-d", "-o", str(yml_dir), str(in_dir)])
        for msyt_rel, real in existing:
            yml = yml_dir / (real[:-5] + ".msyt")
            doc = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
            entries = doc.setdefault("entries", {})
            for label, entry in texts[msyt_rel].items():
                entries[label] = entry
            target = new_dir / (real[:-5] + ".msyt")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False,
                                             default_flow_style=False), encoding="utf-8")
        for msyt_rel, msbt_rel in brand_new:
            doc = {"group_count": 101, "entries": texts[msyt_rel]}
            target = new_dir / (msbt_rel[:-5] + ".msyt")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False,
                                             default_flow_style=False), encoding="utf-8")
        _run_msyt(exe, ["create", "-d", "-B", "-p", "wiiu", "-o", str(out_dir), str(new_dir)])

        writer = _new_writer(msg, bytes(oead.yaz0.decompress(msg_raw)) if msg_wrapped else msg_raw)
        changed = 0
        for msyt_rel, real in existing:
            writer.files[real] = (out_dir / real).read_bytes()
            changed += 1
        for msyt_rel, msbt_rel in brand_new:
            writer.files[msbt_rel] = (out_dir / msbt_rel).read_bytes()
            changed += 1
        new_msg = bytes(writer.write()[1])
        if msg_wrapped:
            new_msg = bytes(oead.yaz0.compress(new_msg))
        pw = _new_writer(pack, raw)
        pw.files[msg_name] = new_msg
        out_pack.parent.mkdir(parents=True, exist_ok=True)
        out_pack.write_bytes(bytes(pw.write()[1]))
        return changed
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _texts_worker(conn, json_path, src_lang, src_pack, out_pack, target_lang):
    try:
        data = json.loads(pathlib.Path(json_path).read_text(encoding="utf-8"))
        n = apply_texts_to_pack(data[src_lang], pathlib.Path(src_pack),
                                pathlib.Path(out_pack), target_lang)
        conn.send(("ok", n, 0))
    except Exception as e:
        conn.send(("err", str(e), 0))
    finally:
        conn.close()


def apply_texts(text_logs, pack_dir: pathlib.Path, vanilla_roots, progress=None):
    """text_logs: [texts.json paths].  Applies each to every Bootup_<lang>.pack of
    the same language (EUen text also goes into USen/GBen/... English packs).
    Returns (edited_langs, problems)."""
    problems, langs_done = [], []
    if msyt_path() is None:
        return langs_done, ["texts.json skipped: install with  pip install pymsyt"]
    # which Bootup packs exist in the game
    packs = {}
    for root in vanilla_roots.get("content", []):
        d = pathlib.Path(root) / "Pack"
        if d.is_dir():
            for f in d.glob("Bootup_*.pack"):
                packs.setdefault(f.stem[len("Bootup_"):], f)
    for tj in text_logs:
        try:
            data = json.loads(tj.read_text(encoding="utf-8"))
        except Exception as e:
            problems.append(f"{tj.parent.parent.name}: texts.json unreadable ({e})")
            continue
        for src_lang in data:
            targets = [l for l in packs if l == src_lang or l[-2:] == src_lang[-2:]]
            for i, lang in enumerate(sorted(targets)):
                rel = pathlib.Path("content") / "Pack" / f"Bootup_{lang}.pack"
                out_path = pack_dir / rel
                src = out_path if out_path.exists() else packs[lang]
                if progress:
                    progress(i / max(len(targets), 1), f"texts ({src_lang} -> {lang})")
                t0 = time.time()
                key = _cache_key("texts", file_hash(tj), src_lang, lang, file_hash(src))
                if _cache_get(key, out_path):
                    if lang not in langs_done:
                        langs_done.append(lang)
                    _log(f"texts {src_lang}->{lang}: cached")
                    continue
                tmp = out_path.with_suffix(".korok_tmp")
                status, info, _ = _run_proc(
                    _texts_worker, (str(tj), src_lang, str(src), str(tmp), lang), 600, src)
                if status == "ok":
                    os.replace(tmp, out_path)
                    _cache_put(key, out_path)
                    if lang not in langs_done:
                        langs_done.append(lang)
                    _log(f"texts {src_lang}->{lang}: {info} message files ok {time.time() - t0:.1f}s")
                else:
                    problems.append(f"texts {src_lang}->{lang}: {info}")
                    _log(f"texts {src_lang}->{lang}: FAILED {info}")
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
    return langs_done, problems


# ---------------------------------------------------------------- eventinfo.yml
EVENTINFO_PACK = "content/Pack/Bootup.pack"
EVENTINFO_INNER = "Event/EventInfo.product.sbyml"


def _to_byml(v):
    """Plain YAML value -> oead BYML value (floats must be F32, ints S32)."""
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, float):
        return oead.F32(v)
    if isinstance(v, int):
        return oead.S32(v) if -2**31 <= v < 2**31 else oead.U32(v)
    if isinstance(v, dict):
        h = oead.byml.Hash()
        for k, x in v.items():
            h[str(k)] = _to_byml(x)
        return h
    if isinstance(v, list):
        a = oead.byml.Array()
        for x in v:
            a.append(_to_byml(x))
        return a
    return v


def _is_map(x):
    return isinstance(x, (dict, oead.byml.Hash))


def _deep_update(base, diff):
    for k, v in diff.items():
        cur = base.get(k) if hasattr(base, "get") else None
        if _is_map(v) and _is_map(cur):
            _deep_update(cur, v)
        else:
            base[k] = v


def apply_eventinfo_to_pack(diff: dict, src_pack: pathlib.Path, out_pack: pathlib.Path) -> int:
    raw = src_pack.read_bytes()
    if raw[:4] == b"Yaz0":
        raw = bytes(oead.yaz0.decompress(raw))
    pack = oead.Sarc(raw)
    f = pack.get_file(INNER := EVENTINFO_INNER)
    if f is None:
        raise FileNotFoundError(f"{INNER} not found in {src_pack.name}")
    data = bytes(f.data)
    wrapped = data[:4] == b"Yaz0"
    byml = oead.byml.from_binary(bytes(oead.yaz0.decompress(data)) if wrapped else data)
    for key, value in diff.items():
        value = _to_byml(value)
        cur = byml.get(key)
        if _is_map(value) and _is_map(cur):
            _deep_update(cur, value)
        else:
            byml[key] = value
    new = bytes(oead.byml.to_binary(byml, big_endian=True))
    if wrapped:
        new = bytes(oead.yaz0.compress(new))
    pw = _new_writer(pack, raw)
    pw.files[INNER] = new
    out_pack.parent.mkdir(parents=True, exist_ok=True)
    out_pack.write_bytes(bytes(pw.write()[1]))
    return len(diff)


def _eventinfo_worker(conn, yml_paths, src_pack, out_pack):
    try:
        import yaml
        merged = {}
        for yp in yml_paths:
            d = yaml.safe_load(pathlib.Path(yp).read_text(encoding="utf-8")) or {}
            for k, v in d.items():
                if isinstance(v, dict) and isinstance(merged.get(k), dict):
                    _deep_update(merged[k], v)
                else:
                    merged[k] = v
        n = apply_eventinfo_to_pack(merged, pathlib.Path(src_pack), pathlib.Path(out_pack))
        conn.send(("ok", n, 0))
    except Exception as e:
        conn.send(("err", str(e), 0))
    finally:
        conn.close()


def apply_eventinfo(yml_paths, pack_dir: pathlib.Path, vanilla_roots):
    """Returns (entries_applied, problems)."""
    if not yml_paths:
        return 0, []
    out_path = pack_dir / EVENTINFO_PACK
    src = out_path if out_path.exists() else _find_vanilla(EVENTINFO_PACK, vanilla_roots)
    if src is None:
        return 0, ["eventinfo: vanilla Bootup.pack not found"]
    tmp = out_path.with_suffix(".korok_tmp")
    t0 = time.time()
    key = _cache_key("event", *[file_hash(y) for y in yml_paths], file_hash(src))
    if _cache_get(key, out_path):
        _log("eventinfo: cached")
        try:
            import yaml
            n = len({k for y in yml_paths
                     for k in (yaml.safe_load(pathlib.Path(y).read_text(encoding="utf-8")) or {})})
        except Exception:
            n = 1
        return n, []
    status, info, _ = _run_proc(
        _eventinfo_worker, ([str(y) for y in yml_paths], str(src), str(tmp)), 300, src)
    if status == "ok":
        os.replace(tmp, out_path)
        _cache_put(key, out_path)
        _log(f"eventinfo: {info} entries applied {time.time() - t0:.1f}s")
        return info, []
    _log(f"eventinfo: FAILED {info}")
    try:
        tmp.unlink()
    except OSError:
        pass
    return 0, [f"eventinfo: {info}"]


def _merge_rel(rel, mod_dirs, pack_dir, vanilla_roots):
    """Builds pack_dir/rel as vanilla + each mod's shipped files in order."""
    problems, done = [], 0
    out_path = pack_dir / rel
    merged_once = False
    for mod_dir in mod_dirs:
        shipped = mod_dir / rel
        if not shipped.exists():
            continue
        base = out_path if merged_once else _find_vanilla(rel, vanilla_roots)
        if base is None:
            _log(f"pack {rel}: no vanilla copy, using shipped file as is")
            if not merged_once:
                _atomic_copy(shipped, out_path)
                merged_once = True
            continue
        t0 = time.time()
        key = _cache_key("pack", file_hash(base), file_hash(shipped))
        if _cache_get(key, out_path):
            merged_once = True
            done += 1
            _log(f"pack {rel}: cached")
            continue
        tmp = out_path.with_name(out_path.name + ".korok_tmp")
        status, info, size = _run_proc(
            _merge_worker, (str(base), str(shipped), str(tmp)), FILE_TIMEOUT * 3, base)
        if status == "ok":
            os.replace(tmp, out_path)
            _cache_put(key, out_path)
            merged_once = True
            done += 1
            _log(f"pack {rel}: vanilla+shipped merged ok {time.time() - t0:.2f}s {size} bytes")
        else:
            problems.append(f"{rel}: pack merge failed ({info})")
            _log(f"pack {rel}: FAILED {info}")
            try:
                tmp.unlink()
            except OSError:
                pass
    return done, problems


def merge_partial_packs(mod_packs, pack_dir: pathlib.Path, vanilla_roots, progress=None):
    """mod_packs: [(mod_dir, [relative pack paths like 'content/Pack/TitleBG.pack'])].
    For each path, rebuilds pack_dir/<path> as vanilla (or the pack from an
    earlier mod) + that mod's shipped files.  Different paths run in parallel.
    Returns (count, problems)."""
    if oead is None:
        return 0, []
    by_rel = {}
    for mod_dir, rels in mod_packs:
        for rel in rels:
            by_rel.setdefault(rel.replace("\\", "/"), []).append(mod_dir)
    problems, done_total, finished = [], 0, 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=_worker_count()) as pool:
        futures = {pool.submit(_merge_rel, rel, mods, pack_dir, vanilla_roots): rel
                   for rel, mods in by_rel.items()}
        for fut in concurrent.futures.as_completed(futures):
            d, pr = fut.result()
            done_total += d
            problems += pr
            finished += 1
            if progress:
                progress(finished / max(len(futures), 1),
                         f"merging pack {finished}/{len(futures)}: {futures[fut]}")
    return done_total, problems


def _worker(conn, logpath, kind, top, src, out, dumpfile=None):
    try:
        if dumpfile:
            import faulthandler
            _fh = open(dumpfile, "a", encoding="utf-8")
            _fh.write(f"--- stack dump if still running after 12s: {top}\n")
            _fh.flush()
            faulthandler.dump_traceback_later(12, file=_fh)
        diffs = parse_diff(_load_log(pathlib.Path(logpath)))
        edits = {}
        for target, diff in diffs.items():
            t, chain = _split_target(target)
            if t == top:
                edits[chain] = diff
        data = _edit_nested(pathlib.Path(src).read_bytes(), edits, kind)
        outp = pathlib.Path(out)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_bytes(data)
        conn.send(("ok", len(edits), len(data)))
    except Exception as e:
        conn.send(("err", str(e), 0))
    finally:
        conn.close()


FILE_TIMEOUT = 60  # seconds per game file before it is skipped


def _run_isolated(logpath, kind, top, src, out):
    """Runs one file edit in a child process so a hang/crash in oead can't
    freeze Korok.  Writes via a temp file so hard-linked originals are never
    modified in place.  Returns (status, info, size)."""
    import multiprocessing as mp
    out = pathlib.Path(out)
    tmp = out.with_name(out.name + ".korok_tmp")
    ctx = mp.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_worker, args=(child, str(logpath), kind, top, str(src), str(tmp), str(LOG_FILE) if LOG_FILE else None))
    proc.start()
    child.close()
    try:
        if parent.poll(FILE_TIMEOUT):
            res = parent.recv()
        else:
            res = ("err", f"TIMEOUT after {FILE_TIMEOUT}s (source {pathlib.Path(src).stat().st_size} bytes)", 0)
    except EOFError:
        res = ("err", "worker process crashed", 0)
    finally:
        if proc.is_alive():
            proc.terminate()
        proc.join(5)
    if res[0] == "ok" and tmp.exists():
        os.replace(tmp, out)
    else:
        try:
            tmp.unlink()
        except OSError:
            pass
    return res


def _edit_one(logname, kind, lp, top, out_path, vanilla_roots):
    """One game file: cache lookup, else isolated edit.  Thread-safe (each
    call touches a different `top`).  Returns (top, status, info, size, seconds, src)."""
    src = out_path if out_path.exists() else _find_vanilla(top, vanilla_roots)
    if src is None:
        return (top, "novanilla", "vanilla file not found, skipped", 0, 0.0, None)
    t0 = time.time()
    key = _cache_key("edit", kind, file_hash(lp), top, file_hash(src))
    if _cache_get(key, out_path):
        return (top, "cached", 0, out_path.stat().st_size, time.time() - t0, src)
    status, info, size = _run_isolated(lp, kind, top, src, out_path)
    if status == "ok":
        _cache_put(key, out_path)
    return (top, status, info, size, time.time() - t0, src)


def apply_logs(log_dirs, pack_dir: pathlib.Path, vanilla_roots, progress=None):
    """Apply aslist/deepmerge logs of every mod in `log_dirs` (in order).

    pack_dir: the output graphic pack (files already there win over vanilla).
    vanilla_roots: dict {'content': [dirs to search], 'aoc': [...]}.
    Returns (applied_count, [problem strings])."""
    if oead is None:
        return 0, ["oead is not installed (pip install oead); merge logs were skipped."]
    problems = []
    applied = 0
    for logs in log_dirs:
        for logname, kind in (("aslist.aamp", "aslist"), ("deepmerge.aamp", "deepmerge"), ("shop.aamp", "shop")):
            lp = logs / logname
            if not lp.exists():
                continue
            try:
                diffs = parse_diff(_load_log(lp))
            except Exception as e:
                problems.append(f"{logname}: could not read ({e})")
                continue
            tops = []
            for target in diffs:
                t = _split_target(target)[0]
                if t not in tops:
                    tops.append(t)
            total = len(tops)
            done = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=_worker_count()) as pool:
                futures = [pool.submit(_edit_one, logname, kind, lp, top, pack_dir / top, vanilla_roots)
                           for top in tops]
                for fut in concurrent.futures.as_completed(futures):
                    top, status, info, size, secs, src = fut.result()
                    done += 1
                    if progress:
                        progress(done / max(total, 1), f"{logname} {done}/{total}: {top}")
                    if status == "novanilla":
                        problems.append(f"{top}: {info}")
                        _log(f"{logname} {top} no vanilla source")
                    elif status in ("ok", "cached"):
                        n_edits = sum(1 for t in diffs if _split_target(t)[0] == top)
                        applied += n_edits
                        _log(f"{logname} {top} {'cached' if status == 'cached' else 'ok'} {secs:.2f}s {size} bytes")
                    else:
                        problems.append(f"{top}: {info}")
                        _log(f"{logname} {top} FAILED {info}")
    return applied, problems


def _find_vanilla(top: str, vanilla_roots):
    head, _, rest = top.partition("/")
    for root in vanilla_roots.get(head, []):
        p = pathlib.Path(root) / rest
        if p.exists():
            return p
    return None


# ------------------------------------------------------- archives several mods change
SARC_EXTS = {".pack", ".sarc", ".ssarc", ".sbactorpack", ".bactorpack", ".sbmodelsh", ".bmodelsh",
             ".sbeventpack", ".beventpack", ".sstera", ".stera", ".sblarc", ".blarc", ".sstats",
             ".stats", ".sbgenv", ".bgenv"}


def _unyaz(b: bytes) -> bytes:
    return bytes(oead.yaz0.decompress(b)) if b[:4] == b"Yaz0" else bytes(b)


def merge_sarc_versions(vanilla, versions: list, depth: int = 0, report: list = None) -> bytes:
    """vanilla: the game's archive (bytes, or None if the game has none); versions: the
    same archive from each mod, in load order (full copies or BNP-style partial ones).
    Each mod contributes the files it changed from vanilla; when several change the same
    file, nested archives are merged the same way and otherwise the last mod wins.
    Compression follows the vanilla file (else the first mod's)."""
    ref = vanilla if vanilla is not None else versions[0]
    wrapped = ref[:4] == b"Yaz0"
    vraw = _unyaz(vanilla) if vanilla is not None else None
    vs = oead.Sarc(vraw) if vraw is not None else None
    base = {f.name: bytes(f.data) for f in vs.get_files()} if vs is not None else {}
    changed = {}                                # name -> [changed versions, in order]
    raws = [vraw] if vraw is not None else []
    for v in versions:
        raw = _unyaz(v)
        raws.append(raw)
        for f in oead.Sarc(raw).get_files():
            d = bytes(f.data)
            if base.get(f.name) == d:
                continue                        # this mod didn't change it
            changed.setdefault(f.name, []).append(d)
    result = dict(base)
    if report is not None:
        owners = {}
        for i, v in enumerate(versions):
            for f in oead.Sarc(_unyaz(v)).get_files():
                if base.get(f.name) != bytes(f.data):
                    owners.setdefault(f.name, []).append(i)
        for name in sorted(owners):
            report.append((name, owners[name], name not in base))
    for name, vers in changed.items():
        ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext in SARC_EXTS and depth < 4 and all(_looks_sarc(x) for x in vers) and \
                (name not in base or _looks_sarc(base[name])):
            try:
                result[name] = merge_sarc_versions(base.get(name), vers, depth + 1)
                continue
            except Exception as e:
                _log(f"merge {name}: nested merge failed ({e}); last mod's copy used")
        result[name] = vers[-1]
    first = oead.Sarc(raws[0])
    writer = _new_writer(first, *raws)
    for name in [n for n in list(writer.files.keys()) if n not in result]:
        del writer.files[name]
    for name, d in result.items():
        writer.files[name] = d
    out = bytes(writer.write()[1])
    return bytes(oead.yaz0.compress(out)) if wrapped else out


def _merge_versions_worker(conn, vanilla_path, version_paths, out):
    try:
        vanilla = pathlib.Path(vanilla_path).read_bytes() if vanilla_path else None
        report = []
        data = merge_sarc_versions(vanilla, [pathlib.Path(p).read_bytes() for p in version_paths],
                                   report=report)
        outp = pathlib.Path(out)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_bytes(data)
        conn.send(("ok", report, len(data)))
    except Exception as e:
        conn.send(("err", f"{type(e).__name__}: {e}", 0))
    finally:
        conn.close()


def _vanilla_for_staged(rel: str, vanilla_roots):
    """Staged paths are content/... and aoc/0010/...; the vanilla aoc roots already end in 0010."""
    if rel.startswith("aoc/0010/"):
        rel = "aoc/" + rel[len("aoc/0010/"):]
    return _find_vanilla(rel, vanilla_roots)


def merge_mod_archives(jobs: dict, pack_dir: pathlib.Path, vanilla_roots, progress=None, labels=None):
    """jobs: {staged relative path: [that file in each mod, in load order]}. Writes the
    merged archive to pack_dir/<path>. labels: {rel: [mod name per version]} for the log.
    Returns (count, problems)."""
    if oead is None:
        return 0, ["oead is not installed (pip install oead); archives several mods change "
                   "were not merged"]
    problems, done = [], 0

    def one(rel, paths):
        out_path = pack_dir / rel
        van = _vanilla_for_staged(rel, vanilla_roots)
        key = _cache_key("archives", rel, file_hash(van) if van else "-", *[file_hash(p) for p in paths])
        if _cache_get(key, out_path):
            _log(f"archive {rel}: cached")
            return True, None
        tmp = out_path.with_name(out_path.name + ".korok_tmp")
        status, info, size = _run_proc(_merge_versions_worker,
                                       (str(van) if van else "", [str(p) for p in paths], str(tmp)),
                                       FILE_TIMEOUT * 6, None)
        if status == "ok":
            if out_path.exists():
                out_path.unlink()                # may be a hard link into a staged mod
            os.replace(tmp, out_path)
            _cache_put(key, out_path)
            names = (labels or {}).get(rel) or [f"#{i + 1}" for i in range(len(paths))]
            lines = [f"archive {rel}: merged {len(paths)} version(s) ({', '.join(names)}) {size} bytes"]
            for name, owners, new_file in info:
                who = [names[i] for i in owners]
                flag = " (new)" if new_file else ""
                is_arc = ("." + name.rsplit(".", 1)[-1].lower()) in SARC_EXTS
                clash = (f"   <- changed by {len(who)} mods, " + ("merged inside" if is_arc else "LAST WINS")
                         if len(who) > 1 else "")
                lines.append(f"    {name}{flag}: {' > '.join(who)}{clash}")
            _log("\n".join(lines))
            return True, None
        try:
            tmp.unlink()
        except OSError:
            pass
        _log(f"archive {rel}: FAILED {info}")
        return False, f"{rel}: {info}"

    finished = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=_worker_count()) as pool:
        futures = {pool.submit(one, rel, paths): rel for rel, paths in jobs.items()}
        for fut in concurrent.futures.as_completed(futures):
            ok, err = fut.result()
            done += ok
            if err:
                problems.append(err)
            finished += 1
            if progress:
                progress(finished / max(len(futures), 1), f"merging {futures[fut]}")
    return done, problems
