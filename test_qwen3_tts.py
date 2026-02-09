#!/usr/bin/env python3
"""
Benchmark Qwen3-TTS 1.7B (4-bit vs 8-bit) via mlx-audio server.

Restarts the server between models for accurate memory measurement.
Also generates voice-cloned samples using data/her.mp3.

Usage:  .venv/bin/python test_qwen3_tts.py
"""

import io
import os
import signal
import subprocess
import sys
import time
import json
import urllib.request
import wave

SERVER = "http://localhost:8880"
VENV_PYTHON = os.path.join(os.path.dirname(__file__), ".venv", "bin", "python")
REF_AUDIO = os.path.join(os.path.dirname(__file__), "data", "her.mp3")

# Focus on 1.7B: 4-bit vs 8-bit
MODELS = [
    ("mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-4bit", "Vivian"),
    ("mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit", "Vivian"),
]

# For voice cloning, need Base models (CustomVoice doesn't support ref_audio)
CLONE_MODELS = [
    "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-4bit",
    "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit",
]

SENTENCES = [
    "Hey there!",
    "I'd be happy to help you with that.",
    "So the main thing to keep in mind is that the process has three steps.",
    "First, you'll want to gather all the required documents and submit them through the online portal.",
    "Let me know if you need any clarification!",
]

CLONE_TEXT = "I think I understand what you're saying. Sometimes things just don't work out the way we planned, and that's okay."


def get_memory_mb():
    """Get wired+active memory on macOS via vm_stat."""
    try:
        out = subprocess.check_output(["vm_stat"], text=True)
        page_size = 16384  # Apple Silicon
        stats = {}
        for line in out.strip().split("\n")[1:]:
            parts = line.split(":")
            if len(parts) == 2:
                key = parts[0].strip()
                val = parts[1].strip().rstrip(".")
                try:
                    stats[key] = int(val)
                except ValueError:
                    pass
        wired = stats.get("Pages wired down", 0) * page_size
        active = stats.get("Pages active", 0) * page_size
        return (wired + active) / (1024 * 1024)
    except Exception:
        return 0


def kill_server():
    """Kill any process on port 8880."""
    try:
        pids = subprocess.check_output(["lsof", "-ti", ":8880"], text=True).strip()
        for pid in pids.split("\n"):
            if pid:
                os.kill(int(pid), signal.SIGKILL)
        time.sleep(2)
    except Exception:
        pass


def start_server():
    """Start mlx-audio server and wait for it to be ready."""
    kill_server()
    proc = subprocess.Popen(
        [VENV_PYTHON, "-m", "mlx_audio.server", "--host", "0.0.0.0", "--port", "8880"],
        stdout=open("/tmp/qwen3-tts.log", "w"),
        stderr=subprocess.STDOUT,
    )
    # Wait for server to respond
    for i in range(30):
        time.sleep(2)
        try:
            urllib.request.urlopen(f"{SERVER}/", timeout=3)
            return proc
        except Exception:
            pass
    print("  ERROR: Server failed to start in 60s")
    return proc


def wav_duration(wav_bytes):
    """Get duration in seconds from WAV bytes."""
    with wave.open(io.BytesIO(wav_bytes)) as wf:
        return wf.getnframes() / wf.getframerate()


def tts_request(text, model, voice="Vivian"):
    """Make a TTS request and measure timings."""
    payload = json.dumps({
        "model": model,
        "voice": voice,
        "input": text,
        "response_format": "wav",
    }).encode()

    req = urllib.request.Request(
        f"{SERVER}/v1/audio/speech",
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    t_start = time.perf_counter()
    resp = urllib.request.urlopen(req, timeout=180)

    first_chunk = resp.read(4096)
    t_first_byte = time.perf_counter()

    chunks = [first_chunk]
    while True:
        chunk = resp.read(65536)
        if not chunk:
            break
        chunks.append(chunk)
    t_done = time.perf_counter()
    resp.close()

    audio_bytes = b"".join(chunks)
    audio_dur = wav_duration(audio_bytes)

    return {
        "ttfab": t_first_byte - t_start,
        "total_time": t_done - t_start,
        "audio_duration": audio_dur,
        "audio_bytes": len(audio_bytes),
        "rtf": (t_done - t_start) / audio_dur if audio_dur > 0 else 0,
        "raw": audio_bytes,
    }


def run_benchmark(model, voice):
    """Benchmark a single model with fresh server restart."""
    short_name = model.split("/")[-1]
    print(f"\n{'='*70}")
    print(f"Model: {short_name}")
    print(f"{'='*70}")

    # Fresh server start for accurate memory
    print("Starting fresh server...")
    mem_before = get_memory_mb()
    proc = start_server()

    # Warmup
    print("Warming up (downloads model on first run)...")
    try:
        warmup = tts_request("Warming up.", model, voice)
        print(f"  Warmup: {warmup['total_time']:.2f}s")
    except Exception as e:
        print(f"  ERROR: {e}")
        try:
            with open("/tmp/qwen3-tts.log") as f:
                for line in reversed(f.readlines()):
                    if "Error" in line or "error" in line:
                        print(f"  Log: {line.strip()[:120]}")
                        break
        except Exception:
            pass
        print("  Skipping.\n")
        kill_server()
        return None

    # Measure memory after model loaded
    time.sleep(1)
    mem_after = get_memory_mb()
    mem_delta = mem_after - mem_before
    print(f"  Memory delta: ~{mem_delta:.0f} MB")

    # Run benchmark (2 passes — use second for stable numbers)
    for pass_num in range(2):
        results = []
        for sentence in SENTENCES:
            r = tts_request(sentence, model, voice)
            results.append(r)
            time.sleep(0.1)

        if pass_num == 0:
            print("  Pass 1 done (warmup pass), running pass 2...")

    # Print results from pass 2
    hdr = f"{'#':<3} {'TTFAB':>7} {'Total':>7} {'Audio':>7} {'RTF':>6}  Text"
    print(f"\n{hdr}")
    print("-" * 75)

    for i, r in enumerate(results):
        preview = SENTENCES[i][:45] + "..." if len(SENTENCES[i]) > 45 else SENTENCES[i]
        print(f"{i+1:<3} {r['ttfab']:>6.2f}s {r['total_time']:>6.2f}s {r['audio_duration']:>6.2f}s {r['rtf']:>5.2f}x  {preview}")

    avg_ttfab = sum(r["ttfab"] for r in results) / len(results)
    avg_rtf = sum(r["rtf"] for r in results) / len(results)
    total_tts = sum(r["total_time"] for r in results)
    total_audio = sum(r["audio_duration"] for r in results)
    min_ttfab = min(r["ttfab"] for r in results)
    max_ttfab = max(r["ttfab"] for r in results)

    print(f"\n  Avg TTFAB:  {avg_ttfab:.2f}s  (min: {min_ttfab:.2f}s, max: {max_ttfab:.2f}s)")
    print(f"  Avg RTF:    {avg_rtf:.2f}x  (lower = faster)")
    print(f"  Totals:     {total_tts:.1f}s generation -> {total_audio:.1f}s audio")
    print(f"  Memory:     ~{mem_delta:.0f} MB over baseline")

    kill_server()

    return {
        "model": short_name,
        "avg_ttfab": avg_ttfab,
        "min_ttfab": min_ttfab,
        "avg_rtf": avg_rtf,
        "mem_delta_mb": mem_delta,
        "total_audio": total_audio,
    }


def generate_clone_samples(ref_audio_path):
    """Generate voice-cloned samples from reference audio."""
    print(f"\n{'='*70}")
    print("VOICE CLONING SAMPLES")
    print(f"Reference: {ref_audio_path} (first 3 seconds)")
    print(f"{'='*70}")

    # Extract first 3s as wav for reference
    from pydub import AudioSegment
    audio = AudioSegment.from_mp3(ref_audio_path)
    clip = audio[:3000]  # first 3 seconds
    clip = clip.set_channels(1).set_frame_rate(24000)
    ref_wav_path = "/tmp/her_ref_3s.wav"
    clip.export(ref_wav_path, format="wav")
    print(f"  Extracted 3s reference -> {ref_wav_path}")

    # We need to use the Python API directly for voice cloning
    # since the server API for ref_audio requires file upload
    for clone_model in CLONE_MODELS:
        short = clone_model.split("/")[-1]
        print(f"\n--- {short} ---")
        print("  Starting server...")
        proc = start_server()

        # The server's /v1/audio/speech doesn't easily support ref_audio
        # Use Python API directly instead
        kill_server()

        print("  Loading model directly (no server)...")
        try:
            mem_before = get_memory_mb()
            t0 = time.perf_counter()

            from mlx_audio.tts.utils import load_model
            import numpy as np
            import soundfile as sf

            model = load_model(clone_model)
            t_load = time.perf_counter() - t0
            mem_after = get_memory_mb()
            print(f"  Model loaded in {t_load:.1f}s (mem delta: ~{mem_after - mem_before:.0f} MB)")

            # Generate with voice cloning
            t0 = time.perf_counter()
            results = list(model.generate(
                text=CLONE_TEXT,
                ref_audio=ref_wav_path,
                ref_text="",  # let it auto-detect or leave empty
            ))
            t_gen = time.perf_counter() - t0

            if results and hasattr(results[0], 'audio') and len(results[0].audio) > 0:
                audio_data = np.array(results[0].audio)
                sr = getattr(results[0], 'sample_rate', 24000)
                if isinstance(sr, type(None)):
                    sr = 24000
                duration = len(audio_data) / sr
                rtf = t_gen / duration if duration > 0 else 0

                out_path = f"data/clone_sample_{short}.wav"
                sf.write(out_path, audio_data, sr)
                print(f"  Generated: {out_path}")
                print(f"  Duration: {duration:.2f}s, Time: {t_gen:.2f}s, RTF: {rtf:.2f}x")
            else:
                print(f"  WARNING: No audio generated")

            # Clean up model from memory
            del model

        except Exception as e:
            print(f"  ERROR: {e}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    print("Qwen3-TTS 1.7B Benchmark: 4-bit vs 8-bit")
    print(f"Server: {SERVER}")
    print(f"Machine: {os.uname().machine}, {os.uname().sysname}\n")

    # Part 1: Benchmark 1.7B 4-bit vs 8-bit with preset voices
    summaries = []
    for model, voice in MODELS:
        result = run_benchmark(model, voice)
        if result:
            summaries.append(result)

    if summaries:
        print(f"\n{'='*70}")
        print("1.7B COMPARISON: 4-bit vs 8-bit")
        print(f"{'='*70}")
        print(f"{'Model':<45} {'TTFAB':>7} {'Min TTFAB':>10} {'RTF':>6} {'Mem':>8}")
        print("-" * 80)
        for s in summaries:
            print(f"{s['model']:<45} {s['avg_ttfab']:>6.2f}s {s['min_ttfab']:>9.2f}s {s['avg_rtf']:>5.2f}x {s['mem_delta_mb']:>6.0f}MB")

    # Part 2: Voice cloning samples
    if os.path.exists(REF_AUDIO):
        generate_clone_samples(REF_AUDIO)
    else:
        print(f"\nSkipping voice cloning: {REF_AUDIO} not found")

    print("\nDone!")
