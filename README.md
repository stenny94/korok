# korok

> **THIS IS A FULLY VIBE-CODED APPLICATION. USE AT YOUR OWN RISK.**
> If something doesn't work, ask an AI to fix it.

A mod manager, save manager and save editor for **The Legend of Zelda: Breath of the Wild**
on **Cemu**. Korok imports mods, merges them together, and writes the result into a
Cemu graphic pack so they work together without conflicts. It also keeps separate save files
for different playthroughs and lets you edit them.

This was made as a stand in for BCML and UKMM because I didn't like how complicated they were to setup and use. If something didn't work, I didn't know why. That's why this was created, it is meant to just "work".

> Korok does not include any game files or mods. You need your own legally
> obtained copy of the game, set up in Cemu.

## Features

- Import mods from `.bnp`, `.zip`, `.7z` or an unpacked folder
- **UKMM packages without UKMM:** Korok reads UKMM's package format itself and rebuilds the game
  files from your Cemu game files. This is a port of UKMM's own code (all of its file types),
  checked file by file against UKMM's output.
- **Install options:** mods that come with options (UKMM packages and BNPs) show a window where you
  pick them, with the mod's defaults already selected
- Archives with several mod folders but no option list (hair styles, colours, ...) are imported as
  one main mod plus a switched-off mod per folder, so you can tick the ones you want
- **Archives several mods change** (like `TitleBG.pack`, `Bootup.pack` or an actor pack) are merged
  file by file, so each mod keeps its own changes inside them; the load order only decides when
  two mods change the same file
- Merges BCML-style (`.bnp`) mods: actor info, game data, save data, area data,
  drop tables, shops, quests, map units, dialogue, event info and more
- **Profiles:** named sets of mods with their own on/off choices and load order
- **Save Manager:** keep several save slots, each with its own save file and, optionally, its own
  profile. Switching swaps the save Cemu sees and applies the profile for you. Korok backs up your
  live save before every switch and can restore from those backups.
- **Save Editor:** view and change rupees, inventory items, and individual game flags in a save
  file, with a backup made before every write
- Launch Cemu and BotW straight from Korok
- 1-click install links (**WIP**)

## Requirements

- Windows 10 or 11
- [Cemu](https://cemu.info/) 2.x with Breath of the Wild (v208 update and DLC installed)
- Python 3.10 or newer

## Installing Python

1. Download the latest Python for Windows from <https://www.python.org/downloads/>.
2. Run the installer and **tick "Add python.exe to PATH"** on the first page, then click *Install Now*.
3. Open a new Command Prompt or PowerShell window and check it worked:

   ```
   python --version
   ```

## Installing Korok

1. Download this repository (green **Code** button, then **Download ZIP**) and extract it,
   or clone it:

   ```
   git clone https://github.com/stenny94/korok.git
   ```

2. Open a terminal in the Korok folder and install the requirements:

   ```
   cd korok
   pip install -r requirements.txt
   ```

## Running

> It is highly advised to backup your save data before running this program.
> To do that, right click on BotW in Cemu, open "Save directory", and copy those folders elsewhere.

From the Korok folder:

```
python main.py
```

On first start Korok opens the **Settings** tab: set your Cemu folder there (Cemu.exe and the game
are filled in automatically when Korok finds them). Then:

1. **Import** tab: import your mods. Each one is added to your library and to the active profile.
2. **Mods** tab: tick the mods you want and set their order (lower in the list wins when two mods
   change the same file).
3. **Import** tab: press **Apply to Cemu**. Korok builds the **Korok Mods** graphic pack.
4. Start Cemu and make sure the **Korok Mods** graphic pack is ticked (Options → Graphic packs).

## The tabs

| Tab | What it's for |
| --- | --- |
| **Mods** | Your profiles (left) and the mods in the active profile (right): turn mods on and off, change the load order, delete mods from your library. |
| **Import** | Import mods, **Apply to Cemu** (rebuild the Korok Mods pack from the active profile) and **Force full rebuild** (rebuild even if nothing changed). A dot on the tab means there are changes that haven't been applied yet. |
| **Save Manager** | Your save slots: switch, create, rename, restore backups, and pick which profile each slot uses. |
| **Save Editor** | Edit rupees, items and game flags in a save. |
| **Settings** | Your Cemu folder, Cemu.exe and the game (a .rpx / .wua file or the game's folder, used by **Launch BotW**), and whether switching profiles applies them to Cemu straight away. |

**Launch Cemu** and **Launch BotW** sit above the tabs.

## Terms

- **Library:** every mod you have imported.
- **Profile:** a named set of mods from your library, with its own on/off choices and load order.
  (Older versions of Korok called these *playlists*.) Only the active profile is built into the
  pack.
- **Save slot:** a save file kept by the Save Manager, optionally tied to a profile. (Older
  versions called these *instances*.)
- **Korok Mods:** the single Cemu graphic pack Korok builds. Korok never turns your other graphic
  packs on or off.

## Saves

- Close Cemu before switching save slots or editing a save.
- Korok backs up the save before every switch and every edit (in the `korok_backups` folder).
  The save editor has had limited testing against real saves, so keep a copy of your own too.

## Compatibility

Mods I have tested with Korok. This is a small list, so a mod not being here doesn't mean it won't work.

### Working

| Mod | Author | Notes |
| --- | --- | --- |
| [The Linkle Mod 3.0](https://gamebanana.com/mods/406768) | lynard killer | Works |
| [Unofficial Linkle 3.0 Armor Icons](https://gamebanana.com/mods/408713) | Benna96 | Works |
| [The Legend of Linkle Dialogue Mod](https://gamebanana.com/mods/439900) | TheFork | Works |
| Zelda's Ballad | | Worked, but Zelda had no hair unless she wore a headdress. Korok now removes the RSTB entries of files a mod replaces so bigger models load. Needs retesting |

### Not working

| Mod | Author | Notes |
| --- | --- | --- |
| [Second Wind: The Ancient Trials](https://gamebanana.com/projects/35468) | CEObrainz | BNP version: the game gets stuck on a black loading screen when starting a new save. UKMM version now imports with Korok's own reader, including its install options (needs retesting in game) |

## Troubleshooting

- **`python` is not recognised**: reinstall Python with *Add to PATH* ticked, or use `py main.py`.
- **A build fails or the game crashes**: send `korok_build.log` and `korok_merge.log` from the
  Korok `logs` folder when asking for help (and `korok_ukmm.log` for UKMM packages).
- **`pip install -r requirements.txt` fails on one package**: install the rest by name, e.g.
  `pip install cbor2 zstandard pymsyt`.

## Credits

Korok builds on the work of others:

- **[BCML](https://github.com/NiceneNerd/BCML)** by Nicene Nerd (GPL-3.0+). The merge logic for
  BNP mods is ported from BCML's mergers.
- **[UKMM](https://github.com/NiceneNerd/ukmm)** by Nicene Nerd (GPL-3.0). Korok's UKMM package
  reader is a port of UKMM's code, and `data/ukmm_zsdic` and `data/ukmm_filemap_wiiu.json.zst`
  are UKMM's own data files. BNP install options follow BCML's behaviour.
- **[oead](https://github.com/zeldamods/oead)** by the Zelda Modding community, for reading and
  writing BYML, AAMP, SARC and Yaz0 files.
- **[rstb](https://github.com/zeldamods/rstb)**, the Resource Size Table format and size formulas.
- **[msyt](https://github.com/NiceneNerd/msyt)** (via `pymsyt`), for editing game text.
- **[PyQt6](https://www.riverbankcomputing.com/software/pyqt/)** for the interface,
  **[PyYAML](https://pyyaml.org/)**, **[py7zr](https://github.com/miurahr/py7zr)**,
  **[cbor2](https://github.com/agronholm/cbor2)** and
  **[zstandard](https://github.com/indygreg/python-zstandard)**.
- **[Cemu](https://cemu.info/)** and the BotW modding community.

*The Legend of Zelda: Breath of the Wild* is a trademark of Nintendo. Korok is an unofficial
fan project and is not affiliated with or endorsed by Nintendo.

## License

Korok is released under the **GNU General Public License v3.0**. See [`LICENSE`](LICENSE).
