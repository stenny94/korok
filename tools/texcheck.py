"""Compares a model's files (model, Tex1, Tex2) between the game, a mod (.bnp/.zip/folder)
and the korok Mods pack: where each copy lives (loose or inside a pack), its Yaz0 header,
whether the decompressed data is identical, and the size-table values.

Usage (from the korok folder):
    python tools/texcheck.py "C:\\path\\to\\mod.bnp" Armor_Default Link Armor_046
"""
import hashlib
import json
import pathlib
import shutil
import struct
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import oead  # noqa: E402

import korok_engine  # noqa: E402
import korok_rstb  # noqa: E402
from korok_ukmm_native import _filemap, unyaz  # noqa: E402

PACKS = ("Pack/TitleBG.pack", "Pack/Bootup.pack")


def describe(data, where, offset=None):
    if data is None:
        return f"{where}: missing"
    raw = unyaz(data)
    h = hashlib.sha1(raw).hexdigest()[:12]
    out = f"{where}: "
    if data[:4] == b"Yaz0":
        out += f"Yaz0 align={struct.unpack('>I', data[8:12])[0]:#x} "
    else:
        out += "uncompressed "
    out += f"size={len(raw)} sha1={h}"
    if offset is not None:
        out += f" (in pack at {offset:#x}, %0x2000={offset % 0x2000:#x})"
    return out


def from_pack(pack_bytes, name):
    if pack_bytes is None:
        return None, None
    raw = unyaz(pack_bytes)
    s = oead.Sarc(raw)
    start = s.get_data_offset()
    for f in s.get_files():
        if f.name == name:
            data = bytes(f.data)
            return data, start + raw[start:].find(data) if data else None
    return None, None


def find(root: pathlib.Path, name: str):
    """(data, label, offset) for a file loose under content/ or inside one of the packs."""
    loose = root / "content" / name
    if loose.is_file():
        return loose.read_bytes(), "loose", None
    for pack in PACKS:
        p = root / "content" / pack
        if p.is_file():
            data, off = from_pack(p.read_bytes(), name)
            if data is not None:
                return data, f"in {pack}", off
    return None, "missing", None


def main(mod, *models):
    e = korok_engine.KorokEngine()
    dump = e.dump()
    pack = e.graphic_packs_dir
    tp = pack / "content" / korok_rstb.RSTB_PATH
    table = korok_rstb.SizeTable(tp.read_bytes()) if tp.exists() else None
    vanilla_table = korok_rstb.load_vanilla(dump, dump.dirs[1])
    tmp = pathlib.Path(tempfile.mkdtemp())
    try:
        src = pathlib.Path(mod)
        if src.is_file():
            e._extract_archive(src, tmp)
            src = tmp
        roots = [p.parent for p in src.rglob("content") if p.is_dir()]
        root = min(roots, key=lambda p: len(p.parts)) if roots else src
        rstb_log = {}
        for j in src.rglob("rstb.json"):
            try:
                rstb_log.update(json.loads(j.read_text(encoding="utf-8")))
            except Exception:
                pass
        for model in models:
            for suffix in ("sbfres", "Tex1.sbfres", "Tex2.sbfres"):
                name = f"Model/{model}.{suffix}"
                canon = name.replace(".s", ".")
                print(f"== {name}   (game keeps it at: {[p for p in _filemap().get(canon, []) if p]})")
                game_path = next((p for p in _filemap().get(canon, []) if p), None)
                if game_path and "//" in game_path:
                    pk, inner = game_path.split("//", 1)
                    gd, goff = from_pack(dump.get_bytes(pk), inner)
                    print("  " + describe(gd, f"game  in {pk}", goff))
                else:
                    for d in dump.dirs[1] + dump.dirs[2]:
                        if (d / name).is_file():
                            print("  " + describe((d / name).read_bytes(), "game  loose"))
                            break
                d, label, off = find(root, name)
                print("  " + describe(d, f"mod   {label}", off))
                d, label, off = find(pack, name)
                print("  " + describe(d, f"korok {label}", off))
                print(f"  size table: game {vanilla_table.get(canon) if vanilla_table else '?'}, "
                      f"mod's rstb.json {rstb_log.get(canon, '-')}, korok {table.get(canon) if table else '?'}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main(*sys.argv[1:])
