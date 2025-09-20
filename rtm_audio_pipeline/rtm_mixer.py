#!/usr/bin/env python3
"""
RTM Mixer - intro BG + narration + outro bed -> polished MP3 using ffmpeg.

- Applies bg_vol ONCE (pre-duck).
- Voice label uses [vo] (avoids rare binding collisions).
- Optional --voice_only switch to verify Clyde path quickly.
- Loudness normalization at the FINAL step only.
- Verbose logging of filter graphs and ffmpeg output.
"""

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

DEBUG = os.getenv("RTM_DEBUG", "0") == "1"

def run(cmd: str, show=True):
    if show:
        print(">>>", cmd)
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if show:
        if p.stdout:
            print(">>> [stdout]\n", p.stdout)
        if p.stderr:
            print(">>> [stderr]\n", p.stderr)
    return p.returncode

def ffprobe_info(label: str, path: Path):
    cmd = f'ffprobe -hide_banner -v error -show_entries stream=channels,sample_rate -show_entries format=duration -of json {shlex.quote(str(path))}'
    print(f">>> ffprobe {label}:", path)
    _ = run(cmd)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--intro", required=True, help="Intro background (bed)")
    ap.add_argument("--narr", required=True, help="Narration (dry voice)")
    ap.add_argument("--outro", required=True, help="Short outro bed (~5s)")
    ap.add_argument("--out", required=True, help="Output MP3 path")

    # Mix controls
    ap.add_argument("--bg_vol", type=float, default=0.25, help="BG volume multiplier (pre-duck)")
    ap.add_argument("--duck_threshold", type=float, default=0.02, help="Sidechain threshold")
    ap.add_argument("--duck_ratio", type=float, default=12.0, help="Sidechain ratio")
    ap.add_argument("--xfade", type=float, default=1.0, help="Crossfade seconds into outro")

    # Loudness
    ap.add_argument("--lufs", type=float, default=-16.0, help="Integrated LUFS target")
    ap.add_argument("--tp", type=float, default=-1.5, help="True peak ceiling dBTP")
    ap.add_argument("--lra", type=float, default=11.0, help="Loudness range")

    # Diagnostics
    ap.add_argument("--voice_only", action="store_true", help="Output voice only for debugging")

    args = ap.parse_args()

    intro = Path(args.intro)
    narr = Path(args.narr)
    outro = Path(args.outro)
    out = Path(args.out)

    if not intro.exists() or not narr.exists() or not outro.exists():
        print("One or more input files do not exist.", file=sys.stderr)
        sys.exit(2)

    if DEBUG:
        ffprobe_info("intro", intro)
        ffprobe_info("narr", narr)
        ffprobe_info("outro", outro)

    core_mix = out.with_suffix(".core_mix.mp3")
    core_plus_outro = out.with_suffix(".core_plus_outro.mp3")

    # ---------- STEP 1: Intro BG + Narration ----------
    if args.voice_only:
        # Voice-only pass-through for quick debugging
        filter1 = "[1:a]aformat=channel_layouts=stereo,aresample=48000,volume=2.0[mix]"
    else:
        # Apply bg_vol once, duck BG under voice, then mix with explicit weights.
        filter1 = f"""
        [0:a]aformat=channel_layouts=stereo,aresample=48000,volume={args.bg_vol}[bgpre];
        [1:a]aformat=channel_layouts=stereo,aresample=48000,volume=1.5[vo];
        [bgpre][vo]sidechaincompress=threshold={args.duck_threshold}:ratio={args.duck_ratio}:attack=5:release=300[bgduck];
        [bgduck][vo]amix=inputs=2:duration=shortest:dropout_transition=0:weights={args.bg_vol} 1.0[mix]
        """.strip().replace("\n", " ")

    print(">>> [filter_complex STEP1]", filter1)

    cmd1 = f"""
ffmpeg -hide_banner -v verbose -y \
  -i {shlex.quote(str(intro))} \
  -i {shlex.quote(str(narr))} \
  -filter_complex "{filter1}" \
  -map "[mix]" -ar 48000 -ac 2 -c:a libmp3lame -b:a 192k {shlex.quote(str(core_mix))}
""".strip()
    rc1 = run(cmd1)
    if rc1 != 0 or not core_mix.exists():
        print("!!! Step 1 failed")
        sys.exit(1)

    # ---------- STEP 2: Crossfade to Outro ----------
    filter2 = f"""
    [0:a]aformat=channel_layouts=stereo,aresample=48000[core];
    [1:a]aformat=channel_layouts=stereo,aresample=48000[out];
    [core][out]acrossfade=d={args.xfade}:c1=tri:c2=tri[preout]
    """.strip().replace("\n", " ")
    print(">>> [filter_complex STEP2]", filter2)

    cmd2 = f"""
ffmpeg -hide_banner -v verbose -y \
  -i {shlex.quote(str(core_mix))} \
  -i {shlex.quote(str(outro))} \
  -filter_complex "{filter2}" \
  -map "[preout]" -ar 48000 -ac 2 -c:a libmp3lame -b:a 192k {shlex.quote(str(core_plus_outro))}
""".strip()
    rc2 = run(cmd2)
    if rc2 != 0 or not core_plus_outro.exists():
        print("!!! Step 2 failed")
        sys.exit(1)

    # ---------- STEP 3: Final Loudness Normalize ----------
    filter3 = f"loudnorm=I={args.lufs}:TP={args.tp}:LRA={args.lra}:print_format=summary"
    print(">>> [filter STEP3]", filter3)

    cmd3 = f"""
ffmpeg -hide_banner -v verbose -y \
  -i {shlex.quote(str(core_plus_outro))} \
  -filter:a "{filter3}" \
  -ar 48000 -ac 2 -c:a libmp3lame -b:a 192k {shlex.quote(str(out))}
""".strip()
    rc3 = run(cmd3)
    if rc3 != 0 or not out.exists():
        print("!!! Step 3 (loudnorm) failed")
        sys.exit(1)

    # Cleanup temp intermediates (keep if debugging is enabled)
    if not DEBUG:
        try:
            core_mix.unlink(missing_ok=True)
            core_plus_outro.unlink(missing_ok=True)
        except Exception:
            pass

    print(f"✅ Done. Wrote: {out}")

if __name__ == "__main__":
    main()
