# Reality Pump / TopWare Media Converter

Converts the video and audio files of Reality Pump / TopWare games to and from standard formats:

- **.twv / .tws** – KnightShift, World War III: Black Gold, Heli Heroes, Panzer Claws, …
- **.wd1** – Earth 2150 videos

<img src="https://github.com/A-t-l-as/TWV_Video_Converter/blob/main/Docs/screen1.png?raw=true" alt="Screenshot"/>

| File | Purpose |
|---|---|
| `twv2mp4.py` | the converter |
| `Convert TWV.bat` | Windows launcher: double-click for the menu, or drop files onto it |
| `README.md` | this document |

## Requirements

- **Python 3.8 or newer.** On Windows, tick **"Add Python to PATH"** during setup.
- **ffmpeg** – optional, needed only for re-encoding. If it is not installed, the converter downloads a verified build on Windows the first time it is needed (BtbN builds from GitHub, ~85 MB, SHA-256 checked) into a `tools` folder next to the script. On Linux or macOS, install it with your package manager.

## Quick start

**Double-click `Convert TWV.bat`.** A menu walks you through each task.

You can also **drop files or a folder onto `Convert TWV.bat`**. The conversion direction is then chosen automatically:

| You drop | You get |
|---|---|
| `.twv` (with its `.tws`) or `.wd1` | `.mp4` with picture and sound |
| `.tws` | `.wav` |
| a normal video (`.mp4`, `.mkv`, `.avi`, …) | `.twv` + `.tws` for the game |
| a normal audio file (`.wav`, `.mp3`, `.mp2`, …) | `.tws` for the game |
| a folder | every `.twv` and `.wd1` in it → `.mp4` (the menu can include subfolders) |

## Menu

| Option | Task | Output choices |
|---|---|---|
| 1 | Game video → normal video | MP4 (H.264 / H.265), WebM, or original quality: `.m1v` + `.mp2`, MKV, MPG |
| 2 | Game audio → normal audio | WAV, FLAC, MP3, M4A, OGG, Opus, MP2, or an exact copy |
| 3 | Video → TWV + TWS | automatic, or custom frame rate, size, quality and sound |
| 4 | Audio → TWS | automatic, or custom bitrate, channels and sample rate |
| 5 | Video → WD1 (Earth 2150) | matched to the file being replaced |
| I | Inspect | shows what is inside a file, changes nothing |
| H | Help | |

## Lossless conversion

- **TWV → `.m1v` + `.mp2`.** "Exact copy" (`--format raw`) needs no ffmpeg.
- **Back into the game unchanged.** That `.m1v` – or an MKV/MPG holding the original streams – converts back to a `.twv` that is **byte-for-byte identical** to the original game file.
- **TWS.** MP2 audio goes into a `.tws` unchanged.
- **WD1.** A `.wd1` is a plain AVI or MPG inside, so the lossless export simply gives it its real extension.

## Replacing game files

When a result overwrites an existing `.twv`, `.tws` or `.wd1`, the original is kept beside it as `name.twv.bak` (or `.tws.bak`, `.wd1.bak`). To undo, delete the new file and rename the backup.

When a new `.wd1` replaces an existing one, it is encoded to match the original's container, size, frame rate and audio.

## Command line

```
python twv2mp4.py Intro2.twv                          # -> Intro2.mp4
python twv2mp4.py Intro2.twv --format raw             # -> Intro2.m1v + Intro2.mp2, lossless
python twv2mp4.py Intro2.twv -o Intro2.mkv            # format taken from the extension
python twv2mp4.py "C:\Games\KnightShift\video" -o mp4 # a whole folder
python twv2mp4.py "C:\Games\KnightShift" -r -o mp4    # the folder and all its subfolders
python twv2mp4.py Intro2.tws --audio-format mp3
python twv2mp4.py "My intro.mp4" -o Intro2.twv         # + Intro2.tws
python twv2mp4.py music.wav -o Intro2.tws
python twv2mp4.py new.mp4 --mode wd1 -o "C:\Earth2150\Video\VideoUCS.wd1"
python twv2mp4.py Intro2.twv --info
python twv2mp4.py --help                              # every option
```

Put file names that contain spaces in quotes.

### Folders and subfolders

A folder input converts the files directly inside it. Add `-r` to include every subfolder as well.

- **Without `-o`:** each result is saved next to its source file.
- **With `-o`:** the subfolder structure is recreated in the output folder. For example, `video\intro\Intro1.twv` becomes `mp4\intro\Intro1.mp4`, so files with the same name in different subfolders do not overwrite each other.
- **Output folder inside the input folder:** it is skipped, so running the same command again does not convert earlier results.

### Main options

| Option | Meaning |
|---|---|
| `--mode auto/video/audio/twv/tws/wd1` | conversion direction (default: by file extension) |
| `-o PATH` | output file (single input) or folder |
| `-r`, `--recursive` | also convert files in all subfolders; with `-o`, the same folder structure is created in the output folder |
| `--format raw/mp4/mkv/mov/avi/webm/mpg` | output container for game video |
| `--vcodec`, `--acodec`, `--crf`, `--size` | codecs, quality and size when re-encoding |
| `--audio-format wav/flac/mp3/m4a/ogg/opus/mp2/raw` | output format for game audio |
| `--fps`, `--width`, `--height`, `-q`, `--stretch` | settings for new TWV / WD1 files |
| `--tws-bitrate`, `--tws-rate`, `--tws-channels` | settings for new TWS files |
| `--reencode` | always re-encode, even when a lossless copy is possible |
| `--info` | show file details only |

## File formats

### TWV (video)

A 20-byte big-endian header: `TWV\0`, version, width, height, and frames per second as 16.16 fixed point.

It is followed by a stripped MPEG-1 video stream:

- I-frames only, one slice per macroblock row
- no sequence header; the default quantiser matrices are used
- 1-byte picture headers
- slice headers without `extra_bit_slice`
- ends with `00 00 01 B7`

The game intros are 640×256 at 25 fps.

Some TWV files have their beginning cut off (seen in World War III: Black Gold). The converter rebuilds the header and estimates the frame rate from the length of the matching `.tws`.

### TWS (audio)

A plain MPEG-1 Layer II (MP2) stream with no header. The game files are 44.1 kHz stereo at 128 or 160 kbps.

### WD1 (Earth 2150 video)

A plain AVI (Cinepak or Indeo video with PCM audio) or an MPEG-1 system stream (MPEG-1 video with MP2 audio), under a different extension.

## Credits

The TWV format was reverse engineered from the KnightShift executable. The PowerShell version of this tool (`RP-TW-Media-Converter.ps1`) contributed the TWS, WD1, lossless-copy and menu features that this Python version is based on.
