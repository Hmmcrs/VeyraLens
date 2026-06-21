# Log Viewer

A desktop viewer for `.log`, `.txt`, and `.gz` log files. It supports level filters, text and regular-expression search, highlighted matches, error summaries, and session bookmarks.

## Run locally

```powershell
python -m pip install -r requirements.txt
python log_viewer.py
```

## Build a Windows executable

```powershell
python -m PyInstaller --noconfirm --clean --onefile --windowed --name "Log Viewer" --icon minecraft-log-viewer.ico --add-data "minecraft-log-viewer.ico;." log_viewer.py
```

The executable is written to `dist/`. Build output is intentionally excluded from Git.
