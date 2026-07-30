# Setup Guide

Everything needed to go from a blank SD card to a running camera stream,
on either a Raspberry Pi 5 or Pi 4.

## Hardware required

| Item | Notes |
|---|---|
| Raspberry Pi 5 or Pi 4 (Model B, 4GB+ RAM) | Pi 5 has no hardware H.264 encoder - `server.py` encodes 4K in software, which is noticeably heavier on CPU than Pi 4. Either works; Pi 5 just runs hotter under recording load. |
| Official power supply | Pi 5: 27W USB-C PD (5V/5A). Pi 4: 5V/3A USB-C. Undersized supplies cause random reboots/throttling under load, especially while recording. |
| microSD card, 32GB+ (A2/U3 rated) | Or boot from USB SSD/NVMe (Pi 5 supports PCIe natively) - recommended if you'll keep the continuous buffer's retention window long, since that's ~1GB/hour at the default bitrate. |
| Camera module | This app was built against an **Arducam 64MP Hawkeye** (autofocus, CSI). An official Raspberry Pi Camera Module (v2/v3/HQ) also works via `camera_auto_detect=1` with no extra overlay - you'll just lose the autofocus-on-capture behavior in `_autofocus_before_capture()`. |
| CSI ribbon cable | Comes with the camera module; make sure it matches your Pi's CSI connector (Pi 5's is a different connector pitch than Pi 4's - use the cable rated for your Pi model). |
| Cooling | Active cooling strongly recommended, especially Pi 5. CPU sits around 55-65°C under normal continuous-recording load in testing here; a passive heatsink alone will let it climb further and throttle. Official Active Cooler (Pi 5) or a case with a fan (Pi 4) is enough. |
| Case with camera mount | Any case that exposes the CSI connector and gives the camera an unobstructed view of what you're monitoring. |
| Network | Ethernet recommended for the live stream/uploads; Wi-Fi works fine too. |

## 1. Flash the OS

1. Download [Raspberry Pi Imager](https://www.raspberrypi.com/software/).
2. Choose **Raspberry Pi OS (64-bit)** - this app requires 64-bit for `picamera2`/`libcamera`.
3. In Imager's settings (gear icon / Ctrl+Shift+X) before writing:
   - Set hostname, enable SSH, set username/password, configure Wi-Fi if needed.
   - This gives you headless SSH access on first boot - no monitor/keyboard needed.
4. Write to the SD card, insert it in the Pi, power on.
5. `ssh <username>@<hostname>.local` (or the DHCP-assigned IP) once it boots.

## 2. Clone this repo and run the installer

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/Rajesh6174/RaspberryPi5HawkeyeCameraApp.git ~/.local/share/camera-stream
cd ~/.local/share/camera-stream
./install.sh
```

`install.sh` is idempotent and handles:
- Installing all required apt packages (`python3-picamera2`, `libcamera`, `ffmpeg`, etc. - see table below).
- Adding `camera_auto_detect=1` to the boot config if missing, and printing the extra
  line needed if you're using the Arducam 64MP.
- Creating `snapshots/`, `recordings/`, `continuous/` data directories.
- Creating `~/.config/camera-stream/` and populating it with `.env` files from the
  `config/*.env.example` templates (placeholders only - you fill in real values, see step 4).
- Installing the systemd user units from `systemd/`, enabling lingering (so services
  survive logout/reboot with no login), and starting `camera-stream.service`.

### What gets installed, if you want to do it by hand instead

```bash
sudo apt-get install -y \
    python3-picamera2 python3-libcamera rpicam-apps \
    python3-requests python3-pil python3-numpy python3-psutil \
    ffmpeg
```

Debian/Raspberry Pi OS marks the system Python as "externally managed" (PEP 668) -
these packages are installed via `apt`, not `pip`. Don't `pip install` them; there's no
requirements.txt for this reason.

## 3. Enable the camera and reboot

For the **Arducam 64MP** (this project's default):
```bash
echo 'dtoverlay=arducam-64mp' | sudo tee -a /boot/firmware/config.txt
sudo reboot
```

For an **official Camera Module** (v2/v3/HQ), `camera_auto_detect=1` (already added by
`install.sh`) is sufficient - no extra overlay line, just reboot.

After reboot, confirm the camera is detected:
```bash
rpicam-hello --list-cameras
```
You should see your camera model and its supported resolutions listed.

## 4. Fill in the config files (the part that can't be automated)

None of these are in git, on purpose - they hold real credentials/secrets, and
`.gitignore` excludes any `*.env` file except the `.example` templates. `install.sh`
creates empty placeholders at `~/.config/camera-stream/{gdrive,location}.env` -
edit them directly, or see below for a faster recovery path.

| File | Required? | What it's for |
|---|---|---|
| `gdrive.env` | Optional | Google Drive backup of snapshots/recordings/continuous buffer. Run `python3 setup_gdrive.py` once - it walks you through Google's device-authorization flow (a URL + code to approve on your phone) and writes `GDRIVE_REFRESH_TOKEN` automatically. Needs `GDRIVE_CLIENT_ID`/`GDRIVE_CLIENT_SECRET` from Google Cloud Console first (OAuth client type "TVs and Limited Input devices"). |
| `location.env` | Optional | Deploy-time default lat/lon for the weather overlay. Without it, defaults to 0,0. Can also be changed anytime from the web UI ("Set Weather Location") without touching this file or restarting - that live value is stored in `location_state.json` and takes precedence once set. |

Restart the service after editing any of these:
```bash
systemctl --user restart camera-stream.service
```

## 5. Verify it's running

```bash
systemctl --user status camera-stream.service --no-pager
curl -s http://127.0.0.1:8000/api/sysinfo | python3 -m json.tool
```

Then open `http://<pi-ip>:8000/` in a browser for the live view.

## Disaster recovery: SD card corrupts, get back up fast

The repo holds all the code and unit files, but **three things live outside git on
purpose** (they're secrets or machine-specific): the contents of
`~/.config/camera-stream/*.env`, and whatever's currently in `snapshots/`,
`recordings/`, `continuous/` on the dead card (the last few hours/days of footage
not yet uploaded to Drive, if Drive backup wasn't enabled).

**Before disaster strikes**, back up the small secrets, not the media - the media
is either in Drive already or is gone regardless of the code:
```bash
tar czf camera-stream-config-backup.tar.gz -C ~/.config camera-stream
```
Store that tarball somewhere durable and *not* on the same SD card - a password
manager attachment, an encrypted note, a private (not public) cloud folder, etc.
It contains real OAuth/bot secrets, so treat it like a password.

**When the card dies**, on a fresh SD card:
```bash
# 1. Flash + boot Raspberry Pi OS (steps 1 above) - few minutes, unavoidable
# 2. Clone + install
git clone https://github.com/Rajesh6174/RaspberryPi5HawkeyeCameraApp.git ~/.local/share/camera-stream
cd ~/.local/share/camera-stream
./install.sh
# 3. Restore your real secrets over the placeholder .env files install.sh created
tar xzf camera-stream-config-backup.tar.gz -C ~/.config
chmod 600 ~/.config/camera-stream/*.env
# 4. If using the Arducam 64MP, add its overlay line (install.sh prints the
#    exact command), then:
sudo reboot
# 5. After reboot:
systemctl --user restart camera-stream.service
```

With the config backup in hand, steps 2-5 take a few minutes; the OS flash/first
boot is the only inherently slow part. Without the config backup, the code and
camera stream still come up fine from step 2 alone - you'd just need to redo the
one-time Google Drive authorization from scratch.

**Fastest possible recovery**: keep a second SD card, run this whole guide on it
once, then `sudo dd if=/dev/mmcblk0 of=pi-camera-backup.img bs=4M status=progress`
(or use `rpi-clone`) to image it while healthy. Swapping in a pre-built spare card
is the only way to get back up in literal minutes rather than "a bit longer than
you'd like."

## Known limitations worth knowing about

- The MJPEG stream on port 8000 has **no authentication** - anyone on your network
  (or beyond, if you port-forward it) can view it. Don't expose port 8000 to the
  public internet without putting a reverse proxy with auth in front of it.
- Pi 5 has no hardware H.264 encoder, so 4K recording (`recording_bitrate`) is
  CPU-bound - expect higher temps and lower headroom for other work while a
  manual recording is active, compared to Pi 4 (which does have hardware encode).
