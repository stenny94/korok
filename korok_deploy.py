"""Builds Korok's graphic pack from the enabled mods: UKMM's deploy.

Every mod in Korok's library is a UKMM package (see korok_library). To deploy,
each file any enabled mod lists is rebuilt from vanilla with every mod's
version folded in, in load order (ModUnpacker::build_file), then written to
the graphic pack's content/ or aoc/0010/. Afterwards the game's resource size
table gets an entry for every rebuilt file (Deployer::apply_rstb).

Port of UKMM (https://github.com/NiceneNerd/ukmm, GPL-3.0) by Nicene Nerd.
"""
import pathlib

import oead

import korok_rstb
from korok_ukmm_native import Converter, Dump, Package, unyaz


class MultiPackage:
    """Several packages read as one: ModReader::get_versions over all mods in order."""

    def __init__(self, packages):
        self.packages = list(packages)
        content, aoc = [], []
        for p in self.packages:
            content += p.content_files
            aoc += p.aoc_files
        self.content_files = sorted(set(content))
        self.aoc_files = sorted(set(aoc))

    def versions(self, canon):
        out = []
        for p in self.packages:
            out += p.versions(canon)
        return out

    def has(self, canon):
        return any(p.has(canon) for p in self.packages)

    def close(self):
        for p in self.packages:
            p.close()


def deploy(mods, dump: Dump, out_dir, progress=None, log=None):
    """mods: [(package path, [option paths]), ...] in load order (last wins).
    Writes out_dir/content and out_dir/aoc/0010. Returns the Converter
    (.built, .unsupported, .problems, .rstb)."""
    if not (dump.dirs[1] or dump.dirs[2]):
        raise FileNotFoundError("the game's files weren't found in Cemu's mlc01 folder "
                                "(base game and v208 update must be installed there)")
    out_dir = pathlib.Path(out_dir)
    packages = [Package(path, options) for path, options in mods]
    multi = MultiPackage(packages)
    try:
        conv = Converter(multi, dump, log=log).convert(out_dir, progress=progress)
        # text packs are written by build_text_packs; size them like unpack_texts does
        for boot in (out_dir / "content" / "Pack").glob("Bootup_*.pack"):
            lang = boot.stem[len("Bootup_"):]
            if len(lang) != 4:
                continue
            try:
                s = oead.Sarc(boot.read_bytes())
                msg = next((bytes(f.data) for f in s.get_files()
                            if f.name == f"Message/Msg_{lang}.product.ssarc"), None)
            except Exception:
                msg = None
            if msg is not None:
                conv.rstb[f"Message/Msg_{lang}.product.sarc"] = korok_rstb.calc_from_size(len(unyaz(msg)), "Msg.sarc")
        table = korok_rstb.load_vanilla(dump, dump.dirs[1])
        if table is not None and conv.rstb:
            table.apply(conv.rstb)
            korok_rstb.write(table, out_dir / "content")
        elif table is None and conv.rstb:
            conv.problems.append("The game's resource size table wasn't found, so it wasn't updated.")
        return conv
    finally:
        multi.close()
