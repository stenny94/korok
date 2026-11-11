"""korok instances: each instance = its own save folder + a mod playlist.

Switching copies the live Cemu save folder back into the instance you are
leaving, then puts the target instance's save in place. The live folder is
always backed up first, and nothing is ever deleted without a copy existing.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET

BASE_DIR = pathlib.Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
CONFIG_DIR.mkdir(exist_ok=True)
INSTANCES_FILE = CONFIG_DIR / "korok_instances.json"
_old = BASE_DIR / "korok_instances.json"
if _old.exists() and not INSTANCES_FILE.exists():
    try:
        _old.replace(INSTANCES_FILE)
    except OSError:
        pass
INSTANCES_DIR = BASE_DIR / "korok_instances"
RESHADE_DIR = BASE_DIR / "korok_reshade"          # ReShade presets kept by korok
RESHADE_OFF = "__off__"                            # slot setting: ReShade with no effects
OFF_PRESET = "korok_off.ini"                       # an empty preset (no effects turned on)
BACKUPS_DIR = BASE_DIR / "korok_backups"
KEEP_BACKUPS = 15
TITLE_LOWS = ("101c9400", "101c9500", "101c9300")  # USA, EUR, JPN


class InstanceError(Exception):
    pass


def _copytree_clean(src: pathlib.Path, dst: pathlib.Path):
    if dst.exists():
        shutil.rmtree(dst)
    if src.exists():
        shutil.copytree(src, dst)
    else:
        dst.mkdir(parents=True)


def _tree_files(p: pathlib.Path) -> int:
    return sum(1 for f in p.rglob("*") if f.is_file()) if p.exists() else 0


class InstanceManager:
    def __init__(self, engine):
        self.engine = engine
        self.data = self._load()

    # ------------------------------------------------------------ state --
    def _load(self) -> dict:
        d = {"active": None, "instances": {}, "cemu_exe": "", "game_path": ""}
        if INSTANCES_FILE.exists():
            try:
                d.update(json.loads(INSTANCES_FILE.read_text(encoding="utf-8")))
            except Exception:
                pass
        return d

    def _save(self):
        tmp = INSTANCES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.replace(tmp, INSTANCES_FILE)

    def set_launch(self, cemu_exe: str, game_path: str):
        self.data["cemu_exe"] = cemu_exe.strip()
        self.data["game_path"] = game_path.strip()
        self._save()

    def cemu_exe(self) -> str:
        exe = self.data.get("cemu_exe")
        if exe:
            return exe
        guess = self.engine.cemu_dir / "Cemu.exe"
        return str(guess) if guess.exists() else ""

    def names(self):
        return sorted(self.data["instances"], key=lambda i: self.data["instances"][i]["name"].lower())

    def info(self, iid):
        return self.data["instances"][iid]

    def active(self):
        a = self.data.get("active")
        return a if a in self.data["instances"] else None

    # ------------------------------------------------------- save paths --
    def live_save_dir(self) -> pathlib.Path:
        """The Cemu save folder for the player account (…/user/80000001)."""
        base = self.engine.user_dir / "mlc01" / "usr" / "save" / "00050000"
        for low in TITLE_LOWS:
            user = base / low / "user"
            if user.is_dir():
                accts = [d for d in user.iterdir() if d.is_dir() and d.name != "common"]
                accts.sort(key=lambda d: (not d.name.startswith("8"), d.name))
                if accts:
                    return accts[0]
                return user / "80000001"
        return base / TITLE_LOWS[0] / "user" / "80000001"

    def inst_dir(self, iid) -> pathlib.Path:
        return INSTANCES_DIR / iid / "save"

    def game_data_files(self, root: pathlib.Path):
        """All game_data.sav under a save folder, newest first."""
        if not root.exists():
            return []
        files = [p for p in root.rglob("game_data.sav") if p.is_file()]
        return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)

    # ---------------------------------------------------------- safety ---
    def cemu_running(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq Cemu.exe", "/NH"],
                capture_output=True, text=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            return "cemu.exe" in out.lower()
        except Exception:
            return False

    def _require_cemu_closed(self):
        if self.cemu_running():
            raise InstanceError("Cemu is running. Close it first so the save isn't overwritten.")

    def backup_live(self, label: str) -> pathlib.Path:
        live = self.live_save_dir()
        if not live.exists() or _tree_files(live) == 0:
            return None
        dst = BACKUPS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_{label}"
        n = 1
        while dst.exists():
            n += 1
            dst = BACKUPS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_{label}_{n}"
        shutil.copytree(live, dst)
        self._prune_backups()
        return dst

    def _prune_backups(self):
        """Keep the newest KEEP_BACKUPS save backups; kept (pinned) ones never count or go."""
        if not BACKUPS_DIR.exists():
            return
        pinned = set(self.data.get("pinned_backups", []))
        dirs = sorted([d for d in BACKUPS_DIR.iterdir() if d.is_dir() and d.name not in pinned],
                      key=lambda d: d.name)
        for d in dirs[:-KEEP_BACKUPS]:
            shutil.rmtree(d, ignore_errors=True)

    def list_backups(self):
        if not BACKUPS_DIR.exists():
            return []
        return sorted([d for d in BACKUPS_DIR.iterdir() if d.is_dir()], key=lambda d: d.name, reverse=True)

    def restore_backup(self, backup: pathlib.Path):
        self._require_cemu_closed()
        self.backup_live("before-restore")
        live = self.live_save_dir()
        _copytree_clean(pathlib.Path(backup), live)

    # ---------------------------------------------------------- backups --
    # Two kinds live in korok_backups/: save backups (a folder: the live Cemu save before a
    # switch/restore, named <YYYYmmdd-HHMMSS>_<reason>) and Cemu settings backups
    # (cemu_settings_<YYYYmmdd-HHMMSS>.xml, before korok changes graphic packs).
    def all_backups(self) -> list:
        """[{name, kind ('save'/'settings'), when, reason, size, pinned, path}], newest first."""
        if not BACKUPS_DIR.exists():
            return []
        pinned = set(self.data.get("pinned_backups", []))
        out = []
        for p in BACKUPS_DIR.iterdir():
            if p.is_dir():
                stamp, _, reason = p.name.partition("_")
                kind, size = "save", sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                reason = reason.replace("before-", "before ").replace("-", " ") or "save backup"
            elif p.name.startswith("cemu_settings_") and p.suffix == ".xml":
                stamp = p.stem[len("cemu_settings_"):]
                kind, size, reason = "settings", p.stat().st_size, "Cemu graphic pack settings"
            else:
                continue
            try:
                when = time.strftime("%Y-%m-%d %H:%M:%S", time.strptime(stamp[:15], "%Y%m%d-%H%M%S"))
            except ValueError:
                when = ""
            out.append({"name": p.name, "kind": kind, "when": when, "reason": reason, "size": size,
                        "pinned": p.name in pinned, "path": p})
        return sorted(out, key=lambda b: (b["when"], b["name"]), reverse=True)

    def set_pinned(self, name: str, on: bool):
        pins = [n for n in self.data.get("pinned_backups", []) if n != name]
        if on:
            pins.append(name)
        self.data["pinned_backups"] = pins
        self._save()

    def delete_backup(self, name: str):
        p = BACKUPS_DIR / name
        if p.is_dir():
            shutil.rmtree(p)
        elif p.is_file():
            p.unlink()
        self.set_pinned(name, False)

    def restore_any_backup(self, name: str):
        """Put a save backup back as the live save, or a settings backup back as Cemu's settings.xml."""
        p = BACKUPS_DIR / name
        if p.is_dir():
            self.restore_backup(p)
        elif p.is_file():
            self._require_cemu_closed()
            target = self.cemu_settings_file()
            if target.is_file():
                shutil.copy2(target, BACKUPS_DIR / f"cemu_settings_{time.strftime('%Y%m%d-%H%M%S')}.xml")
            shutil.copy2(p, target)
        else:
            raise InstanceError("That backup no longer exists.")

    # ------------------------------------------------- export / import --
    EXPORT_FORMAT = "korok-backup-1"

    def export_zip(self, dest, backup_names=(), slot_ids=()) -> str:
        """Writes chosen backups and save slots (save + graphic packs + ReShade preset +
        profile name) into one .zip. Returns a short summary."""
        dest = pathlib.Path(dest)
        if slot_ids and self.active() in slot_ids and not self.cemu_running():
            self.sync_back()                     # include the latest progress
        manifest = {"format": self.EXPORT_FORMAT, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "backups": [], "slots": []}
        tmp = dest.with_name(dest.name + ".korok_tmp")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for name in backup_names:
                p = BACKUPS_DIR / name
                if p.is_dir():
                    for f in sorted(p.rglob("*")):
                        if f.is_file():
                            z.write(f, f"backups/{name}/{f.relative_to(p).as_posix()}")
                elif p.is_file():
                    z.write(p, f"backups/{name}")
                else:
                    continue
                manifest["backups"].append({"name": name, "pinned": name in self.data.get("pinned_backups", [])})
            for iid in slot_ids:
                d = self.data["instances"].get(iid)
                if d is None:
                    continue
                src = self.inst_dir(iid)
                if src.exists():
                    for f in sorted(src.rglob("*")):
                        if f.is_file():
                            z.write(f, f"slots/{iid}/save/{f.relative_to(src).as_posix()}")
                rs = d.get("reshade") or ""
                if rs and rs != RESHADE_OFF and (RESHADE_DIR / f"{rs}.ini").is_file():
                    z.write(RESHADE_DIR / f"{rs}.ini", f"reshade/{rs}.ini")
                manifest["slots"].append({"id": iid, **{k: d.get(k) for k in
                                         ("name", "playlist", "notes", "created", "last_played",
                                          "graphic_packs", "reshade")}})
            z.writestr("korok_export.json", json.dumps(manifest, indent=1))
        os.replace(tmp, dest)
        return f"Exported {len(manifest['backups'])} backup(s) and {len(manifest['slots'])} save slot(s)."

    def import_zip(self, src) -> str:
        """Adds the backups and save slots from a korok export. Nothing existing is replaced:
        names that are taken get a suffix. Imported backups are kept (pinned)."""
        with zipfile.ZipFile(src) as z:
            try:
                manifest = json.loads(z.read("korok_export.json"))
            except KeyError:
                raise InstanceError("This isn't a korok backup export (no korok_export.json inside).")
            if manifest.get("format") != self.EXPORT_FORMAT:
                raise InstanceError("This export was made by a different korok version and can't be read.")
            members = [m for m in z.namelist() if not m.endswith("/")]
            for m in members:
                parts = pathlib.PurePosixPath(m).parts
                if m.startswith("/") or ".." in parts or ":" in m:
                    raise InstanceError(f"Unsafe path inside the export: {m}")

            def extract(prefix, dest: pathlib.Path):
                n = 0
                for m in members:
                    if m.startswith(prefix):
                        out = dest / m[len(prefix):]
                        out.parent.mkdir(parents=True, exist_ok=True)
                        out.write_bytes(z.read(m))
                        n += 1
                return n

            BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
            n_b = 0
            for b in manifest.get("backups", []):
                name = pathlib.PurePosixPath(str(b.get("name", ""))).name
                if not name:
                    continue
                target, k = BACKUPS_DIR / name, 1
                while target.exists():
                    k += 1
                    stem, dot, ext = name.rpartition(".") if name.endswith(".xml") else (name, "", "")
                    target = BACKUPS_DIR / (f"{stem}_imported{k}.{ext}" if dot else f"{name}_imported{k}")
                if f"backups/{name}" in members:          # a single file (settings backup)
                    target.write_bytes(z.read(f"backups/{name}"))
                elif not extract(f"backups/{name}/", target):
                    continue
                self.set_pinned(target.name, True)
                n_b += 1
            n_s = 0
            profiles = set(self.engine.profile_names()) if hasattr(self.engine, "profile_names") else set()
            for s in manifest.get("slots", []):
                old_id = str(s.get("id", ""))
                base = str(s.get("name") or "Imported save").strip()
                name, k = base, 1
                while any(i["name"].lower() == name.lower() for i in self.data["instances"].values()):
                    k += 1
                    name = f"{base} (imported{'' if k == 2 else ' ' + str(k - 1)})"
                iid = self._new_id()
                dst = self.inst_dir(iid)
                dst.mkdir(parents=True, exist_ok=True)
                extract(f"slots/{old_id}/save/", dst)
                rs = s.get("reshade") or ""
                if rs and rs != RESHADE_OFF and f"reshade/{rs}.ini" in members \
                        and not (RESHADE_DIR / f"{rs}.ini").exists():
                    RESHADE_DIR.mkdir(parents=True, exist_ok=True)
                    (RESHADE_DIR / f"{rs}.ini").write_bytes(z.read(f"reshade/{rs}.ini"))
                if rs and rs != RESHADE_OFF and not (RESHADE_DIR / f"{rs}.ini").exists():
                    rs = ""
                pl = s.get("playlist") or ""
                self.data["instances"][iid] = {
                    "name": name, "playlist": pl if pl in profiles else "", "notes": s.get("notes") or "",
                    "created": s.get("created") or time.strftime("%Y-%m-%d %H:%M"),
                    "last_played": s.get("last_played") or "",
                    "graphic_packs": s.get("graphic_packs") or "<GraphicPack />", "reshade": rs}
                n_s += 1
            self._save()
        return f"Imported {n_b} backup(s) and {n_s} save slot(s)."

    # -------------------------------------------------------- instances --
    def _new_id(self):
        return uuid.uuid4().hex[:10]

    def create(self, name: str, source: str = "current", playlist: str = None, notes: str = "") -> str:
        """source: 'current' (copy live save), 'empty' (new game), or a save slot id (duplicate)."""
        name = name.strip()
        if not name:
            raise InstanceError("Give the save slot a name.")
        if any(i["name"].lower() == name.lower() for i in self.data["instances"].values()):
            raise InstanceError(f'An instance called "{name}" already exists.')
        iid = self._new_id()
        dst = self.inst_dir(iid)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if source == "current":
            _copytree_clean(self.live_save_dir(), dst)
        elif source == "empty":
            dst.mkdir(parents=True)
        elif source in self.data["instances"]:
            if source == self.active():
                self.sync_back()
            _copytree_clean(self.inst_dir(source), dst)
        else:
            raise InstanceError("Unknown save source.")
        playlist = playlist or ""      # "" = no playlist (leave mods alone)
        self.data["instances"][iid] = {
            "name": name, "playlist": playlist, "notes": notes,
            "created": time.strftime("%Y-%m-%d %H:%M"), "last_played": ""}
        if source == "empty":
            # a new game starts with no graphic packs at all (korok's own pack aside)
            self.data["instances"][iid]["graphic_packs"] = "<GraphicPack />"
        elif source in self.data["instances"] and source != iid:
            src = self.data["instances"][source]
            for k in ("graphic_packs", "reshade"):
                if src.get(k):
                    self.data["instances"][iid][k] = src[k]
        self._save()
        return iid

    MAIN_NAME = "Main save"

    def ensure_main(self):
        """First run: if the game already has save data and there are no save slots yet,
        make that save the "Main save" slot (and the active one) so it's managed from the
        start. The live save itself isn't touched. Returns the new slot id, or None."""
        if self.data["instances"] or self.active():
            return None
        if _tree_files(self.live_save_dir()) == 0:
            return None
        iid = self.create(self.MAIN_NAME, "current")
        self.data["active"] = iid
        self._save()
        return iid

    def rename(self, iid, name):
        name = name.strip()
        if not name:
            raise InstanceError("Name can't be empty.")
        self.data["instances"][iid]["name"] = name
        self._save()

    def set_playlist(self, iid, playlist):
        self.data["instances"][iid]["playlist"] = playlist
        self._save()

    def follow_playlist(self, playlist):
        """The user picked a playlist on the Mods tab: if the active instance uses
        a playlist, it follows the change."""
        a = self.active()
        if a and self.data["instances"][a].get("playlist") and self.data["instances"][a]["playlist"] != playlist:
            self.data["instances"][a]["playlist"] = playlist
            self._save()

    def rename_playlist_refs(self, old, new):
        changed = False
        for d in self.data["instances"].values():
            if d.get("playlist") == old:
                d["playlist"] = new
                changed = True
        if changed:
            self._save()


    def delete(self, iid):
        if iid == self.active():
            self.data["active"] = None
        self.data["instances"].pop(iid, None)
        shutil.rmtree(INSTANCES_DIR / iid, ignore_errors=True)
        self._save()

    # ------------------------------------------------------- switching ---
    def sync_back(self):
        """Copy the live save into the active save slot (keeps your progress)."""
        a = self.active()
        if not a:
            return False
        self.remember_graphics(a)
        live = self.live_save_dir()
        if not live.exists() or _tree_files(live) == 0:
            return False       # never replace a stored save with an empty one
        _copytree_clean(live, self.inst_dir(a))
        return True

    def switch(self, iid, apply_playlist=True):
        """Make `iid` the active save slot. Returns a short human summary."""
        if iid not in self.data["instances"]:
            raise InstanceError("That save slot no longer exists.")
        self._require_cemu_closed()
        notes = []
        live = self.live_save_dir()
        bk = self.backup_live(f"before-{self.data['instances'][iid]['name']}")
        if bk:
            notes.append("Backed up your current save.")
        if self.active() and self.active() != iid:
            if self.sync_back():
                notes.append(f'Saved progress into "{self.info(self.active())["name"]}".')
        elif self.active() is None and _tree_files(live):
            # live save never belonged to an instance: keep it as one so it isn't lost
            adopt = self.create("Previous save (auto)", "current") if not any(
                i["name"] == "Previous save (auto)" for i in self.data["instances"].values()) else None
            if adopt:
                notes.append('Your existing save was kept as "Previous save (auto)".')
        target = self.inst_dir(iid)
        tmp = live.with_name(live.name + ".korok_new")
        if tmp.exists():
            shutil.rmtree(tmp)
        live.parent.mkdir(parents=True, exist_ok=True)
        _copytree_clean(target, tmp)
        if live.exists():
            shutil.rmtree(live)
        os.replace(tmp, live)
        self.data["active"] = iid
        self.data["instances"][iid]["last_played"] = time.strftime("%Y-%m-%d %H:%M")
        self._save()
        try:
            notes += self.apply_graphics(iid)
        except Exception as e:
            notes.append(f"Graphics settings weren't changed: {e}")
        pl = self.data["instances"][iid].get("playlist")
        if apply_playlist and pl and pl in self.engine.profile_names():
            self.engine.set_active_profile(pl)
            notes.append(f'Playlist set to "{pl}".')
        return " ".join(notes) or "Switched."

    # ------------------------------------------- graphic packs / ReShade --
    # Cemu keeps which graphic packs are on, and the preset picked for each, in the
    # <GraphicPack> element of its settings.xml (rewritten when Cemu closes, so only
    # touched while Cemu is closed). Every save slot keeps its own copy of that element
    # (a new game starts with none); korok's own pack entry always stays as it is.
    def cemu_settings_file(self) -> pathlib.Path:
        return self.engine.user_dir / "settings.xml"

    def read_graphic_packs(self):
        """The <GraphicPack> element of Cemu's settings.xml as text, or None if unreadable."""
        p = self.cemu_settings_file()
        if not p.is_file():
            return None
        el = ET.parse(p).getroot().find("GraphicPack")
        return ET.tostring(el, encoding="unicode") if el is not None else "<GraphicPack />"

    @staticmethod
    def _is_korok_entry(entry) -> bool:
        return "/korokmods/" in entry.get("filename", "").replace("\\", "/").lower()

    def write_graphic_packs(self, xml_text: str):
        p = self.cemu_settings_file()
        tree = ET.parse(p)
        root = tree.getroot()
        old = root.find("GraphicPack")
        new = ET.fromstring(xml_text)
        if old is not None:
            # keep the korok Mods pack exactly as it is now
            for e in [e for e in new if self._is_korok_entry(e)]:
                new.remove(e)
            for e in old:
                if self._is_korok_entry(e):
                    new.append(e)
            root[list(root).index(old)] = new
        else:
            root.append(new)
        BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, BACKUPS_DIR / f"cemu_settings_{time.strftime('%Y%m%d-%H%M%S')}.xml")
        pinned = set(self.data.get("pinned_backups", []))
        olds = sorted(f for f in BACKUPS_DIR.glob("cemu_settings_*.xml") if f.name not in pinned)
        for f in olds[:-KEEP_BACKUPS]:
            f.unlink(missing_ok=True)
        ET.indent(tree)
        tmp = p.with_suffix(".korok_tmp")
        tree.write(tmp, encoding="utf-8", xml_declaration=True)
        os.replace(tmp, p)

    def set_reshade(self, iid, reshade: str):
        self.data["instances"][iid]["reshade"] = reshade or ""
        self._save()

    def remember_graphics(self, iid):
        """Store Cemu's current graphic pack choices in the slot (every slot keeps its own)."""
        d = self.data["instances"].get(iid)
        if not d:
            return
        try:
            xml_text = self.read_graphic_packs()
        except Exception:
            return
        if xml_text is not None:
            d["graphic_packs"] = xml_text
            self._save()

    def apply_graphics(self, iid) -> list:
        """Put the slot's graphic packs and ReShade preset in place. Returns notes."""
        notes = []
        d = self.data["instances"][iid]
        if d.get("graphic_packs"):
            if self.cemu_settings_file().is_file():
                self.write_graphic_packs(d["graphic_packs"])
                notes.append("Graphic packs set for this slot.")
        else:
            self.remember_graphics(iid)      # a slot from before this: start from the current choices
        rs = d.get("reshade") or ""
        if rs:
            note = self.use_reshade_preset(rs)
            notes.append("ReShade: " + ("off." if rs == RESHADE_OFF else f'preset "{rs}".'))
            if note:
                notes.append(note)
        return notes

    def reshade_ini(self) -> pathlib.Path:
        exe = self.cemu_exe()
        folder = pathlib.Path(exe).parent if exe else self.engine.cemu_dir
        return folder / "ReShade.ini"

    def reshade_status(self) -> str:
        """'installed', 'maybe' (ReShade found but not run with Cemu yet) or 'missing'."""
        ini = self.reshade_ini()
        if ini.is_file():
            return "installed"
        folder = ini.parent
        if any((folder / n).is_file() for n in ("opengl32.dll", "dxgi.dll", "ReShade64.dll")):
            return "maybe"
        if sys.platform == "win32":
            try:
                import winreg
                for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                    try:
                        with winreg.OpenKey(hive, r"SOFTWARE\Khronos\Vulkan\ImplicitLayers") as k:
                            i = 0
                            while True:
                                name = winreg.EnumValue(k, i)[0]
                                if "reshade" in name.lower():
                                    return "maybe"
                                i += 1
                    except OSError:
                        continue
            except Exception:
                pass
        return "missing"

    @staticmethod
    def reshade_presets() -> list:
        if not RESHADE_DIR.is_dir():
            return []
        return sorted(p.stem for p in RESHADE_DIR.glob("*.ini") if p.name != OFF_PRESET)

    @staticmethod
    def import_reshade_preset(path) -> str:
        src = pathlib.Path(path)
        RESHADE_DIR.mkdir(parents=True, exist_ok=True)
        dst = RESHADE_DIR / src.name
        if src.resolve() != dst.resolve():
            shutil.copy2(src, dst)
        return dst.stem

    @staticmethod
    def is_reshade_preset(path) -> bool:
        """A ReShade preset .ini (not ReShade's own ReShade.ini settings file)."""
        p = pathlib.Path(path)
        if p.suffix.lower() != ".ini" or p.name.lower() in ("reshade.ini", "reshade_config.ini"):
            return False
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        sections = {ln.strip()[1:-1].strip().upper() for ln in text.splitlines()
                    if ln.strip().startswith("[") and ln.strip().endswith("]")}
        if sections & {"GENERAL", "INPUT", "SCREENSHOT", "OVERLAY", "STYLE", "APP"}:
            return False
        return any(ln.strip().lower().startswith(("techniques=", "techniquesorting="))
                   for ln in text.splitlines())

    def import_reshade_package(self, folder) -> str:
        """An unpacked ReShade preset download: every preset .ini goes to korok's presets, and its
        shaders (a reshade-shaders folder, or loose Shaders/ and Textures/ folders) are copied
        into the reshade-shaders folder next to Cemu.exe, where ReShade looks for them.
        ReShade's own ReShade.ini is never copied over yours. Returns a summary."""
        folder = pathlib.Path(folder)
        presets = [p for p in sorted(folder.rglob("*.ini")) if self.is_reshade_preset(p)]
        if not presets:
            raise InstanceError("No ReShade preset (.ini with a Techniques= line) was found in it.")
        names = [self.import_reshade_preset(p) for p in presets]
        dest = self.reshade_ini().parent / "reshade-shaders"
        copied = 0

        def copy_tree(src: pathlib.Path, dst: pathlib.Path):
            nonlocal copied
            for f in src.rglob("*"):
                if f.is_file():
                    out = dst / f.relative_to(src)
                    out.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, out)
                    copied += 1

        shader_roots = [d for d in folder.rglob("*") if d.is_dir() and d.name.lower() == "reshade-shaders"]
        shader_roots = [d for d in shader_roots if not any(o in d.parents for o in shader_roots)]
        for d in shader_roots:
            copy_tree(d, dest)
        if not shader_roots:
            for d in folder.rglob("*"):
                if d.is_dir() and d.name.lower() in ("shaders", "textures") and \
                        not any(p.name.lower() in ("shaders", "textures") for p in d.parents):
                    copy_tree(d, dest / ("Shaders" if d.name.lower() == "shaders" else "Textures"))
        msg = "Added preset" + ("s " if len(names) > 1 else " ") + ", ".join(f'"{n}"' for n in names) + "."
        if copied:
            msg += f" Copied {copied} shader file(s) into {dest}."
        return msg

    def delete_reshade_preset(self, name: str):
        (RESHADE_DIR / f"{name}.ini").unlink(missing_ok=True)
        for d in self.data["instances"].values():
            if d.get("reshade") == name:
                d["reshade"] = ""
        self._save()

    def current_reshade_preset(self):
        """Path ReShade.ini points at (PresetPath), or None."""
        ini = self.reshade_ini()
        if not ini.is_file():
            return None
        section = ""
        for line in ini.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s.startswith("[") and s.endswith("]"):
                section = s[1:-1].strip().upper()
            elif section == "GENERAL" and s.split("=", 1)[0].strip().lower() == "presetpath":
                return s.split("=", 1)[1].strip() or None
        return None

    def find_reshade_compat_pack(self):
        """settings.xml filename of Cemu's "Reshade Compatibility" graphic pack for BotW, or None."""
        root = self.engine.user_dir / "graphicPacks"
        if not root.is_dir():
            return None
        for rules in root.rglob("rules.txt"):
            try:
                text = rules.read_text(encoding="utf-8", errors="replace").lower()
            except OSError:
                continue
            name = next((ln.split("=", 1)[1] for ln in text.splitlines()
                         if ln.strip().startswith("name") and "=" in ln), "")
            if "reshade" in name and "compatib" in name and "101c9400" in text:
                return rules.relative_to(self.engine.user_dir).as_posix()
        return None

    def set_reshade_compat_pack(self, on: bool) -> str:
        """Turn Cemu's Reshade Compatibility graphic pack on or off in settings.xml.
        Returns a note for the user ("" when nothing changed)."""
        rel = self.find_reshade_compat_pack()
        settings = self.cemu_settings_file()
        if rel is None or not settings.is_file():
            return ""
        if self.cemu_running():
            return ("Close Cemu, then pick the preset again, so korok can turn Cemu's Reshade Compatibility "
                    "graphic pack " + ("on." if on else "off."))
        cur = ET.fromstring(self.read_graphic_packs())
        norm = rel.lower()
        entry = next((e for e in cur if e.get("filename", "").replace("\\", "/").lower() == norm), None)
        if entry is None:
            if not on:
                return ""
            entry = ET.SubElement(cur, "Entry", {"filename": rel})
        elif on == (entry.get("disabled", "false").lower() != "true"):
            return ""
        if on:
            entry.attrib.pop("disabled", None)
        else:
            entry.set("disabled", "true")
        self.write_graphic_packs(ET.tostring(cur, encoding="unicode"))
        return "Reshade Compatibility graphic pack turned " + ("on." if on else "off.")

    def use_reshade_preset(self, name: str) -> str:
        """Point ReShade.ini (next to Cemu.exe) at a korok preset, or at an empty one (off),
        and turn Cemu's Reshade Compatibility graphic pack on/off to match. Returns a note."""
        RESHADE_DIR.mkdir(parents=True, exist_ok=True)
        if name == RESHADE_OFF:
            target = RESHADE_DIR / OFF_PRESET
            if not target.exists():
                target.write_text("Techniques=\nTechniqueSorting=\n", encoding="utf-8")
        else:
            target = RESHADE_DIR / f"{name}.ini"
            if not target.is_file():
                raise InstanceError(f'The ReShade preset "{name}" is missing from {RESHADE_DIR}.')
        ini = self.reshade_ini()
        lines = ini.read_text(encoding="utf-8", errors="replace").splitlines() if ini.is_file() else []
        out, section, done, general_at = [], "", False, None
        for line in lines:
            s = line.strip()
            if s.startswith("[") and s.endswith("]"):
                if section == "GENERAL" and not done:
                    out.append(f"PresetPath={target}")
                    done = True
                section = s[1:-1].strip().upper()
                if section == "GENERAL":
                    general_at = len(out)
            elif section == "GENERAL" and s.split("=", 1)[0].strip().lower() == "presetpath":
                if not done:
                    out.append(f"PresetPath={target}")
                    done = True
                continue
            out.append(line)
        if not done:
            if general_at is None:
                out += (["", "[GENERAL]"] if out else ["[GENERAL]"])
                out.append(f"PresetPath={target}")
            elif section == "GENERAL":
                out.append(f"PresetPath={target}")
        ini.parent.mkdir(parents=True, exist_ok=True)
        ini.write_text("\n".join(out) + "\n", encoding="utf-8")
        try:
            return self.set_reshade_compat_pack(name != RESHADE_OFF)
        except Exception as e:
            return f"Couldn't change the Reshade Compatibility graphic pack: {e}"

    # ----------------------------------------------------------- launch --
    @staticmethod
    def resolve_game(game: str) -> str:
        """The game path Cemu's -g wants. A game folder (the one holding code/, content/,
        meta/) is turned into its code/*.rpx; a folder holding a .wua/.wud/.wux uses that."""
        p = pathlib.Path(game)
        if not p.is_dir():
            return game
        for d in (p, p.parent):
            rpx = sorted((d / "code").glob("*.rpx")) if (d / "code").is_dir() else []
            if rpx:
                return str(rpx[0])
        for ext in ("*.wua", "*.wud", "*.wux", "*.rpx"):
            hits = sorted(p.glob(ext))
            if hits:
                return str(hits[0])
        raise InstanceError(f"No game found in {p} (expected a code/*.rpx, or a .wua/.wud/.wux file).")

    def launch(self, with_game=True):
        exe = self.cemu_exe()
        if not exe or not pathlib.Path(exe).exists():
            raise InstanceError("Set the path to Cemu.exe first (Settings tab).")
        args = [exe]
        game = self.data.get("game_path", "")
        if game and with_game:
            args += ["-g", self.resolve_game(game)]
        return subprocess.Popen(args, cwd=str(pathlib.Path(exe).parent))
