# BSA Conflict Viewer

BSA Conflict Viewer is a read-only Mod Organizer 2 Python tool plugin for Skyrim-style
games. It shows conflicts between files inside BSA/BA2 archives and loose files without
extracting archives or changing your mod setup.

MO2 can show loose-file conflicts, but game archives are harder to reason about because
the game can load files from inside archives at startup. This plugin indexes archive
contents and loose files, then shows the full overwrite chain for each asset path.

## Features

- Scans active BSA/BA2 archive indexes without extraction.
- Scans loose files from enabled mods, Overwrite, and game Data.
- Shows full overwrite chains, including archive-only, loose-only, and mixed conflicts.
- Lets you select a mod or a specific archive and view only its files.
- Filters by mod name, file path, and asset category.
- Sorts mods by name, cleanup size, or visible file count.
- Provides a Cleanup tab grouped by mod, with losing files, size, source type, winner,
  and overwrite depth.
- Exports the current conflict view to CSV.
- Shows scan progress and warnings when archives cannot be read.

## Cleanup Meaning

Cleanup is only an estimate. The plugin does not delete files, extract files, or repack
archives.

For each conflict chain, cleanup counts every losing provider before the final winner.
The winner is never counted.

Examples:

```text
bsa1 -> bsa2 -> loose(win)
cleanup = bsa1 + bsa2

loose1 -> bsa1 -> bsa2 -> loose2(win)
cleanup = loose1 + bsa1 + bsa2
```

For archive entries, the plugin uses indexed archive entry sizes. For loose files, it
uses the real file size on disk.

## Install

1. Download or clone this repository.
2. Copy the `bsa_conflict_viewer` folder into your MO2 `plugins` directory.
3. Restart MO2.
4. Open the tool from MO2 as `BSA Conflict Viewer`.

Example PowerShell command:

```powershell
Copy-Item -Recurse -Force ".\bsa_conflict_viewer" "D:\TES VV\MO2\plugins\bsa_conflict_viewer"
```

## Requirements

- Mod Organizer 2 with Python plugin support.
- PyQt6 as provided by MO2's Python plugin environment.
- A game/plugin setup that exposes MO2 DataArchives for archive ordering.

## Notes

- The plugin is read-only.
- It does not extract BSA/BA2 archives.
- It does not automatically remove cleanup candidates.
- Large mod lists can take time to scan, especially when loose-file scanning walks many
  enabled mod folders.
