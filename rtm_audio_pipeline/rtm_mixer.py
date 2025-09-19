#!/usr/bin/env python3
"""
RTM Mixer - intro BG + narration + outro bed -> polished MP3 using ffmpeg.

Debug aids:
- Prints FULL ffmpeg stderr/stdout so we can see filter graph & warnings.
- If env RTM_DEBUG=1, also ffprobes inputs & logs duration/channel info.

Pipeline:
- Force inputs to stereo @ 48k.
- Apply BG volume BEFORE & AFTER ducking for strong control.
- Mix ends at narration end (duration=shortest).
- Crossfade into short outro.
- Loudness normalize.
"""

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

DEBUG = os.getenv("RTM_DEBUG", "0") == "1"

def run(cmd: str, show=True):
    """Run a shell command, return code; print stdout/stderr for visibility."""
    if show:
        print(">>>", cmd)
    p = subprocess.run(
        cmd, shell=True,
        capture_output=True, text=True
    )
    if show:
        if p.stdout:
            print(">>> [stdout]\n", p.stdout)
        if p.stderr:
            print(">>> [stderr]\n", p.stderr)
    return p.returncode

def ffprobe_info(label: str, path: Path):
    """Log quick ffprobe info (duration, channels, sample_rate)."""
    cmd = f'ffprobe -hide_banner -v error -show_entries stream=channels,sample_rate -show_entries format=duration -of json {shlex.quote(str(path))}'
    print(f">>> ffprobe {label}:", path)
    _ = run(cmd)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--intro", required=True, help="Intro background (long, w/ sonic logo)")
    ap.add_argument("--narr", required=True, help="Narration (dry voice)")
    ap.add_argument("--outro", required=True, help="Short outro bed (~5s)")
    ap.add_argument("--out", required=True, help="Output MP3 path")

    # Mix controls
    ap.add_argument("--bg_vol", type=float, default=0.25, help="BG volume multiplier (applied pre & post duck)")
    ap.add_argument("--duck_threshold", type=float, default=0.02, help="Sidechain threshold (lower = more duck)")
    ap.add_argument("--duck_ratio", type=float, default=12.0, help="Sidechain ratio (higher = more duck)")
    ap.add_argument("--xfade", type=float, default=1.0, help="Crossfade seconds into outro")

    # Loudness
    ap.add_argument("--lufs", type=float, default=-16.0, help="Integrated LUFS target")
    ap.add_argument("--tp", type=float, default=-1.5, help="True peak ceiling dBTP")
    ap.add_argument("--lra", type=float, default=11.0, help="Loudness range")

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

    # STEP 1: Mix intro BG + narration (force stereo/48k, duck, amix)
    cmd1 = f"""
ffmpeg -hide_banner -v verbose -y \
  -i {shlex.quote(str(intro))} \
  -i {shlex.quote(str(narr))} \
  -filter_complex "
    [0:a]aformat=channel_layouts=stereo,aresample=48000,volume={args.bg_vol}[bgpre];
    [1:a]aformat=channel_layouts=stereo,aresample=48000[narr];
    [bgpre][narr]sidechaincompress=threshold={args.duck_threshold}:ratio={args.duck_ratio}:attack=5:release=300[ducked];
    [ducked]volume={args.bg_vol}[bgpost];
    [bgpost][narr]amix=inputs=2:duration=shortest:dropout_transition=0,
      dynaudnorm=f=75:g=10,
      loudnorm=I={args.lufs}:TP={args.tp}:LRA={args.lra}[mix]
  " -map "[mix]" -ar 48000 -ac 2 -c:a libmp3lame -b:a 192k {shlex.quote(str(core_mix))}
""".strip()
    rc1 = run(cmd1)
    if rc1 != 0 or not core_mix.exists():
        print("!!! Step 1 failed")
        sys.exit(1)

    # STEP 2: Crossfade core -> outro (force stereo/48k on both)
    cmd2 = f"""
ffmpeg -hide_banner -v verbose -y \
  -i {shlex.quote(str(core_mix))} \
  -i {shlex.quote(str(outro))} \
  -filter_complex "
    [0:a]aformat=channel_layouts=stereo,aresample=48000[core];
    [1:a]aformat=channel_layouts=stereo,aresample=48000[out];
    [core][out]acrossfade=d={args.xfade}:c1=tri:c2=tri
  " -ar 48000 -ac 2 -c:a libmp3lame -b:a 192k {shlex.quote(str(core_plus_outro))}
""".strip()
    rc2 = run(cmd2)
    if rc2 != 0 or not core_plus_outro.exists():
        print("!!! Step 2 failed")
        sys.exit(1)

    # Finalize
    core_plus_outro.replace(out)
    try:
        core_mix.unlink(missing_ok=True)
    except Exception:
        pass

    print(f"✅ Done. Wrote: {out}")

if __name__ == "__main__":
    main()
