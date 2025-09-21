@app.post("/api/mix")
async def mix(
    intro: UploadFile = File(...),
    narr: UploadFile = File(...),
    outro: UploadFile = File(...),
    bg_vol: Optional[float] = None,
    duck_threshold: Optional[float] = None,
    duck_ratio: Optional[float] = None,
    xfade: Optional[float] = None,
    lufs: Optional[float] = None,
    tp: Optional[float] = None,
    lra: Optional[float] = None,
    voice_only: Optional[int] = 0,
    step1_only: Optional[int] = 0,   # NEW: expose as query param (0/1)
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

    bg_vol = bg_vol if bg_vol is not None else (bg_vol_form if bg_vol_form is not None else 0.25)
    duck_threshold = duck_threshold if duck_threshold is not None else (duck_threshold_form if duck_threshold_form is not None else 0.02)
    duck_ratio = duck_ratio if duck_ratio is not None else (duck_ratio_form if duck_ratio_form is not None else 12.0)
    xfade = xfade if xfade is not None else (xfade_form if xfade_form is not None else 1.0)
    lufs = lufs if lufs is not None else (lufs_form if lufs_form is not None else -16.0)
    tp = tp if tp is not None else (tp_form if tp_form is not None else -1.5)
    lra = lra if lra is not None else (lra_form if lra_form is not None else 11.0)

    # Coerce debug flags
    def _to_int(x): 
        try: return int(x) if x is not None else 0
        except: return 0
    voice_only_val = _to_int(voice_only)
    step1_only_val = _to_int(step1_only)

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

        # Build CLI
        voice_only_flag = "--voice_only" if voice_only_val == 1 else ""
        step1_only_flag = "--step1_only" if step1_only_val == 1 else ""
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
          --lra {lra} \
          {voice_only_flag} {step1_only_flag}
        """.strip()

        rc = _run(cmd)
        if rc != 0 or not out_path.exists():
            raise HTTPException(500, detail="Mixing failed")

        return FileResponse(str(out_path), media_type="audio/mpeg", filename="rtm_final_mix.mp3")
    finally:
        pass
