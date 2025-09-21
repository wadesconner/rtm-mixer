#!/usr/bin/env python3
"""
RTM Mixer — voice-forward, timing controls, robust

Step 1 (core mix):
- Force both inputs to 48k stereo.
- Optional voice preroll delay (narr_delay sec) so the bed plays before Clyde enters.
- Voice: high-pass @120 Hz + gain (voice_gain).
- Plain amix with explicit weights (bg_weight : voice_weight).
- Bed multiplies by bg_vol before mix.

Step 2:
- acrossfade to outro with adjustable gain (outro_gain) and time (xfade).

Step 3:
- loudnorm at the very end (I/TP/LRA).

Diagnostics:
- --voice_only : output processed voice only (stereo, HPF, gain, delay).
- --step1_only : write Step-1 (bed+voice), stop before outro/loudnorm.
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

    # Balance/levels
    ap.add_argument("--bg_vol", type=float, default=0.25)          # overall bed multiplier
    ap.add_argument("--voice_gain", type=float, default=3.0)       # post-HPF gain on voice
    ap.add_argument("--bg_weight", type=float, default=0.35)       # relative weight in amix
    ap.add_argument("--voice_weight", type=float, default=1.00)    # relative weight in amix

    # Timing
    ap.add_argument("--narr_delay", type=float, default=0.6)       # seconds to delay voice vs bed
    ap.add_argument("--xfade", type=float, default=1.2)            # seconds of acrossfade to outro

    # Outro level
    ap.add_argument("--outro_gain", type=float, default=0.9)

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
        f"bg_vol={args.bg_vol} voice_gain={args.voice_gain} weights={args.bg_weight}:{args.voice_weight} "
        f"narr_delay={args.narr_delay}s xfade={args.xfade}s outro_gain={args.outro_gain} "
        f"lufs={args.lufs} tp={args.tp} lra={args.lra} "
        f"voice_only={args.voice_only} step1_only={args.step1_only}"
    )

    if DEBUG:
        ffprobe_info("intro", intro)
        ffprobe_info("narr", narr)
        ffprobe_info("outro", outro)

    core_mix = out.with_suffix(".core_mix.mp3")
    core_plus_outro = out.with_suffix(".core_plus_outro.mp3")

    # ---------- STEP 1: Intro BG + Narration (with optional voice delay) ----------
    delay_ms = max(0, int(round(args.narr_delay * 1000)))
    if args.voice_only:
        filter1 = (
            # Process voice only
            f"[1:a]aresample=48000,aformat=channel_layouts=stereo,"
            f"highpass=f=120,volume={args.voice_gain},adelay={delay_ms}|{delay_ms}[voice];"
            "[voice]anull[mix]"
        )
    else:
        filter1 = (
            # Upmix both to stereo @ 48k
            f"[0:a]aresample=48000,aformat=channel_layouts=stereo,volume={args.bg_vol}[bg];"
            f"[1:a]aresample=48000,aformat=channel_layouts=stereo,highpass=f=120,volume={args.voice_gain},"
            f"adelay={delay_ms}|{delay_ms}[voice];"
            # Plain amix with explicit weights favoring voice
            f"[bg][voice]amix=inputs=2:duration=shortest:dropout_transition=0:weights={args.bg_weight} {args.voice_weight}[mix]"
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

    if args.voice_only or args.step1_only:
        core_mix.replace(out)
        print(f"✅ {'Voice-only' if args.voice_only else 'Step-1-only'} complete. Wrote: {out}")
        return

    # ---------- STEP 2: Crossfade to Outro (with outro_gain) ----------
    filter2 = (
        "[0:a]aformat=channel_layouts=stereo,aresample=48000[core];"
        f"[1:a]aformat=channel_layouts=stereo,aresample=48000,volume={args.outro_gain}[out];"
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
