# Fisheye-to-Cubemap GUI v4

Standalone GUI wrapper for `AM_ImageAndMask_to_cubemap_v4.py`.

## What it does

Provides file/directory pickers for all CLI arguments, a live console, progress bar, and preview of outputs (cube faces, useful pixel mask, mask coverage, fallback mask, run summary).

Supports single-lens and dual-lens (360) workflows.

## Instructions

See [Instructions](Instructions.md) for a detailed guide to every GUI field, support-source priority, output directory layouts, and the preview panel.

## Contents

| File | Purpose |
|---|---|
| `gui.py` | V4 GUI implementation |
| `GUI_v4.vbs` | Double-click launcher (no console window) |
| `requirements.txt` | GUI dependencies |
| `Instructions.md` | Detailed guide to every GUI field and option |

## Setup (one time)

1. Install Python 3.10+ from https://www.python.org
   Make sure **Add Python to PATH** is checked during install.

2. Open a terminal in this folder and run:
   ```
   pip install -r requirements.txt
   ```

## Run

Double-click `GUI_v4.vbs` to launch (no console window).

Or from a terminal:
```
python gui.py
```


