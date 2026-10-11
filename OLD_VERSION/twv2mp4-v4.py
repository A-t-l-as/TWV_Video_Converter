#!/usr/bin/env python3
"""
twv2mp4.py - konwersja wideo TWV (KnightShift / TopWare) <-> MP4 (i inne formaty).

  TWV -> MP4 :  python twv2mp4.py film.twv
  MP4 -> TWV :  python twv2mp4.py film.mp4            (kierunek wg rozszerzenia)
  katalogi   :  python twv2mp4.py video/ -o mp4/        (wszystkie .twv -> .mp4)
                python twv2mp4.py filmy/ -o twv/ --to-twv   (wszystkie filmy -> .twv)

Format TWV (ustalony z dekompilacji silnika i analizy Intro2.twv):

  Naglowek, 5 x uint32 big-endian (20 bajtow):
    0x00 'TWV\\0'   0x04 wersja (1)   0x08 szerokosc   0x0C wysokosc   0x10 fps 16.16

  Dalej "odchudzony" strumien MPEG-1 video (same klatki I, 1 slice na rzad MB):
    * brak sequence header (00 00 01 B3) i GOP - domyslne macierze kwantyzacji,
    * picture header (00 00 01 00) = 1 bajt: 3 bity picture_coding_type + zera,
    * slice header (00 00 01 01..AF) = tylko 5-bitowy quantizer_scale
      (bez extra_bit_slice),
    * na koncu 00 00 01 B7.

Silnik wyswietla film z trzech tekstur 256x256, dlatego przy tworzeniu TWV
domyslnie uzywany jest rozmiar 640x256 @ 25 fps (jak w oryginalnych intrach).

Dzwiek gra trzyma osobno w plikach .tws - ich format nie jest jeszcze znany,
wiec przy MP4 -> TWV powstaje tylko obraz.

Wymagania: Python 3.8+, ffmpeg w PATH.
"""
import argparse
import os
import struct
import subprocess
import sys
import tempfile
import zlib

MAGIC = b"TWV\x00"
HDR = 20
SC = b"\x00\x00\x01"
FPS_CODES = {1: 24000 / 1001, 2: 24.0, 3: 25.0, 4: 30000 / 1001,
             5: 30.0, 6: 50.0, 7: 60000 / 1001, 8: 60.0}
VIDEO_EXT = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".mpg", ".mpeg",
             ".m1v", ".wmv", ".flv", ".gif")


# ---------------------------------------------------------------- wspolne

def maybe_inflate(data):
    if len(data) > 2 and data[0] == 0x78 and data[1] in (0x01, 0x5E, 0x9C, 0xDA):
        try:
            return zlib.decompress(data)
        except zlib.error:
            pass
    return data


def units(data, start=0):
    """Kolejne jednostki strumienia: od start code do nastepnego start code."""
    pos = []
    i = data.find(SC, start)
    while i >= 0:
        pos.append(i)
        i = data.find(SC, i + 3)
    pos.append(len(data))
    for a, b in zip(pos, pos[1:]):
        if b - a >= 4:
            yield data[a:b]


def run_ffmpeg(cmd):
    """Uruchamia ffmpeg; komunikaty dekoduje jako UTF-8 (polskie znaki w sciezkach)."""
    try:
        r = subprocess.run(cmd, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        sys.exit("[!] Nie znaleziono ffmpeg - zainstaluj go i dodaj do PATH.")
    r.stderr = r.stderr.decode("utf-8", errors="replace")
    return r


def clean_path(p):
    """Usuwa cudzyslowy/apostrofy i '& ' dodawane przy przeciaganiu pliku do konsoli."""
    p = p.strip()
    if p.startswith("& "):
        p = p[2:].strip()
    while len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'":
        p = p[1:-1].strip()
    return p


def fix_args(args):
    """Skleja argumenty rozbite na spacjach, np. ['Moj', 'film.mp4'] -> ['Moj film.mp4']."""
    args = [clean_path(x) for x in args if x.strip()]
    out, i = [], 0
    while i < len(args):
        if os.path.exists(args[i]):
            out.append(args[i]); i += 1
            continue
        for j in range(i + 2, len(args) + 1):
            cand = " ".join(args[i:j])
            if os.path.exists(cand):
                out.append(cand); i = j
                break
        else:
            out.append(args[i]); i += 1
    return out


def ask_inputs():
    print("Konwerter TWV <-> MP4")
    print("Przeciagnij tutaj plik lub folder (albo wpisz sciezke) i nacisnij Enter.")
    print("Kilka sciezek oddziel znakiem | . Pusty wiersz konczy.")
    try:
        line = input("> ")
    except EOFError:
        return []
    return [clean_path(x) for x in line.split("|") if x.strip()]


# ---------------------------------------------------------------- TWV -> MPEG-1

DEFAULT_FPS = 25.0


def parse_header(data):
    if data[:4] != MAGIC or len(data) < HDR:
        raise ValueError("brak sygnatury 'TWV\\0' - to nie jest plik TWV")
    _, ver, w, h, raw = struct.unpack(">5I", data[:HDR])
    if not (16 <= w <= 4095 and 16 <= h <= 4095):
        raise ValueError(f"nieprawidlowe wymiary w naglowku: {w}x{h} "
                         f"(naglowek: {data[:HDR].hex(' ')})")
    fps = raw / 65536.0
    if not 1 <= fps <= 120:
        if 1 <= raw <= 120:              # fps zapisane jako zwykla liczba calkowita
            fps = float(raw)
        else:
            fps = 0.0                    # brak - wybierze wywolujacy
    return ver, w, h, fps


def seq_header(w, h, fps):
    code = min(FPS_CODES, key=lambda c: abs(FPS_CODES[c] - fps))
    w1 = (w << 20) | (h << 8) | (1 << 4) | code              # aspect 1:1
    w2 = (0x3FFFF << 14) | (1 << 13) | (112 << 3)            # VBR, marker, vbv
    return SC + b"\xb3" + struct.pack(">II", w1, w2)


def slice_insert_bit(u):
    """TWV -> MPEG-1: wstawia extra_bit_slice=0 za 5-bitowym quantizer_scale."""
    body = u[4:]
    n = len(body) * 8
    v = int.from_bytes(body, "big")
    hi, lo = v >> (n - 5), v & ((1 << (n - 5)) - 1)
    v = ((hi << (n - 4)) | lo) << 7          # n+1 bitow, dopelnione zerami do bajtu
    return u[:4] + v.to_bytes(len(body) + 1, "big")


def twv_to_m1v(data, fps_override=None):
    ver, w, h, fps = parse_header(data)
    if fps_override:
        fps = fps_override
    elif not fps:
        fps = DEFAULT_FPS
    out = bytearray(seq_header(w, h, fps))
    frames = 0
    for u in units(data, HDR):
        c = u[3]
        if c == 0x00:                                   # picture
            ptype = (u[4] >> 5) & 7 if len(u) > 4 else 1
            if frames and frames % 1024 == 0:
                out += seq_header(w, h, fps)
            hdr = ((frames % 1024) << 22) | (ptype << 19) | (0xFFFF << 3)
            out += u[:4] + struct.pack(">I", hdr)
            frames += 1
        elif 0x01 <= c <= 0xAF:                         # slice
            out += slice_insert_bit(u)
        elif c == 0xB7:
            break
    out += SC + b"\xb7"
    return bytes(out), (ver, w, h, fps, frames)


# ---------------------------------------------------------------- MPEG-1 -> TWV

def slice_remove_bit(u):
    """MPEG-1 -> TWV: usuwa extra_bit_slice (musi byc 0) za quantizer_scale."""
    if u[4] & 0x04:
        raise ValueError("slice z extra_information_slice - nieobslugiwany")
    body = u[4:]
    n = len(body) * 8
    v = int.from_bytes(body, "big")
    hi, lo = v >> (n - 5), v & ((1 << (n - 6)) - 1)
    v = ((hi << (n - 6)) | lo) << 1          # n-1 bitow + zero na koncu
    return u[:4] + v.to_bytes(len(body), "big")


def m1v_to_twv(es, w, h, fps):
    out = bytearray(struct.pack(">4sIIII", MAGIC, 1, w, h, int(round(fps * 65536))))
    frames = 0
    for u in units(es):
        c = u[3]
        if c == 0x00:                                   # picture
            ptype = (u[5] >> 3) & 7
            if ptype != 1:
                raise ValueError(f"klatka {frames} nie jest typu I (typ {ptype})")
            out += SC + b"\x00" + bytes([ptype << 5])
            frames += 1
        elif 0x01 <= c <= 0xAF:                         # slice
            out += slice_remove_bit(u)
        # B3 (sequence), B8 (GOP), B2 (user data), B5 (ext.) - pomijane
    out += SC + b"\xb7"
    return bytes(out), frames


# ---------------------------------------------------------------- konwersje

def to_mp4(path, out, a):
    with open(path, "rb") as f:
        data = maybe_inflate(f.read())
    try:
        hdr_fps = parse_header(data)[3]
        es, (ver, w, h, fps, frames) = twv_to_m1v(data, a.fps)
    except ValueError as e:
        print(f"[!] {path}: {e}", file=sys.stderr)
        return False
    if not hdr_fps and not a.fps:
        print(f"[i] {path}: naglowek nie podaje fps (wartosc "
              f"0x{struct.unpack('>I', data[16:20])[0]:08x}) - przyjeto {fps:g} fps; "
              f"inna wartosc ustawisz opcja --fps")
    if frames == 0:
        print(f"[!] {path}: plik nie zawiera zadnych klatek", file=sys.stderr)
        return False
    print(f"[i] {path}: TWV v{ver}, {w}x{h}, {fps:g} fps, {frames} klatek "
          f"({frames / fps:.1f} s)")
    if a.info:
        return True

    if a.m1v:
        out = os.path.splitext(out)[0] + ".m1v"
        with open(out, "wb") as f:
            f.write(es)
        print(f"[ok] {out}")
        return True

    audio = None
    if not a.no_audio:
        audio = a.audio or os.path.splitext(path)[0] + ".tws"
        if not os.path.isfile(audio):
            audio = None

    with tempfile.NamedTemporaryFile(suffix=".m1v", delete=False) as t:
        t.write(es)
        tmp = t.name

    def run(aud):
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-f", "mpegvideo", "-framerate", f"{fps:g}", "-i", tmp]
        if aud:
            cmd += ["-i", aud, "-map", "0:v:0", "-map", "1:a:0",
                    "-c:a", "aac", "-b:a", "160k"]
        cmd += ["-c:v", "libx264", "-crf", str(a.crf), "-preset", "slow",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", out]
        return run_ffmpeg(cmd)

    try:
        r = run(audio)
        if r.returncode != 0 and audio and not a.audio:
            print(f"[i] ffmpeg nie rozpoznal {os.path.basename(audio)} - eksport bez dzwieku")
            audio = None
            r = run(None)
        if r.returncode != 0:
            print(r.stderr, file=sys.stderr)
            print(f"[!] ffmpeg zakonczyl sie bledem dla {path}", file=sys.stderr)
            return False
    finally:
        os.unlink(tmp)
    print(f"[ok] {out}" + ("" if audio else " (bez dzwieku)"))
    return True


def to_twv(path, out, a):
    w, h = a.width, a.height
    fps = a.fps or 25.0
    if w % 16 or h % 16:
        print("[!] szerokosc i wysokosc musza byc wielokrotnoscia 16", file=sys.stderr)
        return False
    if fps not in FPS_CODES.values() and not any(abs(fps - v) < 0.01 for v in FPS_CODES.values()):
        print(f"[!] fps {fps:g} nie jest dozwolone w MPEG-1 "
              f"(dozwolone: 23.976, 24, 25, 29.97, 30, 50, 59.94, 60)", file=sys.stderr)
        return False

    if a.stretch:
        vf = f"scale={w}:{h}"
    else:   # dopasuj z zachowaniem proporcji, dopelnij czarnymi pasami
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
              f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black")
    vf += ",setsar=1"

    with tempfile.NamedTemporaryFile(suffix=".m1v", delete=False) as t:
        tmp = t.name
    try:
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", path,
               "-an", "-vf", vf, "-r", f"{fps:g}",
               "-c:v", "mpeg1video", "-g", "1", "-bf", "0",
               "-qscale:v", str(a.q), "-qmin", "1", "-qmax", str(max(a.q, 31)),
               "-slices", str(h // 16),            # 1 slice na rzad makroblokow
               "-f", "mpeg1video", tmp]
        r = run_ffmpeg(cmd)
        if r.returncode != 0:
            print(r.stderr, file=sys.stderr)
            print(f"[!] ffmpeg zakonczyl sie bledem dla {path}", file=sys.stderr)
            return False
        with open(tmp, "rb") as f:
            es = f.read()
    finally:
        os.unlink(tmp)

    try:
        twv, frames = m1v_to_twv(es, w, h, fps)
    except ValueError as e:
        print(f"[!] {path}: {e}", file=sys.stderr)
        return False

    # kontrola: TWV musi sie poprawnie odczytac z powrotem
    back, _ = twv_to_m1v(twv)
    if back.count(SC + b"\x00") != frames:
        print(f"[!] {path}: kontrola spojnosci nie powiodla sie", file=sys.stderr)
        return False

    with open(out, "wb") as f:
        f.write(twv)
    print(f"[ok] {out}: {w}x{h} @ {fps:g} fps, {frames} klatek "
          f"({frames / fps:.1f} s), {len(twv) / 1e6:.1f} MB")
    return True


# ---------------------------------------------------------------- CLI

def main():
    p = argparse.ArgumentParser(
        description="Konwersja TWV (KnightShift) <-> MP4. Kierunek wybierany wg "
                    "rozszerzenia: .twv -> .mp4, inne filmy -> .twv")
    p.add_argument("inputs", nargs="*",
                   help="pliki lub katalogi (nazwy ze spacjami w cudzyslowie); "
                        "bez argumentow - tryb interaktywny")
    p.add_argument("-o", "--output", help="plik wyjsciowy (1 wejscie) lub katalog")
    p.add_argument("--to-twv", action="store_true",
                   help="dla katalogow: konwertuj filmy do TWV (zamiast TWV do MP4)")
    p.add_argument("--fps", type=float, help="fps (TWV->MP4: nadpisuje naglowek; "
                                             "->TWV: domyslnie 25)")
    g = p.add_argument_group("TWV -> MP4")
    g.add_argument("--crf", type=int, default=16, help="jakosc x264 (domyslnie 16)")
    g.add_argument("--audio", help="plik audio (domyslnie .tws obok .twv)")
    g.add_argument("--no-audio", action="store_true")
    g.add_argument("--m1v", action="store_true",
                   help="zapisz naprawiony strumien MPEG-1 zamiast MP4 (bez rekompresji)")
    g.add_argument("--info", action="store_true", help="tylko wypisz informacje o TWV")
    g = p.add_argument_group("film -> TWV")
    g.add_argument("--width", type=int, default=640, help="szerokosc (domyslnie 640)")
    g.add_argument("--height", type=int, default=256, help="wysokosc (domyslnie 256)")
    g.add_argument("--stretch", action="store_true",
                   help="rozciagnij obraz zamiast dodawac czarne pasy")
    g.add_argument("-q", type=int, default=2,
                   help="kwantyzator MPEG-1 1-31, mniej = lepiej (domyslnie 2)")
    a = p.parse_args()

    interactive = not a.inputs
    inputs = ask_inputs() if interactive else fix_args(a.inputs)
    if not inputs:
        p.print_help()
        return finish(interactive, 1)
    missing = [i for i in inputs if not os.path.exists(i)]
    for m in missing:
        print(f"[!] Nie znaleziono: {m}", file=sys.stderr)
    if missing:
        print("    Wskazowka: nazwe ze spacjami ujmij w cudzyslow, np. "
              "python twv2mp4.py \"Mój film.mp4\"", file=sys.stderr)
        return finish(interactive, 1)
    a.inputs = inputs

    jobs = []
    out_is_dir = any(os.path.isdir(i) for i in a.inputs) or len(a.inputs) > 1
    for i in a.inputs:
        if os.path.isdir(i):
            for f in sorted(os.listdir(i)):
                ext = os.path.splitext(f)[1].lower()
                if (a.to_twv and ext in VIDEO_EXT) or (not a.to_twv and ext == ".twv"):
                    jobs.append(os.path.join(i, f))
        else:
            jobs.append(i)

    failed = 0
    for f in jobs:
        encode = not f.lower().endswith(".twv")
        ext = ".twv" if encode else ".mp4"
        base = os.path.splitext(os.path.basename(f))[0] + ext
        if a.output and not out_is_dir and not os.path.isdir(a.output):
            out = a.output
        else:
            d = a.output or os.path.dirname(f) or "."
            os.makedirs(d, exist_ok=True)
            out = os.path.join(d, base)
        if os.path.abspath(out) == os.path.abspath(f):
            print(f"[!] {f}: plik wyjsciowy nadpisalby wejsciowy - podaj -o", file=sys.stderr)
            failed += 1
            continue
        try:
            ok = to_twv(f, out, a) if encode else to_mp4(f, out, a)
        except Exception as e:          # jeden zly plik nie przerywa calego folderu
            print(f"[!] {f}: nieoczekiwany blad: {type(e).__name__}: {e}", file=sys.stderr)
            ok = False
        failed += not ok
    if len(jobs) > 1:
        print(f"\nPodsumowanie: {len(jobs) - failed} OK, {failed} z bledami "
              f"(z {len(jobs)} plikow)")
    if not jobs:
        print("[!] Nie znaleziono plikow do konwersji.", file=sys.stderr)
        failed = 1
    return finish(interactive, 1 if failed else 0)


def finish(interactive, code):
    if interactive:
        try:
            input("\nGotowe. Nacisnij Enter, aby zamknac...")
        except EOFError:
            pass
    return code


if __name__ == "__main__":
    for st in (sys.stdout, sys.stderr):          # brak bledow przy wypisywaniu polskich znakow
        try:
            st.reconfigure(errors="replace")
        except Exception:
            pass
    sys.exit(main())
