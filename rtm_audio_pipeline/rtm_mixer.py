#!/usr/bin/env python3
"""
RTM Mixer - stitches intro background + narration + outro bed into a single polished MP3 using ffmpeg.

Usage:
  python rtm_mixer.py --intro rtm_intro_bg.mp3 --narr rtm_narration.mp3 --outro rtm_outro_bg.mp3 --out rtm_final_mix.mp3

Requirements:
  - Python 3.8+
  - ffmpeg installed and on PATH (https://ffmpeg.org/)
"""

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

def run(cmd):
    print(">>>", cmd)
    p = subprocess.run(cmd, shell=True)
    if p.returncode != 0:
        sys.exit(p.returncode)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--intro", required=True, help="Path to intro background file (long looped BG with sonic logo)")
    ap.add_argument("--narr", required=True, help="Path to narration file (dry Clyde content)")
    ap.add_argument("--outro", required=True, help="Path to short outro background bed (≈5s)")
    ap.add_argument("--out", required=True, help="Output MP3 path")
    ap.add_argument("--bg_vol", type=float, default=0.25, help="Background volume multiplier (default 0.25)")
    ap.add_argument("--duck_threshold", type=float, default=0.02, help="Sidechain threshold (default 0.02)")
    ap.add_argument("--duck_ratio", type=float, default=12.0, help="Sidechain ratio (default 12)")
    ap.add_argument("--xfade", type=float, default=1.0, help="Crossfade (seconds) from core into outro (default 1.0)")
    ap.add_argument("--lufs", type=float, default=-16.0, help="Integrated LUFS target for loudnorm (default -16.0)")
    ap.add_argument("--tp", type=float, default=-1.5, help="True peak ceiling dBTP (default -1.5)")
    ap.add_argument("--lra", type=float, default=11.0, help="Loudness range for loudnorm (default 11.0)")
    args = ap.parse_args()

    intro = Path(args.intro)
    narr = Path(args.narr)
    outro = Path(args.outro)
    out = Path(args.out)

    if not intro.exists() or not narr.exists() or not outro.exists():
        print("One or more input files do not exist.", file=sys.stderr)
        sys.exit(2)

    core_mix = out.with_suffix(".core_mix.mp3")
    core_plus_outro = out.with_suffix(".core_plus_outro.mp3")

    # Step 1: Mix intro BG + narration (duck BG under narration, stop at narration end)
    cmd1 = f"""
ffmpeg -y -i {shlex.quote(str(intro))} -i {shlex.quote(str(narr))} -filter_complex "
[0:a]volume={args.bg_vol},aresample=48000,pan=stereo|c0=c0|c1=c1[bg];
[1:a]aresample=48000,pan=stereo|c0=c0|c1=c1[narr];
[bg][narr]sidechaincompress=threshold={args.duck_threshold}:ratio={args.duck_ratio}:attack=5:release=300[ducked_bg];
[ducked_bg][narr]amix=inputs=2:duration=shortest:dropout_transition=0,
dynaudnorm=f=75:g=10,
loudnorm=I={args.lufs}:TP={args.tp}:LRA={args.lra}[mix]
" -map "[mix]" -ar 48000 -ac 2 -c:a libmp3lame -b:a 192k {shlex.quote(str(core_mix))}
""".strip()
    run(cmd1)

    # Step 2: Crossfade into the outro bed
    cmd2 = f"""
ffmpeg -y -i {shlex.quote(str(core_mix))} -i {shlex.quote(str(outro))} -filter_complex "acrossfade=d={args.xfade}:c1=tri:c2=tri" -ar 48000 -ac 2 -c:a libmp3lame -b:a 192k {shlex.quote(str(core_plus_outro))}
""".strip()
    run(cmd2)

    # No signoff for now; rename to final output
    core_plus_outro.rename(out)

    # Cleanup intermediates (optional)
    try:
        Path(core_mix).unlink(missing_ok=True)
    except Exception:
        pass

    print(f"✅ Done. Wrote: {out}")

if __name__ == "__main__":
    main()
