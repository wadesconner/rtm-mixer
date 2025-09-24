# audio_mix.py
import subprocess
import shlex
import os
from pathlib import Path

def mix_with_bed(
    narration_path: str,
    bed_path: str,
    out_path: str,
    *,
    bed_gain_db: float = -16.0,       # overall bed level
    duck_db: float = -12.0,           # approx reduction amount under narration (via threshold/ratio below)
    threshold_db: float = -30.0,      # sidechain threshold
    ratio: float = 8.0,               # compression ratio while narration is present
    attack_ms: int = 5,               # how fast ducking engages (ms)
    release_ms: int = 300,            # how fast ducking releases (ms)
    fade_ms: int = 600,               # fade for start/end of music & song
    song_clip: str | None = None,     # optional full path to a song clip
    song_start: float | None = None,  # when to play the song clip (seconds)
    song_gain_db: float = -3.0        # song clip level
) -> None:
    """
    Produce a final mix with:
      - ducked music bed under narration
      - optional song clip (mutes bed while song plays)
    Requires ffmpeg in PATH.
    """
    narration = Path(narration_path)
    bed = Path(bed_path)
    out = Path(out_path)
    assert narration.exists(), f"Missing narration file: {narration}"
    assert bed.exists(), f"Missing bed file: {bed}"

    # Build filter graph
    # a0 = narration, b0 = bed, s0 = optional song
    # Normalize loudness a bit for consistency; then:
    #   - sidechaincompress: bed ducks when narration present
    #   - optional volume "if(between(t,ts,te),0,1)" to mute bed during song
    #   - amix narration + bed
    #   - optional add song with adelay and amix
    filter_parts = []
    inputs = [f"-i {shlex.quote(str(bed))}", f"-i {shlex.quote(str(narration))}"]  # [0]=bed, [1]=nar

    filtergraph = []

    # Labels:
    # [0:a] -> b0 ; [1:a] -> a0
    # Loudness normalize and set bed gain
    filtergraph.append(
        "[0:a]loudnorm=I=-16:TP=-1.5:LRA=11,"
        f"volume={bed_gain_db}dB[b0n]"
    )
    # Normalize narration
    filtergraph.append(
        "[1:a]loudnorm=I=-16:TP=-1.5:LRA=11[a0n]"
    )

    # Ducking: bed side-chained by narration
    # sidechaincompress params: threshold (dB), ratio, attack (ms), release (ms)
    filtergraph.append(
        "[b0n][a0n]sidechaincompress="
        f"threshold={threshold_db}dB:ratio={ratio}:attack={attack_ms}:release={release_ms}:makeup=0:sclevel=1[bed_duck]"
    )

    # Optional SONG handling
    have_song = (song_clip is not None) and (song_start is not None)
    if have_song:
        song = Path(song_clip)
        assert song.exists(), f"Missing song file: {song}"
        inputs.insert(0, f"-i {shlex.quote(str(song))}")  # song becomes [0], bed-> [1], nar -> [2]
        # Adjust labels in filtergraph if we shifted inputs: we did, so fix:
        # We'll rebuild from scratch for clarity if song is present:
        filtergraph = []
        # [0:a]=s0, [1:a]=b0, [2:a]=a0
        filtergraph.append(
            "[1:a]loudnorm=I=-16:TP=-1.5:LRA=11,"
            f"volume={bed_gain_db}dB[b0n]"
        )
        filtergraph.append(
            "[2:a]loudnorm=I=-16:TP=-1.5:LRA=11[a0n]"
        )
        filtergraph.append(
            "[b0n][a0n]sidechaincompress="
            f"threshold={threshold_db}dB:ratio={ratio}:attack={attack_ms}:release={release_ms}:makeup=0:sclevel=1[bed_duck_raw]"
        )

        # We need to mute bed during the song interval:
        # We'll compute song end using asetpts+atrim inside ffmpeg is trickier without probing,
        # but a simpler approach: apply a conditional mute to the bed for a window [song_start, song_start + dur_of_s0].
        # Since we don't know song duration here, we just mute while song plays by delaying the song and amix with dropout_transition,
        # and also explicitly mute bed volume in an interval that's at least the song's duration.
        # To do it robustly without python ffprobe, we’ll let ffmpeg query duration for the enable condition via t.
        # Practical approach: we gate the bed with a fade out/in around the window and rely on amix dropout blending.

        # Define fades for bed around the song window using a small guard equal to fade_ms:
        ts = float(song_start)
        fd = fade_ms / 1000.0
        # We'll do a fast fade out at ts, fade in at ts + SONG_DURATION using a gate;
        # Since dynamic-unknown duration is tough directly, we’ll simply hard mute bed whenever song power is present
        # using 'sidechaingate' with the song as the sidechain. That way, bed is forced off while song plays.
        # Then we also add a tiny fade to smooth transitions.
        filtergraph.append(
            # sidechaingate: mute primary input when sidechain is *above* threshold
            "[bed_duck_raw][0:a]sidechaingate=threshold=-50dB:ratio=20:attack=5:release=200[bed_duck]"
        )

        # So: [bed_duck] is: ducked under voice, and fully muted while the song is active.

        # Now prep the song: fade in/out and level, then delay to song_start
        # adelay needs ms; if stereo, provide both channels
        adelay_ms = int(song_start * 1000)
        filtergraph.append(
            "[0:a]afade=t=in:d={fi},afade=t=out:d={fo},volume={sg}dB,adelay={ms}|{ms}[songd]".format(
                fi=fade_ms/1000.0, fo=fade_ms/1000.0, sg=song_gain_db, ms=adelay_ms
            )
        )

        # Mix narration + bed first
        filtergraph.append(
            "[a0n][bed_duck]amix=inputs=2:duration=longest:dropout_transition=200[mix1]"
        )
        # Add song on top
        filtergraph.append(
            "[mix1][songd]amix=inputs=2:duration=longest:dropout_transition=200[mix]"
        )

    else:
        # No song: just mix narration + ducked bed
        filtergraph.append(
            "[a0n][bed_duck]amix=inputs=2:duration=longest:dropout_transition=200[mix]"
        )

    # Final small fade in/out (nice polish; optional)
    filtergraph.append(
        f"[mix]afade=t=in:d={fade_ms/1000.0},afade=t=out:d={fade_ms/1000.0}[outa]"
    )

    cmd = [
        "ffmpeg",
        "-y",
        *shlex.split(" ".join(inputs)),
        "-filter_complex",
        ";".join(filtergraph),
        "-map", "[outa]",
        "-ac", "2",
        "-ar", "44100",
        "-c:a", "mp3",
        "-b:a", "192k",
        shlex.quote(str(out))
    ]

    # Run
    proc = subprocess.run(" ".join(cmd), shell=True, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\nCMD:\n{' '.join(cmd)}\n\nSTDERR:\n{proc.stderr}")
