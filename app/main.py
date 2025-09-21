# main.py — RTM Mixer API (health + robust coercion + debug endpoints + new knobs)

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

app = FastAPI(title="RTM Mixer API")

@app.get("/", response_class=PlainTextResponse)
def root_get(): return "ok"

@app.head("/", response_class=PlainTextResponse)
def root_head(): return Response(content=b"", media_type="text/plain")

@app.get("/healthz", response_class=PlainTextResponse)
def healthz_get(): return "ok"

@app.head("/healthz", response_class=PlainTextResponse)
def healthz_head(): return Response(content=b"", media_type="text/plain")

PIPELINE_DIR = Path(__file__).resolve().parent.parent / "rtm_audio_pipeline"
MIXER = PIPELINE_DIR / "rtm_mixer.py"

def _run(cmd: str) -> int:
    print(">>>", cmd)
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if p.stdout: print(">>> [stdout]\n", p.stdout)
    if p.stderr: print(">>> [stderr]\n", p.stderr)
    return p.returncode

def _ffprobe(path: Path):
    cmd = (f'ffprobe -hide_banner -v error '
           f'-show_entries stream=channels,sample_rate '
           f'-show_entries format=duration '
           f'-of json {shlex.quote(str(path))}')
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    print(f">>> ffprobe {path.name} rc={p.returncode}")
    if p.stdout:
        try: print(">>> [ffprobe]", json.dumps(json.loads(p.stdout), indent=2))
        except Exception: print(">>> [ffprobe raw]\n", p.stdout[:1000])
    if p.stderr: print(">>> [ffprobe stderr]\n", p.stderr)

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""): h.update(chunk)
    return h.hexdigest()

def _stat(path: Path) -> dict:
    try:
        st = path.stat()
        return {"exists": True, "size": st.st_size, "mtime": datetime.datetime.fromtimestamp(st.st_mtime).isoformat()}
    except FileNotFoundError:
        return {"exists": False}

@app.get("/api/mix")
async def api_mix_get():
    return JSONResponse({
        "status": "ok",
        "how_to_use": "POST multipart/form-data to /api/mix with 3 files and optional knobs.",
        "required_files": {
            "intro": "mp3 (rtm_intro_bg.mp3)",
            "narr": "mp3 (rtm_narration.mp3)",
            "outro": "mp3 (rtm_outro_bg.mp3)"
        },
        "optional_knobs": [
            "bg_vol (float, default 0.25)",
            "voice_gain (float, default 3.0)",
            "bg_weight (float, default 0.35)",
            "voice_weight (float, default 1.0)",
            "narr_delay (sec, default 0.6)",
            "outro_gain (float, default 0.9)",
            "duck_threshold (kept for parity; unused in simplified graph)",
            "duck_ratio (kept for parity; unused in simplified graph)",
            "xfade (sec, default 1.2)",
            "lufs (default -16), tp (-1.5), lra (11.0)",
            "voice_only (0/1), step1_only (0/1)"
        ],
    })

@app.post("/api/mix")
async def mix(
    intro: UploadFile = File(...),
    narr: UploadFile = File(...),
    outro: UploadFile = File(...),

    # query params (also supported via form with *_form names)
    bg_vol: Optional[float] = None,
    voice_gain: Optional[float] = None,
    bg_weight: Optional[float] = None,
    voice_weight: Optional[float] = None,
    narr_delay: Optional[float] = None,
    outro_gain: Optional[float] = None,

    duck_threshold: Optional[float] = None,  # accepted, unused by simplified mixer
    duck_ratio: Optional[float] = None,      # accepted, unused by simplified mixer
    xfade: Optional[float] = None,
    lufs: Optional[float] = None,
    tp: Optional[float] = None,
    lra: Optional[float] = None,
    voice_only: Optional[int] = 0,
    step1_only: Optional[int] = 0,

    # form fallbacks
    bg_vol_form: Optional[float] = Form(None),
    voice_gain_form: Optional[float] = Form(None),
    bg_weight_form: Optional[float] = Form(None),
    voice_weight_form: Optional[float] = Form(None),
    narr_delay_form: Optional[float] = Form(None),
    outro_gain_form: Optional[float] = Form(None),

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

    def _num(v, default):
        try: return float(v)
        except Exception:
            try: return float(default)
            except Exception: return 0.0

    def _flag(v, fb):
        x = v if v is not None else fb
        try: x = int(x)
        except Exception: x = 0
        return 1 if x == 1 else 0

    # prefer query; else form; else default, then cast
    bg_vol       = _num(bg_vol if bg_vol is not None else (bg_vol_form if bg_vol_form is not None else 0.25), 0.25)
    voice_gain   = _num(voice_gain if voice_gain is not None else (voice_gain_form if voice_gain_form is not None else 3.0), 3.0)
    bg_weight    = _num(bg_weight if bg_weight is not None else (bg_weight_form if bg_weight_form is not None else 0.35), 0.35)
    voice_weight = _num(voice_weight if voice_weight is not None else (voice_weight_form if voice_weight_form is not None else 1.0), 1.0)
    narr_delay   = _num(narr_delay if narr_delay is not None else (narr_delay_form if narr_delay_form is not None else 0.6), 0.6)
    outro_gain   = _num(outro_gain if outro_gain is not None else (outro_gain_form if outro_gain_form is not None else 0.9), 0.9)

    duck_threshold = _num(duck_threshold if duck_threshold is not None else (duck_threshold_form if duck_threshold_form is not None else 0.02), 0.02)
    duck_ratio     = _num(duck_ratio if duck_ratio is not None else (duck_ratio_form if duck_ratio_form is not None else 12.0), 12.0)
    xfade          = _num(xfade if xfade is not None else (xfade_form if xfade_form is not None else 1.2), 1.2)
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

        intro_bytes = await intro.read(); intro_path.write_bytes(intro_bytes)
        narr_bytes  = await narr.read()
        if not narr_bytes or len(narr_bytes) < 500:
            raise HTTPException(500, detail="Narration audio is empty or too short")
        narr_path.write_bytes(narr_bytes)
        outro_bytes = await outro.read(); outro_path.write_bytes(outro_bytes)

        print(f"[mix] wrote intro {intro_path} bytes={len(intro_bytes)}")
        print(f"[mix] wrote narr  {narr_path} bytes={len(narr_bytes)}")
        print(f"[mix] wrote outro {outro_path} bytes={len(outro_bytes)}")

        _ffprobe(intro_path); _ffprobe(narr_path); _ffprobe(outro_path)

        flags = []
        if voice_only_val == 1: flags.append("--voice_only")
        if step1_only_val == 1: flags.append("--step1_only")
        extra = " ".join(flags)

        cmd = f"""
        python {shlex.quote(str(MIXER))} \
          --intro {shlex.quote(str(intro_path))} \
          --narr {shlex.quote(str(narr_path))} \
          --outro {shlex.quote(str(outro_path))} \
          --out {shlex.quote(str(out_path))} \
          --bg_vol {bg_vol:.6f} \
          --voice_gain {voice_gain:.6f} \
          --bg_weight {bg_weight:.6f} \
          --voice_weight {voice_weight:.6f} \
          --narr_delay {narr_delay:.6f} \
          --outro_gain {outro_gain:.6f} \
          --duck_threshold {duck_threshold:.6f} \
          --duck_ratio {duck_ratio:.6f} \
          --xfade {xfade:.6f} \
          --lufs {lufs:.6f} \
          --tp {tp:.6f} \
          --lra {lra:.6f} \
          {extra}
        """.strip()

        rc = _run(cmd)
        if rc != 0 or not out_path.exists():
            raise HTTPException(500, detail="Mixing failed")

        return FileResponse(str(out_path), media_type="audio/mpeg", filename="rtm_final_mix.mp3")
    finally:
        pass

# -------------------------- Hardcoded debug endpoints --------------------------
@app.post("/api/mix/voice")
async def mix_voice_only(intro: UploadFile = File(...), narr: UploadFile = File(...), outro: UploadFile = File(...)):
    return await mix(intro=intro, narr=narr, outro=outro, voice_only=1, step1_only=0)

@app.post("/api/mix/step1")
async def mix_step1_only(
    intro: UploadFile = File(...),
    narr: UploadFile = File(...),
    outro: UploadFile = File(...),
    bg_vol_form: Optional[float] = Form(0.25),
    voice_gain_form: Optional[float] = Form(3.0),
    bg_weight_form: Optional[float] = Form(0.35),
    voice_weight_form: Optional[float] = Form(1.0),
    narr_delay_form: Optional[float] = Form(0.6),
):
    return await mix(
        intro=intro, narr=narr, outro=outro,
        bg_vol=bg_vol_form, voice_gain=voice_gain_form,
        bg_weight=bg_weight_form, voice_weight=voice_weight_form,
        narr_delay=narr_delay_form,
        step1_only=1, voice_only=0
    )

# -------------------------- Simple upload form --------------------------
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
          <div>voice_gain: <input type="number" step="0.1" name="voice_gain_form" value="3.0"></div>
          <div>bg_weight: <input type="number" step="0.01" name="bg_weight_form" value="0.35"></div>
          <div>voice_weight: <input type="number" step="0.01" name="voice_weight_form" value="1.0"></div>
          <div>narr_delay (sec): <input type="number" step="0.1" name="narr_delay_form" value="0.6"></div>
          <div>outro_gain: <input type="number" step="0.1" name="outro_gain_form" value="0.9"></div>
          <div>xfade (sec): <input type="number" step="0.1" name="xfade_form" value="1.2"></div>
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
    voice_gain_form: Optional[float] = Form(3.0),
    bg_weight_form: Optional[float] = Form(0.35),
    voice_weight_form: Optional[float] = Form(1.0),
    narr_delay_form: Optional[float] = Form(0.6),
    outro_gain_form: Optional[float] = Form(0.9),
    xfade_form: Optional[float] = Form(1.2),
):
    return await mix(
        intro=intro, narr=narr, outro=outro,
        bg_vol=bg_vol_form, voice_gain=voice_gain_form,
        bg_weight=bg_weight_form, voice_weight=voice_weight_form,
        narr_delay=narr_delay_form, outro_gain=outro_gain_form,
        xfade=xfade_form, lufs=-16.0, tp=-1.5, lra=11.0,
        voice_only=0, step1_only=0,
    )

# -------------------------- ElevenLabs generate --------------------------
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
          <div>voice_gain: <input type="number" step="0.1" name="voice_gain_form" value="3.0"></div>
          <div>bg_weight: <input type="number" step="0.01" name="bg_weight_form" value="0.35"></div>
          <div>voice_weight: <input type="number" step="0.01" name="voice_weight_form" value="1.0"></div>
          <div>narr_delay (sec): <input type="number" step="0.1" name="narr_delay_form" value="0.6"></div>
          <div>outro_gain: <input type="number" step="0.1" name="outro_gain_form" value="0.9"></div>
          <div>xfade: <input type="number" step="0.1" name="xfade_form" value="1.2"></div>
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
    voice_gain_form: Optional[float] = Form(3.0),
    bg_weight_form: Optional[float] = Form(0.35),
    voice_weight_form: Optional[float] = Form(1.0),
    narr_delay_form: Optional[float] = Form(0.6),
    outro_gain_form: Optional[float] = Form(0.9),
    xfade_form: Optional[float] = Form(1.2),
):
    if not ELEVEN_KEY:
        raise HTTPException(500, detail="Missing ELEVENLABS_API_KEY environment variable")

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
        bg_vol=bg_vol_form, voice_gain=voice_gain_form,
        bg_weight=bg_weight_form, voice_weight=voice_weight_form,
        narr_delay=narr_delay_form, outro_gain=outro_gain_form,
        xfade=xfade_form, lufs=-16.0, tp=-1.5, lra=11.0,
        voice_only=0, step1_only=0,
    )
