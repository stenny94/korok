"""korok's mod library and pack builder.

Every mod korok imports is turned into a UKMM mod package (see korok_pack) and
kept in the library (korok_library/<name>/mod.zip):

  UKMM package (.zip)     kept as it is
  BCML package (.bnp)     its merge logs are applied the way UKMM converts BNPs
                          (korok_bnp), then it's packaged
  graphic pack / folder   packaged: every changed file stored as a diff against
                          the game's own version

Applying the active profile then deploys all its enabled mods together, the way
UKMM does (korok_deploy): each changed file is rebuilt from the game's version
with every mod's changes merged in load order, written to Cemu's graphicPacks/
KorokMods, and the game's resource size table is updated for it.
"""
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from datetime import datetime

import korok_ukmm_native as native

try:
    import winreg
except ImportError:  # non-Windows
    winreg = None

# JPN, USA, EUR
BOTW_TITLE_IDS = "00050000101C9400,00050000101C9500,00050000101C9300"

# Anchor everything to this script's folder so launching from a
# bcml:// link (different working directory) uses the same config/library.
BASE_DIR = pathlib.Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
LOGS_DIR = BASE_DIR / "logs"
CONFIG_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

CONFIG_FILE = CONFIG_DIR / "korok_config.json"
STATE_FILE = CONFIG_DIR / "korok_profiles.json"      # profiles + load order
LASTBUILD_FILE = CONFIG_DIR / "korok_lastbuild.json"  # signature of the last pack build
BUILD_LOG = LOGS_DIR / "korok_build.log"
LIBRARY_DIR = BASE_DIR / "korok_library"
PACKAGE_NAME = "mod.zip"
META_NAME = "korok.json"
BNP_RSTB_NAME = "bcml_rstb.json"    # a BNP's own size-table values
PACK_FOLDER_NAME = "KorokMods"
GAME_DIRS = {"content", "aoc"}
BUILD_VERSION = "9"
UPDATE_TITLE_IDS = ("101c9400", "101c9500", "101c9300")
CODE_FILES = ("korok_engine.py", "korok_deploy.py", "korok_ukmm_native.py", "korok_ukmm_formats.py",
              "korok_rstb.py")


def register_bcml_protocol():
    """Registers 'bcml://' so GameBanana 1-click links open korok.
    Note: this replaces BCML's registration for the current user."""
    if sys.platform != "win32" or winreg is None:
        return

    command_str = f'"{sys.executable}" "{pathlib.Path(sys.argv[0]).resolve()}" "%1"'
    protocol_key = r"Software\Classes\bcml"

    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, protocol_key) as key:
            winreg.SetValue(key, "", winreg.REG_SZ, "URL:BCML Protocol")
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER, rf"{protocol_key}\shell\open\command"
        ) as key:
            winreg.SetValue(key, "", winreg.REG_SZ, command_str)
    except Exception as e:
        print(f"Protocol registration info: {e}")


class KorokEngine:

    def __init__(self, library_dir=None):
        self.staging_dir = pathlib.Path(library_dir) if library_dir else LIBRARY_DIR
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.last_rstb_report = ""
        self.last_merge_report = ""
        self.last_build_summary = ""
        self.last_build_skipped = False
        self.last_imported_name = ""
        self.import_notes = []          # information about the last import (not warnings)
        self.option_choices = {}        # {archive path: [option folders]} picked in the UI
        self.state = self._load_state()

        saved_path = self.load_config_path()
        self.cemu_dir = pathlib.Path(saved_path) if saved_path else pathlib.Path("C:/Emulators/Cemu")
        self.update_graphic_packs_dir()

    # ---------- config ----------
    def _read_config(self) -> dict:
        if CONFIG_FILE.exists():
            try:
                d = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                return d if isinstance(d, dict) else {}
            except Exception:
                pass
        return {}

    def _write_config(self, **changes):
        cfg = self._read_config()
        cfg.update(changes)
        try:
            CONFIG_FILE.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
        except Exception as e:
            print(f"Failed to save config: {e}")

    def load_config_path(self) -> str:
        return self._read_config().get("cemu_dir", "")

    def save_config_path(self, cemu_path: str):
        self._write_config(cemu_dir=cemu_path)

    @staticmethod
    def native_ukmm_available() -> bool:
        return native.available()

    @staticmethod
    def is_ukmm_archive(path) -> bool:
        return native.is_package(path)

    def resolve_user_dir(self) -> pathlib.Path:
        """Where Cemu 2.x actually reads graphicPacks/ from.
        1) <cemu>/portable  2) legacy: settings.xml next to Cemu.exe
        3) %APPDATA%/Cemu   4) fall back to the install folder."""
        if (self.cemu_dir / "portable").is_dir():
            return self.cemu_dir / "portable"
        if (self.cemu_dir / "settings.xml").exists():
            return self.cemu_dir
        appdata = os.environ.get("APPDATA")
        if appdata and (pathlib.Path(appdata) / "Cemu").is_dir():
            return pathlib.Path(appdata) / "Cemu"
        return self.cemu_dir

    def update_graphic_packs_dir(self):
        # Cemu 2.x scans graphicPacks/ recursively for rules.txt.
        # Avoid downloadedGraphicPacks: Cemu manages (and may wipe) it.
        self.user_dir = self.resolve_user_dir()
        self.graphic_packs_dir = self.user_dir / "graphicPacks" / PACK_FOLDER_NAME

        # Every place an older korok version (or this one) may have written
        # a pack, so Cemu never sees two copies.
        candidates = []
        for base in {self.cemu_dir, self.user_dir}:
            candidates.append(base / "graphicPacks" / PACK_FOLDER_NAME)
            candidates.append(
                base / "graphicPacks" / "downloadedGraphicPacks"
                / "BreathOfTheWild" / "Mods" / PACK_FOLDER_NAME
            )
        self.legacy_pack_dirs = [c for c in candidates if c != self.graphic_packs_dir]

    def set_cemu_dir(self, cemu_dir: str):
        self.cemu_dir = pathlib.Path(cemu_dir)
        self.update_graphic_packs_dir()
        self.save_config_path(str(cemu_dir))

    def generate_rules_txt(self) -> str:
        return f"""[Definition]
titleIds = {BOTW_TITLE_IDS}
name = "korok Mods"
path = "The Legend of Zelda: Breath of the Wild/Mods/korok Mods"
description = "Consolidated package containing all active korok mods managed by korok."
version = 7
default = true
"""

    # ---------- game files ----------
    def dump(self) -> "native.Dump":
        """The game's own files, from Cemu's mlc01 (base game, update, DLC)."""
        dump = native.dump_from_cemu(self.user_dir)
        if not (dump.dirs[1] or dump.dirs[2]):
            raise RuntimeError(
                "The game's files weren't found in Cemu's mlc01 folder. korok needs the base game "
                "and the v208 update installed in Cemu (and the DLC, if you have it), and your Cemu "
                "folder set on the Settings tab.")
        return dump

    def game_language(self, dump=None) -> str:
        """The language mods' texts are converted for (BNP texts.json): USen if the game has it."""
        dump = dump or self.dump()
        for lang in ("USen", "EUen", "JPja", "EUde", "EUfr", "USfr", "EUes", "USes", "EUit", "EUnl",
                     "EUru", "CNzh", "KRko", "TWzh"):
            if dump.get_bytes(f"Pack/Bootup_{lang}.pack") is not None:
                return lang
        return "USen"

    # ---------- importing ----------
    def _extract_archive(self, archive_path: pathlib.Path, target_dir: pathlib.Path):
        try:
            with zipfile.ZipFile(archive_path, "r") as zip_ref:
                zip_ref.extractall(target_dir)
            return
        except (zipfile.BadZipFile, NotImplementedError):
            pass

        try:
            import py7zr

            with py7zr.SevenZipFile(archive_path, mode="r") as z:
                z.extractall(path=target_dir)
            return
        except ImportError:
            pass

        try:
            res = subprocess.run(
                ["7z", "x", str(archive_path), f"-o{target_dir}", "-y"],
                capture_output=True,
            )
            ok = res.returncode == 0
        except FileNotFoundError:
            ok = False
        if not ok:
            raise RuntimeError(
                f"Could not extract {archive_path.name}. "
                "Install py7zr (`pip install py7zr`) for .bnp/.7z support."
            )

    @staticmethod
    def _safe_name(name: str) -> str:
        ok = " _-.()'+&!,"
        cleaned = "".join(c for c in str(name) if c.isalnum() or c in ok)
        cleaned = " ".join(cleaned.split()).strip(" .")
        return cleaned[:80] or "Unnamed Mod"

    @staticmethod
    def _embedded_name(root: pathlib.Path):
        """Name written by the mod author: BNP info.json "name", else rules.txt "name = ..."."""
        try:
            info = root / "info.json"
            if info.exists():
                n = json.loads(info.read_text(encoding="utf-8-sig")).get("name")
                if isinstance(n, str) and n.strip():
                    return n.strip()
        except Exception:
            pass
        try:
            rules = root / "rules.txt"
            if rules.exists():
                for line in rules.read_text(encoding="utf-8-sig", errors="replace").splitlines():
                    k, _, v = line.partition("=")
                    if k.strip().lower() == "name" and v.strip():
                        return v.strip().strip('"')
        except Exception:
            pass
        return None

    @staticmethod
    def _gamebanana_name(mod_id: str):
        """Best-effort lookup of the mod's title on GameBanana (None if offline/unsupported)."""
        if not str(mod_id).isdigit():
            return None
        try:
            url = f"https://gamebanana.com/apiv11/Mod/{mod_id}/ProfilePage"
            req = urllib.request.Request(url, headers={"User-Agent": "KorokModManager/1.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                n = json.loads(r.read().decode("utf-8", "replace")).get("_sName")
            return n.strip() if isinstance(n, str) and n.strip() else None
        except Exception:
            return None

    @staticmethod
    def _find_pack_roots(base: pathlib.Path) -> list:
        """Every folder that directly contains 'content' or 'aoc', shallowest first.
        Mods with options (hair styles, colours, ...) often ship one such folder
        per option."""
        roots, queue = [], [base]
        while queue:
            cur = queue.pop(0)
            try:
                children = sorted((c for c in cur.iterdir() if c.is_dir()), key=lambda c: c.name.lower())
            except OSError:
                continue
            if any(c.name.lower() in GAME_DIRS for c in children):
                roots.append(cur)
                # don't look inside the game folders themselves
                children = [c for c in children if c.name.lower() not in GAME_DIRS]
            queue.extend(children)
        return roots

    @classmethod
    def _find_pack_root(cls, base: pathlib.Path):
        """Shallowest folder that directly contains 'content' or 'aoc'."""
        roots = cls._find_pack_roots(base)
        return roots[0] if roots else None

    def import_mod_archive(self, file_path: str, mod_display_name: str, _option_of: str = "") -> list:
        """Adds a mod to the library as a UKMM package. Returns a list of warning strings.
        _option_of: internal - set when importing an extra mod folder found in the same
        archive, so it gets its own name and is imported disabled."""
        import korok_bnp
        import korok_pack
        if not native.available():
            raise RuntimeError("korok needs a few Python packages to read mods. Run:  pip install " +
                               " ".join(native.missing_requirements() or ["oead", "cbor2", "zstandard"]))
        if not _option_of:
            self.import_notes = []
        archive_path = pathlib.Path(file_path)
        warnings = []
        options = self.option_choices.get(str(archive_path))

        if native.is_package(archive_path) and archive_path.is_file():
            # already a UKMM package: keep it as it is
            pkg = native.Package(archive_path, options)
            try:
                name = self._safe_name(pkg.name or mod_display_name)
                chosen = list(pkg.options)
                labels = {o["path"]: o["name"] for g in pkg.option_groups() for o in g["options"]}
                has_options = pkg.has_options
            finally:
                pkg.close()
            dest = self.staging_dir / name
            was_known = dest.exists()
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(archive_path, dest / PACKAGE_NAME)
            self._write_meta(dest, {"name": name, "source": "UKMM package", "options": chosen,
                                    "file": archive_path.name})
            if has_options:
                self.import_notes.append("Options used: " + (", ".join(labels.get(o, o) for o in chosen)
                                                             or "none") + ".")
            self._finish_import(name, was_known, enable=True)
            return warnings

        dump = self.dump()
        work = pathlib.Path(tempfile.mkdtemp(prefix="korok_import_"))
        try:
            if archive_path.is_dir():
                scan = archive_path
            else:
                self._extract_archive(archive_path, work / "x")
                scan = work / "x"
            bnp_root = next((p.parent for p in sorted(scan.rglob("info.json"), key=lambda p: len(p.parts))
                             if (p.parent / "content").is_dir() or (p.parent / "aoc").is_dir()
                             or (p.parent / "logs").is_dir()), None)
            if bnp_root is not None and not _option_of:
                # BCML package: apply its logs onto the game's files, like UKMM's BNP converter
                src = work / "bnp"
                shutil.copytree(bnp_root, src)
                meta = korok_bnp.convert_bnp_folder(src, dump, self.game_language(dump), log=self._log)
                groups = native.Package.groups_from_meta(meta)
                valid = [o["path"] for g in groups for o in g["options"]]
                chosen = [o for o in (options if options is not None
                                      else [d for g in groups for d in g["defaults"]]) if o in valid]
                kind = "BCML package (BNP)"
                roots, extra_roots = [src], []
            else:
                roots = self._find_pack_roots(scan)
                if not roots:
                    raise RuntimeError(
                        "No 'content' or 'aoc' folder found in this archive. "
                        "It may be a Switch ('romfs') mod or an unsupported format.")
                src = roots[0]
                extra_roots = [] if _option_of else roots[1:]
                rules = src / "rules.txt"
                meta = korok_bnp.meta_from_rules(rules.read_text(encoding="utf-8-sig", errors="replace")
                                                 if rules.is_file() else "", mod_display_name)
                if _option_of:
                    meta["name"] = mod_display_name
                chosen, groups = [], []
                kind = "graphic pack"
            name = self._safe_name(meta["name"] if not _option_of else mod_display_name)
            dest = self.staging_dir / name
            was_known = dest.exists()
            tmp_pkg = work / PACKAGE_NAME
            packer = korok_pack.pack_folder(src, tmp_pkg, meta, dump, log=self._log)
            dest.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tmp_pkg), str(dest / PACKAGE_NAME))
            self._write_meta(dest, {"name": name, "source": kind, "options": chosen,
                                    "file": archive_path.name, "kept_whole": len(packer.kept_whole)})
            floor = self._bnp_rstb(src, chosen) if kind.startswith("BCML") else {}
            (dest / BNP_RSTB_NAME).unlink(missing_ok=True)
            if floor:
                (dest / BNP_RSTB_NAME).write_text(json.dumps(floor), encoding="utf-8")
            if groups:
                labels = {o["path"]: o["name"] for g in groups for o in g["options"]}
                self.import_notes.append("Options used: " + (", ".join(labels.get(o, o) for o in chosen)
                                                             or "none") + ".")
            whole = sorted({c.rpartition(".")[2] for c, why in packer.kept_whole if "not merged by korok" in why})
            if whole:
                self.import_notes.append(
                    "Some of its files can't be merged with other mods yet and are used whole "
                    f"({', '.join(whole)}); if another mod changes the same file, the lower one in the "
                    "load order wins.")
            self._finish_import(name, was_known, enable=not _option_of)

            if extra_roots:
                added = []
                for extra in extra_roots:
                    label = extra.name if extra != scan else "base"
                    opt_name = self._safe_name(f"{name} - {label}")
                    try:
                        self.import_mod_archive(str(extra), opt_name, _option_of=name)
                        added.append(self.last_imported_name)
                    except Exception as e:
                        warnings.append(f"Mod folder '{label}' could not be imported: {e}")
                self.last_imported_name = name
                if added:
                    warnings.append(
                        f"This archive has {len(roots)} separate mod folders (usually one per option, "
                        f"like hair styles or colours). '{name}' is the main one and is enabled. "
                        f"The others were added DISABLED: {', '.join(added)}. Tick the one(s) you "
                        "want and keep them BELOW the main mod in the load order.")
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return warnings

    @staticmethod
    def _bnp_rstb(root: pathlib.Path, options) -> dict:
        """BCML's own size-table values for a BNP's files (logs/rstb.json, then each chosen option's)."""
        out = {}
        for logs in [root / "logs"] + [root / "options" / o / "logs" for o in options or []]:
            try:
                data = json.loads((logs / "rstb.json").read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, int) and not isinstance(v, bool) and v > 0:
                        out[str(k).replace("\\", "/")] = v
        return out

    def _finish_import(self, name, was_known, enable):
        self.last_imported_name = name
        self._known_before = {name} if was_known else set()
        self._register_mod(name, enable=None if was_known else enable)
        self._known_before = set()

    @staticmethod
    def _log(text: str):
        try:
            with open(LOGS_DIR / "korok_import.log", "a", encoding="utf-8") as f:
                f.write(str(text).rstrip() + "\n")
        except Exception:
            pass

    @staticmethod
    def _read_archive_member(path, member: str):
        """Bytes of one file (by name, anywhere in the archive) from a .zip/.7z/.bnp, or None."""
        path = pathlib.Path(path)
        low = member.lower()
        if path.is_dir():
            hits = sorted(path.rglob(member), key=lambda p: len(p.parts))
            return hits[0].read_bytes() if hits else None
        try:
            if zipfile.is_zipfile(path):
                with zipfile.ZipFile(path) as z:
                    names = sorted((n for n in z.namelist() if n.rstrip("/").lower().rsplit("/", 1)[-1] == low),
                                   key=lambda n: n.count("/"))
                    return z.read(names[0]) if names else None
            import py7zr
            with py7zr.SevenZipFile(path, mode="r") as z:
                names = sorted((n for n in z.getnames() if n.lower().rsplit("/", 1)[-1] == low),
                               key=lambda n: n.count("/"))
            if not names:
                return None
            tmp = pathlib.Path(tempfile.mkdtemp(prefix="korok_member_"))
            try:
                with py7zr.SevenZipFile(path, mode="r") as z:
                    z.extract(path=tmp, targets=[names[0]])
                f = tmp / names[0]
                return f.read_bytes() if f.is_file() else None
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            return None

    def mod_option_groups(self, path) -> list:
        """Install options of a mod archive (UKMM package or BNP), [] if none."""
        if native.is_package(path):
            try:
                if zipfile.is_zipfile(path):
                    return native.package_option_groups(path)
            except Exception:
                return []
            return []
        raw = self._read_archive_member(path, "info.json")
        if not raw:
            return []
        try:
            import korok_bnp
            return native.Package.groups_from_meta(korok_bnp.meta_from_info(json.loads(raw.decode("utf-8-sig"))))
        except Exception:
            return []

    # ---------- library ----------
    @staticmethod
    def _write_meta(mod_path: pathlib.Path, info: dict):
        (mod_path / META_NAME).write_text(json.dumps(info, indent=1), encoding="utf-8")

    def _read_meta(self, name: str) -> dict:
        try:
            return json.loads((self.staging_dir / name / META_NAME).read_text(encoding="utf-8"))
        except Exception:
            return {}

    def get_mod_info(self, name: str) -> dict:
        """'enabled' reflects the ACTIVE profile."""
        info = {"needs_merge": []}
        info.update(self._read_meta(name))
        info["enabled"] = name in self._profile()["enabled"]
        return info

    def set_mod_enabled(self, name: str, enabled: bool):
        prof = self._profile()
        if enabled and name not in prof["enabled"]:
            prof["enabled"].append(name)
        elif not enabled and name in prof["enabled"]:
            prof["enabled"].remove(name)
        self._save_state()

    # ---------- playlists (profiles) & load order ----------
    def _default_state(self) -> dict:
        mods = self.get_staged_mods()
        enabled = []
        for m in mods:
            try:
                meta = json.loads((self.staging_dir / m / META_NAME).read_text(encoding="utf-8"))
                if meta.get("enabled", True):
                    enabled.append(m)
            except Exception:
                enabled.append(m)
        return {"active": "Default",
                "profiles": {"Default": {"order": list(mods), "enabled": enabled}}}

    def _load_state(self) -> dict:
        state = None
        if STATE_FILE.exists():
            try:
                state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                if not isinstance(state.get("profiles"), dict) or not state["profiles"]:
                    state = None
            except Exception:
                state = None
        if state is None:
            state = self._default_state()
        self.state = state
        self._sync_state()
        return state

    def _sync_state(self):
        """Drops mods that no longer exist, appends new ones to every profile."""
        staged = self.get_staged_mods()
        for prof in self.state["profiles"].values():
            prof["order"] = [m for m in prof.get("order", []) if m in staged]
            prof["order"] += [m for m in staged if m not in prof["order"]]
            prof["enabled"] = [m for m in prof.get("enabled", []) if m in staged]
        if self.state.get("active") not in self.state["profiles"]:
            self.state["active"] = next(iter(self.state["profiles"]))
        self._save_state()

    def _save_state(self):
        try:
            STATE_FILE.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"Failed to save profiles: {e}")

    def _profile(self, name=None) -> dict:
        return self.state["profiles"][name or self.state["active"]]

    @property
    def active_profile(self) -> str:
        return self.state["active"]

    def profile_names(self) -> list:
        return list(self.state["profiles"].keys())

    def profile_mods(self, name=None) -> list:
        """Enabled mods of a profile, in load order."""
        prof = self._profile(name)
        return [m for m in prof["order"] if m in prof["enabled"]]

    def ordered_mods(self) -> list:
        """All staged mods in the active profile's load order."""
        return list(self._profile()["order"])

    def set_active_profile(self, name: str):
        if name in self.state["profiles"]:
            self.state["active"] = name
            self._save_state()

    def create_profile(self, name: str, copy_from=None) -> str:
        name = name.strip()
        if not name:
            raise ValueError("Profile name can't be empty.")
        if name in self.state["profiles"]:
            raise ValueError(f"A profile named '{name}' already exists.")
        if copy_from and copy_from in self.state["profiles"]:
            src = self.state["profiles"][copy_from]
            self.state["profiles"][name] = {"order": list(src["order"]), "enabled": list(src["enabled"])}
        else:
            self.state["profiles"][name] = {"order": list(self.get_staged_mods()), "enabled": []}
        self._save_state()
        return name

    def rename_profile(self, old: str, new: str):
        new = new.strip()
        if not new or (new != old and new in self.state["profiles"]):
            raise ValueError("That name is empty or already used.")
        profs = {(new if k == old else k): v for k, v in self.state["profiles"].items()}
        self.state["profiles"] = profs
        if self.state["active"] == old:
            self.state["active"] = new
        self._save_state()

    def delete_profile(self, name: str):
        if len(self.state["profiles"]) <= 1:
            raise ValueError("You need at least one profile.")
        self.state["profiles"].pop(name, None)
        if self.state["active"] == name:
            self.state["active"] = next(iter(self.state["profiles"]))
        self._save_state()

    def move_mod(self, name: str, delta: int) -> bool:
        order = self._profile()["order"]
        if name not in order:
            return False
        i = order.index(name)
        j = max(0, min(len(order) - 1, i + delta))
        if i == j:
            return False
        order.insert(j, order.pop(i))
        self._save_state()
        return True

    def delete_mod(self, name: str):
        """Removes a mod from the library and every profile. No rebuild."""
        mod_path = self.staging_dir / name
        if mod_path.exists():
            shutil.rmtree(mod_path)
        self._sync_state()

    def _register_mod(self, name: str, enable: bool):
        """Called after a mod is staged/re-staged."""
        for prof in self.state["profiles"].values():
            if name not in prof["order"]:
                prof["order"].append(name)
        prof = self._profile()
        if enable and name not in prof["enabled"] and name not in getattr(self, "_known_before", set()):
            prof["enabled"].append(name)
        if enable is False and name in prof["enabled"]:
            prof["enabled"].remove(name)
        self._save_state()

    def handle_one_click_url(self, raw_url: str):
        """Returns (mod_display_name, warnings)."""
        clean_url = raw_url.strip().removeprefix("bcml:").removeprefix("gb:").strip()
        parts = clean_url.split(",")
        download_url = parts[0]
        mod_id = parts[2] if len(parts) > 2 else "GB_Mod"

        ext = ".bnp" if ".bnp" in download_url.lower() else ".zip"
        temp_file = pathlib.Path(tempfile.gettempdir()) / f"korok_download_{mod_id}{ext}"

        req = urllib.request.Request(download_url, headers={"User-Agent": "KorokModManager/1.0"})
        try:
            with urllib.request.urlopen(req) as response, open(temp_file, "wb") as out_file:
                shutil.copyfileobj(response, out_file)
            mod_display_name = self._gamebanana_name(mod_id) or f"GB_Mod_{mod_id}"
            warnings = self.import_mod_archive(str(temp_file), mod_display_name)
        finally:
            temp_file.unlink(missing_ok=True)

        return self.last_imported_name, warnings

    # ---------- building the pack ----------
    def build_signature(self) -> str:
        h = hashlib.sha1(f"{BUILD_VERSION}|{self.graphic_packs_dir}".encode())
        for n in CODE_FILES:
            f = BASE_DIR / n
            try:
                st = f.stat()
                h.update(f"{n}|{st.st_size}|{st.st_mtime_ns}".encode())
            except OSError:
                pass
        for name in self.profile_mods():
            p = self.staging_dir / name / PACKAGE_NAME
            try:
                st = p.stat()
                h.update(f"{name}|{st.st_size}|{st.st_mtime_ns}|{self._read_meta(name).get('options')}".encode())
            except OSError:
                pass
        return h.hexdigest()

    def pending_changes(self) -> bool:
        """True when the pack on disk doesn't match the active profile."""
        try:
            last = json.loads(LASTBUILD_FILE.read_text(encoding="utf-8"))
        except Exception:
            return bool(self.profile_mods())
        if not (self.graphic_packs_dir / "rules.txt").exists():
            return True
        return last.get("sig") != self.build_signature()

    def _remember_build(self, sig: str):
        try:
            LASTBUILD_FILE.write_text(json.dumps({"sig": sig}), encoding="utf-8")
        except Exception:
            pass

    def rebuild_consolidated_pack(self, progress_cb=None, force=False):
        """Deploys the active profile's enabled mods to graphicPacks/KorokMods.
        Skips the work when nothing changed (unless force).
        progress_cb(fraction 0..1, message) may be called from a worker thread."""
        import korok_deploy
        import korok_rstb
        cb = progress_cb or (lambda frac, text: None)
        if not self.cemu_dir.exists():
            return
        self.last_build_skipped = False
        self.last_rstb_report = self.last_merge_report = ""
        sig = self.build_signature()
        if not force and (self.graphic_packs_dir / "rules.txt").exists():
            try:
                if json.loads(LASTBUILD_FILE.read_text(encoding="utf-8")).get("sig") == sig:
                    self.last_build_skipped = True
                    self.last_build_summary = (
                        "The pack is already up to date with this profile - nothing was rebuilt.\n"
                        "In the pack: " + (", ".join(self.profile_mods()) or "nothing"))
                    cb(1.0, "Already up to date")
                    return
            except Exception:
                pass
        cb(0.0, "Preparing...")
        names = [n for n in self.profile_mods() if (self.staging_dir / n / PACKAGE_NAME).is_file()]
        skipped = [n for n in self.get_staged_mods() if n not in names]
        mods = [(self.staging_dir / n / PACKAGE_NAME, self._read_meta(n).get("options")) for n in names]
        dump = self.dump() if mods else None
        korok_rstb.AIDEF_LOADER = (lambda: dump.get_bytes("Actor/AIDef/AIDef_Game.product.sbyml")) if dump else None

        # build next to the pack, then swap it in, so a failed build leaves the old pack alone
        self.graphic_packs_dir.parent.mkdir(parents=True, exist_ok=True)
        out = self.graphic_packs_dir.parent / (PACK_FOLDER_NAME + ".building")
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir()
        conv = None
        if mods:
            floor = {}
            for n in names:
                try:
                    for k, v in json.loads((self.staging_dir / n / BNP_RSTB_NAME).read_text(encoding="utf-8")).items():
                        floor[k] = max(floor.get(k, 0), int(v))
                except Exception:
                    pass
            conv = korok_deploy.deploy(mods, dump, out, progress=lambda f, t: cb(0.02 + f * 0.95, f"Building {t}"),
                                       rstb_floor=floor)
        (out / "rules.txt").write_text(self.generate_rules_txt(), encoding="utf-8")
        for legacy in self.legacy_pack_dirs:
            if legacy.name == PACK_FOLDER_NAME and legacy.exists():
                shutil.rmtree(legacy, ignore_errors=True)
        if self.graphic_packs_dir.exists():
            shutil.rmtree(self.graphic_packs_dir)
        out.rename(self.graphic_packs_dir)

        self.last_build_summary = (f"Profile: {self.active_profile}\n"
                                   "In the pack: " + (", ".join(names) or "nothing (pack only has rules.txt)"))
        if conv is not None:
            sized = sum(1 for v in conv.rstb.values() if v is not None)
            dropped = sum(1 for v in conv.rstb.values() if v is None)
            self.last_rstb_report = (f"Built {conv.built} game file(s). Resource size table: {sized} entr"
                                     f"{'y' if sized == 1 else 'ies'} set" +
                                     (f", {dropped} removed (size unknown)" if dropped else "") + ".")
            lines = []
            for kind, files in sorted(conv.unsupported.items()):
                lines.append(f"{kind}: {len(files)} file(s) (e.g. {files[0]}) - not merged, game's version kept")
            if conv.problems:
                lines.append("Build problems:\n  " + "\n  ".join(conv.problems[:10]))
            self.last_merge_report = "\n".join(lines)
        self._write_build_log(names, skipped, conv)
        self._remember_build(sig)
        cb(1.0, "Done")

    def _write_build_log(self, merged: list, skipped: list, conv):
        try:
            lines = [
                f"Build at {datetime.now():%Y-%m-%d %H:%M:%S}",
                f"Pack: {self.graphic_packs_dir}",
                f"Mods ({len(merged)}, load order): " + (", ".join(merged) or "-"),
                f"Disabled ({len(skipped)}): " + (", ".join(skipped) or "-"),
                self.last_rstb_report,
                self.last_merge_report,
            ]
            if conv is not None and getattr(conv, "textures", None):
                lines.append("Model textures (file: mod it comes from):")
                lines += [f"  {k}: {v}" for k, v in sorted(conv.textures.items())]
            if conv is not None:
                lines.append("Size table entries:")
                lines += [f"  {k}: {v if v is not None else 'removed'}" for k, v in sorted(conv.rstb.items())]
            BUILD_LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception:
            pass

    def _vanilla_roots(self) -> dict:
        title = self.user_dir / "mlc01" / "usr" / "title"
        roots = {"content": [], "aoc": []}
        for tid in UPDATE_TITLE_IDS:
            roots["content"].append(title / "0005000e" / tid / "content")
            roots["content"].append(title / "00050000" / tid / "content")
            roots["content"].append(title / "0005000c" / tid / "content" / "0010")
            roots["aoc"].append(title / "0005000c" / tid / "content" / "0010")
        return roots

    def get_staged_mods(self) -> list:
        if not self.staging_dir.exists():
            return []
        return sorted(d.name for d in self.staging_dir.iterdir()
                      if d.is_dir() and not d.name.startswith(".") and (d / PACKAGE_NAME).is_file())
