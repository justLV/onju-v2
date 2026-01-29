#!/usr/bin/env python3
"""
Interactive serial monitor for ESP32
- Auto-reconnects on disconnect
- Sends keyboard input to device
- Type 'r' to reset ESP32
- Press Ctrl+C to exit
"""
import serial
import sys
import time
import select
import termios
import tty

def connect_serial(port, baud=115200, timeout=1):
    """Attempt to connect to serial port"""
    try:
        ser = serial.Serial(port, baud, timeout=timeout)
        time.sleep(0.1)
        return ser
    except Exception as e:
        return None

def main():
    port = sys.argv[1] if len(sys.argv) > 1 else '/dev/cu.usbmodem1101'
    baud = 115200

    print(f"Serial Monitor - {port} @ {baud} baud")
    print("Commands: 'r' = reset, 'M' = enable mic, 'A' = send multicast, Ctrl+C = exit")
    print("=" * 60)

    # Set terminal to raw mode for immediate key input
    old_settings = None
    if sys.platform != 'win32':
        old_settings = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin.fileno())

    ser = None

    while True:
        # Connect/reconnect
        if ser is None or not ser.is_open:
            if ser is not None:
                try:
                    ser.close()
                except:
                    pass

            print(f"\nConnecting to {port}...", end='', flush=True)
            ser = connect_serial(port, baud)

            if ser is None:
                print(" Failed. Retrying in 2s...")
                time.sleep(2)
                continue
            else:
                print(" Connected!")

        try:
            # Check for incoming serial data
            if ser.in_waiting > 0:
                line = ser.readline().decode('utf-8', errors='ignore').rstrip()
                if line:
                    print(line)

            # Check for keyboard input (non-blocking on Unix)
            if sys.platform != 'win32':
                if select.select([sys.stdin], [], [], 0)[0]:
                    key = sys.stdin.read(1)
                    ser.write(key.encode())
                    if key == 'r':
                        print("\n[Sent reset command]")
                        time.sleep(0.5)  # Give time for reset before reconnect
                    elif key == 'M':
                        print("\n[Sent mic enable command]")

            time.sleep(0.01)

        except serial.SerialException as e:
            print(f"\n[Disconnected: {e}]")
            try:
                ser.close()
            except:
                pass
            ser = None
            time.sleep(1)
        except KeyboardInterrupt:
            print("\n\nExiting...")
            if ser and ser.is_open:
                ser.close()
            if old_settings:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
            sys.exit(0)
        except OSError as e:
            # Device not configured - port disappeared
            if ser:
                try:
                    ser.close()
                except:
                    pass
                ser = None
            time.sleep(1)
        except Exception as e:
            print(f"\n[Error: {e}]")
            try:
                ser.close()
            except:
                pass
            ser = None
            time.sleep(1)

if __name__ == '__main__':
    main()
