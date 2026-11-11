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
        if not BACKUPS_DIR.exists():
            return
        dirs = sorted([d for d in BACKUPS_DIR.iterdir() if d.is_dir()], key=lambda d: d.name)
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

    def set_notes(self, iid, notes):
        self.data["instances"][iid]["notes"] = notes
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
        pl = self.data["instances"][iid].get("playlist")
        if apply_playlist and pl and pl in self.engine.profile_names():
            self.engine.set_active_profile(pl)
            notes.append(f'Playlist set to "{pl}".')
        return " ".join(notes) or "Switched."

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
