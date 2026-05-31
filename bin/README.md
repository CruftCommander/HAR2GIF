# bin/ — bundled binaries

`convert.py` needs **ffmpeg**. It looks for the binary in this order:

1. `--ffmpeg-path PATH` (if you pass it explicitly)
2. your system `PATH` (`shutil.which("ffmpeg")`)
3. **`bin/ffmpeg.exe`** (this folder — checked relative to `convert.py`, not your current directory)

So you have two choices: install ffmpeg system-wide, or drop the executable in this folder.

## Option A — drop the binary here (Windows, no install)

1. Download a Windows build from <https://www.gyan.dev/ffmpeg/builds/> — get the **"essentials"** release build (a `.7z` or `.zip`).
2. Extract it and find `bin/ffmpeg.exe` inside the archive.
3. Copy that `ffmpeg.exe` to **`bin/ffmpeg.exe`** in this repo (next to this README).

`ffmpeg.exe` is git-ignored on purpose — do **not** commit the binary.

## Option B — install system-wide

| Platform | Command |
|----------|---------|
| Windows  | `winget install ffmpeg`  *(or)*  `choco install ffmpeg` |
| Linux    | `sudo apt install ffmpeg` |
| macOS    | `brew install ffmpeg` |

After a system install, ffmpeg is on your `PATH` and `convert.py` finds it automatically — nothing needs to go in this folder.

## Verify

```bash
ffmpeg -version          # if installed on PATH
./bin/ffmpeg.exe -version   # if you dropped it here (Windows)
```
