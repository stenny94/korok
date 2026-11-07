"""Converts UKMM mod packages into plain game files by running UKMM itself.

UKMM packages (.zip with meta.yml + manifest.yml) don't hold game files. Each
file is a zstd-compressed, CBOR-encoded UKMM resource that only UKMM's own code
can turn back into a game file. Rewriting that in Python would mean porting
most of UKMM, so Korok drives UKMM's command line instead:

  1. Copy ukmm.exe into a private folder (korok_ukmm/) and run it with
     --portable, so it uses settings in that folder and never touches the
     user's own UKMM profiles, mods or deploy folder.
  2. Write a settings.yml there, built from the user's UKMM settings (that's
     where UKMM keeps the game dump paths) with storage, profile and deploy
     output redirected into korok_ukmm/.
  3. `ukmm --portable --deploy install <mod>` installs the mod into the empty
     private profile and deploys it to korok_ukmm/deploy/{content,aoc/0010}.

Korok then imports that deployed folder like any other graphic pack.

Needs: UKMM installed and set up once for Wii U (so its settings know where
the game dump is). Mods with install options can't be installed from UKMM's
command line; UKMM refuses them and the error is passed on.
"""
import os
import pathlib
import shutil
import subprocess
import zipfile

import yaml

BASE_DIR = pathlib.Path(__file__).resolve().parent
WORK_DIR = BASE_DIR / "korok_ukmm"
EXE_NAMES = ("ukmm.exe", "ukmm")


class UkmmError(RuntimeError):
    pass


# --------------------------------------------------------------- detection
def is_ukmm_archive(path) -> bool:
    """True for a UKMM package (.zip with meta.yml + manifest.yml at the top)."""
    p = pathlib.Path(path)
    if p.is_dir():
        return (p / "meta.yml").is_file() and (p / "manifest.yml").is_file()
    try:
        with zipfile.ZipFile(p) as z:
            names = {n.rstrip("/").lower() for n in z.namelist()}
    except (zipfile.BadZipFile, OSError):
        return False
    return "meta.yml" in names and "manifest.yml" in names


def package_name(path):
    """The 'name' from a UKMM package's meta.yml, or None."""
    try:
        p = pathlib.Path(path)
        if p.is_dir():
            text = (p / "meta.yml").read_text(encoding="utf-8-sig")
        else:
            with zipfile.ZipFile(p) as z:
                n = next(n for n in z.namelist() if n.rstrip("/").lower() == "meta.yml")
                text = z.read(n).decode("utf-8-sig", "replace")
        n = (_load_yaml(text) or {}).get("name")
        return str(n).strip() if n else None
    except Exception:
        return None


def find_ukmm_exe(saved=""):
    """The saved path if it still exists, else ukmm on PATH, else common install spots."""
    if saved and pathlib.Path(saved).is_file():
        return pathlib.Path(saved)
    for n in EXE_NAMES:
        w = shutil.which(n)
        if w:
            return pathlib.Path(w)
    spots = []
    for env in ("LOCALAPPDATA", "PROGRAMFILES", "USERPROFILE"):
        v = os.environ.get(env)
        if v:
            spots += [pathlib.Path(v) / "ukmm" / "ukmm.exe",
                      pathlib.Path(v) / "UKMM" / "ukmm.exe",
                      pathlib.Path(v) / "Downloads" / "ukmm.exe",
                      pathlib.Path(v) / "Desktop" / "ukmm.exe"]
    return next((s for s in spots if s.is_file()), None)


def _user_settings_path(exe: pathlib.Path):
    """Where the user's UKMM keeps settings.yml: next to the exe when they run it
    portable, else %APPDATA%/ukmm (Linux: ~/.config/ukmm)."""
    candidates = [exe.parent / "config" / "settings.yml"]
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(pathlib.Path(appdata) / "ukmm" / "settings.yml")
    candidates.append(pathlib.Path.home() / ".config" / "ukmm" / "settings.yml")
    return next((c for c in candidates if c.is_file()), None)


# ----------------------------------------- YAML that keeps UKMM's !Tags intact
class _Tagged:
    """A YAML node with a custom tag (serde_yaml writes enum variants as !Variant)."""

    def __init__(self, tag, value):
        self.tag, self.value = tag, value


class _Loader(yaml.SafeLoader):
    pass


class _Dumper(yaml.SafeDumper):
    pass


def _construct_tagged(loader, tag_suffix, node):
    if isinstance(node, yaml.MappingNode):
        value = loader.construct_mapping(node, deep=True)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_scalar(node)
    return _Tagged("!" + tag_suffix, value)


def _represent_tagged(dumper, data):
    if isinstance(data.value, dict):
        return dumper.represent_mapping(data.tag, data.value)
    if isinstance(data.value, list):
        return dumper.represent_sequence(data.tag, data.value)
    return dumper.represent_scalar(data.tag, str(data.value))


_Loader.add_multi_constructor("!", _construct_tagged)
_Dumper.add_representer(_Tagged, _represent_tagged)


def _load_yaml(text):
    return yaml.load(text, Loader=_Loader)


def _dump_yaml(data) -> str:
    return yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True)


def _private_settings(user_settings: dict, work: pathlib.Path) -> dict:
    wiiu = user_settings.get("wiiu_config")
    if not isinstance(wiiu, dict) or not wiiu.get("dump"):
        raise UkmmError(
            "UKMM isn't set up for Wii U yet. Open UKMM once, switch it to Wii U mode and set "
            "your game dump folders (Settings > Wii U), then import this mod again.")
    s = dict(user_settings)
    s["current_mode"] = "WiiU"
    s["storage_dir"] = str(work / "data")
    s["check_updates"] = "None"
    s["show_changelog"] = False
    w = dict(wiiu)
    w["profile"] = "Korok"
    w["deploy_config"] = {
        "output": str(work / "deploy"),
        "method": "Copy",
        "auto": False,
        "cemu_rules": False,
        "executable": None,
        "layout": "WithoutName",
    }
    s["wiiu_config"] = w
    return s


# ---------------------------------------------------------------- convert
def convert(package, ukmm_exe, log=None) -> pathlib.Path:
    """Installs + deploys `package` with a private UKMM. Returns the deploy
    folder (contains content/ and/or aoc/0010/). Raises UkmmError."""
    package = pathlib.Path(package).resolve()
    exe = pathlib.Path(ukmm_exe) if ukmm_exe else None
    if exe is None or not exe.is_file():
        raise UkmmError(
            "This is a UKMM package. Korok converts these with UKMM itself, but ukmm.exe "
            "wasn't found. Download UKMM (github.com/NiceneNerd/ukmm/releases), run it once "
            "and set it up for Wii U, then import the mod again and point Korok at ukmm.exe.")
    settings_file = _user_settings_path(exe)
    if settings_file is None:
        raise UkmmError(
            "UKMM's settings weren't found. Open UKMM once, set it up for Wii U (game dump "
            "folders), close it, then import this mod again.")
    try:
        user_settings = _load_yaml(settings_file.read_text(encoding="utf-8")) or {}
    except Exception as e:
        raise UkmmError(f"Couldn't read UKMM's settings ({settings_file}): {e}")

    # Fresh private UKMM every time, so the deploy only holds this one mod.
    work = WORK_DIR
    for sub in ("data", "deploy", "config"):
        shutil.rmtree(work / sub, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    (work / "config").mkdir()
    private_exe = work / exe.name
    try:
        if not private_exe.exists() or private_exe.stat().st_size != exe.stat().st_size \
                or private_exe.stat().st_mtime_ns < exe.stat().st_mtime_ns:
            if private_exe.exists():
                private_exe.unlink()
            shutil.copy2(exe, private_exe)
    except OSError as e:
        raise UkmmError(f"Couldn't copy ukmm.exe into {work}: {e}")
    (work / "config" / "settings.yml").write_text(
        _dump_yaml(_private_settings(user_settings, work)), encoding="utf-8")

    cmd = [str(private_exe), "--portable", "--deploy", "install", str(package)]
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           stdin=subprocess.DEVNULL, cwd=str(work),
                           creationflags=flags, timeout=3600)
    except subprocess.TimeoutExpired:
        raise UkmmError("UKMM took longer than an hour to install the mod and was stopped.")
    except OSError as e:
        raise UkmmError(f"Couldn't start UKMM: {e}")
    out = (r.stdout + "\n" + r.stderr).strip()
    if log:
        log(f"ukmm {' '.join(cmd[1:])}\nexit {r.returncode}\n{out}")

    deploy = work / "deploy"
    has_files = any((deploy / d).is_dir() and any((deploy / d).iterdir())
                    for d in ("content", "aoc"))
    if r.returncode != 0 or not has_files:
        tail = "\n".join(out.splitlines()[-12:]) or "(no output)"
        hint = ""
        if "configuration options" in out:
            hint = ("\nThis mod has install options, and UKMM only installs those from its own "
                    "window. Install it in UKMM, deploy it, and use Korok's 'Import Folder...' "
                    "on UKMM's deploy folder instead.")
        raise UkmmError(f"UKMM couldn't convert this mod (exit code {r.returncode}).\n{tail}{hint}")
    return deploy
