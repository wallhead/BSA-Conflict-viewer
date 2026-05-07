# BSA Conflict Viewer and CleanUp

This repository contains two read-only Mod Organizer 2 Python tool plugins for
Skyrim-style games:

- `BSA Conflict Viewer`: shows conflicts between files inside BSA/BA2 archives and
  loose files.
- `CleanUp`: shows cleanup candidates grouped by mod, with size information.

MO2 can show loose-file conflicts, but game archives are harder to reason about because
the game can load files from inside archives at startup. These plugins index archive
contents and loose files without extracting archives or changing your mod setup.

## BSA Conflict Viewer

- Scans active BSA/BA2 archive indexes without extraction.
- Scans loose files from enabled mods, Overwrite, and game Data.
- Shows full overwrite chains, including archive-only, loose-only, and mixed conflicts.
- Lets you select a mod or a specific archive and view only its files.
- Filters by mod name, file path, and asset category.
- Sorts mods by name or visible file count.
- Exports the current conflict view to CSV.
- Shows scan progress and warnings when archives cannot be read.

## CleanUp

- Uses the same archive and loose-file scanning logic.
- Shows all losing files from all overwrite chains.
- Groups cleanup candidates by mod.
- Shows file size, source type, archive/source, overwrite depth, and final winner.
- Filters by mod name, file path, asset category, and source type (`All`, `BSA/BA2`,
  or `Loose`).
- Sorts cleanup groups by name, cleanup space, or file quantity.
- Exports the cleanup view to CSV.

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
2. Copy one or both plugin folders into your MO2 `plugins` directory:
   - `bsa_conflict_viewer`
   - `CleanUp`
3. Restart MO2.
4. Open the tools from MO2 as `BSA Conflict Viewer` and/or `CleanUp`.

Example PowerShell commands:

```powershell
Copy-Item -Recurse -Force ".\bsa_conflict_viewer" "D:\TES VV\MO2\plugins\bsa_conflict_viewer"
Copy-Item -Recurse -Force ".\CleanUp" "D:\TES VV\MO2\plugins\CleanUp"
```

## Requirements

- Mod Organizer 2 with Python plugin support.
- PyQt6 as provided by MO2's Python plugin environment.
- A game/plugin setup that exposes MO2 DataArchives for archive ordering.

## Notes

- Both plugins are read-only.
- They do not extract BSA/BA2 archives.
- CleanUp does not automatically remove cleanup candidates.
- Large mod lists can take time to scan, especially when loose-file scanning walks many
  enabled mod folders.
