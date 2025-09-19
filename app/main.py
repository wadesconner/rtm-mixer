import os
import shlex
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.responses import FileResponse, HTMLResponse

app = FastAPI(title="RTM Mixer API")

# Simple healthcheck for / 
@app.get("/")
def root():
    return {"status": "ok"}

# Where the mixer script lives
PIPELINE_DIR = Path(__file__).resolve().parent.parent / "rtm_audio_pipeline"
MIXER = PIPELINE_DIR / "rtm_mixer.py"

def _run(cmd: str) -> int:
    print(">>>", cmd)
    return subprocess.run(cmd, shell=True).returncode

# --- Primary API: POST /api/mix ---------------------------------------------
@app.post("/api/mix")
async def mix(
    intro: UploadFile = File(...),      # rtm_intro_bg.mp3 (long, includes sonic logo)
    narr: UploadFile = File(...),       # rtm_narration.mp3 (dry voice)
    outro: UploadFile = File(...),      # rtm_outro_bg.mp3 (~5s with fade)
    # query (or swagger param) controls:
    bg_vol: float = 0.25,
    duck_threshold: float = 0.02,
    duck_ratio: float = 12.0,
    xfade: float = 1.0,
    lufs: float = -16.0,
    tp: float = -1.5,
    lra: float = 11.0,
):
    """
    Mix intro + narration + outro with radio-style ducking & loudness.
    Returns the final MP3 as the response file.
    """
    if not MIXER.exists():
        raise HTTPException(500, detail=f"Mixer script not found at {MIXER}")

    workdir = Path(tempfile.mkdtemp(prefix="rtm_mix_"))
    try:
        intro_path  = workdir / "rtm_intro_bg.mp3"
        narr_path   = workdir / "rtm_narration.mp3"
        outro_path  = workdir / "rtm_outro_bg.mp3"
        out_path    = workdir / f"rtm_final_{uuid.uuid4().hex}.mp3"

        intro_path.write_bytes(await intro.read())
        narr_path.write_bytes(await narr.read())
        outro_path.write_bytes(await outro.read())

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

        rc = _run(cmd)
        if rc != 0 or not out_path.exists():
            raise HTTPException(500, detail="Mixing failed")

        return FileResponse(str(out_path), media_type="audio/mpeg", filename="rtm_final_mix.mp3")
    finally:
        # keep temp dir for now (easier debugging). We can clean later.
        pass

# --- Simple browser form: /upload -------------------------------------------
@app.get("/upload", response_class=HTMLResponse)
def upload_form():
    return """
    <html><body style="font-family: system-ui; padding: 24px; line-height:1.4">
      <h2>RTM Mixer</h2>
      <form action="/upload" method="post" enctype="multipart/form-data">
        <div>Intro (mp3): <input type="file" name="intro" accept="audio/mpeg" required></div>
        <div>Narration (mp3): <input type="file" name="narr" accept="audio/mpeg" required></div>
        <div>Outro (mp3): <input type="file" name="outro" accept="audio/mpeg" required></div>
        <div style="margin-top:12px"><button type="submit">Mix</button></div>
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
    # Use defaults; you can tune later via /api/mix directly
    return await mix(
        intro=intro, narr=narr, outro=outro,
        bg_vol=0.25, duck_threshold=0.02, duck_ratio=12.0,
        xfade=1.0, lufs=-16.0, tp=-1.5, lra=11.0
    )

# --- Generate narration via ElevenLabs and mix: /generate --------------------
import httpx
ELEVEN_KEY = os.getenv("ELEVENLABS_API_KEY", "")

@app.get("/generate", response_class=HTMLResponse)
def generate_form():
    return """
    <html><body style="font-family: system-ui; padding: 24px; line-height:1.4">
      <h2>Generate Narration & Mix</h2>
      <form action="/generate" method="post" enctype="multipart/form-data">
        <div>Script:</div>
        <textarea name="script" rows="8" cols="80" placeholder="Paste narration text here" required>
Hey Kelli — welcome to Radio Time Machine!
Buckle up — we’re rewinding to the year you were born…
Then we’ll jump to your 18th birthday for the hits that defined your late teens.
Here we go!
        </textarea>
        <div style="margin-top:8px">
          ElevenLabs Voice ID:
          <input name="voice_id" placeholder="voice-id-here" required />
        </div>
        <div style="margin-top:12px">Intro (mp3): <input type="file" name="intro" accept="audio/mpeg" required></div>
        <div>Outro (mp3): <input type="file" name="outro" accept="audio/mpeg" required></div>
        <div style="margin-top:12px"><button type="submit">Generate & Mix</button></div>
      </form>
    </body></html>
    """

@app.post("/generate")
async def generate_and_mix(
    script: str = Form(...),
    voice_id: str = Form(...),
    intro: UploadFile = File(...),
    outro: UploadFile = File(...),
):
    if not ELEVEN_KEY:
        raise HTTPException(500, detail="Missing ELEVENLABS_API_KEY environment variable")

    # Call ElevenLabs v1 TTS stream API (returns MP3 bytes)
    tts_url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
    headers = {
        "xi-api-key": ELEVEN_KEY,
        "accept": "audio/mpeg",
        "Content-Type": "application/json",
    }
    payload = {"text": script, "voice_settings": {"stability": 0.5, "similarity_boost": 0.8}}

    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(tts_url, headers=headers, json=payload)
        if r.status_code != 200 or not r.content:
            raise HTTPException(500, detail=f"TTS failed: {r.status_code} {r.text[:200]}")

        # Wrap TTS result like an UploadFile for reuse with /api/mix
        class MemUpload:
            filename = "rtm_narration.mp3"
            async def read(self):
                return r.content
        narr = MemUpload()

    # Reuse the main mixer with defaults
    return await mix(
        intro=intro, narr=narr, outro=outro,
        bg_vol=0.25, duck_threshold=0.02, duck_ratio=12.0,
        xfade=1.0, lufs=-16.0, tp=-1.5, lra=11.0
    )
