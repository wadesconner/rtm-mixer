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
