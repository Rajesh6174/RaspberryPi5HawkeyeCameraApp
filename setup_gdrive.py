#!/usr/bin/env python3
"""
One-time interactive Google Drive authorization for camera-stream.

Run manually (not under systemd): python3 setup_gdrive.py

Reads GDRIVE_CLIENT_ID / GDRIVE_CLIENT_SECRET from ~/.config/camera-stream/gdrive.env
(create that file first from gdrive.env.example with a client from Google Cloud
Console -> APIs & Services -> Credentials -> Create OAuth client -> "TVs and Limited
Input devices"). Runs the OAuth device-authorization flow: prints a URL + short code
for you to enter on your phone/laptop, polls until you approve, then appends
GDRIVE_REFRESH_TOKEN to that same env file.
"""
import os
import stat
import sys
import time

import requests

ENV_PATH = os.path.expanduser("~/.config/camera-stream/gdrive.env")
DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/drive.file"


def load_env(path):
    values = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                values[key.strip()] = val.strip()
    return values


def main():
    env = load_env(ENV_PATH)
    client_id = env.get("GDRIVE_CLIENT_ID")
    client_secret = env.get("GDRIVE_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit(
            f"GDRIVE_CLIENT_ID / GDRIVE_CLIENT_SECRET not found in {ENV_PATH}.\n"
            "Copy gdrive.env.example to gdrive.env, fill in the client ID/secret "
            "from Google Cloud Console (OAuth client type 'TVs and Limited Input "
            "Devices'), then re-run this script."
        )
    if env.get("GDRIVE_REFRESH_TOKEN"):
        print(f"{ENV_PATH} already has a GDRIVE_REFRESH_TOKEN. Delete that line first "
              "if you want to re-authorize.")
        return

    device_resp = requests.post(DEVICE_CODE_URL, data={
        "client_id": client_id,
        "scope": SCOPE,
    }, timeout=15)
    device_resp.raise_for_status()
    device = device_resp.json()

    print()
    print("=" * 60)
    print(f"  Go to: {device['verification_url']}")
    print(f"  Enter code: {device['user_code']}")
    print("=" * 60)
    print()

    interval = device.get("interval", 5)
    deadline = time.time() + device.get("expires_in", 1800)

    while time.time() < deadline:
        time.sleep(interval)
        token_resp = requests.post(TOKEN_URL, data={
            "client_id": client_id,
            "client_secret": client_secret,
            "device_code": device["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }, timeout=15)
        payload = token_resp.json()

        if token_resp.status_code == 200:
            refresh_token = payload["refresh_token"]
            with open(ENV_PATH, "a") as f:
                f.write(f"GDRIVE_REFRESH_TOKEN={refresh_token}\n")
            os.chmod(ENV_PATH, stat.S_IRUSR | stat.S_IWUSR)
            print(f"Authorized. Refresh token saved to {ENV_PATH}.")
            print("Next: add 'EnvironmentFile=%h/.config/camera-stream/gdrive.env' to "
                  "camera-stream.service, then systemctl --user daemon-reload && "
                  "systemctl --user restart camera-stream.service")
            return

        error = payload.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        sys.exit(f"Authorization failed: {payload.get('error_description', error)}")

    sys.exit("Timed out waiting for authorization. Re-run this script to try again.")


if __name__ == "__main__":
    main()
