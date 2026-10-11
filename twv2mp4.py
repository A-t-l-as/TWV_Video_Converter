#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
twv2mp4.py - video and audio converter for Reality Pump / TopWare games

  .twv / .tws  KnightShift, World War III: Black Gold, Heli Heroes, Panzer Claws ...
  .wd1         Earth 2150 videos

Run without arguments (or double-click "Convert TWV.bat") for an interactive menu.

Modes (--mode):
  video  game video -> video     .twv (+ .tws) / .wd1 -> mp4 mkv mov avi webm mpg,
                                  or the original streams (.m1v + .mp2 / .avi / .mpg)
  audio  game audio -> audio     .tws / .wd1 soundtrack -> wav flac mp3 m4a ogg opus mp2
  twv    video -> .twv + .tws    MPEG-1 I-frame video goes into the TWV without re-encoding
  tws    audio -> .tws           MP2 audio goes into the TWS without re-encoding
  wd1    video -> .wd1           an AVI / MPG the game can play is copied in unchanged
  auto   (default) by extension: .twv/.wd1 -> video, .tws -> audio,
         other audio -> tws, other video -> twv

File formats (reverse engineered from the game engine and its files):
  TWV  20-byte big-endian header: 'TWV\\0', version, width, height, fps (16.16),
       followed by a stripped MPEG-1 video stream: I-frames only, no sequence header,
       1-byte picture headers, slice headers without extra_bit_slice, ends with 00 00 01 B7.
  TWS  a plain MPEG-1 Layer II (MP2) stream; the games use 44.1 kHz stereo, 128/160 kbps.
  WD1  a plain AVI (Cinepak / Indeo + PCM) or MPEG-1 system stream with another extension.

Works without ffmpeg: reading and writing TWV, repairing truncated headers, --info and
every "original streams" conversion. Everything else needs ffmpeg - if it is missing,
a verified build (BtbN, GitHub) is downloaded once on Windows into the "tools" folder
next to this script.

Examples:
  python twv2mp4.py                                    # interactive menu
  python twv2mp4.py Intro2.twv                         # -> Intro2.mp4 (with sound from Intro2.tws)
  python twv2mp4.py Intro2.twv --format raw            # -> Intro2.m1v + Intro2.mp2, lossless
  python twv2mp4.py Intro2.twv -o Intro2.mkv           # format taken from the extension
  python twv2mp4.py "C:\\Games\\KnightShift\\video" -o mp4
  python twv2mp4.py "C:\\Games\\KnightShift" -r -o mp4  # all subfolders, same structure in mp4
  python twv2mp4.py Intro2.tws --audio-format mp3
  python twv2mp4.py MyIntro.mp4 -o Intro2.twv           # + Intro2.tws from the video's sound
  python twv2mp4.py music.wav -o Intro2.tws
  python twv2mp4.py new.mp4 --mode wd1 -o "C:\\Earth2150\\Video\\VideoUCS.wd1"
  python twv2mp4.py Intro2.twv --info

Requires Python 3.8+. Replaced game files are kept as *.bak.
"""
import argparse
import hashlib
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import uuid
import zipfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ON_WINDOWS = os.name == "nt"

MAGIC = b"TWV\x00"
HDR = 20
SC = b"\x00\x00\x01"
GAME_W, GAME_H = 640, 256
DEFAULT_FPS = 25.0
# MPEG-1 frame_rate_code -> fps (index = code, 0 is reserved)
FPS_LIST = [0.0, 24000 / 1001, 24.0, 25.0, 30000 / 1001, 30.0, 50.0, 60000 / 1001, 60.0]
MP2_RATES = [32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384]
CONTAINERS = ("mp4", "mkv", "mov", "avi", "webm", "mpg")
AUDIO_FORMATS = ("raw", "wav", "flac", "mp3", "m4a", "ogg", "opus", "mp2")
VIDEO_EXT = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".mpg", ".mpeg",
             ".m1v", ".wmv", ".flv", ".gif")
AUDIO_EXT = (".wav", ".mp3", ".mp2", ".ogg", ".oga", ".opus", ".flac", ".m4a", ".aac",
             ".wma", ".aif", ".aiff", ".ac3")


class ConvError(Exception):
    """An error with a message meant for the user."""


# ============================================================== messages

_COLOR = False


def _setup_console():
    global _COLOR
    for st in (sys.stdout, sys.stderr):        # never crash on characters the console can't show
        try:
            st.reconfigure(errors="replace")
        except Exception:
            pass
    try:
        if sys.stdout.isatty():
            if ON_WINDOWS:
                os.system("")                  # enables ANSI colours in the Windows 10+ console
            _COLOR = True
    except Exception:
        pass


def _paint(code, s):
    return f"\033[{code}m{s}\033[0m" if _COLOR else s


def info(m):
    print(f"[i] {m}", flush=True)


def ok(m):
    print(_paint("32", f"[ok] {m}"), flush=True)


def warn(m):
    print(_paint("33", f"[!] {m}"), flush=True)


def err(m):
    print(_paint("31", f"[!] {m}"), flush=True)


# ============================================================== small helpers

def ext_of(p):
    return os.path.splitext(p)[1].lower()


def base(p):
    return os.path.basename(p)


def change_ext(p, e):
    return os.path.splitext(p)[0] + e


def read_bytes(p):
    with open(p, "rb") as f:
        return f.read()


def read_head(p, n):
    with open(p, "rb") as f:
        return f.read(n)


def write_bytes(p, data):
    with open(p, "wb") as f:
        f.write(data)


def temp_file(e):
    return os.path.join(tempfile.gettempdir(), "twv_" + uuid.uuid4().hex + e)


def remove_quiet(p):
    try:
        os.remove(p)
    except OSError:
        pass


def move_over(src, dst):
    if os.path.exists(dst):
        os.remove(dst)
    shutil.move(src, dst)


def find_sibling(p, e):
    for x in (e.lower(), e.upper()):
        c = change_ext(p, x)
        if os.path.isfile(c):
            return c
    return None


def backup_if_exists(p):
    """Before overwriting a .twv/.tws/.wd1 (most likely an original game file), keep one copy."""
    if not os.path.isfile(p):
        return
    bak = p + ".bak"
    if os.path.exists(bak):
        return                                   # the first original is already backed up
    shutil.copy2(p, bak)
    info(f"existing file backed up: {bak}")


def fmt_fps(x):
    return f"{x:.3f}".rstrip("0").rstrip(".")


def fps_arg(x):
    return f"{x:.6f}".rstrip("0").rstrip(".")


def fps_code(fps):
    return min(range(1, 9), key=lambda c: abs(FPS_LIST[c] - fps))


def snap_fps(fps):
    return FPS_LIST[fps_code(fps)]


def kbps_value(b):
    """'160k' / '160' -> 160 ; '' / None -> 0"""
    if not b:
        return 0
    m = re.match(r"^\s*(\d+)\s*[kK]?\s*$", str(b))
    if not m:
        raise ConvError(f"invalid bitrate '{b}' (use e.g. 160k)")
    return int(m.group(1))


def maybe_inflate(data):
    if len(data) > 2 and data[0] == 0x78 and data[1] in (0x01, 0x5E, 0x9C, 0xDA):
        try:
            return zlib.decompress(data)
        except zlib.error:
            pass
    return data


# ============================================================== MPEG-1 bitstream

def start_codes(d, start=0):
    """Positions of every 00 00 01 from 'start', plus the data length as a sentinel."""
    pos = []
    find = d.find
    i = find(SC, start)
    while i >= 0:
        pos.append(i)
        i = find(SC, i + 3)
    pos.append(len(d))
    return pos


def seq_header(w, h, fps):
    w1 = (w << 20) | (h << 8) | (1 << 4) | fps_code(fps)     # aspect 1:1
    w2 = (0x3FFFF << 14) | (1 << 13) | (112 << 3)            # VBR, marker, vbv, default matrices
    return SC + b"\xb3" + struct.pack(">II", w1, w2)


# A slice body is treated as one n-bit integer v; hi = the 5-bit quantiser_scale
# kept in place:
#   TWV -> MPEG-1  insert extra_bit_slice=0 after the quantiser :  (v + hi) << 7   (n+8 bits)
#   MPEG-1 -> TWV  remove extra_bit_slice (must be 0)            :  2v - hi         (n bits)

def slice_insert(body):
    n = len(body) * 8
    v = int.from_bytes(body, "big")
    hi = v >> (n - 5) << (n - 5)
    return ((v + hi) << 7).to_bytes(len(body) + 1, "big")


def slice_remove(body):
    n = len(body) * 8
    v = int.from_bytes(body, "big")
    hi = v >> (n - 5) << (n - 5)
    r = (2 * v - hi).to_bytes(len(body), "big")
    # Removing the bit can leave a trailing all-zero byte - the padding added by
    # TWV -> MPEG-1. Dropping it makes TWV -> M1V -> TWV reproduce the file exactly,
    # but only when that byte cannot hold the end of the last EOB code ('10').
    L = len(r)
    if L > 1 and r[-1] == 0:
        j = L - 2
        while j >= 0 and r[j] == 0:
            j -= 1
        if j >= 0 and (j < L - 2 or (r[j] & 1) == 0):
            r = r[:-1]
    return r


# ============================================================== TWV

def read_twv_header(d, width_default=GAME_W):
    """Returns ver, w, h, fps (0 = unknown), raw, start (data offset), repaired."""
    if d[:4] == MAGIC and len(d) >= HDR:
        _, ver, w, h, raw = struct.unpack(">5I", d[:HDR])
        if not (16 <= w <= 4095 and 16 <= h <= 4095):
            raise ConvError(f"invalid dimensions in header: {w}x{h} "
                            f"(header: {d[:HDR].hex(' ')})")
        fps = raw / 65536.0
        if not 1 <= fps <= 120:
            fps = float(raw) if 1 <= raw <= 120 else 0.0      # fps as a plain integer / missing
        return dict(ver=ver, w=w, h=h, fps=fps, raw=raw, start=HDR, repaired=False)

    # No 'TWV\0': possibly a file with its start cut off (header and part of the first
    # frame), seen e.g. in World War III: Black Gold. Rebuild it if it holds a TWV stream.
    pos = start_codes(d, 0)
    first, max_slice, pics = -1, 0, 0
    for k in range(len(pos) - 1):
        p = pos[k]
        if p + 3 >= len(d):
            break
        c = d[p + 3]
        if c == 0:
            pics += 1
            if first < 0:
                first = p
            if pos[k + 1] - p != 5 and pics < 3:    # a TWV picture header is exactly 1 byte
                first = -2
                break
        elif 1 <= c <= 0xAF:
            max_slice = max(max_slice, c)
    if first < 0 or pics < 1 or max_slice < 1:
        raise ConvError("no 'TWV\\0' signature - not a TWV file")
    return dict(ver=1, w=width_default, h=max_slice * 16, fps=0.0, raw=0,
                start=first, repaired=True)


def twv_to_m1v(d, hdr, fps):
    seq = seq_header(hdr["w"], hdr["h"], fps)
    out = bytearray(seq)
    frames = 0
    pos = start_codes(d, hdr["start"])
    for k in range(len(pos) - 1):
        a, b = pos[k], pos[k + 1]
        if b - a < 4:
            continue
        c = d[a + 3]
        if c == 0x00:                                       # picture
            ptype = (d[a + 4] >> 5) & 7 if b - a > 4 else 1
            if frames and frames % 1024 == 0:
                out += seq
            ph = ((frames % 1024) << 22) | (ptype << 19) | (0xFFFF << 3)
            out += SC + b"\x00" + struct.pack(">I", ph)
            frames += 1
        elif 0x01 <= c <= 0xAF:                             # slice
            if not frames or b - a < 5:                     # partial frame before the first one
                continue
            out += d[a:a + 4] + slice_insert(d[a + 4:b])
        elif c == 0xB7:
            break
    out += SC + b"\xb7"
    return bytes(out), frames


def m1v_to_twv(es, w, h, fps):
    out = bytearray(struct.pack(">4sIIII", MAGIC, 1, w, h, int(round(fps * 65536))))
    frames = 0
    pos = start_codes(es)
    for k in range(len(pos) - 1):
        a, b = pos[k], pos[k + 1]
        if b - a < 4:
            continue
        c = es[a + 3]
        if c == 0x00:                                       # picture
            ptype = (es[a + 5] >> 3) & 7
            if ptype != 1:
                raise ConvError(f"frame {frames} is not an I-frame (type {ptype})")
            out += SC + b"\x00" + bytes([ptype << 5])
            frames += 1
        elif 0x01 <= c <= 0xAF:                             # slice
            if not frames or b - a < 5:
                continue
            if es[a + 4] & 0x04:
                raise ConvError("slice with extra_information_slice is not supported")
            out += es[a:a + 4] + slice_remove(es[a + 4:b])
        # B3 (sequence), B8 (GOP), B2 (user data), B5 (extension), B7 are dropped
    out += SC + b"\xb7"
    return bytes(out), frames


def twv_copyable(es):
    """Can this MPEG-1 stream be put into a TWV without re-encoding?
    Needs: MPEG-1 (no extensions), I-frames only, default intra matrix,
    width/height multiples of 16, one slice per macroblock row."""
    r = dict(ok=False, reason="", w=0, h=0, fps=0.0)
    pos = start_codes(es)
    rows = expect = pics = 0
    for k in range(len(pos) - 1):
        a, b = pos[k], pos[k + 1]
        if b - a < 4:
            continue
        c = es[a + 3]
        if c == 0xB3:
            if b - a < 12:
                r["reason"] = "short sequence header"
                return r
            w = (es[a + 4] << 4) | (es[a + 5] >> 4)
            h = ((es[a + 5] & 0xF) << 8) | es[a + 6]
            frc = es[a + 7] & 0xF
            if (es[a + 11] >> 1) & 1:
                r["reason"] = "custom quantiser matrix"
                return r
            if not 1 <= frc <= 8:
                r["reason"] = "unknown frame rate"
                return r
            if w % 16 or h % 16:
                r["reason"] = f"size {w}x{h} is not a multiple of 16"
                return r
            if r["w"] and (r["w"], r["h"]) != (w, h):
                r["reason"] = "size changes mid-stream"
                return r
            r.update(w=w, h=h, fps=FPS_LIST[frc])
            rows = h // 16
        elif c == 0xB5:
            r["reason"] = "MPEG-2 stream"
            return r
        elif c == 0x00:
            if not rows:
                r["reason"] = "no sequence header"
                return r
            if pics and expect != rows:
                r["reason"] = f"frame {pics - 1} has {expect} slices, needs {rows} (one per row)"
                return r
            if (es[a + 5] >> 3) & 7 != 1:
                r["reason"] = "not all frames are I-frames"
                return r
            pics += 1
            expect = 0
        elif 0x01 <= c <= 0xAF:
            expect += 1
            if c != expect:
                r["reason"] = f"frame {pics - 1}: slices are not one per row"
                return r
            if es[a + 4] & 0x04:
                r["reason"] = "extra slice information"
                return r
    if not pics:
        r["reason"] = "no frames"
        return r
    if expect != rows:
        r["reason"] = "last frame is incomplete"
        return r
    r["ok"] = True
    return r


# ============================================================== TWS (MP2)

def mp2_header(p):
    """Reads the first MPEG-1 Layer II frame (.tws / .mp2). None if it is not MP2."""
    try:
        hb = read_head(p, 4)
        size = os.path.getsize(p)
    except OSError:
        return None
    if len(hb) < 4 or hb[0] != 0xFF or (hb[1] & 0xFE) != 0xFC:   # sync + MPEG-1 + Layer II
        return None
    bi, si = (hb[2] >> 4) & 0xF, (hb[2] >> 2) & 3
    if bi in (0, 15) or si == 3:
        return None
    kbps = MP2_RATES[bi - 1]
    return dict(kbps=kbps, rate=(44100, 48000, 32000)[si],
                ch=1 if (hb[3] >> 6) & 3 == 3 else 2,
                duration=size * 8.0 / (kbps * 1000.0))


def ch_name(ch):
    return "mono" if ch == 1 else "stereo"


# ============================================================== ffmpeg

FFMPEG = None


def _tools_dirs():
    d = [os.path.join(HERE, "tools")]
    la = os.environ.get("LOCALAPPDATA")
    if la:
        d += [os.path.join(la, "RP-TW-Media-Converter", "tools"),   # shared with the .ps1 version
              os.path.join(la, "TWV-Converter", "tools")]
    return d


def init_ffmpeg(a):
    global FFMPEG
    if FFMPEG:
        return FFMPEG
    exe = "ffmpeg.exe" if ON_WINDOWS else "ffmpeg"
    if a.ffmpeg:
        p = os.path.join(a.ffmpeg, exe) if os.path.isdir(a.ffmpeg) else a.ffmpeg
        if not os.path.isfile(p):
            raise ConvError(f"ffmpeg not found: {a.ffmpeg}")
        FFMPEG = p
        return p
    for d in _tools_dirs():
        p = os.path.join(d, exe)
        if os.path.isfile(p):
            FFMPEG = p
            return p
    p = shutil.which("ffmpeg")
    if p:
        FFMPEG = p
        return p
    if a.no_download:
        raise ConvError("ffmpeg not found (and --no-download was given)")
    FFMPEG = install_ffmpeg()
    return FFMPEG


def _writable_tools_dir():
    for d in _tools_dirs()[:2] + [os.path.join(tempfile.gettempdir(), "RP-TW-Media-Converter", "tools")]:
        try:
            os.makedirs(d, exist_ok=True)
            t = os.path.join(d, ".write-test")
            with open(t, "w") as f:
                f.write("x")
            os.remove(t)
            return d
        except OSError:
            continue
    raise ConvError("no writable folder to store ffmpeg in")


def _download(url, dest):
    with urllib.request.urlopen(url, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got, last = 0, -1
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if total and (got * 10 // total) != last:
                last = got * 10 // total
                print(f"    {got * 100 // total}%", flush=True)


def install_ffmpeg():
    if not ON_WINDOWS:
        raise ConvError('ffmpeg not found - install it with your package manager '
                        '(e.g. "sudo apt install ffmpeg" or "brew install ffmpeg")')
    plat = "winarm64" if os.environ.get("PROCESSOR_ARCHITECTURE", "").upper() == "ARM64" else "win64"
    name = f"ffmpeg-master-latest-{plat}-gpl-shared.zip"
    url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest"
    tools = _writable_tools_dir()
    info(f"ffmpeg not found - downloading {name} (~85 MB, one time only)")
    info(f"  from: {url}")
    info(f"  into: {tools}")
    zpath = os.path.join(tempfile.gettempdir(), name)
    try:
        _download(f"{url}/{name}", zpath)
        with urllib.request.urlopen(f"{url}/checksums.sha256", timeout=60) as r:
            sums = r.read().decode("ascii", "replace")
    except OSError as e:
        remove_quiet(zpath)
        raise ConvError(f"could not download ffmpeg: {e}")
    expected = None
    for line in sums.splitlines():
        f = line.split()
        if len(f) >= 2 and f[1].lstrip("*") == name:
            expected = f[0].lower()
    h = hashlib.sha256()
    with open(zpath, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if not expected or h.hexdigest() != expected:
        remove_quiet(zpath)
        raise ConvError("the ffmpeg download failed SHA-256 verification "
                        "(the build may have just been updated - try again)")
    info("checksum OK, unpacking...")
    with zipfile.ZipFile(zpath) as z:                   # only bin\ffmpeg.exe and its DLLs
        for e in z.infolist():
            m = re.search(r"/bin/([^/]+\.(?:exe|dll))$", e.filename, re.I)
            if m and m.group(1).lower() not in ("ffplay.exe", "ffprobe.exe"):
                with z.open(e) as src, open(os.path.join(tools, m.group(1)), "wb") as dst:
                    shutil.copyfileobj(src, dst)
    remove_quiet(zpath)
    exe = os.path.join(tools, "ffmpeg.exe")
    if not os.path.isfile(exe):
        raise ConvError("ffmpeg.exe was not found in the downloaded archive")
    ok(f"ffmpeg installed: {exe}")
    return exe


def run_ffmpeg(args):
    """Returns (exit code, messages). Messages are read as UTF-8 (non-ASCII paths)."""
    cmd = [FFMPEG, "-nostdin", "-y", "-hide_banner", "-loglevel", "error"] + [str(x) for x in args]
    r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE)
    return r.returncode, r.stderr.decode("utf-8", "replace")


def media_info(p):
    """Stream info from 'ffmpeg -i' output (no ffprobe needed)."""
    r = subprocess.run([FFMPEG, "-nostdin", "-hide_banner", "-i", p], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    i = dict(vcodec="", w=0, h=0, fps=0.0, acodec="", arate=0, ach=0, akbps=0)
    for line in r.stderr.decode("utf-8", "replace").splitlines():
        if not i["vcodec"]:
            m = re.search(r"Stream #.*Video: (\w+)", line)
            if m:
                i["vcodec"] = m.group(1)
                m = re.search(r"[ ,](\d{2,5})x(\d{2,5})[ ,\[]", line)
                if m:
                    i["w"], i["h"] = int(m.group(1)), int(m.group(2))
                m = re.search(r"[ ,](\d+(?:\.\d+)?) fps", line) or re.search(r"[ ,](\d+(?:\.\d+)?) tbr", line)
                if m:
                    i["fps"] = float(m.group(1))
        if not i["acodec"]:
            m = re.search(r"Stream #.*Audio: (\w+)", line)
            if m:
                i["acodec"] = m.group(1)
                m = re.search(r", (\d+) Hz", line)
                if m:
                    i["arate"] = int(m.group(1))
                if ", stereo" in line:
                    i["ach"] = 2
                elif ", mono" in line:
                    i["ach"] = 1
                else:
                    m = re.search(r", (\d+) channels", line)
                    if m:
                        i["ach"] = int(m.group(1))
                m = re.search(r", (\d+) kb/s", line)
                if m:
                    i["akbps"] = int(m.group(1))
    return i


# ============================================================== settings

V_ALLOWED = {"webm": ("vp9", "av1"), "mpg": ("copy", "mpeg1"), "raw": ("copy",),
             "mp4": ("copy", "h264", "h265", "av1", "mpeg1"), "mov": ("copy", "h264", "h265", "mpeg1"),
             "avi": ("copy", "h264", "mpeg1"), "mkv": ("copy", "h264", "h265", "vp9", "av1", "mpeg1")}
A_ALLOWED = {"webm": ("opus", "vorbis"), "mpg": ("copy", "mp2", "mp3"), "raw": ("copy",),
             "mp4": ("copy", "aac", "mp3", "mp2", "opus", "flac"), "mov": ("copy", "aac", "mp3", "mp2", "pcm"),
             "avi": ("copy", "mp3", "mp2", "aac", "pcm"),
             "mkv": ("copy", "aac", "mp3", "mp2", "opus", "vorbis", "flac", "pcm")}
CRF_DEFAULT = {"h264": 16, "h265": 20, "vp9": 28, "av1": 30, "mpeg1": 2}


def check_mp2_combo(k, ch, what):
    if ch == 2 and k in (32, 48, 56, 80):
        raise ConvError(f"{what} {k}k is not allowed for stereo MP2 (stereo: 64k-384k, not 80k)")
    if ch == 1 and k > 192:
        raise ConvError(f"{what} {k}k is not allowed for mono MP2 (mono: 32k-192k)")


def finalize(a):
    """Fills in defaults and rejects settings that cannot work. Runs once, before any file."""
    if a.to_twv and a.mode == "auto":
        a.mode = "twv"
    oe = ext_of(a.output) if a.output else ""
    if a.m1v:
        a.format = "raw"
    if a.format is None:                       # format from the -o extension, else mp4
        a.format = {".m1v": "raw", ".mpeg": "mpg"}.get(oe, oe[1:] if oe[1:] in CONTAINERS else "mp4")
    if a.audio_format is None:
        a.audio_format = oe[1:] if oe[1:] in AUDIO_FORMATS[1:] else "wav"

    a.set_by_user = {k for k in ("width", "height", "q", "stretch", "fps",
                                 "tws_bitrate", "tws_rate", "tws_channels")
                     if getattr(a, k) not in (None, False)}
    if a.force_video is None:
        a.force_video = bool(a.reencode or a.set_by_user & {"width", "height", "q", "stretch", "fps"})
    if a.force_audio is None:
        a.force_audio = bool(a.reencode or a.set_by_user & {"tws_bitrate", "tws_rate", "tws_channels"})
    a.width = a.width or GAME_W
    a.height = a.height or GAME_H
    a.q = a.q or 1
    a.tws_bitrate = a.tws_bitrate or "160k"
    a.tws_rate = a.tws_rate or 44100
    a.tws_channels = a.tws_channels or 2

    c = a.format
    if a.vcodec == "auto":
        a.vcodec = {"mp4": "h264", "mov": "h264", "webm": "vp9"}.get(c, "copy")
    if a.acodec == "auto":
        a.acodec = {"mp4": "aac", "mov": "aac", "webm": "opus"}.get(c, "copy")
    if a.vcodec not in V_ALLOWED[c]:
        raise ConvError(f"video codec '{a.vcodec}' is not available for .{c} (use: {', '.join(V_ALLOWED[c])})")
    if a.acodec not in A_ALLOWED[c]:
        raise ConvError(f"audio codec '{a.acodec}' is not available for .{c} (use: {', '.join(A_ALLOWED[c])})")
    if a.vcodec == "copy" and (a.size or a.vbitrate):
        raise ConvError("--size / --vbitrate need a re-encode: pick --vcodec h264/h265/vp9/av1/mpeg1")
    if a.size and a.size != "double" and not re.match(r"^\d+x\d+$", a.size):
        raise ConvError(f"invalid --size '{a.size}' (use e.g. 1280x512, or double)")
    if a.crf is not None:
        if not 0 <= a.crf <= 63:
            raise ConvError("--crf must be 0-63")
        if a.vcodec in ("h264", "h265") and a.crf > 51:
            raise ConvError(f"--crf for {a.vcodec} must be 0-51")
    if not 1 <= a.q <= 31:
        raise ConvError("-q must be 1-31")
    if a.width % 16 or a.height % 16:
        if a.mode in ("twv", "auto") and a.set_by_user & {"width", "height"}:
            raise ConvError("TWV width and height must be multiples of 16")

    tk = kbps_value(a.tws_bitrate)
    if tk not in MP2_RATES:
        raise ConvError(f"--tws-bitrate {a.tws_bitrate} is not an MP2 bitrate "
                        f"(use: {', '.join(f'{x}k' for x in MP2_RATES)})")
    a.tws_bitrate = f"{tk}k"
    check_mp2_combo(tk, a.tws_channels, "--tws-bitrate")
    if a.abitrate:
        k = kbps_value(a.abitrate)
        a.abitrate = f"{k}k"
        if a.audio_format == "mp2" and a.mode in ("audio", "auto"):
            if k not in MP2_RATES:
                raise ConvError(f"--abitrate {a.abitrate} is not an MP2 bitrate")
            check_mp2_combo(k, a.channels or 2, "--abitrate")
    return a


def crf_for(a, codec):
    return a.crf if a.crf is not None else CRF_DEFAULT.get(codec, 16)


def out_size(a, w, h):
    if not a.size:
        return None
    if a.size == "double":
        return (w * 2, h * 2) if w else None
    x, y = a.size.split("x")
    return int(x), int(y)


def video_args(a, codec, src_fps, size=None):
    if codec == "copy":
        return ["-c:v", "copy"]
    crf = crf_for(a, codec)
    if codec == "h264":
        out = ["-c:v", "libx264", "-preset", a.preset]
    elif codec == "h265":
        out = ["-c:v", "libx265", "-preset", a.preset, "-x265-params", "log-level=error"]
        if a.format in ("mp4", "mov"):
            out += ["-tag:v", "hvc1"]
    elif codec == "vp9":
        out = ["-c:v", "libvpx-vp9", "-row-mt", "1", "-deadline", "good", "-cpu-used", "2"]
    elif codec == "av1":
        out = ["-c:v", "libsvtav1", "-preset", a.preset if a.preset.isdigit() else "6"]
    else:                                                     # mpeg1
        out = ["-c:v", "mpeg1video", "-g", "15", "-bf", "2",
               "-r", fps_arg(snap_fps(src_fps) if src_fps > 0 else DEFAULT_FPS)]
    if a.vbitrate:
        out += ["-b:v", a.vbitrate]
    elif codec == "mpeg1":
        out += ["-q:v", str(max(1, min(31, crf)))]
    else:
        out += ["-crf", str(crf)]
        if codec == "vp9":
            out += ["-b:v", "0"]
    if size:
        out += ["-vf", f"scale={size[0]}:{size[1]}:flags=lanczos"]
    return out + ["-pix_fmt", "yuv420p"]


def rate_args(a):
    r = []
    if a.rate:
        r += ["-ar", str(a.rate)]
    if a.channels:
        r += ["-ac", str(a.channels)]
    return r


def audio_args(a, codec, default_br):
    br = a.abitrate or default_br
    m = {"aac": ["-c:a", "aac", "-b:a", br], "mp3": ["-c:a", "libmp3lame", "-b:a", br],
         "opus": ["-c:a", "libopus", "-b:a", br], "vorbis": ["-c:a", "libvorbis", "-b:a", br],
         "mp2": ["-c:a", "mp2", "-b:a", br], "flac": ["-c:a", "flac"], "pcm": ["-c:a", "pcm_s16le"]}
    if codec == "copy":
        return ["-c:a", "copy"]
    return m[codec] + rate_args(a)


def audio_format_args(a, fmt):
    if fmt == "wav":
        return audio_args(a, "pcm", "")
    if fmt == "flac":
        return audio_args(a, "flac", "")
    if fmt == "mp3":
        return audio_args(a, "mp3", "") if a.abitrate else ["-c:a", "libmp3lame", "-q:a", "2"] + rate_args(a)
    if fmt == "m4a":
        return audio_args(a, "aac", "192k") + ["-movflags", "+faststart"]
    if fmt == "ogg":
        return audio_args(a, "vorbis", "") if a.abitrate else ["-c:a", "libvorbis", "-q:a", "6"] + rate_args(a)
    if fmt == "opus":
        return audio_args(a, "opus", "160k")
    return audio_args(a, "mp2", "160k") + ["-f", "mp2"]          # mp2 / raw


# ============================================================== 1) game video -> video

def game_to_video(path, out, a):
    if ext_of(path) == ".wd1":
        return wd1_to_video(path, out, a)
    data = maybe_inflate(read_bytes(path))
    hdr = read_twv_header(data, a.width)

    tws = None
    if not a.no_audio:
        tws = a.audio or find_sibling(path, ".tws")
        if a.audio and not os.path.isfile(a.audio):
            raise ConvError(f"audio file not found: {a.audio}")

    # fps: --fps > header > (repaired files) estimated from the TWS length > 25
    fps, note = hdr["fps"], None
    if a.fps:
        fps = a.fps
    elif not fps:
        fps = DEFAULT_FPS
        note = f"header has no fps - assumed {fmt_fps(fps)} (override with --fps)"
        if hdr["repaired"] and tws:
            mh = mp2_header(tws)
            if mh and mh["duration"] > 0:
                _, fr = twv_to_m1v(data, hdr, fps)
                est = fr / mh["duration"]
                fps = snap_fps(est)
                note = (f"fps estimated from the length of {base(tws)}: {est:.2f} -> {fmt_fps(fps)} "
                        f"(override with --fps)")

    es, frames = twv_to_m1v(data, hdr, fps)
    if hdr["repaired"]:
        warn(f"{path}: header missing (truncated file) - rebuilt as {hdr['w']}x{hdr['h']}, "
             f"partial first frame dropped (wrong picture? try --width)")
    if note:
        info(f"{path}: {note}")
    if not frames:
        err(f"{path}: file contains no frames")
        return False
    info(f"{path}: TWV v{hdr['ver']}, {hdr['w']}x{hdr['h']}, {fmt_fps(fps)} fps, "
         f"{frames} frames ({frames / fps:.1f} s)")
    if tws:
        mh = mp2_header(tws)
        if mh:
            info(f"{base(tws)}: MP2 audio {mh['rate']} Hz {ch_name(mh['ch'])} {mh['kbps']}k, "
                 f"{mh['duration']:.1f} s")
    if a.info:
        return True

    if a.format == "raw":                               # no ffmpeg needed
        o = change_ext(out, ".m1v")
        write_bytes(o, es)
        ok(o)
        if tws:
            oa = change_ext(out, ".mp2")
            shutil.copyfile(tws, oa)
            ok(f"{oa} (TWS audio is already MP2 - copied as is)")
        return True

    init_ffmpeg(a)
    vargs = video_args(a, a.vcodec, fps, out_size(a, hdr["w"], hdr["h"]))
    aargs = audio_args(a, a.acodec, "160k" if a.acodec == "opus" else "192k")
    tmp = temp_file(".m1v")
    write_bytes(tmp, es)
    try:
        def run(aud):
            args = ["-fflags", "+genpts", "-f", "mpegvideo", "-framerate", fps_arg(fps), "-i", tmp]
            if aud:
                args += ["-i", aud, "-map", "0:v:0", "-map", "1:a:0"] + aargs
            args += vargs
            if a.format in ("mp4", "mov"):
                args += ["-movflags", "+faststart"]
            return run_ffmpeg(args + [out])

        code, msg = run(tws)
        if code and tws and not a.audio:
            info(f"ffmpeg could not read {base(tws)} - exporting without sound")
            tws = None
            code, msg = run(None)
        if code:
            print(msg)
            err(f"ffmpeg failed for {path}")
            return False
    finally:
        remove_quiet(tmp)
    ok(out + ("" if tws else " (no sound)"))
    return True


# ============================================================== 2) game audio -> audio

def tws_to_audio(path, out, a):
    if ext_of(path) == ".wd1":
        return wd1_to_audio(path, out, a)
    mh = mp2_header(path)
    if mh:
        info(f"{path}: TWS (MP2 {mh['rate']} Hz {ch_name(mh['ch'])} {mh['kbps']}k), "
             f"{mh['duration']:.1f} s")
    else:
        warn(f"{path}: does not start with an MP2 header - trying anyway")
    if a.info:
        return True
    reenc = bool(a.abitrate or a.rate or a.channels)
    if not reenc and (a.audio_format == "raw" or (a.audio_format == "mp2" and mh)):
        shutil.copyfile(path, out)                       # a TWS already is MP2
        ok(f"{out} (original stream copied as is)")
        return True
    init_ffmpeg(a)
    code, msg = run_ffmpeg(["-i", path, "-vn"] + audio_format_args(a, a.audio_format) + [out])
    if code:
        print(msg)
        err(f"ffmpeg failed for {path}")
        return False
    ok(out)
    return True


# ============================================================== 3) video -> TWV + TWS

def twv_fps(a, path):
    if a.fps:
        if any(abs(a.fps - v) < 0.01 for v in FPS_LIST[1:]):
            return a.fps
        raise ConvError(f"{fmt_fps(a.fps)} fps is not allowed in MPEG-1 "
                        f"(allowed: 23.976, 24, 25, 29.97, 30, 50, 59.94, 60)")
    src = media_info(path)["fps"]                       # keep the source rate, snapped to MPEG-1
    if src > 0:
        fps = snap_fps(src)
        if abs(fps - src) < 0.01:
            info(f"{path}: source is {fmt_fps(src)} fps - kept")
        else:
            info(f"{path}: {fmt_fps(src)} fps is not allowed in MPEG-1 - using the nearest, "
                 f"{fmt_fps(fps)} fps (override with --fps)")
        return fps
    info(f"{path}: could not read the source fps - using {fmt_fps(DEFAULT_FPS)} (override with --fps)")
    return DEFAULT_FPS


def encode_m1v(a, path, w, h, fps):
    if a.stretch:
        vf = f"scale={w}:{h}"
    else:   # keep the aspect ratio, pad with black bars
        vf = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black"
    tmp = temp_file(".m1v")
    try:
        code, msg = run_ffmpeg(["-i", path, "-map", "0:v:0", "-an", "-vf", vf + ",setsar=1",
                                "-r", fps_arg(fps), "-c:v", "mpeg1video", "-g", "1", "-bf", "0",
                                "-qscale:v", a.q, "-qmin", "1", "-qmax", max(a.q, 31),
                                "-slices", h // 16,              # one slice per macroblock row
                                "-f", "mpeg1video", tmp])
        if code:
            print(msg)
            raise ConvError(f"ffmpeg failed for {path}")
        return read_bytes(tmp)
    finally:
        remove_quiet(tmp)


def to_twv(path, out, a):
    w, h = a.width, a.height
    if w % 16 or h % 16:
        err("TWV width and height must be multiples of 16")
        return False
    is_es = ext_of(path) in (".m1v", ".mpv")

    # 1) lossless: an MPEG-1 I-frame stream goes into the TWV unchanged
    es, fps, how = None, None, "re-encoded"
    if not a.force_video:
        if is_es:
            es = read_bytes(path)                       # no ffmpeg needed
        else:
            init_ffmpeg(a)
            vc = media_info(path)["vcodec"]
            if vc == "mpeg1video":
                tmp = temp_file(".m1v")
                try:
                    code, _ = run_ffmpeg(["-i", path, "-map", "0:v:0", "-c:v", "copy", "-f", "mpeg1video", tmp])
                    if code == 0:
                        es = read_bytes(tmp)
                finally:
                    remove_quiet(tmp)
            elif vc:
                info(f"{path}: source video is {vc}, not MPEG-1 - encoding with the game's settings")
        if es:
            chk = twv_copyable(es)
            if chk["ok"]:
                w, h, fps = chk["w"], chk["h"], chk["fps"]
                how = "video copied as is (lossless)"
                if (w, h) != (GAME_W, GAME_H):
                    warn(f"{path}: stream is {w}x{h}, game intros are {GAME_W}x{GAME_H} "
                         f"(use --reencode to resize)")
            else:
                info(f"{path}: video can't be put into a TWV as is ({chk['reason']}) - re-encoding")
                es = None
    # 2) encode with the game's settings
    if es is None:
        init_ffmpeg(a)
        fps = twv_fps(a, path)
        es = encode_m1v(a, path, w, h, fps)

    twv, frames = m1v_to_twv(es, w, h, fps)
    _, back = twv_to_m1v(twv, read_twv_header(twv), fps)      # self-check: read it back
    if back != frames or not frames:
        err(f"{path}: consistency check failed")
        return False
    backup_if_exists(out)
    write_bytes(out, twv)
    ok(f"{out}: {w}x{h} @ {fmt_fps(fps)} fps, {frames} frames ({frames / fps:.1f} s), "
       f"{len(twv) / 1e6:.1f} MB, {how}")

    if not a.no_tws:
        src = a.audio or path
        if not a.audio and is_es:                       # raw .m1v: the .mp2 / .tws beside it
            src = find_sibling(path, ".mp2") or find_sibling(path, ".tws")
            if not src:
                info(f"{path}: no .mp2 beside this .m1v - .tws not created")
                return True
        write_tws(src, change_ext(out, ".tws"), a, optional=True)
    return True


# ============================================================== 4) audio -> TWS

def write_tws(src, tws, a, optional=False):
    if os.path.abspath(src) == os.path.abspath(tws):
        err(f"{src}: output would overwrite the input")
        return False
    if not a.force_audio:
        mh = mp2_header(src)
        if mh:                                          # no ffmpeg needed
            backup_if_exists(tws)
            shutil.copyfile(src, tws)
            return tws_result(tws, mh, "copied as is (lossless)")
        init_ffmpeg(a)
        ac = media_info(src)["acodec"]
        # MPEG audio inside a container: extract only. (MP4 labels MP2 as "mp3", so the
        # frame header decides: Layer II is copied, real MP3 is re-encoded.)
        if ac in ("mp2", "mp3"):
            tmp = temp_file(".mp2")
            code, _ = run_ffmpeg(["-i", src, "-vn", "-map", "0:a:0", "-c:a", "copy", "-f", "mp2", tmp])
            if code == 0 and mp2_header(tmp):
                backup_if_exists(tws)
                move_over(tmp, tws)
                return tws_result(tws, mp2_header(tws), "copied as is (lossless)")
            remove_quiet(tmp)
            if ac == "mp3":
                ac = "MP3 (Layer III)"
        if ac:
            info(f"{src}: source audio is {ac}, not MP2 - encoding with the TWS settings")
    init_ffmpeg(a)
    tmp = temp_file(".mp2")
    code, msg = run_ffmpeg(["-i", src, "-vn", "-map", "0:a:0", "-ac", a.tws_channels, "-ar", a.tws_rate,
                            "-c:a", "mp2", "-b:a", a.tws_bitrate, "-f", "mp2", tmp])
    if code:
        remove_quiet(tmp)
        if optional:
            info(f"{src}: no audio track - .tws not created")
            if os.path.isfile(tws):
                warn(f"the existing {base(tws)} was left in place - the game will play the old sound "
                     f"(give new sound with --audio)")
            return False
        print(msg)
        err(f"{src}: no usable audio track (or ffmpeg failed)")
        return False
    backup_if_exists(tws)
    move_over(tmp, tws)
    return tws_result(tws, mp2_header(tws), "encoded")


def tws_result(tws, h, how):
    if not h:
        ok(f"{tws} ({how})")
        return True
    ok(f"{tws}: TWS (MP2 {h['rate']} Hz {ch_name(h['ch'])} {h['kbps']}k), {h['duration']:.1f} s, {how}")
    hint = " (use --reencode to convert it)" if how.startswith("copied") else ""
    if h["rate"] != 44100 or h["ch"] != 2:
        warn(f"the game's own TWS files are 44100 Hz stereo - this may not play in-game{hint}")
    elif h["kbps"] not in (128, 160):
        info(f"note: {h['kbps']}k - the game's own TWS files use 128k or 160k")
    return True


# ============================================================== Earth 2150 .wd1
# A .wd1 video is a plain AVI (Cinepak / Indeo + PCM) or an MPEG-1 system stream
# (MPEG-1 video + MP2) with another extension. Detected by content, natively.

FOURCC_NAMES = {"cvid": "Cinepak", "iv50": "Indeo 5", "iv41": "Indeo 4", "iv32": "Indeo 3",
                "iv31": "Indeo 3", "msvc": "Microsoft Video 1", "cram": "Microsoft Video 1", "mpg1": "MPEG-1"}
WAVE_NAMES = {1: "PCM", 2: "MS ADPCM", 17: "IMA ADPCM", 80: "MPEG audio", 85: "MP3"}
WD1_FOURCCS = ("cvid", "iv50", "iv41", "iv32", "iv31", "msvc", "cram")    # codecs known from the game
WD1_WAVE_TAGS = (0, 1, 2, 17, 80, 85)
# what each container can take as a stream copy, and what to encode when it can't
COPY_V = {"mkv": ("indeo5", "indeo4", "indeo3", "cinepak", "msvideo1", "mpeg1video"), "avi": ("*",),
          "mov": ("indeo5", "cinepak", "mpeg1video"), "mp4": ("mpeg1video",), "mpg": ("mpeg1video",), "webm": ()}
COPY_A = {"mkv": ("pcm_s16le", "pcm_u8", "mp2", "mp3", "adpcm_ms", "adpcm_ima_wav"), "avi": ("*",),
          "mov": ("pcm_s16le", "pcm_u8", "mp2"), "mp4": ("mp2", "mp3"), "mpg": ("mp2", "mp3"), "webm": ()}
FALL_V = {"mkv": "h264", "avi": "h264", "mov": "h264", "mp4": "h264", "mpg": "mpeg1", "webm": "vp9"}
FALL_A = {"mkv": "aac", "avi": "mp3", "mov": "aac", "mp4": "aac", "mpg": "mp2", "webm": "opus"}


def wd1_kind(p):
    """'avi' / 'mpg' (+ whether it is MPEG-2), or None."""
    try:
        d = read_head(p, 16)
    except OSError:
        return None
    if len(d) < 12:
        return None
    if d[:4] == b"RIFF" and d[8:12] == b"AVI ":
        return dict(ext="avi", mpeg2=False)
    if d[:4] == b"\x00\x00\x01\xba":
        return dict(ext="mpg", mpeg2=(d[4] & 0xC0) == 0x40)
    return None


def avi_info(p):
    d = read_head(p, 1 << 20)
    u32 = lambda o: struct.unpack_from("<I", d, o)[0]
    u16 = lambda o: struct.unpack_from("<H", d, o)[0]
    r = dict(vfourcc="", w=0, h=0, fps=0.0, frames=0, atag=0, arate=0, ach=0, abits=0, has_audio=False)
    pos, cur = 12, b""
    while pos + 8 <= len(d):
        cid, sz = d[pos:pos + 4], u32(pos + 4)
        if cid == b"LIST":
            lt = d[pos + 8:pos + 12]
            if lt == b"movi":
                break
            if lt in (b"hdrl", b"strl"):
                pos += 12
                continue
        elif cid == b"avih" and pos + 48 <= len(d):
            us = u32(pos + 8)
            if us:
                r["fps"] = 1e6 / us
            r["frames"], r["w"], r["h"] = u32(pos + 24), u32(pos + 40), u32(pos + 44)
        elif cid == b"strh" and pos + 36 <= len(d):
            cur = d[pos + 8:pos + 12]
            if cur == b"vids":
                r["vfourcc"] = d[pos + 12:pos + 16].decode("latin-1").lower()
                scale, rate = u32(pos + 28), u32(pos + 32)
                if scale:
                    r["fps"] = rate / scale
        elif cid == b"strf" and pos + 28 <= len(d):
            if cur == b"vids":
                bc = d[pos + 24:pos + 28].decode("latin-1").lower()
                if bc.strip("\0"):
                    r["vfourcc"] = bc
            elif cur == b"auds":
                r.update(has_audio=True, atag=u16(pos + 8), ach=u16(pos + 10),
                         arate=u32(pos + 12), abits=u16(pos + 22))
        pos += 8 + sz + (sz & 1)
    return r


def ps_info(p):
    d = read_head(p, 1 << 20)
    r = dict(mpeg2=len(d) > 4 and (d[4] & 0xC0) == 0x40, w=0, h=0, fps=0.0, alayer=0, akbps=0, arate=0, ach=0)
    i = d.find(b"\x00\x00\x01\xb3")
    if 0 <= i and i + 8 <= len(d):
        r["w"] = (d[i + 4] << 4) | (d[i + 5] >> 4)
        r["h"] = ((d[i + 5] & 0xF) << 8) | d[i + 6]
        frc = d[i + 7] & 0xF
        if 1 <= frc <= 8:
            r["fps"] = FPS_LIST[frc]
    i = d.find(b"\x00\x00\x01\xc0")
    if 0 <= i and i + 16 < len(d):
        k = i + 6                                       # MPEG-1 PES header
        while k < len(d) and d[k] == 0xFF:
            k += 1
        if k < len(d) and d[k] & 0xC0 == 0x40:
            k += 2
        if k < len(d):
            t = d[k] & 0xF0
            k += 5 if t == 0x20 else 10 if t == 0x30 else 1
        for k in range(k, min(len(d) - 4, k + 4096)):
            if d[k] != 0xFF or d[k + 1] & 0xF0 != 0xF0:
                continue
            layer = 4 - ((d[k + 1] >> 1) & 3)
            bi, si = (d[k + 2] >> 4) & 0xF, (d[k + 2] >> 2) & 3
            if layer > 3 or bi in (0, 15) or si == 3:
                continue
            r["alayer"] = layer
            if layer == 2:
                r["akbps"] = MP2_RATES[bi - 1]
            r["arate"] = (44100, 48000, 32000)[si]
            r["ach"] = 1 if (d[k + 3] >> 6) & 3 == 3 else 2
            break
    return r


def wd1_info_text(p, kind):
    if kind["ext"] == "avi":
        i = avi_info(p)
        vn = FOURCC_NAMES.get(i["vfourcc"], i["vfourcc"].upper())
        dur = f" ({i['frames'] / i['fps']:.1f} s)" if i["fps"] > 0 else ""
        t = f"AVI: {vn} {i['w']}x{i['h']} {fmt_fps(i['fps'])} fps, {i['frames']} frames{dur}"
        if i["has_audio"]:
            an = WAVE_NAMES.get(i["atag"], f"format 0x{i['atag']:X}")
            t += f"; audio {an} {i['abits']}-bit {i['arate']} Hz {ch_name(i['ach'])}"
        else:
            t += "; no audio"
        return t
    i = ps_info(p)
    m = "MPEG-2" if i["mpeg2"] else "MPEG-1"
    t = f"{m} system stream: {m} video {i['w']}x{i['h']} {fmt_fps(i['fps'])} fps"
    if i["alayer"]:
        kb = f" {i['akbps']}k" if i["akbps"] else ""
        t += f"; audio MP{i['alayer']}{kb} {i['arate']} Hz {ch_name(i['ach'])}"
    return t


def wd1_playable(p, kind):
    """'' if the game should play this file copied in unchanged, else the reason why not."""
    if kind["ext"] == "avi":
        i = avi_info(p)
        if i["vfourcc"] not in WD1_FOURCCS:
            return f"video codec '{i['vfourcc'].upper()}' is not one the game uses (Cinepak, Indeo, MPEG-1)"
        if i["has_audio"] and i["atag"] not in WD1_WAVE_TAGS:
            return f"audio format 0x{i['atag']:X} is not one the game uses (PCM)"
        return ""
    if kind["mpeg2"]:
        return "MPEG-2 stream (the game files are MPEG-1)"
    i = ps_info(p)
    if not i["w"]:
        return "no MPEG-1 video found"
    if i["alayer"] and i["alayer"] != 2:
        return f"audio is MPEG layer {i['alayer']} (the game files use MP2)"
    return ""


def wd1_to_video(path, out, a):
    kind = wd1_kind(path)
    if not kind:
        err(f"{path}: not an AVI or MPEG file - unknown .wd1 content")
        return False
    info(f"{path}: {wd1_info_text(path, kind)}")
    if a.info:
        return True
    if a.format == "raw":                               # it already is an AVI / MPG
        o = change_ext(out, "." + kind["ext"])
        shutil.copyfile(path, o)
        ok(f"{o} (original file, copied as is)")
        return True
    init_ffmpeg(a)
    mi = media_info(path)
    c = a.format
    vc, ac = a.vcodec, a.acodec
    if vc == "copy" and "*" not in COPY_V[c] and mi["vcodec"] not in COPY_V[c]:
        vc = FALL_V[c]
        info(f"{path}: {mi['vcodec']} video can't go into .{c} as is - encoding {vc}")
    has_a = bool(mi["acodec"]) and not a.no_audio
    if has_a and ac == "copy" and "*" not in COPY_A[c] and mi["acodec"] not in COPY_A[c]:
        ac = FALL_A[c]
        info(f"{path}: {mi['acodec']} audio can't go into .{c} as is - encoding {ac}")
    args = ["-fflags", "+genpts", "-i", path, "-map", "0:v:0"]
    if has_a:
        args += ["-map", "0:a:0"] + audio_args(a, ac, "160k" if ac == "opus" else "192k")
        # MPEG-1 MP2 only has 32 / 44.1 / 48 kHz (e.g. 22050 Hz AVIs need resampling)
        if ac == "mp2" and not a.rate and mi["arate"] not in (32000, 44100, 48000):
            args += ["-ar", "44100"]
    else:
        args += ["-an"]
    args += video_args(a, vc, mi["fps"], out_size(a, mi["w"], mi["h"]))
    if c in ("mp4", "mov"):
        args += ["-movflags", "+faststart"]
    code, msg = run_ffmpeg(args + [out])
    if code:
        print(msg)
        err(f"ffmpeg failed for {path}")
        return False
    how = ["video copied" if vc == "copy" else f"video {vc}"]
    if has_a:
        how.append("audio copied" if ac == "copy" else f"audio {ac}")
    ok(f"{out} ({', '.join(how)})")
    return True


def wd1_to_audio(path, out, a):
    kind = wd1_kind(path)
    if not kind:
        err(f"{path}: not an AVI or MPEG file - unknown .wd1 content")
        return False
    info(f"{path}: {wd1_info_text(path, kind)}")
    if a.info:
        return True
    init_ffmpeg(a)
    mi = media_info(path)
    if not mi["acodec"]:
        err(f"{path}: has no audio track")
        return False
    reenc = bool(a.abitrate or a.rate or a.channels)
    fmt = a.audio_format
    if fmt == "raw":
        fmt = {"mp2": "mp2", "mp3": "mp3"}.get(mi["acodec"], "wav")
    copy = not reenc and ((fmt == "mp2" and mi["acodec"] == "mp2") or (fmt == "mp3" and mi["acodec"] == "mp3")
                          or (fmt == "wav" and mi["acodec"] == "pcm_s16le"))
    cargs = ["-c:a", "copy"] + (["-f", "mp2"] if fmt == "mp2" else []) if copy else audio_format_args(a, fmt)
    out = change_ext(out, "." + fmt)
    code, msg = run_ffmpeg(["-i", path, "-vn", "-map", "0:a:0"] + cargs + [out])
    if code:
        print(msg)
        err(f"ffmpeg failed for {path}")
        return False
    ok(f"{out} ({'original audio, copied as is' if copy else 'encoded'})")
    return True


def to_wd1(path, out, a):
    # the .wd1 being replaced (if any) decides the format to match
    tgt = None
    if os.path.isfile(out):
        tk = wd1_kind(out)
        if tk:
            i = avi_info(out) if tk["ext"] == "avi" else ps_info(out)
            tgt = dict(kind=tk["ext"], w=i["w"], h=i["h"], fps=i["fps"], arate=i["arate"],
                       ach=i["ach"], akbps=i.get("akbps", 0))
            info(f"replacing {base(out)}: {wd1_info_text(out, tk)}")

    # 1) lossless: an AVI / MPG the game can play is copied in unchanged
    force = a.force_video or a.wd1_format != "auto" or a.abitrate or a.rate or a.channels
    if not force:
        sk = wd1_kind(path)
        if sk:
            why = wd1_playable(path, sk)
            if not why:
                if tgt and tgt["kind"] != sk["ext"]:
                    warn(f"{path}: is {sk['ext'].upper()} but the file being replaced is {tgt['kind'].upper()} "
                         f"(use --reencode to match it)")
                backup_if_exists(out)
                shutil.copyfile(path, out)
                ok(f"{out}: {wd1_info_text(out, sk)}, copied as is (lossless)")
                return True
            info(f"{path}: can't be copied as is ({why}) - re-encoding")
        else:
            info(f"{path}: not an AVI or MPG the game can play as is - encoding it for the game")

    # 2) encode, matching the file being replaced
    init_ffmpeg(a)
    src = media_info(path)
    if not src["vcodec"]:
        err(f"{path}: no video stream")
        return False
    fmt = a.wd1_format if a.wd1_format != "auto" else (tgt["kind"] if tgt else "mpg")
    if a.set_by_user & {"width", "height"}:
        w, h = a.width, a.height
    elif tgt and tgt["w"]:
        w, h = tgt["w"], tgt["h"]
    else:
        w, h = src["w"], src["h"]
    m = 4 if fmt == "avi" else 2                        # Cinepak: multiple of 4, MPEG-1: even
    w, h = w - w % m, h - h % m
    if w < 16 or h < 16:
        err(f"{path}: could not determine a frame size - give --width and --height")
        return False
    fps = a.fps or (tgt["fps"] if tgt and tgt["fps"] > 0 else 0) or src["fps"] or DEFAULT_FPS
    if fmt == "mpg":
        fps = snap_fps(fps)
    rate = a.rate or (tgt["arate"] if tgt and tgt["arate"] else 44100)
    if fmt == "mpg" and rate not in (32000, 44100, 48000):
        info(f"{path}: MPEG-1 audio can't be {rate} Hz - using 44100 Hz")
        rate = 44100
    ch = a.channels or (tgt["ach"] if tgt and tgt["ach"] else 2)
    if a.stretch:
        vf = f"scale={w}:{h}"
    else:
        vf = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black"
    args = ["-i", path, "-map", "0:v:0"] + (["-map", "0:a:0"] if src["acodec"] else [])
    args += ["-vf", vf + ",setsar=1", "-r", fps_arg(fps)]
    if fmt == "mpg":
        q = a.q if "q" in a.set_by_user else 2
        br = a.abitrate or (f"{tgt['akbps']}k" if tgt and tgt["akbps"] else "160k")
        check_mp2_combo(kbps_value(br), ch, "audio bitrate")
        args += ["-c:v", "mpeg1video", "-q:v", q, "-g", "15", "-bf", "2", "-pix_fmt", "yuv420p"]
        if src["acodec"]:
            args += ["-c:a", "mp2", "-b:a", br, "-ar", rate, "-ac", ch]
        args += ["-f", "mpeg"]
        desc = f"MPEG-1 {w}x{h} {fmt_fps(fps)} fps" + (f", MP2 {br} {rate} Hz" if src["acodec"] else "")
    else:
        args += ["-c:v", "cinepak"]
        if src["acodec"]:
            args += ["-c:a", "pcm_s16le", "-ar", rate, "-ac", ch]
        args += ["-f", "avi"]
        desc = f"AVI Cinepak {w}x{h} {fmt_fps(fps)} fps" + (f", PCM {rate} Hz" if src["acodec"] else "")
    tmp = temp_file("." + fmt)
    code, msg = run_ffmpeg(args + [tmp])
    if code:
        remove_quiet(tmp)
        print(msg)
        err(f"ffmpeg failed for {path}")
        return False
    backup_if_exists(out)
    move_over(tmp, out)
    match = " (matching the file it replaces)" if tgt and a.wd1_format == "auto" else ""
    ok(f"{out}: {desc}, encoded{match}")
    return True


# ============================================================== job list

EXPECT = {"video": "a game video (.twv / .wd1)", "twv": "a video file", "audio": "game audio (.tws / .wd1)",
          "tws": "an audio or video file", "wd1": "a video file"}


def file_kind(p):
    e = ext_of(p)
    if e in (".twv", ".tws", ".wd1"):
        return e[1:]
    return "audio" if e in AUDIO_EXT else "video"


def folder_files(root, a):
    """(file, path of its folder relative to root) for every file in root - and, with
    --recursive, in all its subfolders. The output folder is skipped, so results of an
    earlier run saved inside the input folder are not converted again."""
    skip = os.path.normcase(os.path.abspath(a.output)) if a.output else None
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted((x for x in dirs if os.path.normcase(os.path.abspath(os.path.join(d, x))) != skip),
                         key=str.lower) if a.recursive else []
        rel = os.path.relpath(d, root)
        for name in sorted(files, key=str.lower):
            yield os.path.join(d, name), "" if rel == "." else rel


def build_jobs(mode, inputs, a):
    """Returns (file, mode, subfolder relative to the input folder) for every file to convert."""
    jobs = []
    for i in inputs:
        if os.path.isdir(i):
            dmode = "video" if mode == "auto" else mode
            found = 0
            for f, rel in folder_files(i, a):
                e = ext_of(f)
                take = {"video": e in (".twv", ".wd1") or (a.info and e == ".tws"),
                        "twv": e in VIDEO_EXT, "audio": e in (".tws", ".wd1"),
                        "tws": e in AUDIO_EXT, "wd1": e in VIDEO_EXT}[dmode]
                if take:
                    jobs.append((os.path.abspath(f), "audio" if a.info and e == ".tws" else dmode, rel))
                    found += 1
            if not found:
                where = "this folder or its subfolders" if a.recursive else \
                        "this folder (use --recursive to include subfolders)"
                warn(f"{i}: no matching files in {where}")
            continue
        full = os.path.abspath(i)
        kind = file_kind(full)
        d = mode
        if d == "auto":
            d = {"twv": "video", "wd1": "video", "tws": "audio", "audio": "tws"}.get(kind, "twv")
        if a.info and d not in ("video", "audio"):
            warn(f"{base(full)}: not a game file (.twv / .tws / .wd1) - nothing to inspect")
            continue
        if d == "video" and kind == "tws":              # a .tws given for game video: use its .twv
            tw = find_sibling(full, ".twv")
            if not tw:
                err(f"{full}: no matching .twv next to this .tws (for sound only, use mode 'audio')")
                continue
            full, kind = tw, "twv"
        bad = {"video": kind not in ("twv", "wd1"), "twv": kind not in ("video", "wd1"),
               "audio": kind not in ("tws", "wd1"), "tws": kind in ("twv", "tws"),
               "wd1": kind not in ("video", "wd1")}[d]
        if bad:
            warn(f"{base(full)}: is not {EXPECT[d]} - skipped")
            continue
        jobs.append((full, d, ""))
    return jobs


def out_ext(job_mode, f, a):
    if job_mode == "video":
        if a.format != "raw":
            return "." + a.format
        if ext_of(f) == ".wd1":
            k = wd1_kind(f)
            return "." + (k["ext"] if k else "avi")
        return ".m1v"
    if job_mode == "audio":
        return ".mp2" if a.audio_format == "raw" else "." + a.audio_format   # .wd1: set by the converter
    return {"twv": ".twv", "tws": ".tws", "wd1": ".wd1"}[job_mode]


def run_jobs(mode, inputs, a):
    """Runs one batch. Returns the number of failed files."""
    missing = [i for i in inputs if not os.path.exists(i)]
    for m in missing:
        err(f"not found: {m}")
    if missing:
        print('    Tip: put names with spaces in quotes, e.g. python twv2mp4.py "My video.mp4"')
        return 1
    jobs = build_jobs(mode, inputs, a)
    out_is_dir = len(inputs) > 1 or any(os.path.isdir(i) for i in inputs)
    failed = 0
    seen = {}
    for f, d, rel in jobs:
        name = os.path.splitext(base(f))[0] + out_ext(d, f, a)
        if a.output and not out_is_dir and ext_of(a.output) and not os.path.isdir(a.output):
            out = a.output
        else:
            # with -o, files from subfolders keep the same folder structure under it
            od = os.path.join(a.output, rel) if a.output else os.path.dirname(f)
            if not a.info:
                os.makedirs(od, exist_ok=True)
            out = os.path.join(od, name)
        out = os.path.abspath(out)
        if not a.info:
            os.makedirs(os.path.dirname(out), exist_ok=True)
            if os.path.normcase(out) == os.path.normcase(f):
                err(f"{f}: the result would overwrite this file - choose another place to save (-o)")
                failed += 1
                continue
            key = os.path.normcase(out)
            if key in seen:
                err(f"{f}: same output as {seen[key]} - skipped (rename one, or save somewhere else)")
                failed += 1
                continue
            seen[key] = f
        try:
            done = {"video": game_to_video, "audio": tws_to_audio, "twv": to_twv,
                    "tws": lambda p, o, x: write_tws(p, o, x), "wd1": to_wd1}[d](f, out, a)
        except ConvError as e:
            err(f"{f}: {e}")
            done = False
        except Exception as e:                          # one bad file does not stop the batch
            err(f"{f}: unexpected error: {type(e).__name__}: {e}")
            done = False
        failed += not done
    if len(jobs) > 1:
        print(f"\nSummary: {len(jobs) - failed} OK, {failed} failed (of {len(jobs)} files)")
    if not jobs:
        err("no files to convert")
        failed = 1
    return failed


# ============================================================== command-line paths

def clean_path(p):
    """Strips the quotes and '& ' that dragging a file into a console adds."""
    p = p.strip()
    if p.startswith("& "):
        p = p[2:].strip()
    while len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'":
        p = p[1:-1].strip()
    return p


def fix_args(args):
    """Rejoins paths split at spaces, e.g. ['My', 'video.mp4'] -> ['My video.mp4']."""
    args = [clean_path(x) for x in args if x.strip()]
    out, i = [], 0
    while i < len(args):
        if os.path.exists(args[i]):
            out.append(args[i])
            i += 1
            continue
        for j in range(i + 2, len(args) + 1):
            cand = " ".join(args[i:j])
            if os.path.exists(cand):
                out.append(cand)
                i = j
                break
        else:
            out.append(args[i])
            i += 1
    return out


def split_path_list(line):
    """Splits a typed / dropped line into paths: "quoted paths", & 'paths' from PowerShell
    drag-and-drop, several dropped files at once, and | as a separator."""
    out = []
    for part in line.split("|"):
        toks = []
        for m in re.finditer(r'"([^"]*)"|\'([^\']*)\'|(\S+)', part):
            if m.group(1) is not None:
                toks.append((m.group(1), True))
            elif m.group(2) is not None:
                toks.append((m.group(2), True))
            elif m.group(3) != "&":
                toks.append((m.group(3), False))
        i = 0
        while i < len(toks):
            t, quoted = toks[i]
            if quoted or os.path.exists(t):
                out.append(t)
                i += 1
                continue
            for j in range(i + 1, len(toks)):           # unquoted path with spaces
                cand = " ".join(x for x, _ in toks[i:j + 1])
                if os.path.exists(cand):
                    out.append(cand)
                    i = j + 1
                    break
            else:
                out.append(t)
                i += 1
    return [x for x in out if x]


# ============================================================== interactive menu

_NO_INPUT = False

ACTION_NAMES = {"video": "Game video -> normal video", "audio": "Game audio -> normal audio",
                "twv": "Video -> TWV + TWS game files", "tws": "Audio -> TWS game file",
                "wd1": "Video -> WD1 game file (Earth 2150)", "info": "Inspect game files"}
ACTION_INPUTS = {"video": ".twv or .wd1 files, or a folder with them",
                 "audio": ".tws or .wd1 files, or a folder with them",
                 "twv": "video files (.mp4 .mkv .avi .mpg .m1v ...), or a folder with them",
                 "tws": "audio files (.wav .mp3 .flac .mp2 ...), or a folder with them",
                 "wd1": "video files (.mp4 .mkv .avi .mpg ...), or a folder with them",
                 "info": ".twv, .tws or .wd1 files, or a folder with them"}


def read_line(prompt):
    global _NO_INPUT
    if _NO_INPUT:
        return None
    try:
        return input(prompt).strip()
    except EOFError:
        _NO_INPUT = True
        return None


def clear_screen():
    try:
        if sys.stdin.isatty() and sys.stdout.isatty():
            os.system("cls" if ON_WINDOWS else "clear")
    except Exception:
        pass


def title(t):
    print()
    print(_paint("36", f"  {t}"))
    print(_paint("36", "  " + "=" * len(t)))


def section(t):
    print()
    print(_paint("1", f"  {t}"))


def note(t):
    for l in t.split("\n"):
        print(_paint("90", f"  {l}"))


def item(key, label, desc="", default=False):
    line = _paint("33", f"  {key:>3}) ") + label
    if default:
        line += _paint("32", "   <- Enter")
    print(line)
    for l in (desc.split("\n") if desc else []):
        print(_paint("90", f"       {l}"))


def read_choice(keys, default):
    for _ in range(20):
        v = read_line(f"  Your choice (Enter = {default}): " if default else "  Your choice: ")
        if v is None:
            return "Q"
        v = v.upper()
        if v == "" and default:
            return default.upper()
        if v in keys:
            return v
        err(f"please type one of: {', '.join(keys)}")
    return "Q"


def read_menu(question, items, default=1, back=True):
    """Numbered list; returns 1..n, or 0 for Back."""
    section(question)
    keys = []
    for n, (label, desc) in enumerate(items, 1):
        keys.append(str(n))
        item(str(n), label, desc, n == default)
    if back:
        item("B", "Back")
        keys += ["B", "Q"]
    c = read_choice(keys, str(default))
    return 0 if c in ("B", "Q") else int(c)


def read_default(prompt, default):
    v = read_line(f"  {prompt} (Enter = {default}): ")
    return default if not v else v


def read_size(prompt, mult):
    for _ in range(20):
        v = read_line(f"  {prompt} (e.g. 640x480): ")
        if v is None:
            return None
        m = re.match(r"^(\d+)\s*[xX]\s*(\d+)$", v)
        if m and int(m.group(1)) % mult == 0 and int(m.group(2)) % mult == 0:
            return int(m.group(1)), int(m.group(2))
        err("type width x height" + (f" (multiples of {mult})" if mult > 1 else ""))
    return None


def read_bitrate(prompt, default):
    for _ in range(20):
        v = read_default(prompt, default)
        try:
            k = kbps_value(v)
            if k > 0:
                return f"{k}k"
        except ConvError:
            pass
        err("type a bitrate like 192k")
    return default


# ---------------------------------------------------------------- 1) game video -> video
def video_options(a):
    title("Game video -> normal video")
    note("Turns .twv videos (TWV games) and .wd1 videos (Earth 2150) into files you can play or edit.")
    section("To watch  (uses ffmpeg - downloaded once if missing)")
    item("1", "MP4 - plays everywhere (Windows, phones, browsers)", "H.264 + AAC", True)
    item("2", "MP4 - smaller file", "H.265 + AAC (older devices may not play it)")
    item("3", "WebM - for web pages", "VP9 + Opus")
    section("Original quality  (nothing is re-encoded)")
    item("4", "Exact copy of the original streams",
         ".twv -> .m1v (video) + .mp2 (sound)     .wd1 -> .avi or .mpg\n"
         "Plays in VLC / MPC-HC. No ffmpeg needed.")
    item("5", "One MKV file with the original streams", "Video and sound in one file. Plays in VLC / MPC-HC.")
    item("6", "One MPG file (classic MPEG-1)", "Lossless for .twv; .wd1 AVI videos are converted to MPEG-1.")
    section("Other")
    item("7", "Advanced - choose everything yourself", "container, codecs, quality, size, sound")
    item("B", "Back")
    c = read_choice(["1", "2", "3", "4", "5", "6", "7", "B", "Q"], "1")
    presets = {"1": ("mp4", "h264", "aac", "MP4, H.264 + AAC 192 kbps"),
               "2": ("mp4", "h265", "aac", "MP4, H.265 + AAC 192 kbps"),
               "3": ("webm", "vp9", "opus", "WebM, VP9 + Opus 160 kbps"),
               "4": ("raw", "copy", "copy", "exact copy of the original streams (.m1v + .mp2, or .avi / .mpg)"),
               "5": ("mkv", "copy", "copy", "one MKV file, original streams"),
               "6": ("mpg", "copy", "copy", "one MPG file, original streams where possible")}
    if c == "7":
        return video_advanced(a)
    if c in presets:
        a.format, a.vcodec, a.acodec, a.choice_text = presets[c]
        return True
    return False


def video_advanced(a):
    conts = ["mp4", "mkv", "mov", "avi", "webm", "mpg"]
    n = read_menu("File type (container)", [("MP4", "plays everywhere"), ("MKV", "holds any stream, VLC / MPC-HC"),
                                            ("MOV", "QuickTime"), ("AVI", "classic Windows video"),
                                            ("WebM", "for web pages (VP9 / AV1 only)"), ("MPG", "classic MPEG-1")])
    if not n:
        return False
    a.format = conts[n - 1]
    vnames = {"copy": ("Keep the original video", "lossless; encoded only if the container cannot hold it"),
              "h264": ("H.264", "plays everywhere"), "h265": ("H.265", "smaller, newer devices"),
              "vp9": ("VP9", "web"), "av1": ("AV1", "smallest, slow to encode, newest devices"),
              "mpeg1": ("MPEG-1", "very old players")}
    vok = V_ALLOWED[a.format]
    n = read_menu("Video", [vnames[v] for v in vok])
    if not n:
        return False
    a.vcodec = vok[n - 1]
    size_txt = ""
    if a.vcodec != "copy":
        d = CRF_DEFAULT[a.vcodec]
        q = read_default(f"Video quality: a number (lower = better, {d} is very good) or a bitrate like 4M", str(d))
        if q.isdigit():
            a.crf = int(q)
        else:
            a.vbitrate = q
        n = read_menu("Picture size", [("Original", ""), ("Double", "e.g. 640x256 -> 1280x512"), ("Type a size", "")])
        if not n:
            return False
        if n == 2:
            a.size, size_txt = "double", ", double size"
        elif n == 3:
            wh = read_size("Size", 2)
            if wh:
                a.size = f"{wh[0]}x{wh[1]}"
                size_txt = f", {a.size}"
    anames = {"copy": ("Keep the original sound", "lossless; encoded only if the container cannot hold it"),
              "aac": ("AAC", "plays everywhere"), "mp3": ("MP3", ""), "mp2": ("MP2", ""),
              "opus": ("Opus", "small, good quality"), "vorbis": ("Vorbis", ""),
              "flac": ("FLAC", "lossless"), "pcm": ("PCM / WAV", "lossless, large")}
    aok = A_ALLOWED[a.format]
    n = read_menu("Sound", [anames[x] for x in aok])
    if not n:
        return False
    a.acodec = aok[n - 1]
    if a.acodec in ("aac", "mp3", "mp2", "opus", "vorbis"):
        a.abitrate = read_bitrate("Sound bitrate", "160k" if a.acodec == "opus" else "192k")
    vq = ("original" if a.vcodec == "copy" else
          f"{a.vcodec} {a.vbitrate}" if a.vbitrate else f"{a.vcodec} quality {crf_for(a, a.vcodec)}")
    aq = "original" if a.acodec == "copy" else f"{a.acodec} {a.abitrate or ''}".strip()
    a.choice_text = f".{a.format}, video {vq}{size_txt}, sound {aq}"
    return True


# ---------------------------------------------------------------- 2) game audio -> audio
def audio_options(a):
    title("Game audio -> normal audio")
    note("Takes the sound from .tws files (TWV games) or from .wd1 videos (Earth 2150).")
    section("Lossless")
    item("1", "WAV  - lossless, plays everywhere, large", "", True)
    item("2", "FLAC - lossless, about half the size of WAV")
    item("3", "Exact copy of the original sound",
         ".tws -> .mp2     .wd1 -> .wav or .mp2 (whatever is inside)\nNo ffmpeg needed for .tws.")
    section("Smaller file  (uses ffmpeg)")
    item("4", "MP3  - plays everywhere")
    item("5", "M4A  - AAC, plays everywhere")
    item("6", "OGG  - Vorbis")
    item("7", "Opus - smallest for the quality")
    item("8", "MP2  - same format as the game, different bitrate")
    item("B", "Back")
    c = read_choice(["1", "2", "3", "4", "5", "6", "7", "8", "B", "Q"], "1")
    if c in ("B", "Q"):
        return False
    a.audio_format = {"1": "wav", "2": "flac", "3": "raw", "4": "mp3", "5": "m4a",
                      "6": "ogg", "7": "opus", "8": "mp2"}[c]
    a.choice_text = {"raw": "exact copy of the original sound", "wav": "WAV (lossless)",
                     "flac": "FLAC (lossless)", "mp3": "MP3", "m4a": "M4A (AAC)", "ogg": "OGG (Vorbis)",
                     "opus": "Opus", "mp2": "MP2"}[a.audio_format]
    menus = {"mp3": ("MP3 quality", [("Best - variable bitrate, about 190 kbps", ""), ("320 kbps", ""),
                                     ("256 kbps", ""), ("192 kbps", ""), ("128 kbps", "smallest")],
                     [None, "320k", "256k", "192k", "128k"]),
             "m4a": ("M4A quality", [("192 kbps", "very good"), ("256 kbps", ""), ("128 kbps", "smaller")],
                     ["192k", "256k", "128k"]),
             "ogg": ("OGG quality", [("Best - variable bitrate, about 190 kbps", ""), ("256 kbps", ""),
                                     ("128 kbps", "smaller")], [None, "256k", "128k"]),
             "opus": ("Opus quality", [("160 kbps", "very good"), ("128 kbps", ""), ("96 kbps", "smaller")],
                      ["160k", "128k", "96k"]),
             "mp2": ("MP2 bitrate", [("160 kbps", "like most game files"), ("128 kbps", ""), ("192 kbps", ""),
                                     ("256 kbps", ""), ("384 kbps", "highest")],
                     ["160k", "128k", "192k", "256k", "384k"])}
    if a.audio_format in menus:
        q, items, vals = menus[a.audio_format]
        n = read_menu(q, items)
        if not n:
            return False
        a.abitrate = vals[n - 1]
        a.choice_text += f" {a.abitrate}" if a.abitrate else " best quality"
    if a.audio_format != "raw":
        n = read_menu("Sample rate and channels", [("Keep them as they are", ""), ("44.1 kHz stereo", "CD quality"),
                                                   ("48 kHz stereo", ""), ("22 kHz mono", "small, for voice")])
        if not n:
            return False
        a.rate, a.channels = {1: (None, None), 2: (44100, 2), 3: (48000, 2), 4: (22050, 1)}[n]
    return True


# ---------------------------------------------------------------- 3) video -> TWV + TWS
def twv_options(a):
    title("Video -> TWV + TWS game files")
    note("Makes a .twv video and its .tws sound for the TWV games (KnightShift,\nWorld War III, Heli Heroes, Panzer Claws ...).")
    n = read_menu("How?", [
        ("Automatic", "Lossless where possible: MPEG-1 video made by option 1 goes back unchanged.\n"
                      "Anything else is converted to the game's own format: 640x256, same\n"
                      "frame rate, best quality, MP2 160 kbps stereo sound."),
        ("Choose the settings yourself", "frame rate, size, quality, sound")])
    if not n:
        return False
    if n == 1:
        a.force_video = a.force_audio = False
        a.choice_text = "automatic (lossless where possible, else the game's own format)"
        return True
    a.force_video = True
    n = read_menu("Frame rate", [("Same as the source video", "rounded to the nearest the game supports"),
                                 ("25 fps", ""), ("24 fps", ""), ("30 fps", ""), ("29.97 fps", ""), ("23.976 fps", "")])
    if not n:
        return False
    if n > 1:
        a.fps = [0, 0, 25.0, 24.0, 30.0, 30000 / 1001, 24000 / 1001][n]
    n = read_menu("Picture size", [("640 x 256", "the size the game uses"), ("Type another size", "multiples of 16")])
    if not n:
        return False
    if n == 2:
        wh = read_size("Size", 16)
        if wh:
            a.width, a.height = wh
    n = read_menu("If the shape is different", [("Add black bars", "keeps the picture shape"), ("Stretch to fill", "")])
    if not n:
        return False
    a.stretch = n == 2
    n = read_menu("Picture quality", [("Best", "what the game files use (quantiser 1)"), ("Very good", "smaller file (2)"),
                                      ("Good", "much smaller (4)"), ("Type a value", "1-31, lower = better")])
    if not n:
        return False
    if n <= 3:
        a.q = (1, 2, 4)[n - 1]
    else:
        v = read_default("Quality 1-31", "1")
        if v.isdigit() and 1 <= int(v) <= 31:
            a.q = int(v)
    n = read_menu("Sound (.tws)", [("Make it - MP2 160 kbps stereo", "like most game files"),
                                   ("Make it - MP2 128 kbps stereo", ""), ("Make it - choose the settings", ""),
                                   ("No sound file", "video only")])
    if not n:
        return False
    a.force_audio = True
    if n == 1:
        a.tws_bitrate = "160k"
    elif n == 2:
        a.tws_bitrate = "128k"
    elif n == 3:
        if not tws_encode_options(a):
            return False
    else:
        a.no_tws = True
    fps_t = f"{fmt_fps(a.fps)} fps" if a.fps else "same frame rate"
    a.choice_text = (f"{a.width or GAME_W}x{a.height or GAME_H}, {fps_t}, quality {a.q or 1}; "
                     + ("no .tws" if a.no_tws else f".tws MP2 {a.tws_bitrate}"))
    return True


def tws_encode_options(a):
    n = read_menu("Channels", [("Stereo", "like the game files"), ("Mono", "")])
    if not n:
        return False
    a.tws_channels = (2, 1)[n - 1]
    rates = (160, 128, 192, 224, 256, 320, 384, 112, 96, 64) if a.tws_channels == 2 else \
            (160, 128, 192, 112, 96, 80, 64, 56, 48, 32)
    n = read_menu("Bitrate", [(f"{r} kbps", "used by the game files" if r in (128, 160) else "") for r in rates])
    if not n:
        return False
    a.tws_bitrate = f"{rates[n - 1]}k"
    n = read_menu("Sample rate", [("44.1 kHz", "like the game files"), ("48 kHz", ""), ("32 kHz", "")])
    if not n:
        return False
    a.tws_rate = (44100, 48000, 32000)[n - 1]
    return True


# ---------------------------------------------------------------- 4) audio -> TWS
def tws_options(a):
    title("Audio -> TWS game file")
    note("Makes a .tws sound file for the TWV games.")
    n = read_menu("How?", [("Automatic", "MP2 sound goes in unchanged. Anything else is converted to the\n"
                                         "game's own format: MP2 160 kbps, 44.1 kHz stereo."),
                           ("Choose the settings yourself", "bitrate, channels, sample rate")])
    if not n:
        return False
    if n == 1:
        a.force_audio = False
        a.choice_text = "automatic (MP2 kept as is, else MP2 160 kbps 44.1 kHz stereo)"
        return True
    a.force_audio = True
    if not tws_encode_options(a):
        return False
    a.choice_text = f"MP2 {a.tws_bitrate}, {a.tws_rate} Hz, {ch_name(a.tws_channels)}"
    return True


# ---------------------------------------------------------------- 5) video -> WD1
def wd1_options(a):
    title("Video -> WD1 game file (Earth 2150)")
    note("Makes a .wd1 video for Earth 2150.")
    n = read_menu("How?", [("Automatic", "An AVI / MPG the game can already play goes in unchanged. Anything\n"
                                         "else is converted to match the .wd1 it replaces (format, size, frame\n"
                                         "rate, sound), or to MPEG-1 if it replaces nothing."),
                           ("Choose the settings yourself", "format, size, frame rate, quality, sound")])
    if not n:
        return False
    if n == 1:
        a.force_video = a.force_audio = False
        a.choice_text = "automatic (copied if the game can play it, else converted to match)"
        return True
    a.force_video = a.force_audio = True
    n = read_menu("Format", [("Same as the .wd1 it replaces", "MPEG-1 if it replaces nothing"),
                             ("MPEG-1 video + MP2 sound", "like videoED.wd1"),
                             ("AVI Cinepak video + PCM sound", "like the AVI game videos")])
    if not n:
        return False
    a.wd1_format = ("auto", "mpg", "avi")[n - 1]
    n = read_menu("Picture size", [("Same as the .wd1 it replaces", "else the size of your video"),
                                   ("256 x 192", "in-game briefing videos"), ("640 x 480", ""), ("Type a size", "")])
    if not n:
        return False
    if n == 2:
        a.width, a.height = 256, 192
    elif n == 3:
        a.width, a.height = 640, 480
    elif n == 4:
        wh = read_size("Size", 4)
        if wh:
            a.width, a.height = wh
    n = read_menu("Frame rate", [("Same as the .wd1 it replaces", "else the frame rate of your video"), ("15 fps", ""),
                                 ("24 fps", ""), ("25 fps", ""), ("30 fps", "")])
    if not n:
        return False
    if n > 1:
        a.fps = [0, 0, 15.0, 24.0, 25.0, 30.0][n]
    n = read_menu("If the shape is different", [("Add black bars", "keeps the picture shape"), ("Stretch to fill", "")])
    if not n:
        return False
    a.stretch = n == 2
    if a.wd1_format != "avi":
        n = read_menu("Picture quality (MPEG-1)", [("Very good", "quantiser 2"), ("Best", "bigger file (1)"),
                                                   ("Good", "smaller (4)")])
        if not n:
            return False
        a.q = (2, 1, 4)[n - 1]
        n = read_menu("Sound bitrate (MP2)", [("Same as the .wd1 it replaces", "else 160 kbps"),
                                              ("112 kbps", "like videoED.wd1"), ("160 kbps", ""), ("192 kbps", "")])
        if not n:
            return False
        if n > 1:
            a.abitrate = ("", "112k", "160k", "192k")[n - 1]
    n = read_menu("Sound sample rate", [("Same as the .wd1 it replaces", "else 44.1 kHz"),
                                        ("22 kHz", "like the AVI game videos"), ("44.1 kHz", "")])
    if not n:
        return False
    a.rate = {1: None, 2: 22050, 3: 44100}[n]
    a.choice_text = "converted: " + {"auto": "same format as the file it replaces", "mpg": "MPEG-1 + MP2",
                                     "avi": "AVI Cinepak + PCM"}[a.wd1_format]
    return True


# ---------------------------------------------------------------- help
def show_help():
    title("Help")
    section("Game file types")
    note("  .twv   video of the TWV games: KnightShift, World War III: Black Gold,\n"
         "         Heli Heroes, Panzer Claws ... (MPEG-1 video inside)")
    note("  .tws   the sound for the .twv with the same name (MP2 audio inside)")
    note("  .wd1   Earth 2150 video - really an AVI or MPG file with another name")
    section("Original quality vs. converted")
    note("  The \"original quality\" options copy the video and sound exactly as they are in\n"
         "  the game file - nothing is lost, and copying back gives the identical game file.\n"
         "  Converting re-encodes: you can pick any format, but some quality is lost each time.")
    section("Replacing game files")
    note("  Options 3-5 ask which game file to replace. The original is kept next to it as\n"
         "  name.twv.bak / name.tws.bak / name.wd1.bak - rename it back to undo.\n"
         "  A new .wd1 is made to match the file it replaces.")
    section("Downloads")
    note("  Converting needs ffmpeg. The first time it is needed it is downloaded\n"
         "  (about 85 MB, checksum verified) into the \"tools\" folder next to this script.")
    section("Choosing files")
    note("  Drag files or a folder into this window and press Enter. Several files can be\n"
         "  dropped at once, or separated with  |")
    read_line("\n  Press Enter to go back ")


# ---------------------------------------------------------------- file selection
def read_inputs(mode):
    section("Which files?")
    note(f"Expected: {ACTION_INPUTS[mode]}")
    note("Drag them (or a folder) into this window and press Enter.  B = back")
    for _ in range(20):
        line = read_line("  Files: ")
        if line is None:
            return None
        if line == "":
            continue
        if line.upper() in ("B", "Q"):
            return None
        lst = split_path_list(line)
        missing = [x for x in lst if not os.path.exists(x)]
        if lst and not missing:
            return lst
        for m in missing:
            err(f"not found: {m}")
        note("Try again, or type B to go back.")
    return None


def read_replace_target(mode, inputs, a):
    e = {"twv": ".twv", "tws": ".tws", "wd1": ".wd1"}[mode]
    section("Replace a game file?  (optional)")
    single = len(inputs) == 1 and os.path.isfile(inputs[0])
    if single:
        note(f"Drag the game's {e} file to replace into this window (it is kept as {e}.bak),")
        note("drag a folder to save into, or just press Enter to save next to your file.")
        if mode == "wd1":
            note("Replacing a .wd1 makes the new video match its format, size and sound.")
    else:
        note("Drag a folder to save the new files into (files with the same names are replaced")
        note(f"and kept as {e}.bak), or just press Enter to save next to each input.")
    for _ in range(20):
        line = read_line("  Replace / save to: ")
        if not line:
            return
        t = split_path_list(line)
        if len(t) != 1:
            err("please give one file or folder")
            continue
        t = t[0]
        if os.path.isdir(t):
            a.output = os.path.abspath(t)
            return
        if single and os.path.isdir(os.path.dirname(os.path.abspath(t))):
            a.output = change_ext(os.path.abspath(t), e)
            return
        err(f"not found: {t}")


def confirm_run(mode, inputs, a):
    """'go' | 'back' | 'quit'"""
    for _ in range(20):
        section("Ready")
        sub = " (with subfolders)" if a.recursive else ""
        names = [f"folder {base(os.path.normpath(x))}{sub}" if os.path.isdir(x) else base(x) for x in inputs]
        files = ", ".join(names) if len(names) <= 3 else f"{', '.join(names[:3])} and {len(names) - 3} more"
        if not a.output:
            dest = "next to each input file"
        elif os.path.isfile(a.output):
            dest = f"{a.output}  (replaces it; the original is kept as .bak)"
        else:
            dest = a.output
        print(_paint("90", "    Task   : ") + ACTION_NAMES[mode])
        print(_paint("90", "    How    : ") + a.choice_text)
        print(_paint("90", "    Files  : ") + files)
        print(_paint("90", "    Save to: ") + dest)
        print()
        print("  " + _paint("32", "Enter") + " = start    " +
              ("" if mode == "info" else _paint("33", "S") + " = save somewhere else    ") +
              _paint("33", "B") + " = back to the menu    " + _paint("33", "Q") + " = quit")
        v = read_line("  Your choice (Enter = start): ")
        if v is None:
            return "quit"
        v = v.upper()
        if v == "":
            return "go"
        if v == "B":
            return "back"
        if v == "Q":
            return "quit"
        if v == "S" and mode != "info":
            l = read_line("  Drag a folder here (or type one; it is created if needed): ")
            t = split_path_list(l or "")
            if len(t) == 1:
                a.output = os.path.abspath(t[0])
        else:
            err("press Enter, S, B or Q")
    return "quit"


def show_menu():
    clear_screen()
    title("Reality Pump / TopWare - Media Converter")
    note("Videos and sounds of the TWV games (KnightShift, World War III, Heli Heroes,\nPanzer Claws ...) and Earth 2150.")
    section("Get media OUT of the game  (to watch, share or edit)")
    item("1", "Game video -> normal video", ".twv  .wd1   ->  .mp4  .mkv  .avi ...")
    item("2", "Game audio -> normal audio", ".tws  .wd1   ->  .wav  .mp3  .flac ...")
    section("Make files FOR the game  (to mod or replace)")
    item("3", "Video -> TWV + TWS          (TWV games)", "any video  ->  .twv + .tws")
    item("4", "Audio -> TWS                (TWV games)", "any audio  ->  .tws")
    item("5", "Video -> WD1                (Earth 2150)", "any video  ->  .wd1")
    section("Other")
    item("I", "Inspect files - show what is inside, change nothing")
    item("H", "Help - file types, original quality, backups")
    item("Q", "Quit")
    c = read_choice(["1", "2", "3", "4", "5", "I", "H", "Q"], "")
    return {"1": "video", "2": "audio", "3": "twv", "4": "tws", "5": "wd1", "I": "info", "H": "help"}.get(c)


def interactive(parser):
    while True:
        a = new_args(parser, [])
        mode = show_menu()
        if not mode:
            return 0
        if mode == "help":
            show_help()
            if _NO_INPUT:
                return 0
            continue
        options = {"video": video_options, "audio": audio_options, "twv": twv_options,
                   "tws": tws_options, "wd1": wd1_options}.get(mode)
        if options:
            chosen = options(a)
        else:                                            # inspect
            a.info, a.choice_text, chosen = True, "show what is inside, change nothing", True
        if _NO_INPUT:
            return 0
        if not chosen:
            continue
        inputs = read_inputs(mode)
        if not inputs:
            if _NO_INPUT:
                return 0
            continue
        if any(os.path.isdir(x) for x in inputs):
            n = read_menu("Subfolders", [("Only the files in the folder itself", ""),
                                         ("Include all subfolders",
                                          "with a save folder, the same subfolder structure is created in it")])
            if not n:
                if _NO_INPUT:
                    return 0
                continue
            a.recursive = n == 2
        if mode in ("twv", "tws", "wd1"):
            read_replace_target(mode, inputs, a)
        go = confirm_run(mode, inputs, a)
        if go == "quit":
            return 0
        if go == "back":
            continue
        print()
        a.mode = "auto" if mode == "info" else mode
        try:
            finalize(a)
            run_jobs(a.mode, inputs, a)
        except ConvError as e:
            err(str(e))
        v = read_line("\n  Finished. Enter = menu, Q = quit: ")
        if v is None or v.upper() == "Q":
            return 0


# ============================================================== CLI

def build_parser():
    p = argparse.ArgumentParser(
        prog="twv2mp4.py", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Video and audio converter for Reality Pump / TopWare games "
                    "(.twv / .tws, Earth 2150 .wd1). Run without arguments for a menu.",
        epilog="examples:" + __doc__.split("Examples:")[1] if "Examples:" in __doc__ else None)
    p.add_argument("inputs", nargs="*", help="files or folders (put names with spaces in quotes)")
    p.add_argument("-o", "--output", help="output file (for a single input) or folder")
    p.add_argument("-r", "--recursive", action="store_true",
                   help="also convert files in all subfolders of the given folders "
                        "(with -o, the folder structure is recreated under it)")
    p.add_argument("--mode", choices=["auto", "video", "audio", "twv", "tws", "wd1"], default="auto",
                   help="video: game->video, audio: game->audio, twv: video->TWV+TWS, tws: audio->TWS, "
                        "wd1: video->WD1; auto (default): by file extension")
    p.add_argument("--to-twv", action="store_true", help="same as --mode twv")
    p.add_argument("--fps", type=float, help="TWV->video: overrides the header fps; ->TWV/WD1: target fps")
    p.add_argument("--info", action="store_true", help="only show what is inside the game files")

    g = p.add_argument_group("game video -> video (--mode video)")
    g.add_argument("--format", choices=("raw",) + CONTAINERS,
                   help="raw = original streams, no ffmpeg (.m1v + .mp2 / .avi / .mpg); "
                        "default: from the -o extension, else mp4")
    g.add_argument("--m1v", action="store_true", help="same as --format raw")
    g.add_argument("--vcodec", choices=["auto", "h264", "h265", "vp9", "av1", "mpeg1", "copy"], default="auto",
                   help="auto: h264 for mp4/mov, vp9 for webm, else copy (original, lossless)")
    g.add_argument("--crf", type=int, help="quality, lower = better (defaults: h264 16, h265 20, vp9 28, av1 30)")
    g.add_argument("--preset", default="slow", help="x264/x265 preset (ultrafast..veryslow), AV1: 0-13")
    g.add_argument("--vbitrate", help="fixed bitrate instead of CRF, e.g. 4M")
    g.add_argument("--size", help="rescale the output, e.g. 1280x512, or double")
    g.add_argument("--acodec", choices=["auto", "aac", "mp3", "mp2", "opus", "vorbis", "flac", "pcm", "copy"],
                   default="auto", help="auto: aac for mp4/mov, opus for webm, else copy")
    g.add_argument("--audio", help="audio file (default: the .tws next to the .twv; ->TWV: sound source)")
    g.add_argument("--no-audio", action="store_true", help="no sound")

    g = p.add_argument_group("audio (game video and game audio)")
    g.add_argument("--abitrate", help="audio bitrate, e.g. 192k")
    g.add_argument("--rate", type=int, choices=[22050, 32000, 44100, 48000], help="sample rate")
    g.add_argument("--channels", type=int, choices=[1, 2], help="number of channels")
    g.add_argument("--audio-format", choices=AUDIO_FORMATS,
                   help="game audio -> audio; raw = original (.mp2); default: from the -o extension, else wav")

    g = p.add_argument_group("video -> TWV / WD1")
    g.add_argument("--width", type=int, help=f"width (default {GAME_W})")
    g.add_argument("--height", type=int, help=f"height (default {GAME_H})")
    g.add_argument("--stretch", action="store_true", help="stretch instead of letterboxing")
    g.add_argument("-q", type=int, help="MPEG-1 quantiser 1-31, lower = better (default 1, as in the game)")
    g.add_argument("--reencode", action="store_true",
                   help="always re-encode, even when the streams could be copied as is")
    g.add_argument("--tws-bitrate", help="bitrate of the .tws (default 160k; the games use 128k/160k)")
    g.add_argument("--tws-rate", type=int, choices=[32000, 44100, 48000], help="sample rate of the .tws (default 44100)")
    g.add_argument("--tws-channels", type=int, choices=[1, 2], help="channels of the .tws (default 2)")
    g.add_argument("--no-tws", action="store_true", help="do not create a .tws")
    g.add_argument("--wd1-format", choices=["auto", "mpg", "avi"], default="auto",
                   help="auto: same as the .wd1 being replaced, else mpg; avi = Cinepak + PCM")

    g = p.add_argument_group("ffmpeg")
    g.add_argument("--ffmpeg", help="path to ffmpeg (the file or its folder)")
    g.add_argument("--no-download", action="store_true", help="never download ffmpeg automatically")
    return p


def new_args(parser, argv):
    a = parser.parse_intermixed_args(argv)            # files may also come after the options
    a.force_video = None
    a.force_audio = None
    a.choice_text = ""
    return a


def main(argv=None):
    _setup_console()
    parser = build_parser()
    a = new_args(parser, argv)
    if not a.inputs:
        return interactive(parser)
    inputs = fix_args(a.inputs)
    try:
        finalize(a)
    except ConvError as e:
        err(str(e))
        return 1
    return 1 if run_jobs(a.mode, inputs, a) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
