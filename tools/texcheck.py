"""Compares one model's texture files: the game's own copy, the mod's copy (from a
.bnp/.zip/folder), and what korok put in the korok Mods pack.

Usage (from the korok folder):
    python tools/texcheck.py "C:\\path\\to\\ZeldasBallad.bnp" Armor_046
"""
import json
import pathlib
import shutil
import struct
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import korok_engine  # noqa: E402
import korok_rstb  # noqa: E402


def head(data):
    if data is None:
        return "missing"
    if data[:4] == b"Yaz0":
        size, align = struct.unpack(">II", data[4:12])
        return f"Yaz0 size={size} align={align:#x} file={len(data)}"
    return f"raw {data[:4]!r} size={len(data)}"


def main(mod, model):
    e = korok_engine.KorokEngine()
    dump = e.dump()
    pack = e.graphic_packs_dir
    table = None
    p = pack / "content" / korok_rstb.RSTB_PATH
    if p.exists():
        table = korok_rstb.SizeTable(p.read_bytes())
    vanilla_table = korok_rstb.load_vanilla(dump, dump.dirs[1])
    tmp = pathlib.Path(tempfile.mkdtemp())
    try:
        src = pathlib.Path(mod)
        if src.is_file():
            e._extract_archive(src, tmp)
            src = tmp
        rstb_log = {}
        for j in src.rglob("rstb.json"):
            try:
                rstb_log.update(json.loads(j.read_text(encoding="utf-8")))
            except Exception:
                pass
        for suffix in ("sbfres", "Tex1.sbfres", "Tex2.sbfres"):
            name = f"Model/{model}.{suffix}"
            canon = name.replace(".s", ".")
            print(f"== {name}")
            for which, dirs in (("update", dump.dirs[1]), ("base", dump.dirs[2])):
                for d in dirs:
                    f = d / name
                    if f.exists():
                        print(f"  game ({which}): {head(f.read_bytes())}")
            hits = [h for h in src.rglob(f"{model}.{suffix}")]
            for h in hits:
                print(f"  mod  ({h.relative_to(src)}): {head(h.read_bytes())}")
            k = pack / "content" / name
            print(f"  korok pack: {head(k.read_bytes()) if k.exists() else 'missing'}")
            print(f"  size table: game {vanilla_table.get(canon) if vanilla_table else '?'}, "
                  f"mod's rstb.json {rstb_log.get(canon, rstb_log.get(name, '-'))}, "
                  f"korok pack {table.get(canon) if table else '?'}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main(*sys.argv[1:3])
