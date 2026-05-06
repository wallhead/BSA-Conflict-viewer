# BSA Conflict Viewer

Read-only Mod Organizer 2 Python tool plugin.

It scans active BSA/BA2 archive indexes and loose virtual files, then shows every asset
path with a full overwrite chain. It does not extract or modify archives.

## Install

Copy the `bsa_conflict_viewer` folder into the MO2 `plugins` directory, then restart
MO2.

For this machine:

```powershell
Copy-Item -Recurse -Force ".\bsa_conflict_viewer" "D:\TES VV\MO2\plugins\bsa_conflict_viewer"
```

The tool appears as `BSA Conflict Viewer`.

