# main.py — RTM Mixer API (stable health + diagnostics + robust coercion + debug endpoints)

import os
import shlex
import subprocess
import tempfile
import uuid
import json
import hashlib
import datetime
from io import BytesIO
from pathlib import Path
from typing import Optional

import httpx
from starlette.datastructures import UploadFile as StarletteUploadFile
from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response

# -------------------------- App --------------------------
app = FastAPI(title="RTM Mixer API")

# -------------------------- Health Endpoints --------------------------
@app.get("/", response_class=PlainTextResponse)
def root_get():
    return "ok"

@app.head("/", response_class=PlainTextResponse)
def root_head():
    return Response(content=b"", media_type="text/plain")

@app.get("/healthz", response_class=PlainTextResponse)
def healthz_get():
    return "ok"

@app.head("/healthz", response_class=PlainTextResponse)
def healthz_head():
    return Response(content=b"", media_type="text/plain")

# -------------------------- Paths --------------------------
PIPELINE_DIR = Path(__file__).resolve().parent.parent / "rtm_audio_pipeline"
MIXER = PIPELINE_DIR / "rtm_mixer.py"

# -------------------------- Shell + debug helpers --------------------------
def _run(cmd: str) -> int:
    print(">>>", cmd)
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if p.stdout:
        print(">>> [stdout]\n", p.stdout)
    if p.stderr:
        print(">>> [stderr]\n", p.stderr)
    return p.returncode

def _ffprobe(path: Path):
    cmd = (
        f'ffprobe -hide_banner -v error '
        f'-show_entries stream=channels,sample_rate '
        f'-show_entries format=duration '
        f'-of json {shlex.quote(str(path))}'
    )
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    print(f">>> ffprobe {path.name} rc={p.returncode}")
    if p.stdout:
        try:
            j = json.loads(p.stdout)
            print(">>> [ffprobe]", json.dumps(j, indent=2))
        except Exception:
            print(">>> [ffprobe raw]\n", p.stdout[:1000])
    if p.stderr:
        print(">>> [ffprobe stderr]\n", p.stderr)

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def _stat(path: Path) -> dict:
    try:
        st = path.stat()
        return {"exists": True, "size": st.st_size, "mtime": datetime.datetime.fromtimestamp(st.st_mtime).isoformat()}
    except FileNotFoundError:
        return {"exists": False}

# -------------------------- /api/mix (GET helper) --------------------------
@app.get("/api/mix")
async def api_mix_get():
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
            "lra (float, default 11.0)",
            "voice_only (0/1 diagnostic, query or form)",
            "step1_only (0/1 diagnostic, query or form)"
        ],
        "example_curl": [
            "curl -X POST https://YOUR_DOMAIN/api/mix \\",
            "  -F intro=@rtm_intro_bg.mp3 \\",
            "  -F narr=@rtm_narration.mp3 \\",
            "  -F outro=@rtm_outro_bg.mp3 \\",
            "  -F bg_vol=0.25 \\",
            "  --output mix.mp3"
        ],
    })

# -------------------------- /api/mix (POST real mixer) --------------------------
@app.post("/api/mix")
async def mix(
    intro: UploadFile = File(...),      # rtm_intro_bg.mp3 (bed)
    narr: UploadFile = File(...),       # rtm_narration.mp3 (voice)
    outro: UploadFile = File(...),      # rtm_outro_bg.mp3
    # query params (optional)
    bg_vol: Optional[float] = None,
    duck_threshold: Optional[float] = None,
    duck_ratio: Optional[float] = None,
    xfade: Optional[float] = None,
    lufs: Optional[float] = None,
    tp: Optional[float] = None,
    lra: Optional[float] = None,
    voice_only: Optional[int] = 0,
    step1_only: Optional[int] = 0,
    # form fallbacks
    bg_vol_form: Optional[float] = Form(None),
    duck_threshold_form: Optional[float] = Form(None),
    duck_ratio_form: Optional[float] = Form(None),
    xfade_form: Optional[float] = Form(None),
    lufs_form: Optional[float] = Form(None),
    tp_form: Optional[float] = Form(None),
    lra_form: Optional[float] = Form(None),
    voice_only_form: Optional[int] = Form(None),
    step1_only_form: Optional[int] = Form(None),
):
    if not MIXER.exists():
        raise HTTPException(500, detail=f"Mixer script not found at {MIXER}")

    # ---------- robust coercion helpers ----------
    def _num(v, default):
        # Force to float even if v is weird/annotated; fall back to default
        try:
            return float(v)
        except Exception:
            try:
                return float(default)
            except Exception:
                return 0.0

    def _flag(v, fb):
        # Prefer query v; else form fb; coerce to 0/1
        x = v if v is not None else fb
        try:
            x = int(x)
        except Exception:
            x = 0
        return 1 if x == 1 else 0

    # prefer query; else form; else default, then HARD-CAST to float
    bg_vol         = _num(bg_vol if bg_vol is not None else (bg_vol_form if bg_vol_form is not None else 0.25), 0.25)
    duck_threshold = _num(duck_threshold if duck_threshold is not None else (duck_threshold_form if duck_threshold_form is not None else 0.02), 0.02)
    duck_ratio     = _num(duck_ratio if duck_ratio is not None else (duck_ratio_form if duck_ratio_form is not None else 12.0), 12.0)
    xfade          = _num(xfade if xfade is not None else (xfade_form if xfade_form is not None else 1.0), 1.0)
    lufs           = _num(lufs if lufs is not None else (lufs_form if lufs_form is not None else -16.0), -16.0)
    tp             = _num(tp if tp is not None else (tp_form if tp_form is not None else -1.5), -1.5)
    lra            = _num(lra if lra is not None else (lra_form if lra_form is not None else 11.0), 11.0)

    voice_only_val = _flag(voice_only, voice_only_form)
    step1_only_val = _flag(step1_only, step1_only_form)

    workdir = Path(tempfile.mkdtemp(prefix="rtm_mix_"))
    try:
        intro_path  = workdir / "rtm_intro_bg.mp3"
        narr_path   = workdir / "rtm_narration.mp3"
        outro_path  = workdir / "rtm_outro_bg.mp3"
        out_path    = workdir / f"rtm_final_{uuid.uuid4().hex}.mp3"

        intro_bytes = await intro.read()
        intro_path.write_bytes(intro_bytes)
        print(f"[mix] wrote intro {intro_path} bytes={len(intro_bytes)}")

        narr_bytes = await narr.read()
        if not narr_bytes or len(narr_bytes) < 500:
            raise HTTPException(500, detail="Narration audio is empty or too short")
        narr_path.write_bytes(narr_bytes)
        print(f"[mix] wrote narr  {narr_path} bytes={len(narr_bytes)}")

        outro_bytes = await outro.read()
        outro_path.write_bytes(outro_bytes)
        print(f"[mix] wrote outro {outro_path} bytes={len(outro_bytes)}")

        _ffprobe(intro_path); _ffprobe(narr_path); _ffprobe(outro_path)

        print(f"[mix] Using MIXER at {MIXER}")
        print(f"[mix] MIXER stat: {_stat(MIXER)}")
        try:
            print(f"[mix] MIXER sha256: {_sha256(MIXER)}")
        except Exception as e:
            print(f"[mix] MIXER sha256: <error: {e}>")

        # Build CLI with sanitized floats/flags
        voice_only_flag = "--voice_only" if voice_only_val == 1 else ""
        step1_only_flag = "--step1_only" if step1_only_val == 1 else ""

        cmd = f"""
        python {shlex.quote(str(MIXER))} \
          --intro {shlex.quote(str(intro_path))} \
          --narr {shlex.quote(str(narr_path))} \
          --outro {shlex.quote(str(outro_path))} \
          --out {shlex.quote(str(out_path))} \
          --bg_vol {bg_vol:.6f} \
          --duck_threshold {duck_threshold:.6f} \
          --duck_ratio {duck_ratio:.6f} \
          --xfade {xfade:.6f} \
          --lufs {lufs:.6f} \
          --tp {tp:.6f} \
          --lra {lra:.6f} \
          {voice_only_flag} {step1_only_flag}
        """.strip()

        rc = _run(cmd)
        if rc != 0 or not out_path.exists():
            raise HTTPException(500, detail="Mixing failed")

        return FileResponse(str(out_path), media_type="audio/mpeg", filename="rtm_final_mix.mp3")
    finally:
        # keep temp dir for debugging
        pass

# -------------------------- Hardcoded debug endpoints --------------------------
@app.post("/api/mix/voice")
async def mix_voice_only(
    intro: UploadFile = File(...),
    narr: UploadFile = File(...),
    outro: UploadFile = File(...),
):
    # Always force voice-only
    return await mix(intro=intro, narr=narr, outro=outro, voice_only=1, step1_only=0)

@app.post("/api/mix/step1")
async def mix_step1_only(
    intro: UploadFile = File(...),
    narr: UploadFile = File(...),
    outro: UploadFile = File(...),
    bg_vol_form: Optional[float] = Form(0.25),
    duck_threshold_form: Optional[float] = Form(0.02),
    duck_ratio_form: Optional[float] = Form(12.0),
):
    # Always force Step-1 only (bed+voice), with tunable knobs
    return await mix(
        intro=intro, narr=narr, outro=outro,
        bg_vol=bg_vol_form, duck_threshold=duck_threshold_form, duck_ratio=duck_ratio_form,
        step1_only=1, voice_only=0
    )

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
          <div>voice_only: <input type="number" step="1" name="voice_only_form" value="0" min="0" max="1"></div>
          <div>step1_only: <input type="number" step="1" name="step1_only_form" value="0" min="0" max="1"></div>
        </fieldset>
        <div style="margin-top:12px"><button type="submit">Mix</button></div>
      </form>

      <h3 style="margin-top:28px">Debug: Inspect Upload</h3>
      <form action="/debug/inspect_upload" method="post" enctype="multipart/form-data">
        <div>Intro (mp3): <input type="file" name="intro" accept="audio/mpeg" required></div>
        <div>Narration (mp3): <input type="file" name="narr" accept="audio/mpeg" required></div>
        <div>Outro (mp3): <input type="file" name="outro" accept="audio/mpeg" required></div>
        <div style="margin-top:12px"><button type="submit">Inspect</button></div>
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
    voice_only_form: Optional[int] = Form(0),
    step1_only_form: Optional[int] = Form(0),
):
    return await mix(
        intro=intro, narr=narr, outro=outro,
        bg_vol=bg_vol_form, duck_threshold=duck_threshold_form, duck_ratio=duck_ratio_form,
        xfade=xfade_form, lufs=-16.0, tp=-1.5, lra=11.0,
        voice_only=voice_only_form, step1_only=step1_only_form,
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
          <div>voice_only: <input type="number" step="1" name="voice_only_form" value="0" min="0" max="1"></div>
          <div>step1_only: <input type="number" step="1" name="step1_only_form" value="0" min="0" max="1"></div>
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
    voice_only_form: Optional[int] = Form(0),
    step1_only_form: Optional[int] = Form(0),
):
    if not ELEVEN_KEY:
        raise HTTPException(500, detail="Missing ELEVENLABS_API_KEY environment variable")

    # ElevenLabs v1 TTS stream → MP3 bytes
    tts_url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
    headers = {"xi-api-key": ELEVEN_KEY, "accept": "audio/mpeg", "Content-Type": "application/json"}
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

        narr = StarletteUploadFile(filename="rtm_narration.mp3", file=BytesIO(r.content), content_type="audio/mpeg")

    return await mix(
        intro=intro, narr=narr, outro=outro,
        bg_vol=bg_vol_form, duck_threshold=duck_threshold_form, duck_ratio=duck_ratio_form,
        xfade=xfade_form, lufs=-16.0, tp=-1.5, lra=11.0,
        voice_only=voice_only_form, step1_only=step1_only_form,
    )

# -------------------------- Debug endpoints --------------------------
@app.get("/debug/mixer")
def debug_mixer():
    if not MIXER.exists():
        raise HTTPException(404, detail=f"Mixer not found at {MIXER}")
    txt = (MIXER.read_text(errors="ignore")).splitlines()
    head = "\n".join(txt[:140])
    return {"path": str(MIXER), "stat": _stat(MIXER), "sha256": _sha256(MIXER), "head": head}

@app.post("/debug/inspect_upload")
async def inspect_upload(intro: UploadFile = File(...), narr: UploadFile = File(...), outro: UploadFile = File(...)):
    intro_b = await intro.read()
    narr_b = await narr.read()
    outro_b = await outro.read()
    return {
        "intro_bytes": len(intro_b),
        "narr_bytes": len(narr_b),
        "outro_bytes": len(outro_b),
        "intro_name": intro.filename,
        "narr_name": narr.filename,
        "outro_name": outro.filename,
        "note": "These are the raw sizes that /api/mix would receive."
    }
