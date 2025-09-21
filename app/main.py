# main.py
import os, uuid, shlex, tempfile, subprocess, shutil
from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from typing import Optional

app = FastAPI()

# ---------- CONFIG ----------
DEFAULT_BED = os.getenv("RTM_DEFAULT_BED", "assets/rtm_outro_bg.mp3")  # change if you prefer intro
OUTPUT_BITRATE = "192k"
TMP_PREFIX = "rtm_mix_"
# ---------------------------

class MixError(Exception):
    pass

def run(cmd: str) -> str:
    """Run a shell command; raise MixError on nonzero exit."""
    p = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise MixError(p.stderr.strip() or f"Command failed: {cmd}")
    return p.stdout

def probe_has_audio(path: str) -> None:
    """Basic sanity check that ffprobe can read duration."""
    out = run(f'ffprobe -v error -show_entries format=duration -of csv=p=0 {shlex.quote(path)}').strip()
    if not out or out in ("N/A", "0", "0.0"):
        raise MixError(f"Undecodable or zero-length audio: {path}")

def mix_ffmpeg(narr_path: str, bed_path: str) -> str:
    """Implements Step A in a safe temp workspace."""
    work = tempfile.mkdtemp(prefix=TMP_PREFIX)
    nar_wav = os.path.join(work, "nar_48k_mono.wav")
    bed_wav = os.path.join(work, "bed_48k_mono.wav")
    bed_loop = os.path.join(work, "bed_loop.wav")
    out_mp3 = os.path.join(work, f"rtm_mix_{uuid.uuid4().hex}.mp3")

    # Preflight: existence / readability
    for p in (narr_path, bed_path):
        if not (os.path.exists(p) and os.path.getsize(p) > 1024):
            raise MixError(f"Missing or too-small file: {p}")
        probe_has_audio(p)

    # Normalize both to 48k mono WAV
    run(f'ffmpeg -y -i {shlex.quote(narr_path)} -ac 1 -ar 48000 {shlex.quote(nar_wav)}')
    run(f'ffmpeg -y -i {shlex.quote(bed_path)}  -ac 1 -ar 48000 {shlex.quote(bed_wav)}')

    # Duration of narration
    dur = run(f'ffprobe -v error -show_entries format=duration -of csv=p=0 {shlex.quote(nar_wav)}').strip()

    # Loop/trim bed to narration length
    run(f'ffmpeg -y -stream_loop -1 -t {dur} -i {shlex.quote(bed_wav)} -ac 1 -ar 48000 {shlex.quote(bed_loop)}')

    # Sidechain compressor (duck bed under voice) + mix
    filt = (
        '[1:a][0:a]sidechaincompress='
        'threshold=0.05:ratio=8:attack=5:release=250:makeup=3[ducked];'
        '[ducked][0:a]amix=inputs=2:duration=first:weights=1 1'
    )
    run(
        f'ffmpeg -y -i {shlex.quote(nar_wav)} -i {shlex.quote(bed_loop)} '
        f'-filter_complex "{filt}" -c:a libmp3lame -b:a {OUTPUT_BITRATE} {shlex.quote(out_mp3)}'
    )

    # Final sanity check
    probe_has_audio(out_mp3)
    return out_mp3

def save_upload(u: UploadFile) -> str:
    """Save an UploadFile to /tmp and return its path."""
    if not u:
        raise HTTPException(status_code=400, detail="Expected file upload.")
    tmp_path = os.path.join("/tmp", f"{uuid.uuid4().hex}_{u.filename}")
    with open(tmp_path, "wb") as f:
        shutil.copyfileobj(u.file, f)
    return tmp_path

# Simple HTML form for manual testing
@app.get("/generate", response_class=HTMLResponse)
async def generate_form():
    return """
<!doctype html><html><body style="font-family:system-ui;max-width:680px;margin:2rem auto">
  <h2>RTM Mix Tester</h2>
  <form action="/mix" method="post" enctype="multipart/form-data">
    <div><label>Narration (required) <input type="file" name="narration" required></label></div>
    <div><label>Bed (optional; leave empty to use default) <input type="file" name="bed"></label></div>
    <div style="margin-top:1rem"><button type="submit">Mix</button></div>
  </form>
  <p style="color:#555;margin-top:1rem">Tip: If no Bed is provided, server uses <code>assets/rtm_outro_bg.mp3</code> (configurable via <code>RTM_DEFAULT_BED</code>).</p>
</body></html>
"""

# Main API: accepts either both files, or narration only (will use DEFAULT_BED)
@app.post("/mix")
async def mix_endpoint(
    narration: UploadFile = File(...),
    bed: Optional[UploadFile] = File(None),
):
    try:
        nar_path = save_upload(narration)
        if bed is not None and bed.filename:
            bed_path = save_upload(bed)
        else:
            if not (os.path.exists(DEFAULT_BED) and os.path.getsize(DEFAULT_BED) > 1024):
                raise HTTPException(status_code=400, detail=f"Default bed not found or too small: {DEFAULT_BED}")
            bed_path = DEFAULT_BED

        out_path = mix_ffmpeg(nar_path, bed_path)

        # Stream as download and also leave the file on disk (helpful for debugging).
        def iterfile():
            with open(out_path, "rb") as f:
                yield from f

        filename = os.path.basename(out_path)
        headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
        return StreamingResponse(iterfile(), media_type="audio/mpeg", headers=headers)

    except MixError as e:
        raise HTTPException(status_code=400, detail=f"Mix failed: {e}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Server error: {e}")
