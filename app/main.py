# app/main.py
import os, tempfile, subprocess, shlex, uuid
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
from pathlib import Path

app = FastAPI(title="RTM Mixer API")

from fastapi import Form
from fastapi.responses import HTMLResponse

@app.get("/upload", response_class=HTMLResponse)
def upload_form():
    return """
    <html><body style="font-family: system-ui; padding: 24px">
      <h2>RTM Mixer</h2>
      <form action="/upload" method="post" enctype="multipart/form-data">
        <div>Intro (mp3): <input type="file" name="intro" accept="audio/mpeg" required></div>
        <div>Narration (mp3): <input type="file" name="narr" accept="audio/mpeg" required></div>
        <div>Outro (mp3): <input type="file" name="outro" accept="audio/mpeg" required></div>
        <div style="margin-top:12px">
          <button type="submit">Mix</button>
        </div>
      </form>
      <p style="margin-top:24px"><a href="/generate">Or generate narration from text →</a></p>
    </body></html>
    """

@app.post("/upload")
async def upload_and_mix(
    intro: UploadFile = File(...),
    narr: UploadFile = File(...),
    outro: UploadFile = File(...),
):
    # reuse your existing /api/mix logic with defaults
    return await mix(
        intro=intro, narr=narr, outro=outro,
        bg_vol=0.25, duck_threshold=0.02, duck_ratio=12.0,
        xfade=1.0, lufs=-16.0, tp=-1.5, lra=11.0
    )

@app.get("/")
def root():
    return {"status": "ok"}


PIPELINE_DIR = Path(__file__).resolve().parent.parent / "rtm_audio_pipeline"
MIXER = PIPELINE_DIR / "rtm_mixer.py"

@app.post("/api/mix")
async def mix(
    intro: UploadFile = File(...),      # rtm_intro_bg.mp3 (long, includes sonic logo)
    narr: UploadFile = File(...),       # rtm_narration.mp3 (dry)
    outro: UploadFile = File(...),      # rtm_outro_bg.mp3 (~5s)
    bg_vol: float = 0.25,
    duck_threshold: float = 0.02,
    duck_ratio: float = 12.0,
    xfade: float = 1.0,
    lufs: float = -16.0,
    tp: float = -1.5,
    lra: float = 11.0
):
    if not MIXER.exists():
        raise HTTPException(500, detail="Mixer script not found on server")

    workdir = Path(tempfile.mkdtemp(prefix="rtm_mix_"))
    try:
        intro_path  = workdir / "rtm_intro_bg.mp3"
        narr_path   = workdir / "rtm_narration.mp3"
        outro_path  = workdir / "rtm_outro_bg.mp3"
        out_path    = workdir / f"rtm_final_{uuid.uuid4().hex}.mp3"

        # Save uploads
        intro_bytes = await intro.read()
        narr_bytes  = await narr.read()
        outro_bytes = await outro.read()
        intro_path.write_bytes(intro_bytes)
        narr_path.write_bytes(narr_bytes)
        outro_path.write_bytes(outro_bytes)

        # Build command
        cmd = f"""
        python {shlex.quote(str(MIXER))} \
          --intro {shlex.quote(str(intro_path))} \
          --narr {shlex.quote(str(narr_path))} \
          --outro {shlex.quote(str(outro_path))} \
          --out {shlex.quote(str(out_path))} \
          --bg_vol {bg_vol} \
          --duck_threshold {duck_threshold} \
          --duck_ratio {duck_ratio} \
          --xfade {xfade} \
          --lufs {lufs} \
          --tp {tp} \
          --lra {lra}
        """.strip()

        proc = subprocess.run(cmd, shell=True)
        if proc.returncode != 0 or not out_path.exists():
            raise HTTPException(500, detail="Mixing failed")

        # Return the MP3
        return FileResponse(str(out_path), media_type="audio/mpeg", filename="rtm_final_mix.mp3")
    finally:
        # Optional: keep temp files for debugging; otherwise, clean up
        pass
