#!/usr/bin/env python3
"""
One-off helper to (1) save the reference "closed" image and (2) measure
pixel-diff values so you can pick a sane GARAGE_DIFF_THRESHOLD.

Usage:
  python3 calibrate.py save-reference   # run once while door is confirmed CLOSED
  python3 calibrate.py test             # run any time to see current diff vs reference
"""
import subprocess
import sys
import os

from PIL import Image
import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STREAM_URL = "http://127.0.0.1:8000/stream.mjpg"
FRAME_PATH = "/tmp/garage_calib.jpg"
CROP_BOX = (600, 370, 1000, 480)
REF_PATH = os.path.join(BASE_DIR, "reference_closed.jpg")


def grab():
    subprocess.run(
        ["ffmpeg", "-y", "-i", STREAM_URL, "-frames:v", "1", FRAME_PATH],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8, check=True,
    )
    return Image.open(FRAME_PATH).convert("L").crop(CROP_BOX)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "test"
    frame = grab()

    if mode == "save-reference":
        frame.save(REF_PATH)
        print(f"Saved reference (closed) to {REF_PATH}")
        print("Now open the garage door and run: python3 calibrate.py test")
    else:
        if not os.path.exists(REF_PATH):
            raise SystemExit("No reference yet — run 'save-reference' first while door is closed.")
        ref = Image.open(REF_PATH).convert("L")
        arr_a = np.array(frame, dtype=np.int16)
        arr_b = np.array(ref, dtype=np.int16)
        diff = float(np.abs(arr_a - arr_b).mean())
        print(f"Mean abs diff vs closed-reference: {diff:.2f}")
        print("Run this once with door CLOSED (should be near 0) and once OPEN (should be clearly higher).")
        print("Then set GARAGE_DIFF_THRESHOLD in telegram.env to roughly the midpoint.")


if __name__ == "__main__":
    main()
