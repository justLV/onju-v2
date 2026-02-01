#!/usr/bin/env python3
"""
Test streaming TTS from ElevenLabs to ESP32
Shows timing improvement with streaming
Supports both PCM and Opus compression

Usage:
  python test_streaming_tts.py [ESP32_IP] [--opus]

Examples:
  python test_streaming_tts.py                    # PCM to default IP
  python test_streaming_tts.py --opus             # Opus to default IP
  python test_streaming_tts.py 192.168.68.95      # PCM to specified IP
  python test_streaming_tts.py 192.168.68.95 --opus  # Opus to specified IP
"""
import socket
import time
import io
import sys
import struct
from pydub import AudioSegment
from elevenlabs import ElevenLabs

# Config
ELEVENLABS_API_KEY = "sk_9928c246a666f54cbccee0f0f1e199ea1241356c3524c320"
ESP32_IP = sys.argv[1] if len(sys.argv) > 1 else "192.168.68.97"
ESP32_PORT = 3001
USE_OPUS = "--opus" in sys.argv  # Add --opus flag to use Opus compression

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
    print(f"Compression: {'Opus' if USE_OPUS else 'PCM'}")
    print(f"Text: {TEXT}")
    print()

    # Initialize Opus encoder if needed
    opus_encoder = None
    if USE_OPUS:
        try:
            import opuslib
            SAMPLE_RATE = 16000
            CHANNELS = 1
            FRAME_SIZE = 320  # 20ms @ 16kHz
            opus_encoder = opuslib.Encoder(SAMPLE_RATE, CHANNELS, opuslib.APPLICATION_VOIP)
            print(f"Opus encoder initialized (20ms frames)")
        except ImportError:
            print("ERROR: opuslib not installed. Run: pip install opuslib")
            return
        except Exception as e:
            print(f"ERROR initializing Opus encoder: {e}")
            return

    # Initialize ElevenLabs
    client = ElevenLabs(api_key=ELEVENLABS_API_KEY)

    # Connect to ESP32
    print(f"[{time.time():.3f}] Connecting to ESP32...")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10.0)

    try:
        sock.connect((ESP32_IP, ESP32_PORT))
        print(f"[{time.time():.3f}] Connected!")

        # Send header (0xAA + 60s timeout + volume 14 + compression type)
        compression_type = 2 if USE_OPUS else 0  # 0=PCM, 2=Opus
        header = bytearray([0xAA, 0x00, 60, 14, 5, compression_type])
        sock.send(header)
        print(f"[{time.time():.3f}] Header sent (compression_type={compression_type})")

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
        pcm_buffer = b''  # Buffer for Opus encoding (need 640-byte chunks)
        opus_frames_sent = 0
        total_opus_bytes = 0

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

            if USE_OPUS:
                # Buffer PCM data and encode in 640-byte (20ms) frames
                pcm_buffer += pcm_chunk

                # Encode all complete 640-byte frames
                while len(pcm_buffer) >= 640:
                    pcm_frame = pcm_buffer[:640]
                    pcm_buffer = pcm_buffer[640:]

                    # Encode to Opus
                    try:
                        opus_frame = opus_encoder.encode(pcm_frame, 320)  # 320 samples
                        frame_len = len(opus_frame)

                        # Send with 2-byte length prefix (big-endian)
                        sock.send(struct.pack('>H', frame_len))
                        sock.send(opus_frame)

                        total_opus_bytes += frame_len
                        opus_frames_sent += 1
                    except Exception as e:
                        print(f"ERROR encoding Opus frame: {e}")
                        continue
            else:
                # Send PCM directly
                sock.send(pcm_chunk)

            send_end = time.time()
            send_duration = (send_end - send_start) * 1000  # Convert to ms

            send_times.append(send_duration)
            total_bytes += len(pcm_chunk)
            chunk_count += 1

            if USE_OPUS:
                print(f"[{time.time():.3f}] Sent chunk {chunk_count} ({len(pcm_chunk):,} bytes PCM → {opus_frames_sent} Opus frames, {total_opus_bytes:,} compressed bytes, wait: {wait_time:.2f}ms, send: {send_duration:.2f}ms)")
            else:
                print(f"[{time.time():.3f}] Sent chunk {chunk_count} ({len(pcm_chunk):,} bytes, wait: {wait_time:.2f}ms, send: {send_duration:.2f}ms, total: {total_bytes:,} bytes)")

        # Flush remaining PCM buffer for Opus
        if USE_OPUS and len(pcm_buffer) > 0:
            # Pad to 640 bytes if needed
            if len(pcm_buffer) < 640:
                pcm_buffer += b'\x00' * (640 - len(pcm_buffer))

            try:
                opus_frame = opus_encoder.encode(pcm_buffer[:640], 320)
                frame_len = len(opus_frame)
                sock.send(struct.pack('>H', frame_len))
                sock.send(opus_frame)
                total_opus_bytes += frame_len
                opus_frames_sent += 1
                print(f"[{time.time():.3f}] Flushed final Opus frame ({frame_len} bytes)")
            except Exception as e:
                print(f"ERROR encoding final Opus frame: {e}")

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

        # Calculate throughput (use compressed bytes if Opus)
        bytes_sent = total_opus_bytes if USE_OPUS else total_bytes
        throughput_kbps = (bytes_sent * 8) / (total_send_time / 1000) / 1000 if total_send_time > 0 else 0
        audio_playback_rate_kbps = (total_opus_bytes * 8) / (total_bytes / 32000) / 1000 if USE_OPUS and total_bytes > 0 else 256

        print()
        print("="*60)
        print("RESULTS:")
        print("="*60)
        print(f"Total pipeline time:   {end_time - start_time:.3f}s")
        print(f"Time to first chunk:   {first_chunk_time - start_time:.3f}s")
        print(f"Total audio generated: {total_bytes:,} bytes PCM ({total_bytes/32000:.1f}s of audio @ 16kHz)")
        print(f"Chunks received:       {chunk_count}")
        print(f"Average chunk size:    {total_bytes/chunk_count:,.0f} bytes")

        if USE_OPUS:
            compression_ratio = total_bytes / total_opus_bytes if total_opus_bytes > 0 else 0
            pcm_kbps = 256  # 16kHz * 16-bit
            opus_kbps = (total_opus_bytes * 8) / (total_bytes / 32000) / 1000 if total_bytes > 0 else 0
            print()
            print("OPUS COMPRESSION:")
            print(f"Opus frames sent:      {opus_frames_sent}")
            print(f"Compressed size:       {total_opus_bytes:,} bytes")
            print(f"Compression ratio:     {compression_ratio:.1f}x")
            print(f"PCM bandwidth:         {pcm_kbps} kbps")
            print(f"Opus bandwidth:        {opus_kbps:.1f} kbps")
            print(f"Bandwidth savings:     {pcm_kbps - opus_kbps:.1f} kbps ({(1 - opus_kbps/pcm_kbps)*100:.0f}%)")

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
