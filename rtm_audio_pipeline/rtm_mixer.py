#!/usr/bin/env python3
"""
RTM Mixer — intro BG + narration + outro bed -> polished MP3 using ffmpeg.

Diagnostics & voice-forward:
- Unique labels to avoid binding conflicts.
- Voice: true-stereo (pan), high-pass 120 Hz, gain (default 3.0x).
- amix weights favor voice (BG:Voice = 0.30:1.00) + sidechain duck.
- FINAL loudnorm only.
- --voice_only     : output narration only (sanity check).
- --step1_only     : return Step-1 (bed+voice) BEFORE outro/loudnorm.
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
    ap.add_argument("--intro", required=True)
    ap.add_argument("--narr", required=True)
    ap.add_argument("--outro", required=True)
    ap.add_argument("--out", required=True)

    # Mix controls
    ap.add_argument("--bg_vol", type=float, default=0.25)
    ap.add_argument("--duck_threshold", type=float, default=0.02)
    ap.add_argument("--duck_ratio", type=float, default=12.0)
    ap.add_argument("--xfade", type=float, default=1.0)

    # Voice-forward knobs
    ap.add_argument("--voice_gain", type=float, default=3.0)
    ap.add_argument("--bg_weight", type=float, default=0.30)
    ap.add_argument("--voice_weight", type=float, default=1.00)

    # Loudness
    ap.add_argument("--lufs", type=float, default=-16.0)
    ap.add_argument("--tp", type=float, default=-1.5)
    ap.add_argument("--lra", type=float, default=11.0)

    # Diagnostics
    ap.add_argument("--voice_only", action="store_true")
    ap.add_argument("--step1_only", action="store_true")

    args = ap.parse_args()

    intro = Path(args.intro)
    narr = Path(args.narr)
    outro = Path(args.outro)
    out = Path(args.out)

    if not intro.exists() or not narr.exists() or not outro.exists():
        print("One or more input files do not exist.", file=sys.stderr)
        sys.exit(2)

    print(
        "=== RTM MIX PARAMS === "
        f"bg_vol={args.bg_vol} duck_threshold={args.duck_threshold} duck_ratio={args.duck_ratio} "
        f"voice_gain={args.voice_gain} weights={args.bg_weight}:{args.voice_weight} "
        f"xfade={args.xfade} lufs={args.lufs} tp={args.tp} lra={args.lra} "
        f"voice_only={args.voice_only} step1_only={args.step1_only}"
    )

    if DEBUG:
        ffprobe_info("intro", intro)
        ffprobe_info("narr", narr)
        ffprobe_info("outro", outro)

    core_mix = out.with_suffix(".core_mix.mp3")
    core_plus_outro = out.with_suffix(".core_plus_outro.mp3")

    # ---------- STEP 1: Intro BG + Narration ----------
    if args.voice_only:
        filter1 = (
            "[1:a]aformat=channel_layouts=mono,aresample=48000[voice_in];"
            "[voice_in]pan=stereo|c0=c0|c1=c0,highpass=f=120,volume=2.0[mix]"
        )
    else:
        filter1 = (
            "[0:a]aformat=channel_layouts=stereo,aresample=48000[bg_in];"
            f"[bg_in]volume={args.bg_vol}[bg_pre];"
            "[1:a]aformat=channel_layouts=mono,aresample=48000[voice_in];"
            f"[voice_in]pan=stereo|c0=c0|c1=c0,highpass=f=120,volume={args.voice_gain}[voice_pre];"
            f"[bg_pre][voice_pre]sidechaincompress=threshold={args.duck_threshold}:ratio={args.duck_ratio}:attack=5:release=300[bg_duck];"
            f"[bg_duck][voice_pre]amix=inputs=2:duration=shortest:dropout_transition=0:weights={args.bg_weight} {args.voice_weight}[mix]"
        )

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

    # Voice-only or Step-1-only exit
    if args.voice_only or args.step1_only:
        core_mix.replace(out)
        print(f"✅ {'Voice-only' if args.voice_only else 'Step-1-only'} complete. Wrote: {out}")
        return

    # ---------- STEP 2: Crossfade to Outro ----------
    filter2 = (
        "[0:a]aformat=channel_layouts=stereo,aresample=48000[core];"
        "[1:a]aformat=channel_layouts=stereo,aresample=48000[out];"
        f"[core][out]acrossfade=d={args.xfade}:c1=tri:c2=tri[preout]"
    )
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

    if not DEBUG:
        try:
            core_mix.unlink(missing_ok=True)
            core_plus_outro.unlink(missing_ok=True)
        except Exception:
            pass

    print(f"✅ Done. Wrote: {out}")

if __name__ == "__main__":
    main()
