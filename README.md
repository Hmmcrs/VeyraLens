# VeyraLens

A simple log viewer built with PySide6.

Originally made for searching through Minecraft server logs, but it works with any `.log`, `.txt`, or compressed `.gz` log file.

The goal was to make digging through large logs quicker than using a text editor, with built-in filtering, searching, and bookmarking.

## Features

* Search by text or regular expression
* Filter by log level
* Highlight matching results
* Log specific annotations
* Error summary mode
* Line bookmarks
* Debugging timeline for server events, warnings, and failures
* Collapsible Java stack traces
* Search across all loaded log files
* Supports `.log`, `.txt`, and `.gz` files

## Download

Download the latest executable from the [latest release page](../../releases/latest).

## Running from Source

```powershell
python -m pip install -r requirements.txt
python log_viewer.py
```
