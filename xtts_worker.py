"""
XTTS worker - clones the voice from a short sample and speaks each phrase.

This file is started by app.py (you do not run it yourself). It reads a job file
(JSON), loads the free XTTS v2 model once, and writes one .wav per phrase.

Talks to app.py through simple lines on stdout:
    LOG <text>          status message
    OK <id>             phrase <id> finished
    FAIL <id> <reason>  phrase <id> could not be voiced
    PROGRESS <n> <N>    n of N phrases processed
"""

import json
import os
import sys
import wave

os.environ.setdefault("COQUI_TOS_AGREED", "1")  # accept the model licence prompt


def say(*parts):
    print(*parts, flush=True)


def save_wav(path, wav, rate=24000):
    import numpy as np

    if hasattr(wav, "detach"):  # torch tensor
        wav = wav.detach().cpu().numpy()
    a = np.clip(np.asarray(wav, dtype=np.float32).squeeze(), -1.0, 1.0)
    pcm = (a * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def main(job_path):
    with open(job_path, encoding="utf-8") as f:
        job = json.load(f)

    import torch
    from TTS.api import TTS

    device = "cuda" if torch.cuda.is_available() else "cpu"
    say(f"LOG Loading the XTTS model on {device} "
        f"(first run downloads about 1.8 GB, please wait)...")
    tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(device)
    say("LOG Model ready. Cloning your voice sample...")

    lang, ref, out_dir = job["language"], job["reference"], job["out_dir"]
    items = job["items"]

    # Fast path: compute the voice fingerprint once and reuse it for every phrase.
    model, latents = None, None
    try:
        model = tts.synthesizer.tts_model
        latents = model.get_conditioning_latents(audio_path=[ref])
    except Exception as e:  # fall back to the simple (slower) API
        say(f"LOG (using the simple XTTS path: {type(e).__name__})")
        model = None

    total = len(items)
    ok_count = fail_count = 0
    for n, item in enumerate(items, 1):
        out = os.path.join(out_dir, f"x{item['id']:05}.wav")
        try:
            if model is not None:
                gpt_cond_latent, speaker_embedding = latents
                res = model.inference(
                    item["text"], lang, gpt_cond_latent, speaker_embedding,
                    temperature=0.7, enable_text_splitting=True)
                save_wav(out, res["wav"])
            else:
                tts.tts_to_file(text=item["text"], speaker_wav=[ref],
                                language=lang, file_path=out)
            say(f"OK {item['id']}")
            ok_count += 1
        except Exception as e:
            say(f"FAIL {item['id']} {type(e).__name__}: {e}")
            fail_count += 1
            if ok_count == 0 and fail_count >= 3:
                # nothing works at all: stop now instead of failing every phrase
                say("ABORT the first phrases all failed, so the voice engine is broken")
                sys.exit(3)
        say(f"PROGRESS {n} {total}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: xtts_worker.py job.json")
        sys.exit(2)
    main(sys.argv[1])
