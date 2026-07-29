#!/usr/bin/env python3
"""
Standalone garage-door state watcher -> Telegram alert.

No Claude/LLM involved: grabs a frame from the existing camera-stream
service, does simple pixel-diff against a saved reference "closed" image,
and pings Telegram on a closed -> open transition. Meant to run forever
under systemd (see garage-watch.service).
"""
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request

from PIL import Image
import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

STREAM_URL = "http://127.0.0.1:8000/stream.mjpg"
FRAME_PATH = "/tmp/garage_frame.jpg"
CROP_BOX = (600, 370, 1000, 480)  # x0, y0, x1, y1 in the 1280x720 stream frame

REFERENCE_CLOSED = os.path.join(BASE_DIR, "reference_closed.jpg")
STATE_FILE = os.path.join(BASE_DIR, "garage_state.json")

DIFF_THRESHOLD = float(os.environ.get("GARAGE_DIFF_THRESHOLD", "18"))
CHECK_INTERVAL_SEC = int(os.environ.get("GARAGE_CHECK_INTERVAL", "20"))
ALERT_ON_CLOSE = os.environ.get("GARAGE_ALERT_ON_CLOSE", "false").lower() == "true"

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


def grab_frame():
    subprocess.run(
        ["ffmpeg", "-y", "-i", STREAM_URL, "-frames:v", "1", FRAME_PATH],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8, check=True,
    )
    return Image.open(FRAME_PATH).convert("L").crop(CROP_BOX)


def mean_abs_diff(img_a, img_b):
    arr_a = np.array(img_a, dtype=np.int16)
    arr_b = np.array(img_b, dtype=np.int16)
    return float(np.abs(arr_a - arr_b).mean())


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": message}).encode()
    with urllib.request.urlopen(url, data=data, timeout=10) as resp:
        resp.read()


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"door_open": False}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def main():
    if not os.path.exists(REFERENCE_CLOSED):
        raise SystemExit(
            f"No reference image at {REFERENCE_CLOSED}. Run calibrate.py save-reference "
            "while the door is confirmed CLOSED first."
        )
    # reference_closed.jpg is saved already-cropped by calibrate.py, so load it as-is
    reference = Image.open(REFERENCE_CLOSED).convert("L")
    state = load_state()

    while True:
        try:
            current = grab_frame()
            diff = mean_abs_diff(current, reference)
            is_open = diff > DIFF_THRESHOLD

            if is_open and not state["door_open"]:
                send_telegram("Garage door G08 just opened")
                state["door_open"] = True
                save_state(state)
            elif not is_open and state["door_open"]:
                state["door_open"] = False
                save_state(state)
                if ALERT_ON_CLOSE:
                    send_telegram("Garage door G08 closed")

            print(f"diff={diff:.2f} threshold={DIFF_THRESHOLD} open={is_open}", flush=True)
        except Exception as e:
            print(f"error: {e}", flush=True)

        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
