"""fl_jam — a tiny music engine for FL Studio (and any DAW).

Two outputs from one engine:
  * GENERATE   compose beats / chords / basslines / melodies and write a .mid
               file you drag (or have vinceo.ai open) into FL Studio.
  * LIVE       stream those same notes in real time to a MIDI port FL Studio
               listens on (via a loopMIDI virtual cable), so the agent "plays"
               FL Studio live.

It is deliberately self-contained (only `mido` + `python-rtmidi`, no imports from
the rest of the project) so it runs standalone AND can be dropped into the
desktop agent's jail and run there.

------------------------------------------------------------------------------
QUICK START
------------------------------------------------------------------------------
  python fl_jam.py ports                      # list MIDI ports (find loopMIDI)
  python fl_jam.py gen song  -o song.mid      # full loop: drums+bass+chords
  python fl_jam.py gen beat  -o beat.mid --style boombap --bars 4
  python fl_jam.py gen chords -o keys.mid --key Am --prog i-VI-III-VII
  python fl_jam.py live --port "loopMIDI Port" --part song --bpm 128 --loop

LIVE SETUP (one time):
  1. Install loopMIDI (https://www.tobias-erichsen.de/software/loopmidi.html)
     and click + to create a port named "loopMIDI Port".
  2. In FL Studio: Options > MIDI settings > Input > enable "loopMIDI Port",
     set it to a controller type (e.g. "(generic controller)") and enable it.
     Arm a channel / pattern to record or just monitor.
  3. `python fl_jam.py ports` should now list "loopMIDI Port" as an output.
------------------------------------------------------------------------------
"""
from __future__ import annotations

import argparse
import os
import random
import shutil
import sys
import time
from pathlib import Path

try:
    import mido
except ImportError:
    sys.exit("mido is not installed. Run:  pip install mido python-rtmidi")

TPB = 480  # ticks per beat (MIDI PPQ)

# --- music theory (just enough to be fun) ----------------------------------
_NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_ALIAS = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#"}
# scale step patterns (semitones from the root)
SCALES = {
    "major":      [0, 2, 4, 5, 7, 9, 11],
    "minor":      [0, 2, 3, 5, 7, 8, 10],
    "dorian":     [0, 2, 3, 5, 7, 9, 10],
    "phrygian":   [0, 1, 3, 5, 7, 8, 10],
    "pentaminor": [0, 3, 5, 7, 10],
    "pentamajor": [0, 2, 4, 7, 9],
}
# roman numeral -> (scale-degree index, quality). lower = minor, upper = major.
_ROMAN = {"i": (0, "m"), "ii": (1, "m"), "iii": (2, "m"), "iv": (3, "m"),
          "v": (4, "m"), "vi": (5, "m"), "vii": (6, "dim"),
          "I": (0, "M"), "II": (1, "M"), "III": (2, "M"), "IV": (3, "M"),
          "V": (4, "M"), "VI": (5, "M"), "VII": (6, "M")}
_TRIAD = {"M": [0, 4, 7], "m": [0, 3, 7], "dim": [0, 3, 6], "aug": [0, 4, 8]}

# General MIDI drum notes (channel 10 / index 9)
KICK, SNARE, CH_HAT, OP_HAT, CLAP, RIDE = 36, 38, 42, 46, 39, 51


def note_num(name: str, octave: int = 4) -> int:
    name = _ALIAS.get(name, name)
    return _NOTES.index(name) + (octave + 1) * 12  # C4 = 60


def parse_key(key: str) -> tuple[int, str]:
    """'Am' -> (root=A, 'minor'); 'C' -> (C, 'major'); 'Ddorian' -> (D,'dorian')."""
    key = key.strip()
    for suffix, scale in (("dorian", "dorian"), ("phrygian", "phrygian")):
        if key.lower().endswith(suffix):
            return _root(key[:-len(suffix)]), scale
    if key.endswith("m") and not key.endswith("dim"):
        return _root(key[:-1]), "minor"
    return _root(key), "major"


def _root(s: str) -> int:
    s = s.strip().capitalize()
    s = _ALIAS.get(s, s)
    if s not in _NOTES:
        s = "C"
    return _NOTES.index(s)


def scale_notes(root_pc: int, scale: str, octave: int, count: int) -> list[int]:
    steps = SCALES.get(scale, SCALES["minor"])
    out = []
    i = 0
    while len(out) < count:
        deg = steps[i % len(steps)] + 12 * (i // len(steps))
        out.append(root_pc + deg + (octave + 1) * 12)
        i += 1
    return out


def chord_from_roman(roman: str, root_pc: int, scale: str, octave: int) -> list[int]:
    idx, qual = _ROMAN.get(roman, (0, "m"))
    steps = SCALES.get(scale, SCALES["minor"])
    chord_root = root_pc + steps[idx % len(steps)]
    quality = {"M": "M", "m": "m", "dim": "dim"}.get(qual, "m")
    base = chord_root + (octave + 1) * 12
    return [base + iv for iv in _TRIAD[quality]]


# --- drum patterns (16th-note grids; 1 = hit) ------------------------------
DRUM_STYLES = {
    "fourfloor": {  # house / techno
        KICK:  "1000100010001000",
        CLAP:  "0000100000001000",
        CH_HAT:"1010101010101010",
        OP_HAT:"0010001000100010",
    },
    "boombap": {  # classic hip-hop
        KICK:  "1000000010000000",
        SNARE: "0000100000001000",
        CH_HAT:"1010101010101010",
    },
    "trap": {
        KICK:  "1000001000100000",
        SNARE: "0000100000001000",
        CH_HAT:"1011101110111011",  # rolls
    },
    "rock": {
        KICK:  "1000000010000000",
        SNARE: "0000100000001000",
        CH_HAT:"1010101010101010",
    },
}
DEFAULT_PROG = ["i", "VI", "III", "VII"]  # minor-key favourite


# ===========================================================================
# The engine: build a list of (track_name, channel, [(note, start_tick, dur,
# velocity)]) — a resolution-independent score we can either write to a .mid
# file or stream live.
# ===========================================================================
def build_score(part: str, *, key: str, bars: int, style: str,
                prog: list[str], seed: int | None) -> list[dict]:
    rng = random.Random(seed)
    root_pc, scale = parse_key(key)
    beats_per_bar = 4
    bar_ticks = TPB * beats_per_bar
    step_ticks = bar_ticks // 16
    tracks: list[dict] = []

    def drums() -> dict:
        grid = DRUM_STYLES.get(style, DRUM_STYLES["boombap"])
        notes = []
        for bar in range(bars):
            for drum, pattern in grid.items():
                for s, ch in enumerate(pattern):
                    if ch == "1":
                        t = bar * bar_ticks + s * step_ticks
                        vel = 110 if drum in (KICK, SNARE) else rng.randint(60, 90)
                        notes.append((drum, t, step_ticks - 5, vel))
        return {"name": "Drums", "channel": 9, "notes": notes}

    def chords() -> dict:
        notes = []
        for bar in range(bars):
            roman = prog[bar % len(prog)]
            voicing = chord_from_roman(roman, root_pc, scale, octave=4)
            t = bar * bar_ticks
            for n in voicing:
                notes.append((n, t, bar_ticks - 10, 70))
        return {"name": "Chords", "channel": 0, "notes": notes}

    def bass() -> dict:
        notes = []
        for bar in range(bars):
            roman = prog[bar % len(prog)]
            root = chord_from_roman(roman, root_pc, scale, octave=2)[0]
            # simple eighth-note root bounce with an octave accent
            for e in range(8):
                t = bar * bar_ticks + e * (bar_ticks // 8)
                n = root + (12 if e in (5, 7) else 0)
                notes.append((n, t, (bar_ticks // 8) - 10, 90))
        return {"name": "Bass", "channel": 1, "notes": notes}

    def melody() -> dict:
        pool = scale_notes(root_pc, scale, octave=5, count=len(SCALES[scale]) * 2)
        notes, prev = [], rng.choice(pool)
        for bar in range(bars):
            for e in range(8):
                if rng.random() < 0.30:      # leave space
                    continue
                # step mostly to a neighbour for a singable line
                idx = max(0, min(len(pool) - 1, pool.index(prev) + rng.choice([-2, -1, 1, 1, 2])))
                n = pool[idx]
                prev = n
                t = bar * bar_ticks + e * (bar_ticks // 8)
                notes.append((n, t, (bar_ticks // 8) - 10, rng.randint(70, 100)))
        return {"name": "Melody", "channel": 2, "notes": notes}

    parts = {"beat": [drums], "drums": [drums], "chords": [chords],
             "bass": [bass], "melody": [melody],
             "song": [drums, bass, chords, melody]}
    for maker in parts.get(part, parts["song"]):
        tracks.append(maker())
    return tracks


# --- output 1: write a Standard MIDI File ----------------------------------
def write_midi(tracks: list[dict], path: str, bpm: float) -> None:
    mid = mido.MidiFile(ticks_per_beat=TPB)
    meta = mido.MidiTrack()
    meta.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm)))
    mid.tracks.append(meta)
    for tr in tracks:
        mt = mido.MidiTrack()
        mt.append(mido.MetaMessage("track_name", name=tr["name"]))
        # flatten (note_on/note_off) events, sort by tick, emit as deltas
        events = []
        for note, start, dur, vel in tr["notes"]:
            events.append((start, "on", note, vel))
            events.append((start + max(1, dur), "off", note, 0))
        events.sort(key=lambda e: (e[0], e[1] == "on"))
        last = 0
        for tick, kind, note, vel in events:
            delta = tick - last
            last = tick
            mt.append(mido.Message("note_on" if kind == "on" else "note_off",
                                   note=note, velocity=vel,
                                   channel=tr["channel"], time=delta))
        mid.tracks.append(mt)
    mid.save(path)


# --- output 2: stream the score live to a MIDI port ------------------------
def play_live(tracks: list[dict], port_name: str, bpm: float, loop: bool) -> None:
    sec_per_tick = (60.0 / bpm) / TPB
    # merge all tracks into one timeline of (tick, kind, channel, note, vel)
    timeline = []
    for tr in tracks:
        for note, start, dur, vel in tr["notes"]:
            timeline.append((start, "on", tr["channel"], note, vel))
            timeline.append((start + max(1, dur), "off", tr["channel"], note, 0))
    timeline.sort(key=lambda e: (e[0], e[1] == "on"))
    total_ticks = max((e[0] for e in timeline), default=0)

    out = _open_output(port_name)
    print(f"[live] playing to {out.name!r} @ {bpm:g} BPM "
          f"({'looping — Ctrl+C to stop' if loop else 'once'})")
    try:
        while True:
            t0 = time.perf_counter()
            for tick, kind, ch, note, vel in timeline:
                target = t0 + tick * sec_per_tick
                dt = target - time.perf_counter()
                if dt > 0:
                    time.sleep(dt)
                out.send(mido.Message("note_on" if kind == "on" else "note_off",
                                      note=note, velocity=vel, channel=ch))
            # let the last bar ring out to the barline
            time.sleep(max(0.0, t0 + total_ticks * sec_per_tick - time.perf_counter()))
            if not loop:
                break
    except KeyboardInterrupt:
        print("\n[live] stopped.")
    finally:
        _all_notes_off(out)
        out.close()


def _open_output(port_name: str | None):
    outs = mido.get_output_names()
    if not outs:
        sys.exit("No MIDI outputs found. Install loopMIDI and create a port "
                 "(see the LIVE SETUP notes in this file's header).")
    if port_name:
        match = next((o for o in outs if port_name.lower() in o.lower()), None)
        if match is None:
            sys.exit(f"Port {port_name!r} not found. Available: {outs}")
        return mido.open_output(match)
    print(f"[live] no --port given; using first output {outs[0]!r}")
    return mido.open_output(outs[0])


def _all_notes_off(out) -> None:
    for ch in range(16):
        out.send(mido.Message("control_change", control=123, value=0, channel=ch))


# --- new-project scaffolding ------------------------------------------------
# FL Studio's .flp is a closed binary format, so we don't author one from
# scratch. Instead we copy one of FL's OWN shipped template .flp files (a valid,
# openable project) to a new name and seed it with a generated .mid. That's a
# real new project without touching the proprietary format.
_FL_VERSIONS = ("FL Studio 2024", "FL Studio 2025", "FL Studio 21", "FL Studio 20")


def _template_roots() -> list[Path]:
    pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    pfx = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    roots = []
    for base in (pf, pfx):
        for v in _FL_VERSIONS:
            roots.append(Path(base) / "Image-Line" / v / "Data" / "Templates")
    return roots


def find_templates() -> dict[str, Path]:
    """Map template name (the .flp stem) -> path, from FL's install."""
    found: dict[str, Path] = {}
    for root in _template_roots():
        if root.is_dir():
            for flp in sorted(root.rglob("*.flp")):
                found.setdefault(flp.stem, flp)
    return found


def _pick_template(name: str | None, templates: dict[str, Path]) -> Path | None:
    if not name or not templates:
        return None
    exact = templates.get(name)
    if exact:
        return exact
    matches = [p for stem, p in templates.items() if name.lower() in stem.lower()]
    return min(matches, key=lambda p: len(p.stem)) if matches else None


def _safe_name(name: str) -> str:
    keep = "-_ ()"
    cleaned = "".join(c for c in (name or "").strip() if c.isalnum() or c in keep)
    return cleaned.rstrip(". ") or "Untitled"


def _find_fl_exe() -> str | None:
    pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    for v in _FL_VERSIONS:
        exe = Path(pf) / "Image-Line" / v / "FL64.exe"
        if exe.is_file():
            return str(exe)
    return shutil.which("FL64")


def new_project(name: str, *, out_dir: str, template: str | None,
                part: str, key: str, bars: int, bpm: float, style: str,
                prog: list[str], seed: int | None, open_it: bool) -> Path:
    proj_name = _safe_name(name)
    proj_dir = Path(out_dir).expanduser() / proj_name
    proj_dir.mkdir(parents=True, exist_ok=True)

    made: list[Path] = []
    templates = find_templates()
    tmpl = _pick_template(template or "Empty", templates)
    if tmpl:
        dest_flp = proj_dir / f"{proj_name}.flp"
        shutil.copyfile(tmpl, dest_flp)
        made.append(dest_flp)
        print(f"[newproject] .flp from template {tmpl.stem!r} -> {dest_flp}")
    else:
        if template:
            print(f"[newproject] template {template!r} not found "
                  f"(try `python fl_jam.py templates`); making a MIDI-only project.")

    # seed it with a starter idea as an importable .mid
    tracks = build_score(part, key=key, bars=bars, style=style, prog=prog, seed=seed)
    mid_path = proj_dir / f"{proj_name}.mid"
    write_midi(tracks, str(mid_path), bpm)
    made.append(mid_path)
    print(f"[newproject] starter MIDI ({part}, {bars} bars, key {key}, "
          f"{bpm:g} BPM) -> {mid_path}")

    print(f"[newproject] created project folder: {proj_dir}")
    if open_it:
        exe = _find_fl_exe()
        target = made[0]  # prefer the .flp; else the .mid
        if exe:
            import subprocess
            subprocess.Popen([exe, str(target)])
            print(f"[newproject] opening {target.name} in FL Studio ...")
        else:
            print("[newproject] FL Studio exe not found; open the folder manually.")
    else:
        print(f"      Open it:  drag {made[0].name} into FL Studio, or add --open")
    return proj_dir


# --- CLI --------------------------------------------------------------------
def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Generate MIDI files or play them live into FL Studio.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ports", help="list available MIDI input/output ports")
    sub.add_parser("templates", help="list FL Studio project templates you can use")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--part", default="song",
                        choices=["song", "beat", "drums", "chords", "bass", "melody"])
    common.add_argument("--key", default="Am", help="e.g. C, Am, Ddorian")
    common.add_argument("--bars", type=int, default=4)
    common.add_argument("--bpm", type=float, default=120)
    common.add_argument("--style", default="boombap",
                        choices=list(DRUM_STYLES), help="drum style")
    common.add_argument("--prog", default="-".join(DEFAULT_PROG),
                        help="chord progression, e.g. i-VI-III-VII or I-V-vi-IV")
    common.add_argument("--seed", type=int, default=None, help="reproducible randomness")

    g = sub.add_parser("gen", parents=[common], help="write a .mid file")
    g.add_argument("-o", "--out", default="fl_jam.mid")

    l = sub.add_parser("live", parents=[common], help="stream live to a MIDI port")
    l.add_argument("--port", default=None, help="MIDI output port (substring ok), e.g. loopMIDI")
    l.add_argument("--loop", action="store_true", help="loop until Ctrl+C")

    np_ = sub.add_parser("newproject", parents=[common],
                         help="scaffold a new FL Studio project (folder + .flp + starter .mid)")
    np_.add_argument("name", help="project name, e.g. \"My Tune\"")
    np_.add_argument("--template", default="Empty",
                     help="FL template to copy (see `templates`), e.g. Trap, EDM-House, Empty")
    np_.add_argument("--dir", default="fl_projects", help="where to create the project folder")
    np_.add_argument("--open", action="store_true", help="open it in FL Studio when done")

    args = ap.parse_args(argv)

    if args.cmd == "templates":
        tmpls = find_templates()
        if not tmpls:
            print("No FL Studio templates found (is FL Studio installed?).")
            return
        print("FL Studio project templates (use the name with --template):")
        for stem in sorted(tmpls):
            print(f"  {stem}")
        return

    if args.cmd == "ports":
        print("MIDI outputs (send TO these — pick your loopMIDI port for --port):")
        for o in mido.get_output_names() or ["  (none)"]:
            print(f"  out  {o}")
        print("MIDI inputs:")
        for i in mido.get_input_names() or ["  (none)"]:
            print(f"  in   {i}")
        return

    prog = [p for p in args.prog.split("-") if p]
    tracks = build_score(args.part, key=args.key, bars=args.bars,
                         style=args.style, prog=prog, seed=args.seed)
    n = sum(len(t["notes"]) for t in tracks)

    if args.cmd == "gen":
        write_midi(tracks, args.out, args.bpm)
        print(f"[gen] wrote {args.out}  ({args.part}, {args.bars} bars, key {args.key}, "
              f"{len(tracks)} track(s), {n} notes, {args.bpm:g} BPM)")
        print(f"      Open it in FL Studio (File > Open, or drag it in), or:")
        print(f"      curl -X POST localhost:8000/desktop -d '{{\"message\":\"open FL Studio\"}}'")
    elif args.cmd == "live":
        play_live(tracks, args.port, args.bpm, args.loop)
    elif args.cmd == "newproject":
        new_project(args.name, out_dir=args.dir, template=args.template,
                    part=args.part, key=args.key, bars=args.bars, bpm=args.bpm,
                    style=args.style, prog=prog, seed=args.seed, open_it=args.open)


if __name__ == "__main__":
    main()
