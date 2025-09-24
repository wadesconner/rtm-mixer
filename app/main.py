# main.py
import os
from pathlib import Path
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

# mixer helper
from rtm.audio_mix import mix_with_bed

# ──────────────────────────────────────────────────────────────────────────────
# basic app + config
# ──────────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Radio Time Machine", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten later if needed
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ASSETS_DIR = Path(os.getenv("RTM_ASSETS_DIR", "assets")).resolve()
TMP_DIR = Path(os.getenv("RTM_TMP_DIR", "/tmp/rtm")).resolve()
OUTPUT_DIR = TMP_DIR / "out"
DEFAULT_BED = ASSETS_DIR / "rtm_intro_bg.mp3"  # change if your default is different

for p in [ASSETS_DIR, TMP_DIR, OUTPUT_DIR]:
    p.mkdir(parents=True, exist_ok=True)

# ──────────────────────────────────────────────────────────────────────────────
# models
# ──────────────────────────────────────────────────────────────────────────────
class MixRequest(BaseModel):
    narration_path: str = Field(..., description="Full path to narration MP3")
    bed_path: Optional[str] = Field(None, description="Full path to bed MP3; defaults to assets/rtm_intro_bg.mp3")
    out_path: str = Field(..., description="Full output path for final MP3")
    bed_gain_db: Optional[float] = None
    threshold_db: Optional[float] = None
    ratio: Optional[float] = None
    attack_ms: Optional[int] = None
    release_ms: Optional[int] = None
    fade_ms: Optional[int] = None
    song_clip: Optional[str] = None
    song_start: Optional[float] = None
    song_gain_db: Optional[float] = None

class PreviewRequest(BaseModel):
    name: str
    date: str  # "YYYY-MM-DD" or free-form (we'll show it as-is)
    location: str
    voice: Optional[str] = "Clyde"  # just flavor text
    extra_notes: Optional[str] = None

class GenerateRequest(BaseModel):
    # inputs for a full run; you can extend with whatever your TTS expects
    preview: PreviewRequest
    # if you already have a narration mp3 (from your TTS), pass it in directly
    narration_path: Optional[str] = None
    # optional audio assets
    bed_path: Optional[str] = None
    song_clip: Optional[str] = None
    song_start: Optional[float] = None
    # output filename (we’ll place it into OUTPUT_DIR)
    output_filename: str = "rtm_final.mp3"
    # mixer tuning (optional)
    bed_gain_db: Optional[float] = None
    threshold_db: Optional[float] = None
    ratio: Optional[float] = None
    attack_ms: Optional[int] = None
    release_ms: Optional[int] = None
    fade_ms: Optional[int] = None
    song_gain_db: Optional[float] = None

# ──────────────────────────────────────────────────────────────────────────────
# utilities
# ──────────────────────────────────────────────────────────────────────────────
def ensure_file(path: Path, label: str):
    if not path.exists():
        raise HTTPException(status_code=400, detail=f"{label} not found: {path}")

def build_script(p: PreviewRequest) -> str:
    """Very simple template with a tiny bit of variation hooks."""
    try:
        date_obj = datetime.fromisoformat(p.date)
        date_spoken = date_obj.strftime("%B %-d, %Y")
    except Exception:
        # if it's not ISO, just echo back
        date_spoken = p.date

    voice = p.voice or "Clyde"
    lines = [
        f"Good evening folks! This is {voice} with your Radio Time Machine.",
        f"On this day — {date_spoken} — we’re dialing in from {p.location}.",
        f"We’ve got a little time-capsule just for {p.name}.",
        "We’ll spin a few headlines, share a quick weather snapshot,",
        "and drop in a song clip from the era before we sign off.",
    ]
    if p.extra_notes:
        lines.append(f"Producer notes: {p.extra_notes}")

    lines += [
        "Stick around — and as always — enjoy the ride.",
        "This is Radio Time Machine."
    ]
    return "\n".join(lines)

def to_abs(path_str: Optional[str]) -> Optional[Path]:
    if not path_str:
        return None
    return Path(path_str).resolve()

# ──────────────────────────────────────────────────────────────────────────────
# routes
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"

@app.post("/preview-script")
def preview_script(req: PreviewRequest):
    """
    Return a text-only preview based on inputs (no audio generation).
    """
    script = build_script(req)
    return {"ok": True, "script": script}

@app.post("/api/mix")
def api_mix(req: MixRequest):
    """
    Mix narration + bed (with ducking), optional song clip that mutes bed.
    """
    narration = to_abs(req.narration_path)
    bed = to_abs(req.bed_path) if req.bed_path else DEFAULT_BED
    out = to_abs(req.out_path)

    if not narration or not out:
        raise HTTPException(400, "narration_path and out_path are required")

    ensure_file(narration, "Narration")
    ensure_file(bed, "Bed")

    kwargs = dict(
        narration_path=str(narration),
        bed_path=str(bed),
        out_path=str(out),
        bed_gain_db=req.bed_gain_db if req.bed_gain_db is not None else -16.0,
        threshold_db=req.threshold_db if req.threshold_db is not None else -30.0,
        ratio=req.ratio if req.ratio is not None else 8.0,
        attack_ms=req.attack_ms if req.attack_ms is not None else 5,
        release_ms=req.release_ms if req.release_ms is not None else 300,
        fade_ms=req.fade_ms if req.fade_ms is not None else 600,
        song_clip=req.song_clip,
        song_start=req.song_start,
        song_gain_db=req.song_gain_db if req.song_gain_db is not None else -3.0,
    )

    try:
        mix_with_bed(**kwargs)
        return {"ok": True, "output": str(out)}
    except Exception as e:
        raise HTTPException(500, f"Mix failed: {e}")

@app.post("/generate")
def generate(req: GenerateRequest):
    """
    High-level wrapper:
      1) build script (returned to caller so front-end can show it)
      2) (optional) run your TTS to produce narration.mp3 (NOT implemented here)
      3) mix with bed + optional song
    For now:
      - If narration_path is provided, we proceed to mixing.
      - Otherwise we return the script and ask caller to TTS, then call /api/mix.
    """
    script_text = build_script(req.preview)

    # 1) If you have your TTS wired up, this is where you'd call it.
    # Pseudocode example:
    # narration_path = run_tts(script_text, voice=req.preview.voice, out_dir=TMP_DIR)
    # For now we depend on caller to provide narration_path.
    if not req.narration_path:
        return JSONResponse(
            {
                "ok": True,
                "step": "script_ready",
                "script": script_text,
                "message": "No narration_path provided. Generate TTS and call /api/mix next.",
            }
        )

    # 2) Mix
    narration = to_abs(req.narration_path)
    bed = to_abs(req.bed_path) if req.bed_path else DEFAULT_BED
    out = OUTPUT_DIR / req.output_filename

    ensure_file(narration, "Narration")
    ensure_file(bed, "Bed")

    kwargs = dict(
        narration_path=str(narration),
        bed_path=str(bed),
        out_path=str(out),
        bed_gain_db=req.bed_gain_db if req.bed_gain_db is not None else -16.0,
        threshold_db=req.threshold_db if req.threshold_db is not None else -30.0,
        ratio=req.ratio if req.ratio is not None else 8.0,
        attack_ms=req.attack_ms if req.attack_ms is not None else 5,
        release_ms=req.release_ms if req.release_ms is not None else 300,
        fade_ms=req.fade_ms if req.fade_ms is not None else 600,
        song_clip=req.song_clip,
        song_start=req.song_start,
        song_gain_db=req.song_gain_db if req.song_gain_db is not None else -3.0,
    )

    try:
        mix_with_bed(**kwargs)
        return {
            "ok": True,
            "step": "mix_complete",
            "script": script_text,
            "output": str(out),
            "download_url": f"/download/{out.name}",
        }
    except Exception as e:
        raise HTTPException(500, f"Generate failed: {e}")

@app.get("/download/{filename}")
def download(filename: str):
    """
    Serve generated files from OUTPUT_DIR (read-only).
    """
    target = (OUTPUT_DIR / filename).resolve()
    # security: ensure path stays inside OUTPUT_DIR
    if OUTPUT_DIR not in target.parents:
        raise HTTPException(400, "Invalid path.")
    if not target.exists():
        raise HTTPException(404, "File not found.")
    return FileResponse(str(target), media_type="audio/mpeg", filename=target.name)
