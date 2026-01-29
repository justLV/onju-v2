#!/usr/bin/env python3
"""
Test streaming TTS from ElevenLabs to ESP32
Shows timing improvement with streaming
"""
import socket
import time
import io
from pydub import AudioSegment
from elevenlabs import ElevenLabs

# Config
ELEVENLABS_API_KEY = "sk_9928c246a666f54cbccee0f0f1e199ea1241356c3524c320"
ESP32_IP = "192.168.68.97"
ESP32_PORT = 3001

# Use Rachel voice (default ElevenLabs voice)
VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # Rachel

# Test text
TEXT = "Hello! This is a streaming text to speech test. Notice how the audio starts playing before the full sentence is generated. Pretty cool, right?"

def convert_mp3_chunk_to_pcm(mp3_chunk):
    """Convert MP3 chunk to 16kHz mono PCM"""
    audio = AudioSegment.from_mp3(io.BytesIO(mp3_chunk))
    audio = audio.set_channels(1)  # Mono
    audio = audio.set_frame_rate(16000)  # 16kHz
    audio = audio.set_sample_width(2)  # 16-bit
    return audio.raw_data

def main():
    print("="*60)
    print("ElevenLabs Streaming TTS Test")
    print("="*60)
    print(f"ESP32: {ESP32_IP}:{ESP32_PORT}")
    print(f"Text: {TEXT}")
    print()

    # Initialize ElevenLabs
    client = ElevenLabs(api_key=ELEVENLABS_API_KEY)

    # Connect to ESP32
    print(f"[{time.time():.3f}] Connecting to ESP32...")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10.0)

    try:
        sock.connect((ESP32_IP, ESP32_PORT))
        print(f"[{time.time():.3f}] Connected!")

        # Send header (0xAA + 60s timeout + volume 14)
        header = bytearray([0xAA, 0x00, 60, 14, 5, 0])
        sock.send(header)
        print(f"[{time.time():.3f}] Header sent")

        # Start timing
        start_time = time.time()
        first_chunk_time = None
        total_bytes = 0
        chunk_count = 0
        send_times = []  # Track time to send each chunk
        chunk_arrival_times = []  # Track when chunks arrive from ElevenLabs
        wait_times = []  # Track time between chunk arrivals

        print(f"[{time.time():.3f}] Starting TTS generation...")

        # Stream from ElevenLabs - use PCM format directly
        audio_stream = client.text_to_speech.convert(
            voice_id=VOICE_ID,
            text=TEXT,
            model_id="eleven_monolingual_v1",
            output_format="pcm_16000"  # 16kHz PCM, mono, 16-bit
        )

        last_chunk_arrival = start_time
        for pcm_chunk in audio_stream:
            # Record chunk arrival time
            chunk_arrival = time.time()
            chunk_arrival_times.append(chunk_arrival)

            # Calculate wait time (time since last chunk arrived)
            wait_time = (chunk_arrival - last_chunk_arrival) * 1000  # Convert to ms
            wait_times.append(wait_time)
            last_chunk_arrival = chunk_arrival

            if first_chunk_time is None:
                first_chunk_time = chunk_arrival
                print(f"[{first_chunk_time:.3f}] First audio chunk received! (Latency: {first_chunk_time - start_time:.3f}s)")

            # Measure send time
            send_start = time.time()
            sock.send(pcm_chunk)
            send_end = time.time()
            send_duration = (send_end - send_start) * 1000  # Convert to ms

            send_times.append(send_duration)
            total_bytes += len(pcm_chunk)
            chunk_count += 1

            print(f"[{time.time():.3f}] Sent chunk {chunk_count} ({len(pcm_chunk):,} bytes, wait: {wait_time:.2f}ms, send: {send_duration:.2f}ms, total: {total_bytes:,} bytes)")

        end_time = time.time()

        # Close connection
        sock.close()

        # Stats
        total_send_time = sum(send_times)
        avg_send_time = total_send_time / len(send_times) if send_times else 0
        min_send_time = min(send_times) if send_times else 0
        max_send_time = max(send_times) if send_times else 0

        total_wait_time = sum(wait_times)
        avg_wait_time = total_wait_time / len(wait_times) if wait_times else 0
        min_wait_time = min(wait_times) if wait_times else 0
        max_wait_time = max(wait_times) if wait_times else 0

        # Calculate throughput
        throughput_kbps = (total_bytes * 8) / (total_send_time / 1000) / 1000 if total_send_time > 0 else 0
        audio_playback_rate_kbps = 256  # 16kHz * 16-bit

        print()
        print("="*60)
        print("RESULTS:")
        print("="*60)
        print(f"Total pipeline time:   {end_time - start_time:.3f}s")
        print(f"Time to first chunk:   {first_chunk_time - start_time:.3f}s")
        print(f"Total audio generated: {total_bytes:,} bytes ({total_bytes/32000:.1f}s of audio @ 16kHz)")
        print(f"Chunks received:       {chunk_count}")
        print(f"Average chunk size:    {total_bytes/chunk_count:,.0f} bytes")
        print()
        print("GENERATION STATS (waiting for ElevenLabs):")
        print(f"Total wait time:       {total_wait_time:.2f}ms ({total_wait_time/1000:.2f}s)")
        print(f"Average wait time:     {avg_wait_time:.2f}ms per chunk")
        print(f"Min wait time:         {min_wait_time:.2f}ms")
        print(f"Max wait time:         {max_wait_time:.2f}ms")
        print()
        print("TRANSMISSION STATS (sending to ESP32):")
        print(f"Total send time:       {total_send_time:.2f}ms ({total_send_time/1000:.2f}s)")
        print(f"Average send time:     {avg_send_time:.2f}ms per chunk")
        print(f"Min send time:         {min_send_time:.2f}ms")
        print(f"Max send time:         {max_send_time:.2f}ms")
        print(f"Network throughput:    {throughput_kbps:.1f} kbps")
        print(f"Audio playback rate:   {audio_playback_rate_kbps} kbps")
        print(f"Throughput margin:     {throughput_kbps/audio_playback_rate_kbps:.1f}x faster than playback")
        print()
        print("TIME BREAKDOWN:")
        total_pipeline_ms = (end_time - start_time) * 1000
        print(f"Total pipeline time:   {total_pipeline_ms/1000:.2f}s")
        print(f"  Waiting for chunks:  {total_wait_time/1000:.2f}s (time between arrivals)")
        print(f"  Sending to ESP32:    {total_send_time/1000:.2f}s (overlaps with waiting)")
        print(f"  Average concurrency: {(total_wait_time + total_send_time) / total_pipeline_ms:.2f}x")
        print(f"  (>1.0 means sending and generating happen in parallel)")
        print()
        print("LATENCY IMPROVEMENT:")
        print(f"With streaming:    {first_chunk_time - start_time:.3f}s to start playback")
        print(f"Without streaming: {end_time - start_time:.3f}s to start playback")
        print(f"Savings:           {(end_time - start_time) - (first_chunk_time - start_time):.3f}s faster!")
        print("="*60)

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        sock.close()

if __name__ == '__main__':
    main()
