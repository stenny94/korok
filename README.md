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

- Import mods from `.zip`, `.7z`, `.bnp` or a plain folder
- Import UKMM mod packages without UKMM: Korok reads UKMM's package format itself and rebuilds
  the game files from your Cemu game files. This is a port of UKMM's own code (all of its file
  types), checked file by file against UKMM's output. Mods with install options show a window to
  pick them. Needs `cbor2` and `zstandard` (in requirements.txt).
- Archives with several option folders (hair styles, colours, ...) are imported as one main
  mod plus a disabled mod per option, so you can pick the ones you want
- Merges BCML-style (`.bnp`) mods: actor info, game data, save data, area data,
  drop tables, shops, quests, map units, dialogue, event info and more
- Load order and mod playlists
- **Save manager (instances):** each instance has its own save file and, optionally, its own
  mod playlist. Switching swaps the save Cemu sees and applies the playlist for you. Korok backs
  up your live save before every switch and can restore from those backups.
- **Save editor:** view and change rupees, inventory items, and individual game flags in a save
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

On first start, point Korok at your Cemu folder if it asks. Then use **Import** to
add a mod, enable it, and let Korok build the graphic pack. Start Cemu and make sure the
**KorokMods** graphic pack is enabled.

## Saves

- Close Cemu before switching instances or editing a save.
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
| [Second Wind: The Ancient Trials](https://gamebanana.com/projects/35468) | CEObrainz | Does not work yet. The game gets stuck on a black loading screen when starting a new save (BNP version), UKMM version is now read by Korok itself (needs retesting) |

## Troubleshooting

- **`python` is not recognised**: reinstall Python with *Add to PATH* ticked, or use `py main.py`.
- **A build fails or the game crashes**: send the log file `korok_merge.log` from the Korok
  logs folder when asking for help.

## Credits

Korok builds on the work of others:

- **[BCML](https://github.com/NiceneNerd/BCML)** by Nicene Nerd (GPL-3.0+). The merge logic for
  BNP mods is ported from BCML's mergers.
- **[UKMM](https://github.com/NiceneNerd/ukmm)** by Nicene Nerd, for the reference on how the
  mod formats and deploy layouts work.
- **[oead](https://github.com/zeldamods/oead)** by the Zelda Modding community, for reading and
  writing BYML, AAMP, SARC and Yaz0 files.
- **[rstb](https://github.com/zeldamods/rstb)**, the Resource Size Table format and size formulas.
- **[msyt](https://github.com/NiceneNerd/msyt)** (via `pymsyt`), for editing game text.
- **[PyQt6](https://www.riverbankcomputing.com/software/pyqt/)** for the interface,
  **[PyYAML](https://pyyaml.org/)** and **[py7zr](https://github.com/miurahr/py7zr)**.
- **[Cemu](https://cemu.info/)** and the BotW modding community.

*The Legend of Zelda: Breath of the Wild* is a trademark of Nintendo. Korok is an unofficial
fan project and is not affiliated with or endorsed by Nintendo.

## License

Korok is released under the **GNU General Public License v3.0**. See [`LICENSE`](LICENSE).
