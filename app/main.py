import os
import shlex
import subprocess
import tempfile
import uuid
from io import BytesIO
from starlette.datastructures import UploadFile as StarletteUploadFile

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
import httpx

app = FastAPI(title="RTM Mixer API")

# Healthcheck
@app.get("/")
def root():
    return {"status": "ok"}

# Paths
PIPELINE_DIR = Path(__file__).resolve().parent.parent / "rtm_audio_pipeline"
MIXER = PIPELINE_DIR / "rtm_mixer.py"

def _run(cmd: str) -> int:
    print(">>>", cmd)
    return subprocess.run(cmd, shell=True).returncode

# -------------------------- /api/mix (GET helper) --------------------------
@app.get("/api/mix")
async def api_mix_get():
    """
    Friendly GET so opening /api/mix in a browser doesn't 405.
    Explains how to POST multipart/form-data with intro/narr/outro files.
    """
    return JSONResponse({
        "status": "ok",
        "how_to_use": "POST multipart/form-data to /api/mix with 3 files and optional knobs.",
        "required_files": {
            "intro": "mp3 (e.g., rtm_intro_bg.mp3, long bed with sonic logo)",
            "narr": "mp3 (dry voice narration, from TTS or upload)",
            "outro": "mp3 (e.g., rtm_outro_bg.mp3, ~5s fade)"
        },
        "optional_knobs": [
            "bg_vol (float, default 0.25)",
            "duck_threshold (float, default 0.02)",
            "duck_ratio (float, default 12.0)",
            "xfade (float, default 1.0)",
            "lufs (float, default -16.0)",
            "tp (float, default -1.5)",
            "lra (float, default 11.0)"
        ],
        "example_curl": [
            "curl -X POST https://YOUR_DOMAIN/api/mix \\",
            "  -F intro=@rtm_intro_bg.mp3 \\",
            "  -F narr=@rtm_narration.mp3 \\",
            "  -F outro=@rtm_outro_bg.mp3 \\",
            "  -F bg_vol=0.0 \\",
            "  --output mix.mp3"
        ],
    })

# -------------------------- /api/mix (POST real mixer) --------------------------
# Accepts knobs as query params OR form fields (both work).
@app.post("/api/mix")
async def mix(
    intro: UploadFile = File(...),      # rtm_intro_bg.mp3 (long, with sonic logo)
    narr: UploadFile = File(...),       # rtm_narration.mp3 (dry voice)
    outro: UploadFile = File(...),      # rtm_outro_bg.mp3 (~5s fade bed)
    # query params (optional)
    bg_vol: Optional[float] = None,
    duck_threshold: Optional[float] = None,
    duck_ratio: Optional[float] = None,
    xfade: Optional[float] = None,
    lufs: Optional[float] = None,
    tp: Optional[float] = None,
    lra: Optional[float] = None,
    # form fallbacks (so the HTML forms can pass these too)
    bg_vol_form: Optional[float] = Form(None),
    duck_threshold_form: Optional[float] = Form(None),
    duck_ratio_form: Optional[float] = Form(None),
    xfade_form: Optional[float] = Form(None),
    lufs_form: Optional[float] = Form(None),
    tp_form: Optional[float] = Form(None),
    lra_form: Optional[float] = Form(None),
):
    if not MIXER.exists():
        raise HTTPException(500, detail=f"Mixer script not found at {MIXER}")

    # prefer query value; else form; else default
    bg_vol = bg_vol if bg_vol is not None else (bg_vol_form if bg_vol_form is not None else 0.25)
    duck_threshold = duck_threshold if duck_threshold is not None else (duck_threshold_form if duck_threshold_form is not None else 0.02)
    duck_ratio = duck_ratio if duck_ratio is not None else (duck_ratio_form if duck_ratio_form is not None else 12.0)
    xfade = xfade if xfade is not None else (xfade_form if xfade_form is not None else 1.0)
    lufs = lufs if lufs is not None else (lufs_form if lufs_form is not None else -16.0)
    tp = tp if tp is not None else (tp_form if tp_form is not None else -1.5)
    lra = lra if lra is not None else (lra_form if lra_form is not None else 11.0)

    workdir = Path(tempfile.mkdtemp(prefix="rtm_mix_"))
    try:
        intro_path  = workdir / "rtm_intro_bg.mp3"
        narr_path   = workdir / "rtm_narration.mp3"
        outro_path  = workdir / "rtm_outro_bg.mp3"
        out_path    = workdir / f"rtm_final_{uuid.uuid4().hex}.mp3"

        intro_path.write_bytes(await intro.read())
        narr_bytes = await narr.read()
        if not narr_bytes or len(narr_bytes) < 500:
            raise HTTPException(500, detail="Narration audio is empty or too short")
        narr_path.write_bytes(narr_bytes)
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
        # keep temp dir for now (debuggability)
        pass

# -------------------------- /upload (simple browser form) --------------------------
@app.get("/upload", response_class=HTMLResponse)
def upload_form():
    return """
    <html><body style="font-family: system-ui; padding: 24px; line-height:1.4">
      <h2>RTM Mixer</h2>
      <form action="/upload" method="post" enctype="multipart/form-data">
        <div>Intro (mp3): <input type="file" name="intro" accept="audio/mpeg" required></div>
        <div>Narration (mp3): <input type="file" name="narr" accept="audio/mpeg" required></div>
        <div>Outro (mp3): <input type="file" name="outro" accept="audio/mpeg" required></div>
        <fieldset style="margin-top:16px">
          <legend>Mix Settings</legend>
          <div>bg_vol: <input type="number" step="0.01" name="bg_vol_form" value="0.25"></div>
          <div>duck_threshold: <input type="number" step="0.001" name="duck_threshold_form" value="0.02"></div>
          <div>duck_ratio: <input type="number" step="1" name="duck_ratio_form" value="12"></div>
          <div>xfade: <input type="number" step="0.1" name="xfade_form" value="1.0"></div>
        </fieldset>
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
    bg_vol_form: Optional[float] = Form(0.25),
    duck_threshold_form: Optional[float] = Form(0.02),
    duck_ratio_form: Optional[float] = Form(12.0),
    xfade_form: Optional[float] = Form(1.0),
):
    # hand values through to /api/mix
    return await mix(
        intro=intro, narr=narr, outro=outro,
        bg_vol=bg_vol_form, duck_threshold=duck_threshold_form, duck_ratio=duck_ratio_form,
        xfade=xfade_form, lufs=-16.0, tp=-1.5, lra=11.0
    )

# -------------------------- /generate (ElevenLabs TTS) --------------------------
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
        <fieldset style="margin-top:16px">
          <legend>Mix Settings</legend>
          <div>bg_vol: <input type="number" step="0.01" name="bg_vol_form" value="0.25"></div>
          <div>duck_threshold: <input type="number" step="0.001" name="duck_threshold_form" value="0.02"></div>
          <div>duck_ratio: <input type="number" step="1" name="duck_ratio_form" value="12"></div>
          <div>xfade: <input type="number" step="0.1" name="xfade_form" value="1.0"></div>
        </fieldset>
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
    bg_vol_form: Optional[float] = Form(0.25),
    duck_threshold_form: Optional[float] = Form(0.02),
    duck_ratio_form: Optional[float] = Form(12.0),
    xfade_form: Optional[float] = Form(1.0),
):
    if not ELEVEN_KEY:
        raise HTTPException(500, detail="Missing ELEVENLABS_API_KEY environment variable")

    # ElevenLabs v1 TTS stream → MP3 bytes
    tts_url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
    headers = {
        "xi-api-key": ELEVEN_KEY,
        "accept": "audio/mpeg",
        "Content-Type": "application/json",
    }
    payload = {
        "text": script,
        "model_id": "eleven_turbo_v2",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.8},
        "output_format": "mp3_44100_128"
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(tts_url, headers=headers, json=payload)
        print(">>> TTS status:", r.status_code, "bytes:", len(r.content))
        if r.status_code != 200 or not r.content or len(r.content) < 500:
            preview = r.text[:200] if r.text else ""
            raise HTTPException(500, detail=f"TTS failed or returned no audio. Status={r.status_code} {preview}")

       # Wrap TTS bytes as a REAL UploadFile for reuse with /api/mix
narr = StarletteUploadFile(
    filename="rtm_narration.mp3",
    file=BytesIO(r.content),
    content_type="audio/mpeg"
)


    # forward to mixer with the user-selected knobs
    return await mix(
        intro=intro, narr=narr, outro=outro,
        bg_vol=bg_vol_form, duck_threshold=duck_threshold_form, duck_ratio=duck_ratio_form,
        xfade=xfade_form, lufs=-16.0, tp=-1.5, lra=11.0
    )
