# Garage Door Watcher — non-Claude, Telegram alerts

## Why replace the Claude cron watcher

The Claude-based watcher (a cron job that wakes an LLM agent, grabs a frame,
and visually inspects it) works but has real problems for a 24/7 home-monitoring
job:

- **Cost/latency**: every check is a full model call just to answer "is this
  patch of pixels lighter or darker than before."
- **Not actually unattended**: `PushNotification` silently refuses to send
  when it thinks your terminal is active — exactly when you're *not* watching
  is when you need the alert to work.
- **Session-bound**: dies when the Claude session ends, and cron jobs here
  auto-expire after 7 days regardless.
- **Fragile to drift**: relies on the model re-reading pixel-coordinate
  instructions each time.

The replacement is a small always-on **systemd service** (same pattern as the
camera stream itself) that:

1. Grabs a frame from the existing MJPEG stream (`http://127.0.0.1:8000/stream.mjpg`)
2. Crops the same door region
3. Compares it to a saved reference "closed" image with plain pixel-diff (no
   LLM, sub-millisecond, no API cost)
4. Sends a Telegram message directly via the Bot API on a closed→open
   transition

Diagram:

```
camera-stream.service (picamera2, already running)
        |
        v
 MJPEG http://127.0.0.1:8000/stream.mjpg
        |
        v
 garage_watch.py  --(pixel diff vs reference_closed.jpg)-->  state change?
        |                                                         |
        v                                                         v
   loop every 20s                                    Telegram Bot API -> your phone
```

## Files already created

| File | Purpose |
|---|---|
| `~/.local/share/camera-stream/garage_watch.py` | the watcher loop |
| `~/.local/share/camera-stream/calibrate.py` | one-off helper to save a reference image and measure diff values |
| `~/.config/systemd/user/garage-watch.service` | systemd unit |
| `~/.config/camera-stream/telegram.env.example` | template for secrets/config |

Nothing is installed/enabled yet — you need a Telegram bot token and chat ID
first (below), and a one-time calibration.

## Step 1: Create a Telegram bot

1. In Telegram, message **@BotFather**.
2. Send `/newbot`, give it a name and a username (must end in `bot`, e.g.
   `garage_g08_bot`).
3. BotFather replies with a token like `123456789:AAExampleTokenFromBotFather`.
   That's your `TELEGRAM_BOT_TOKEN`.

## Step 2: Get your chat ID

1. Send any message (e.g. "hi") to your new bot from your Telegram account.
2. From this Pi (or any machine), run:
   ```
   curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates"
   ```
3. In the JSON response, find `"chat":{"id":123456789, ...}` — that number is
   your `TELEGRAM_CHAT_ID`.

## Step 3: Fill in the config

```
cp ~/.config/camera-stream/telegram.env.example ~/.config/camera-stream/telegram.env
chmod 600 ~/.config/camera-stream/telegram.env
nano ~/.config/camera-stream/telegram.env   # fill in real token + chat id
```

## Step 4: Calibrate the detection threshold

The door region in frame is a fixed crop `(600, 370, 1000, 480)` — same box
used throughout this session. Pixel-diff against a reference beats a raw
brightness threshold because it cancels out the fence bars in the foreground
(they're static, so they diff to ~0 either way).

```
cd ~/.local/share/camera-stream

# 1. Make sure the garage door is actually CLOSED right now, then:
python3 calibrate.py save-reference

# 2. Open the garage door, then:
python3 calibrate.py test
# note the printed diff number — it should be clearly higher than ~0

# 3. Close it again and run test once more to confirm it drops back near 0
python3 calibrate.py test
```

Pick `GARAGE_DIFF_THRESHOLD` roughly halfway between the "closed" (~0-5) and
"open" readings you observed, and set it in `telegram.env`. The default of
`18` is a placeholder — replace it with your real measured midpoint.

## Step 5: Install and start the service

```
systemctl --user daemon-reload
systemctl --user enable --now garage-watch.service
systemctl --user status garage-watch.service --no-pager
journalctl --user -u garage-watch -f    # watch live diff readings
```

Because lingering is already enabled for this user (set up for the camera
stream), this survives logout and reboot with no login required, just like
the camera stream itself.

## Step 6: Test end-to-end

Open the garage door and watch `journalctl --user -u garage-watch -f` — you
should see the diff value jump above threshold and a Telegram message should
arrive within `GARAGE_CHECK_INTERVAL` seconds (default 20s).

## Step 7: Retire the Claude-based watcher

Once the systemd version is confirmed working, tell Claude to cancel the
cron-based watcher (it's redundant and will double-alert). In-session:
`CronDelete` on the garage-watcher job id (currently `d3bba2f3`), or just say
"cancel the garage door cron job."

## Tuning / troubleshooting

- **False positives** (alerts with nothing changed): usually a lighting
  swing (cloud passing, sun angle) crossing the threshold. Raise
  `GARAGE_DIFF_THRESHOLD` a bit, or re-run calibration at a different time of
  day and average.
- **Missed opens**: threshold set too high, or the RAV4 (or whatever's
  parked) is fully blocking the crop region on some days — check
  `journalctl` for the diff readings for a day and adjust the crop box in
  both `garage_watch.py` and `calibrate.py` if the framing changed.
- **No Telegram message but service running**: test the bot API directly:
  ```
  curl -s -X POST "https://api.telegram.org/bot<TOKEN>/sendMessage" \
    -d chat_id=<CHAT_ID> -d text="test"
  ```
  If that fails, the token/chat ID is wrong, not the watcher logic.
- **Service won't start**: check `camera-stream.service` is running first —
  `garage-watch.service` depends on it (`Requires=`) since it reads the same
  MJPEG stream.
