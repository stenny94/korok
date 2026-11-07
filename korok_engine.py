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

import korok_rstb
import korok_merge
import korok_mergers

try:
    import winreg
except ImportError:  # non-Windows
    winreg = None

# JPN, USA, EUR
BOTW_TITLE_IDS = "00050000101C9400,00050000101C9500,00050000101C9300"

# Anchor everything to this script's folder so launching from a
# bcml:// link (different working directory) uses the same config/staging.
BASE_DIR = pathlib.Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
LOGS_DIR = BASE_DIR / "logs"
CONFIG_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)


def _migrate(name: str, dest_dir: pathlib.Path):
    """Move a file left in the main folder by an older Korok into its new home."""
    old, new = BASE_DIR / name, dest_dir / name
    if old.exists() and not new.exists():
        try:
            old.replace(new)
        except OSError:
            pass
    return new


CONFIG_FILE = _migrate("korok_config.json", CONFIG_DIR)
PACK_FOLDER_NAME = "KorokMods"
GAME_DIRS = {"content", "aoc"}
# Files inside a BNP's logs/ folder that don't need BCML-style merging.
HARMLESS_BNP_LOGS = {"rstb.json", "deletions.json", "packs.json"}
MERGE_LOGS_DIR = "_korok_logs"
RSTB_LOG_NAME = "_korok_rstb.json"  # per-mod RSTB sizes kept in staging
META_NAME = "_korok_meta.json"      # per-mod enabled flag / merge notes
BUILD_LOG = _migrate("korok_build.log", LOGS_DIR)
STATE_FILE = _migrate("korok_profiles.json", CONFIG_DIR)      # playlists + load order
LASTBUILD_FILE = _migrate("korok_lastbuild.json", CONFIG_DIR)  # signature of the last pack build
CACHE_DIR = BASE_DIR / "korok_cache"                # reusable merge results
BUILD_VERSION = "7"
UPDATE_TITLE_IDS = ("101c9400", "101c9500", "101c9300")


def register_bcml_protocol():
    """Registers 'bcml://' so GameBanana 1-click links open Korok.
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

    def __init__(self, staging_dir=None):
        self.staging_dir = pathlib.Path(staging_dir) if staging_dir else BASE_DIR / "korok_staging"
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.last_rstb_report = ""
        self.last_merge_report = ""
        self.last_build_summary = ""

        self.last_build_skipped = False
        self.last_imported_name = ""
        self.state = self._load_state()

        saved_path = self.load_config_path()
        self.cemu_dir = pathlib.Path(saved_path) if saved_path else pathlib.Path("C:/Emulators/Cemu")
        self.update_graphic_packs_dir()

    # ---------- config ----------
    def load_config_path(self) -> str:
        if CONFIG_FILE.exists():
            try:
                return json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("cemu_dir", "")
            except Exception:
                pass
        return ""

    def save_config_path(self, cemu_path: str):
        try:
            CONFIG_FILE.write_text(
                json.dumps({"cemu_dir": cemu_path}, indent=4), encoding="utf-8"
            )
        except Exception as e:
            print(f"Failed to save config: {e}")

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

        # Every place an older Korok version (or this one) may have written
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
name = "Korok Mods"
path = "The Legend of Zelda: Breath of the Wild/Mods/Korok Mods"
description = "Consolidated package containing all active Korok mods managed by Korok."
version = 7
default = true
"""

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
    def _is_ukmm_package(base: pathlib.Path) -> bool:
        """UKMM .zip packages: meta.yml + manifest.yml next to zstd/CBOR 'diff' files
        that are not game-ready.  (A UKMM *deployed* graphic pack has content/aoc
        folders and no manifest.yml.)"""
        try:
            for cur in (base, *[c for c in base.iterdir() if c.is_dir()]):
                if (cur / "manifest.yml").is_file() and (cur / "meta.yml").is_file():
                    return True
        except OSError:
            pass
        return False

    @staticmethod
    def _find_pack_root(base: pathlib.Path):
        """Shallowest folder that directly contains 'content' or 'aoc'."""
        queue = [base]
        while queue:
            cur = queue.pop(0)
            children = [c for c in cur.iterdir() if c.is_dir()]
            if any(c.name.lower() in GAME_DIRS for c in children):
                return cur
            queue.extend(children)
        return None

    def import_mod_archive(self, file_path: str, mod_display_name: str) -> list:
        """Stages a mod. Returns a list of warning strings."""
        archive_path = pathlib.Path(file_path)
        safe_name = self._safe_name(mod_display_name)
        staged_mod_path = self.staging_dir / safe_name
        warnings = []

        from_dir = archive_path.is_dir()
        temp_extract = pathlib.Path(tempfile.mkdtemp(prefix="korok_extract_"))
        try:
            if from_dir:
                scan = archive_path          # a folder: read it in place, never move its files
            else:
                self._extract_archive(archive_path, temp_extract)
                scan = temp_extract

            if self._is_ukmm_package(scan):
                raise RuntimeError(
                    "This is a UKMM package (it has meta.yml + manifest.yml). Its files are stored as "
                    "compressed UKMM diffs, not game files, so Korok can't use them directly.\n"
                    "Fix: install it in UKMM, deploy it to a folder (UKMM > Settings > Deploy > "
                    "output folder), then use Korok's 'Import Folder...' on that deployed folder.\n"
                    "Or download the BNP (BCML) version of the mod if it has one.")

            root = self._find_pack_root(scan)
            if root is None:
                raise RuntimeError(
                    "No 'content' or 'aoc' folder found in this archive. "
                    "It may be a Switch ('romfs') mod or an unsupported format."
                )

            # Prefer the mod's own name (BNP info.json / graphic pack rules.txt)
            nice = self._embedded_name(root)
            if nice:
                safe_name = self._safe_name(nice)
                staged_mod_path = self.staging_dir / safe_name
            self.last_imported_name = safe_name

            logs = root / "logs"
            needs_merge = []
            if logs.is_dir():
                supported = korok_merge.supported_logs()
                needs_merge = sorted(
                    f.name for f in logs.iterdir()
                    if f.name.lower() not in HARMLESS_BNP_LOGS
                    and f.name.lower() not in supported
                )
                wanted = [n for n in korok_merge.SUPPORTED_LOGS if (logs / n).exists()]
                if wanted and not korok_merge.available():
                    warnings.append(
                        f"'{safe_name}' needs the 'oead' package to apply its merge logs. "
                        "Run:  pip install oead   then re-import it.")
                elif "eventinfo.yml" in wanted and "eventinfo.yml" not in supported:
                    warnings.append(
                        f"'{safe_name}' needs PyYAML for its event table. Run:  pip install pyyaml  "
                        "then re-import it.")
                elif "texts.json" in wanted and "texts.json" not in supported:
                    warnings.append(
                        f"'{safe_name}' changes dialogue (texts.json). Run:  pip install pymsyt pyyaml  "
                        "then re-import it.")
                if needs_merge:
                    warnings.append(
                        f"'{safe_name}' is a BNP with merge logs ({', '.join(needs_merge[:5])}). "
                        "BNPs ship only the files a merger can't handle; BCML/UKMM rebuild the "
                        "rest from these logs, and Korok can't. It was imported DISABLED so it "
                        "can't hang your game - tick it in the list to try it anyway."
                    )

            was_known = staged_mod_path.exists()
            if staged_mod_path.exists():
                shutil.rmtree(staged_mod_path)
            staged_mod_path.mkdir(parents=True)

            for child in root.iterdir():
                if child.is_dir() and child.name.lower() in GAME_DIRS:
                    dest = staged_mod_path / child.name.lower()
                    if dest.exists():
                        self._merge_directory(child, dest)
                    elif from_dir:
                        shutil.copytree(child, dest)
                    else:
                        shutil.move(str(child), str(dest))

            if logs.is_dir():
                for n in korok_merge.SUPPORTED_LOGS | {"packs.json"}:
                    if (logs / n).exists():
                        (staged_mod_path / MERGE_LOGS_DIR).mkdir(exist_ok=True)
                        shutil.copy2(logs / n, staged_mod_path / MERGE_LOGS_DIR / n)

            self._write_meta(staged_mod_path, {"needs_merge": needs_merge})
            self._known_before = {safe_name} if was_known else set()
            self._register_mod(safe_name, enable=not needs_merge)
            self._known_before = set()

            entries = self._load_rstb_log(logs)
            if entries:
                (staged_mod_path / RSTB_LOG_NAME).write_text(json.dumps(entries), encoding="utf-8")
            elif not (staged_mod_path / "content" / "System" / "Resource"
                      / "ResourceSizeTable.product.srsizetable").exists():
                warnings.append(
                    f"'{safe_name}' has no RSTB data (logs/rstb.json). If it contains files "
                    "larger than the originals, the game may hang while loading."
                )
        finally:
            shutil.rmtree(temp_extract, ignore_errors=True)

        return warnings

    @staticmethod
    def _write_meta(mod_path: pathlib.Path, info: dict):
        (mod_path / META_NAME).write_text(json.dumps(info), encoding="utf-8")

    def get_mod_info(self, name: str) -> dict:
        """'enabled' reflects the ACTIVE playlist."""
        info = {"enabled": False, "needs_merge": []}
        f = self.staging_dir / name / META_NAME
        if f.exists():
            try:
                info.update(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                pass
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
        """Drops mods that no longer exist, appends new ones to every playlist."""
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
            print(f"Failed to save playlists: {e}")

    def _profile(self, name=None) -> dict:
        return self.state["profiles"][name or self.state["active"]]

    @property
    def active_profile(self) -> str:
        return self.state["active"]

    def profile_names(self) -> list:
        return list(self.state["profiles"].keys())

    def profile_mods(self, name=None) -> list:
        """Enabled mods of a playlist, in load order."""
        prof = self._profile(name)
        return [m for m in prof["order"] if m in prof["enabled"]]

    def ordered_mods(self) -> list:
        """All staged mods in the active playlist's load order."""
        return list(self._profile()["order"])

    def set_active_profile(self, name: str):
        if name in self.state["profiles"]:
            self.state["active"] = name
            self._save_state()

    def create_profile(self, name: str, copy_from=None) -> str:
        name = name.strip()
        if not name:
            raise ValueError("Playlist name can't be empty.")
        if name in self.state["profiles"]:
            raise ValueError(f"A playlist named '{name}' already exists.")
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
            raise ValueError("You need at least one playlist.")
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
        """Removes a mod from the library and every playlist. No rebuild."""
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
        if not enable and name in prof["enabled"]:
            prof["enabled"].remove(name)
        self._save_state()

    @staticmethod
    def _load_rstb_log(logs: pathlib.Path) -> dict:
        """Reads BCML's precomputed RSTB sizes: {canonical resource path: size}."""
        out = {}
        j = logs / "rstb.json"
        if j.exists():
            try:
                data = json.loads(j.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                            out[k.replace("\\", "/")] = v
            except Exception:
                pass
        legacy = logs / "rstb.log"  # old BCML: "name,size" lines
        if not out and legacy.exists():
            for line in legacy.read_text(encoding="utf-8", errors="ignore").splitlines():
                name, _, size = line.rpartition(",")
                if name and size.strip().isdigit():
                    out[name.strip()] = int(size)
        return out

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
    # ---------- change detection ----------
    @staticmethod
    def _mod_fingerprint(mod_dir: pathlib.Path) -> str:
        h = hashlib.sha1()
        for root, dirs, files in os.walk(mod_dir):
            dirs.sort()
            for f in sorted(files):
                p = pathlib.Path(root) / f
                try:
                    st = p.stat()
                except OSError:
                    continue
                h.update(f"{p.relative_to(mod_dir)}|{st.st_size}|{st.st_mtime_ns}\n".encode())
        return h.hexdigest()

    def build_signature(self) -> str:
        h = hashlib.sha1(f"{BUILD_VERSION}|{self.graphic_packs_dir}".encode())
        for f in (pathlib.Path(__file__), pathlib.Path(korok_merge.__file__), pathlib.Path(korok_mergers.__file__)):
            try:
                st = f.stat()
                h.update(f"{f.name}|{st.st_size}|{st.st_mtime_ns}".encode())
            except OSError:
                pass
        for name in self.profile_mods():
            d = self.staging_dir / name
            if d.is_dir():
                h.update(f"{name}|{self._mod_fingerprint(d)}".encode())
        return h.hexdigest()

    def pending_changes(self) -> bool:
        """True when the pack on disk doesn't match the active playlist."""
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
        """Rebuilds the unified graphic pack from the active playlist.
        Skips the work when nothing changed (unless force).
        progress_cb(fraction 0..1, message) is optional and may be called
        from a worker thread."""
        cb = progress_cb or (lambda frac, text: None)
        if not self.cemu_dir.exists():
            return
        self.last_build_skipped = False
        sig = self.build_signature()
        if not force and (self.graphic_packs_dir / "rules.txt").exists():
            try:
                if json.loads(LASTBUILD_FILE.read_text(encoding="utf-8")).get("sig") == sig:
                    self.last_build_skipped = True
                    self.last_rstb_report = self.last_merge_report = ""
                    self.last_build_summary = (
                        "The pack is already up to date with this playlist - nothing was rebuilt.\n"
                        "In the pack: " + (", ".join(self.profile_mods()) or "nothing"))
                    cb(1.0, "Already up to date")
                    return
            except Exception:
                pass
        cb(0.0, "Preparing pack...")
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        korok_merge.CACHE_DIR = CACHE_DIR
        korok_merge.prune_cache()

        # Remove the old location so Cemu doesn't load two Korok packs.
        for legacy in self.legacy_pack_dirs:
            if legacy.name == PACK_FOLDER_NAME and legacy.exists():
                shutil.rmtree(legacy, ignore_errors=True)

        if self.graphic_packs_dir.name == PACK_FOLDER_NAME and self.graphic_packs_dir.exists():
            shutil.rmtree(self.graphic_packs_dir)
        self.graphic_packs_dir.mkdir(parents=True, exist_ok=True)

        (self.graphic_packs_dir / "rules.txt").write_text(
            self.generate_rules_txt(), encoding="utf-8"
        )

        # Sorted for a stable load order (later mods overwrite earlier ones).
        # Only content/ and aoc/ are merged, never bookkeeping files.
        enabled_names = self.profile_mods()
        mods = [self.staging_dir / n for n in enabled_names if (self.staging_dir / n).is_dir()]
        self._build_mods = [m.name for m in mods]
        skipped = [n for n in self.get_staged_mods() if n not in enabled_names]
        self.last_build_summary = (
            f"Playlist: {self.active_profile}\n"
            "In the pack: " + (", ".join(m.name for m in mods) or "nothing (pack only has rules.txt)")
        )
        steps = len(mods) + 2  # +1 merge logs, +1 RSTB
        for i, mod_dir in enumerate(mods):
            cb(i / steps, f"Merging {mod_dir.name} ({i + 1}/{len(mods)})")
            for sub in mod_dir.iterdir():
                if sub.is_dir() and sub.name.lower() in GAME_DIRS:
                    target = self.graphic_packs_dir / sub.name.lower()
                    target.mkdir(parents=True, exist_ok=True)
                    self._merge_directory(sub, target)

        # Apply aslist/deepmerge logs from BNPs onto the shipped/vanilla files.
        log_dirs = [m / MERGE_LOGS_DIR for m in mods if (m / MERGE_LOGS_DIR).is_dir()]
        self.last_merge_report = ""
        pack_report = ""
        if log_dirs and korok_merge.available():
            korok_merge.LOG_FILE = LOGS_DIR / "korok_merge.log"
            try:
                korok_merge.LOG_FILE.write_text("", encoding="utf-8")
            except Exception:
                pass
            # BNPs ship partial packs; rebuild them as vanilla + shipped files
            mod_packs = []
            for m in mods:
                pj = m / MERGE_LOGS_DIR / "packs.json"
                if pj.exists():
                    try:
                        rels = [v.replace("\\", "/") for v in json.loads(pj.read_text(encoding="utf-8")).values()]
                        mod_packs.append((m, rels))
                    except Exception:
                        pass
            if mod_packs:
                cb(len(mods) / steps, "Merging shipped packs with the vanilla game files...")
                pn, pprob = korok_merge.merge_partial_packs(
                    mod_packs, self.graphic_packs_dir, self._vanilla_roots(),
                    progress=lambda f, t: cb(len(mods) / steps, t))
                pack_report = f"Packs merged with vanilla: {pn}." + (
                    "\nPack problems:\n  " + "\n  ".join(pprob[:8]) if pprob else "")
        self.rstb_delete_names = []
        self.rstb_set_sizes = {}
        if log_dirs and korok_merge.available():
            if korok_merge.LOG_FILE is None:
                korok_merge.LOG_FILE = LOGS_DIR / "korok_merge.log"
            cb(len(mods) / steps, "Merging actor info / game data / drop tables...")
            try:
                lines, mprob = korok_mergers.apply_merge_logs(
                    log_dirs, self.graphic_packs_dir, self._vanilla_roots(),
                    progress=lambda f, t: cb(len(mods) / steps, t))
            except Exception as ex:
                lines, mprob = [], [f"merge logs: {ex}"]
            if lines or mprob:
                pack_report = (pack_report + "\n" if pack_report else "") + "\n".join(lines) + (
                    "\nMerge problems:\n  " + "\n  ".join(mprob[:8]) if mprob else "")
            self.rstb_delete_names.extend(korok_mergers.RSTB_DELETE)
            self.rstb_set_sizes = dict(korok_mergers.RSTB_SET)
        self.text_langs_edited = []
        text_logs = [d / "texts.json" for d in log_dirs if (d / "texts.json").exists()]
        text_report = ""
        if text_logs and korok_merge.available():
            if korok_merge.LOG_FILE is None:
                korok_merge.LOG_FILE = LOGS_DIR / "korok_merge.log"
            cb(len(mods) / steps, "Applying dialogue (texts.json)...")
            langs, tprob = korok_merge.apply_texts(
                text_logs, self.graphic_packs_dir, self._vanilla_roots(),
                progress=lambda f, t: cb(len(mods) / steps, t))
            self.text_langs_edited = langs
            text_report = ("Dialogue applied to: " + (", ".join(langs) or "nothing")) + (
                "\nText problems:\n  " + "\n  ".join(tprob[:6]) if tprob else "")
            pack_report = (pack_report + "\n" if pack_report else "") + text_report
        self.eventinfo_applied = False
        ev_logs = [d / "eventinfo.yml" for d in log_dirs if (d / "eventinfo.yml").exists()]
        if ev_logs and korok_merge.available():
            if korok_merge.LOG_FILE is None:
                korok_merge.LOG_FILE = LOGS_DIR / "korok_merge.log"
            cb(len(mods) / steps, "Applying cutscene/event table (eventinfo.yml)...")
            en, eprob = korok_merge.apply_eventinfo(ev_logs, self.graphic_packs_dir, self._vanilla_roots())
            self.eventinfo_applied = en > 0
            pack_report = (pack_report + "\n" if pack_report else "") + (
                f"Event table: {en} entries applied." + (
                    "\nEvent problems:\n  " + "\n  ".join(eprob) if eprob else ""))
        if log_dirs:
            cb(len(mods) / steps, "Applying BNP merge logs...")
            n, problems = korok_merge.apply_logs(
                log_dirs, self.graphic_packs_dir, self._vanilla_roots(),
                progress=lambda f, t: cb((len(mods) + f) / steps, "Applying merge logs: " + t),
            )
            self.last_merge_report = (pack_report + "\n" if pack_report else "") + f"Merge logs: {n} file edit(s) applied." + (
                "\nMerge problems:\n  " + "\n  ".join(problems[:8]) if problems else "")

        cb((len(mods) + 1) / steps, "Patching resource size table (RSTB)...")
        try:
            self.last_rstb_report = self._patch_rstb()
        except Exception as e:
            self.last_rstb_report = f"RSTB patch failed: {e}"
        self._write_build_log(mods, skipped)
        self._remember_build(sig)
        cb(1.0, "Done")

    def _write_build_log(self, merged: list, skipped: list):
        try:
            lines = [
                f"Build at {datetime.now():%Y-%m-%d %H:%M:%S}",
                f"Pack: {self.graphic_packs_dir}",
                f"Merged ({len(merged)}): " + (", ".join(m.name for m in merged) or "-"),
                f"Skipped/disabled ({len(skipped)}): " + (", ".join(skipped) or "-"),
                self.last_rstb_report,
            getattr(self, "last_merge_report", ""),
            ]
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

    def _find_vanilla_rstb(self):
        for tid in UPDATE_TITLE_IDS:
            p = (self.user_dir / "mlc01" / "usr" / "title" / "0005000e" / tid
                 / "content" / "System" / "Resource" / "ResourceSizeTable.product.srsizetable")
            if p.exists():
                return p
        return None

    def _patch_rstb(self) -> str:
        """Patches the *vanilla* RSTB with the sizes the mods need and writes
        it into the pack (never builds a table from scratch)."""
        entries = {}
        for mod_name in getattr(self, "_build_mods", self.profile_mods()):
            mod_dir = self.staging_dir / mod_name
            f = mod_dir / RSTB_LOG_NAME
            if mod_dir.is_dir() and f.exists():
                try:
                    for k, v in json.loads(f.read_text(encoding="utf-8")).items():
                        # some BNPs write Windows-style paths; the RSTB hashes forward-slash names
                        k = k.replace("\\", "/")
                        entries[k] = max(entries.get(k, 0), int(v))
                except Exception:
                    pass
        # Mods that ship a whole ResourceSizeTable (graphic packs deployed by UKMM, many
        # older mods): keep the entries that are bigger than vanilla's.
        shipped_tables = []
        for mod_name in getattr(self, "_build_mods", self.profile_mods()):
            p = (self.staging_dir / mod_name / "content" / "System" / "Resource"
                 / "ResourceSizeTable.product.srsizetable")
            if p.is_file():
                try:
                    shipped_tables.append((mod_name, korok_rstb.SizeTable.from_file(p)))
                except Exception:
                    pass
        if (not entries and not shipped_tables and not getattr(self, "text_langs_edited", [])
                and not getattr(self, "rstb_delete_names", [])
                and not getattr(self, "rstb_set_sizes", {})
                and not getattr(self, "eventinfo_applied", False)):
            return "No RSTB entries to apply."

        vanilla = self._find_vanilla_rstb()
        if vanilla is None:
            return (f"RSTB not patched: vanilla ResourceSizeTable not found under "
                    f"{self.user_dir / 'mlc01'} (is the v208 update installed there?)")

        table = korok_rstb.SizeTable.from_file(vanilla)
        added = updated = deleted = 0
        for _mn, st in shipped_tables:
            a, u = table.merge_larger(st)
            added += a
            updated += u
        for name, size in entries.items():
            # BCML convention: size 0 = "size can't be calculated, remove the
            # entry" so the game sizes the resource itself. Keeping the vanilla
            # (smaller) entry for a changed file under-allocates it.
            r = table.delete(name) if size == 0 else table.raise_size(name, size)
            added += r == "added"
            updated += r == "updated"
            deleted += r == "deleted"

        if getattr(self, "eventinfo_applied", False):
            if table.delete("Event/EventInfo.product.byml") == "deleted":
                deleted += 1
        for nm, size in getattr(self, "rstb_set_sizes", {}).items():
            # files rebuilt by a merge log: store the size BCML would calculate for them
            r = table.raise_size(nm, size)
            added += r == "added"
            updated += r == "updated"
        for nm in getattr(self, "rstb_delete_names", []):
            # rebuilt by a merge log: the vanilla size is too small, let the game size it
            if table.delete(nm) == "deleted":
                deleted += 1
        for lang in getattr(self, "text_langs_edited", []):
            # edited text archives grow; let the game size them itself
            if table.delete(f"Message/Msg_{lang}.product.sarc") == "deleted":
                deleted += 1

        out_dir = self.graphic_packs_dir / "content" / "System" / "Resource"
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / "ResourceSizeTable.product.srsizetable"
        if target.exists():
            target.unlink()  # may be a hard link to a staged mod's file
        table.save(target)
        return (f"RSTB patched: {updated} entries enlarged, {added} added, "
                f"{deleted} removed (from {len(entries)} mod entries).")

    @staticmethod
    def _link_or_copy(src: pathlib.Path, dst: pathlib.Path):
        """Hard-link when possible (instant, no extra disk); every later write
        to the pack goes through a temp file + rename, so staged originals are
        never modified."""
        try:
            if dst.exists() or dst.is_symlink():
                dst.unlink()
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)

    def _merge_directory(self, src: pathlib.Path, dst: pathlib.Path):
        for item in src.iterdir():
            target_path = dst / item.name
            if item.is_dir():
                target_path.mkdir(parents=True, exist_ok=True)
                self._merge_directory(item, target_path)
            else:
                self._link_or_copy(item, target_path)

    def get_staged_mods(self) -> list:
        if not self.staging_dir.exists():
            return []
        return sorted(
            d.name for d in self.staging_dir.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )
