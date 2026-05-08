# BSA Conflict Viewer

BSA Conflict Viewer is a Mod Organizer 2 Python tool plugin for Skyrim-style
games. It shows overwrite chains involving files inside BSA/BA2 archives and
loose files, without extracting archives or changing your mod setup.

MO2 already shows loose-file conflicts well, but files inside archives are harder
to inspect because the game can load them at startup. This plugin indexes archive
contents and enabled loose files, then shows which provider wins and which
providers are overwritten.

## Features

- Scans active BSA/BA2 archive indexes without extraction.
- Scans loose files from enabled mods, Overwrite, and game Data.
- Shows full overwrite chains for archive-only, loose-only, and mixed conflicts.
- Includes BSA versus loose-file cases, in both directions.
- Uses MO2 load/mod order so winners match the active profile order.
- Shows mods with archives in the left panel, with archives expandable under each mod.
- Lets you select a mod or a specific archive and view only its files.
- Shows winning conflicts, losing conflicts, and files without conflicts.
- Filters by partial mod name, partial file path, and asset category.
- Sorts the mod panel by name or visible file count.
- Exports the selected mod conflict view to CSV.
- Shows scan progress and warnings when archives cannot be read.

## What It Does Not Do

BSA Conflict Viewer is read-only. It does not delete files, extract archives,
repack archives, or clean mods.

Cleanup features live in the separate CleanUp plugin:

[https://github.com/wallhead/CleanUp](https://github.com/wallhead/CleanUp)

## Install

1. Download or clone this repository.
2. Copy the `bsa_conflict_viewer` folder into your MO2 `plugins` directory.
3. Restart MO2.
4. Open `BSA Conflict Viewer` from the MO2 tools menu.

Example PowerShell command:

```powershell
Copy-Item -Recurse -Force ".\bsa_conflict_viewer" "D:\TES VV\MO2\plugins\bsa_conflict_viewer"
```

## Requirements

- Mod Organizer 2 with Python plugin support.
- PyQt6 as provided by MO2's Python plugin environment.
- A game/plugin setup that exposes MO2 DataArchives for archive ordering.

## Notes

- Version 1.3.0 is the current BCV plugin version.
- Large mod lists can take time to scan, especially when loose-file scanning walks
  many enabled mod folders.
- Archive entries use indexed archive paths and sizes; archives are not unpacked.
