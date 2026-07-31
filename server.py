import base64
import hmac
import io
import json
import logging
import os
import re
import shutil
import socketserver
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http import server
from threading import Condition, Event, Lock, Thread

import psutil
from libcamera import Transform
from PIL import Image, ImageDraw, ImageFont
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder, JpegEncoder
from picamera2.outputs import FfmpegOutput, FileOutput

import gdrive_upload

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_DIR = os.path.join(BASE_DIR, "snapshots")
os.makedirs(SNAPSHOT_DIR, exist_ok=True)
RECORDING_DIR = os.path.join(BASE_DIR, "recordings")
os.makedirs(RECORDING_DIR, exist_ok=True)
CONTINUOUS_DIR = os.path.join(BASE_DIR, "continuous")
os.makedirs(CONTINUOUS_DIR, exist_ok=True)

THERMAL_ZONE_PATH = "/sys/class/thermal/thermal_zone0/temp"
FAN_COOLING_DEVICE = "/sys/class/thermal/cooling_device0"
ROTATION_STATE_FILE = os.path.join(BASE_DIR, "rotation_state.json")
LOCATION_STATE_FILE = os.path.join(BASE_DIR, "location_state.json")

# Performance/resolution knobs, all overridable from
# ~/.config/camera-stream/performance.env (see config/performance.env.example).
# Defaults below match the original fixed values this app shipped with (tuned for
# Pi 4/5); a Raspberry Pi Zero 2 W or other low-RAM/low-CPU board needs a much
# smaller profile - see the "Low-Power Boards" section in SETUP.md.
def _env_int(name, default):
    return int(os.environ.get(name, default))


def _env_bool(name, default):
    val = os.environ.get(name)
    return default if val is None else val not in ("0", "false", "False", "")


SNAPSHOT_NAME_RE = re.compile(r"^snapshot_(full_)?\d{8}_\d{6}\.jpg$")
RECORDING_NAME_RE = re.compile(r"^recording_\d{8}_\d{6}\.mp4$")
RECORDING_BITRATE = _env_int("RECORDING_BITRATE", 20_000_000)  # 4K H.264, software-encoded on Pi 5 (no hw encoder)

CONTINUOUS_NAME_RE = re.compile(r"^continuous_\d{8}_\d{6}\.mp4$")
CONTINUOUS_BITRATE = _env_int("CONTINUOUS_BITRATE", 2_000_000)  # 720p H.264, low CPU/storage cost for an always-on buffer
CONTINUOUS_SEGMENT_SECONDS = _env_int("CONTINUOUS_SEGMENT_SECONDS", 10 * 60)
CONTINUOUS_RETENTION_SECONDS = _env_int("CONTINUOUS_RETENTION_SECONDS", 3 * 60 * 60)
CONTINUOUS_DEFAULT_ENABLED = _env_bool("CONTINUOUS_DEFAULT_ENABLED", True)

# Recording resolution - the "main" stream stays configured at this size at all times
# so 4K recording (or whatever size is set here) can start instantly with no mode
# switch. On low-RAM boards, shrinking this is the single biggest lever - it's an
# always-on buffer cost, not just a during-recording one.
LIVE_MAIN_SIZE = (_env_int("CAMERA_MAIN_WIDTH", 3840), _env_int("CAMERA_MAIN_HEIGHT", 2160))
LIVE_LORES_SIZE = (_env_int("CAMERA_LORES_WIDTH", 1280), _env_int("CAMERA_LORES_HEIGHT", 720))  # live view / MJPEG preview resolution
CAMERA_BUFFER_COUNT = _env_int("CAMERA_BUFFER_COUNT", 6)

# HTTP Basic Auth, from ~/.config/camera-stream/auth.env (see config/auth.env.example).
# Optional, same pattern as Drive/location/performance config - unset means the
# server is open on the LAN with no login, matching this app's original behavior.
AUTH_USER = os.environ.get("CAMERA_USER")
AUTH_PASS = os.environ.get("CAMERA_PASS")
AUTH_ENABLED = bool(AUTH_USER and AUTH_PASS)
AUTH_REALM = "Hawkeye Camera"

ZOOM_MAX = 8.0
CONTRAST_UI_MAX = 2.0
SATURATION_UI_MAX = 2.0
STALE_FRAME_SECONDS = 5.0

# Deploy-time default location for the weather overlay, from
# ~/.config/camera-stream/location.env (see config/location.env.example).
# Overridden live from the web UI, which persists to LOCATION_STATE_FILE and
# takes precedence once set - same relationship ROTATED has with rotate_state.
WEATHER_LAT_DEFAULT = float(os.environ.get("WEATHER_LAT", "0"))
WEATHER_LON_DEFAULT = float(os.environ.get("WEATHER_LON", "0"))
WEATHER_REFRESH_SECONDS = 15 * 60


def weather_url(lat, lon):
    return (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,weather_code"
        "&temperature_unit=fahrenheit&timezone=auto"
    )
WEATHER_CODES = {
    0: "Clear", 1: "Mostly Clear", 2: "Partly Cloudy", 3: "Overcast",
    45: "Fog", 48: "Fog",
    51: "Light Drizzle", 53: "Drizzle", 55: "Heavy Drizzle",
    56: "Freezing Drizzle", 57: "Freezing Drizzle",
    61: "Light Rain", 63: "Rain", 65: "Heavy Rain",
    66: "Freezing Rain", 67: "Freezing Rain",
    71: "Light Snow", 73: "Snow", 75: "Heavy Snow", 77: "Snow Grains",
    80: "Rain Showers", 81: "Rain Showers", 82: "Violent Showers",
    85: "Snow Showers", 86: "Snow Showers",
    95: "Thunderstorm", 96: "Thunderstorm+Hail", 99: "Thunderstorm+Hail",
}

SERVER_START = time.time()

PAGE = """\
<html>
<head>
<title>Pi Camera Live Feed</title>
<style>
  body { margin:0; background:#111; color:#eee; font-family:sans-serif; text-align:center; }
  h2 { margin: 10px 0 2px 0; }
  #subtitle { margin: 0 0 10px 0; font-size: 12px; color: #888; }
  #layout { display:flex; align-items:flex-start; justify-content:center; gap:16px; flex-wrap:wrap; padding: 0 12px; }
  #stream-wrap { position:relative; max-width:100%; flex:1 1 480px; min-width:0; }
  img#stream { max-width:100%; width:100%; height:auto; display:block; }
  #panel { width:320px; flex:0 0 320px; margin: 0 0 16px 0; padding: 12px 16px; background:#1c1c1c; border-radius:8px; text-align:left; }
  .row { display:flex; align-items:center; gap:10px; margin:10px 0; }
  .row label { width:70px; flex-shrink:0; font-size:14px; }
  .row input[type=range] { flex:1; min-width:0; }
  .row input[type=number] { flex:1; min-width:0; background:#2a2a2a; color:#eee; border:1px solid #444; border-radius:4px; padding:5px 8px; font-size:13px; }
  .val { width:52px; text-align:right; font-variant-numeric:tabular-nums; font-size:13px; color:#9cf; flex-shrink:0; }
  button { background:#2a6; color:#fff; border:none; padding:8px 14px; border-radius:5px; cursor:pointer; font-size:14px; }
  button:hover { background:#3b7; }
  button.secondary { background:#444; }
  button.secondary:hover { background:#555; }
  #rotate-btn.active { background:#3987e5; }
  #rotate-btn.active:hover { background:#4f97e8; }
  #continuous-btn.active { background:#3987e5; }
  #continuous-btn.active:hover { background:#4f97e8; }
  #drive-upload-btn.active { background:#3987e5; }
  #drive-upload-btn.active:hover { background:#4f97e8; }
  button:disabled { background:#333; color:#777; cursor:default; }
  .btn-row { display:flex; gap:8px; flex-wrap:wrap; margin:10px 0; }
  #capture-btn, #capture-full-btn { font-size:15px; padding:10px 14px; flex:1; }
  #video-controls {
    position:absolute; bottom:44px; left:50%; transform:translateX(-50%); z-index:10;
    display:flex; align-items:center; gap:12px; flex-wrap:wrap; justify-content:center;
    background:rgba(0,0,0,0.55); border-radius:10px; padding:8px 12px 8px 6px; max-width:92%;
  }
  #video-controls.dragging { opacity:0.85; }
  #video-controls-handle {
    cursor:grab; color:rgba(255,255,255,0.55); font-size:16px; padding:4px 6px;
    user-select:none; touch-action:none; line-height:1; align-self:stretch;
    display:flex; align-items:center;
  }
  #video-controls-handle:hover { color:rgba(255,255,255,0.85); }
  #video-controls.dragging #video-controls-handle { cursor:grabbing; }
  #video-zoom-row { display:flex; align-items:center; gap:8px; }
  #video-zoom-row input[type=range] { width:110px; }
  #video-zoom-row .val { color:#9cf; font-size:12px; width:auto; }
  #video-zoom-row button { padding:5px 10px; font-size:12px; background:rgba(255,255,255,0.15); }
  #video-zoom-row button:hover { background:rgba(255,255,255,0.28); }
  @media (max-width: 700px) {
    #panel { width:auto; max-width:640px; flex:1 1 100%; }
  }
  @media (min-width: 701px) {
    /* Keep the whole page fitting in one viewport on desktop: the panel's control
       list scrolls within its own box instead of growing the page taller than the
       screen, so the video stays visible without having to scroll past it. */
    #panel { max-height: calc(100vh - 90px); overflow-y: auto; margin-bottom: 0; }
    body { margin-bottom: 0; }
  }
  #toast { position:fixed; bottom:20px; left:50%; transform:translateX(-50%); background:#2a6; color:#fff;
           padding:10px 18px; border-radius:6px; opacity:0; transition:opacity .3s; pointer-events:none; z-index:300; }
  #toast.show { opacity:1; }
  .disabled { opacity:0.4; pointer-events:none; }
  .dimmed { opacity:0.4; }
  #dpad { display:grid; grid-template-columns: 28px 28px 28px; grid-template-rows: 28px 28px 28px; gap:3px; }
  #dpad.disabled { opacity:0.35; pointer-events:none; }
  .dpad-btn {
    width:28px; height:28px; padding:0; font-size:12px; line-height:28px;
    background:rgba(255,255,255,0.15); color:#fff;
  }
  .dpad-btn:hover { background:rgba(255,255,255,0.28); }
  .dpad-up { grid-column:2; grid-row:1; }
  .dpad-left { grid-column:1; grid-row:2; }
  .dpad-center { grid-column:2; grid-row:2; background:rgba(255,255,255,0.28); }
  .dpad-center:hover { background:rgba(255,255,255,0.4); }
  .dpad-right { grid-column:3; grid-row:2; }
  .dpad-down { grid-column:2; grid-row:3; }
  #stat-grid { display:grid; grid-template-columns: 1fr 1fr; gap:8px; margin-bottom:6px; transition:opacity .2s; }
  #stat-grid.stale { opacity:0.45; }
  #stats-status { display:none; font-size:11px; color:#fab219; margin-bottom:14px; }
  #stats-status.show { display:block; }
  .stat-tile { background:#262625; border-radius:6px; padding:8px 10px; }
  .stat-tile-wide { grid-column: 1 / -1; }
  .stat-label { font-size:11px; color:#a6a39b; text-transform:uppercase; letter-spacing:.04em; margin-bottom:3px; }
  .stat-value { font-size:19px; font-weight:600; color:#eee; font-variant-numeric:tabular-nums; }
  .stat-detail { font-size:11px; color:#a6a39b; margin-top:2px; }
  .stat-good { color:#0ca30c; }
  .stat-warning { color:#fab219; }
  .stat-serious { color:#ec835a; }
  .stat-critical { color:#d03b3b; }
  .stat-muted { color:#a6a39b; }
  .stat-active { color:#3987e5; }
  #temp-spark { display:block; margin-top:4px; }
  #live-badge {
    position:absolute; top:8px; right:8px; z-index:10;
    background:rgba(0,0,0,0.6); font-size:11px; font-weight:700; letter-spacing:.05em;
    padding:4px 8px; border-radius:4px;
  }
  #live-badge.live-ok { color:#3ecf5e; }
  #live-badge.live-bad { color:#ec4747; }
  #rec-badge {
    position:absolute; top:8px; left:8px; z-index:10;
    background:rgba(0,0,0,0.6); font-size:11px; font-weight:700; letter-spacing:.05em;
    padding:4px 8px; border-radius:4px; color:#ec4747; display:none; align-items:center; gap:5px;
  }
  #rec-badge.show { display:flex; }
  #rec-badge .dot { width:8px; height:8px; border-radius:50%; background:#ec4747; animation: rec-pulse 1s infinite; }
  @keyframes rec-pulse { 0%,100% { opacity:1; } 50% { opacity:.25; } }
  #record-btn { flex:1; font-size:15px; font-weight:600; padding:10px 14px; letter-spacing:.02em; }
  #record-btn.recording { background:#a33; }
  #record-btn.recording:hover { background:#c44; }
  .gallery-tabs { display:flex; gap:6px; margin-bottom:12px; }
  .gallery-tab { background:#333; border:none; color:#ccc; padding:6px 14px; border-radius:5px; cursor:pointer; font-size:13px; }
  .gallery-tab.active { background:#2a6; color:#fff; }
  .chip-tabs { display:flex; gap:6px; }
  .chip-tab { background:#333; border:none; color:#ccc; padding:6px 14px; border-radius:5px; cursor:pointer; font-size:13px; }
  .chip-tab.active { background:#2a6; color:#fff; }
  .video-card video { width:100%; height:100px; object-fit:cover; display:block; background:#000; }
  .drive-card .drive-icon {
    height:100px; display:flex; align-items:center; justify-content:center;
    background:#1c1c1c; font-size:28px; color:#5eead4;
  }
  .drive-card .drive-folder-badge { font-size:10px; color:#5eead4; text-transform:uppercase; letter-spacing:0.05em; }
  #fullscreen-btn {
    position:absolute; bottom:8px; right:8px; z-index:10;
    background:rgba(0,0,0,0.55); font-size:16px; padding:6px 10px; line-height:1;
  }
  #fullscreen-btn:hover { background:rgba(0,0,0,0.75); }
  #stream-wrap:fullscreen { display:flex; align-items:center; justify-content:center; background:#000; }
  #stream-wrap:fullscreen img#stream { width:auto; height:100vh; max-width:100vw; object-fit:contain; }
  #gallery-overlay {
    display:none; position:fixed; inset:0; background:rgba(0,0,0,0.75);
    z-index:100; align-items:center; justify-content:center; padding:24px;
  }
  #gallery-overlay.show { display:flex; }
  #gallery-modal {
    background:#1c1c1c; border-radius:8px; padding:16px 20px; width:100%;
    max-width:900px; max-height:85vh; display:flex; flex-direction:column;
  }
  #gallery-header { display:flex; align-items:center; justify-content:space-between; margin-bottom:12px; }
  #gallery-header h3 { margin:0; font-size:16px; }
  #gallery-close { background:#444; }
  #gallery-close:hover { background:#555; }
  #gallery-bulk-bar { display:flex; align-items:center; gap:12px; margin-bottom:10px; font-size:13px; color:#c3c2b7; }
  #gallery-bulk-bar label { display:flex; align-items:center; gap:6px; cursor:pointer; }
  #delete-selected-btn { background:#a33; margin-left:auto; padding:6px 14px; font-size:13px; }
  #delete-selected-btn:hover { background:#c44; }
  #delete-selected-btn:disabled { background:#333; color:#777; cursor:default; }
  #gallery-grid {
    overflow-y:auto; display:grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
    grid-auto-rows: min-content; gap:12px; padding-right:4px;
  }
  .photo-card { position:relative; background:#262625; border-radius:6px; overflow:hidden; text-align:center; }
  .card-select {
    position:absolute; top:6px; left:6px; z-index:5; width:18px; height:18px;
    cursor:pointer; accent-color:#2a6;
  }
  .photo-card img { width:100%; height:100px; object-fit:cover; display:block; cursor:pointer; }
  .photo-meta { padding:6px 8px; font-size:11px; color:#c3c2b7; }
  .photo-meta .photo-size { color:#a6a39b; }
  .photo-actions { display:flex; gap:6px; padding:0 8px 8px; }
  .photo-actions a, .photo-actions button {
    flex:1; color:#fff; text-decoration:none; font-size:12px;
    padding:5px 0; border-radius:4px; border:none; cursor:pointer;
  }
  .photo-actions a { background:#2a6; }
  .photo-actions a:hover { background:#3b7; }
  .photo-actions button.photo-delete { background:#a33; }
  .photo-actions button.photo-delete:hover { background:#c44; }
  #gallery-empty { color:#a6a39b; padding:20px; text-align:center; }
  #lightbox-overlay {
    display:none; position:fixed; inset:0; background:rgba(0,0,0,0.92);
    z-index:200; align-items:center; justify-content:center; flex-direction:column;
  }
  #lightbox-overlay.show { display:flex; }
  #lightbox-img { max-width:90vw; max-height:80vh; object-fit:contain; }
  #lightbox-caption { color:#c3c2b7; font-size:13px; margin-top:10px; }
  #lightbox-close {
    position:absolute; top:16px; right:20px; font-size:24px; background:none;
    border:none; color:#eee; cursor:pointer; padding:4px 10px;
  }
  #lightbox-prev, #lightbox-next {
    position:absolute; top:50%; transform:translateY(-50%); background:rgba(255,255,255,0.12);
    font-size:18px; width:44px; height:44px; border-radius:50%; padding:0;
  }
  #lightbox-prev:hover, #lightbox-next:hover, #lightbox-close:hover { background:rgba(255,255,255,0.22); }
  #lightbox-prev { left:20px; }
  #lightbox-next { right:20px; }
</style>
</head>
<body>
<h2>Pi Camera Live Feed</h2>
<div id="subtitle"></div>
<div id="layout">
<div id="stream-wrap">
  <img id="stream" src="stream.mjpg" />
  <div id="live-badge" class="live-ok">LIVE</div>
  <div id="rec-badge"><span class="dot"></span><span id="rec-time">REC</span></div>
  <button id="fullscreen-btn" aria-label="Toggle fullscreen">&#9974;</button>

  <div id="video-controls">
    <div id="video-controls-handle" aria-label="Drag to move controls" title="Drag to move">⠿</div>
    <div id="dpad">
      <button id="pan-up" class="dpad-btn dpad-up" aria-label="Pan up">&#9650;</button>
      <button id="pan-left" class="dpad-btn dpad-left" aria-label="Pan left">&#9664;</button>
      <button id="pan-center" class="dpad-btn dpad-center" aria-label="Reset pan">&#8226;</button>
      <button id="pan-right" class="dpad-btn dpad-right" aria-label="Pan right">&#9654;</button>
      <button id="pan-down" class="dpad-btn dpad-down" aria-label="Pan down">&#9660;</button>
    </div>
    <div id="video-zoom-row">
      <input type="range" id="zoom" min="1" max="8" step="0.1" value="1">
      <span class="val" id="zoom-val">1.0x</span>
      <button id="zoom-reset">Reset</button>
    </div>
  </div>
</div>

<div id="panel">
  <div id="stat-grid">
    <div class="stat-tile">
      <div class="stat-label">CPU Temp</div>
      <div class="stat-value stat-muted" id="stat-temp">--</div>
      <canvas id="temp-spark" width="70" height="20"></canvas>
    </div>
    <div class="stat-tile">
      <div class="stat-label">Fan</div>
      <div class="stat-value stat-muted" id="stat-fan">--</div>
    </div>
    <div class="stat-tile">
      <div class="stat-label">CPU Usage</div>
      <div class="stat-value stat-muted" id="stat-cpu">--</div>
    </div>
    <div class="stat-tile">
      <div class="stat-label">RAM Usage</div>
      <div class="stat-value stat-muted" id="stat-ram">--</div>
      <div class="stat-detail" id="stat-ram-detail"></div>
    </div>
    <div class="stat-tile">
      <div class="stat-label">Storage</div>
      <div class="stat-value stat-muted" id="stat-disk">--</div>
      <div class="stat-detail" id="stat-disk-detail"></div>
    </div>
    <div class="stat-tile">
      <div class="stat-label">Uptime</div>
      <div class="stat-value stat-active" id="stat-uptime">--</div>
    </div>
    <div class="stat-tile stat-tile-wide">
      <div class="stat-label">Rolling Buffer</div>
      <div class="stat-value stat-muted" id="stat-buffer">--</div>
      <div class="stat-detail" id="stat-buffer-detail"></div>
    </div>
  </div>
  <div id="stats-status">Stats offline - retrying...</div>

  <div class="btn-row">
    <button id="capture-btn">Take Photo</button>
    <button id="capture-full-btn" class="secondary">Full-Res Photo</button>
    <span id="capture-status" class="val" style="width:auto;"></span>
  </div>
  <div class="btn-row">
    <button id="record-btn">&#9679; Start Recording (4K)</button>
    <span id="record-status" class="val" style="width:auto;"></span>
  </div>
  <div class="btn-row">
    <button id="gallery-btn" class="secondary" style="width:100%;">View Photos &amp; Recordings</button>
  </div>

  <div class="btn-row">
    <button id="rotate-btn" class="secondary" style="width:100%;">Rotate 180&deg;</button>
  </div>

  <div class="btn-row">
    <button id="drive-upload-btn" class="secondary" style="width:100%;">Google Drive: Loading&hellip;</button>
  </div>

  <div class="btn-row">
    <button id="continuous-btn" class="secondary" style="width:100%;">Stop Continuous Recording</button>
  </div>

  <div class="btn-row">
    <button id="af-auto">Auto Focus</button>
    <button id="af-trigger" class="secondary">Focus Once</button>
    <span class="val" id="focus-mode-val" style="width:auto;">auto</span>
  </div>
  <div class="row" id="focus-row">
    <label for="focus">Focus</label>
    <input type="range" id="focus" min="0" max="15" step="0.1" value="1">
    <span class="val" id="focus-val">1.0</span>
  </div>

  <div class="row">
    <label>AF Range</label>
    <div class="chip-tabs" id="af-range-tabs">
      <button class="chip-tab" data-range="normal" type="button">Normal</button>
      <button class="chip-tab" data-range="macro" type="button">Macro</button>
      <button class="chip-tab" data-range="full" type="button">Full</button>
    </div>
  </div>

  <div class="row">
    <label for="brightness">Brightness</label>
    <input type="range" id="brightness" min="-1" max="1" step="0.05" value="0">
    <span class="val" id="brightness-val">0.0</span>
  </div>

  <div class="row">
    <label for="contrast">Contrast</label>
    <input type="range" id="contrast" min="0" max="2" step="0.05" value="1">
    <span class="val" id="contrast-val">1.0</span>
  </div>

  <div class="row">
    <label for="saturation">Saturation</label>
    <input type="range" id="saturation" min="0" max="2" step="0.05" value="1">
    <span class="val" id="saturation-val">1.0</span>
  </div>

  <div class="btn-row">
    <button id="reset-image" class="secondary" style="width:100%;">Reset Image (Brightness/Contrast/Saturation)</button>
  </div>

  <div class="row">
    <label for="weather-lat">Lat</label>
    <input type="number" id="weather-lat" step="0.0001" min="-90" max="90" value="0">
    <label for="weather-lon" style="width:auto;margin-left:4px;">Lon</label>
    <input type="number" id="weather-lon" step="0.0001" min="-180" max="180" value="0">
  </div>
  <div class="btn-row">
    <button id="set-location-btn" class="secondary" style="width:100%;">Set Weather Location</button>
  </div>
</div>
</div>

<div id="toast"></div>

<div id="gallery-overlay">
  <div id="gallery-modal">
    <div id="gallery-header">
      <h3 id="gallery-title">Captured Photos</h3>
      <button id="gallery-close">Close</button>
    </div>
    <div class="gallery-tabs">
      <button class="gallery-tab active" id="tab-photos" type="button">Photos</button>
      <button class="gallery-tab" id="tab-recordings" type="button">Recordings</button>
      <button class="gallery-tab" id="tab-continuous" type="button">Continuous</button>
      <button class="gallery-tab" id="tab-drive" type="button">Drive Backups</button>
    </div>
    <div id="gallery-bulk-bar">
      <label><input type="checkbox" id="select-all-checkbox"> Select all</label>
      <span id="selected-count">0 selected</span>
      <button id="delete-selected-btn" type="button" disabled>Delete Selected</button>
    </div>
    <div id="gallery-grid"></div>
  </div>
</div>

<div id="lightbox-overlay">
  <button id="lightbox-close" aria-label="Close preview">&times;</button>
  <button id="lightbox-prev" aria-label="Previous photo">&#9664;</button>
  <img id="lightbox-img" src="" alt="">
  <button id="lightbox-next" aria-label="Next photo">&#9654;</button>
  <div id="lightbox-caption"></div>
</div>

<script>
function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove('show'), 2500);
}

function debounce(fn, ms) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}

async function postAction(path) {
  const res = await fetch(path, { method: 'POST' });
  const data = await res.json();
  if (!data.ok) throw new Error(data.error || 'request failed');
  applyState(data);
  return data;
}

let rotatedState = false;
let continuousEnabled = true;
let driveUploadEnabled = true;

function applyState(s) {
  document.getElementById('subtitle').textContent =
    `${s.sensor_model} · ${s.full_res_size[0]}×${s.full_res_size[1]} native · records up to ${s.record_size[0]}×${s.record_size[1]}`;

  rotatedState = s.rotated;
  const rotateBtn = document.getElementById('rotate-btn');
  rotateBtn.classList.toggle('active', s.rotated);
  rotateBtn.textContent = s.rotated ? 'Rotated 180° (on)' : 'Rotate 180°';

  const zoom = document.getElementById('zoom');
  zoom.max = s.zoom_max; zoom.value = s.zoom;
  document.getElementById('zoom-val').textContent = s.zoom.toFixed(1) + 'x';
  document.getElementById('dpad').classList.toggle('disabled', s.zoom <= 1.0);

  const focus = document.getElementById('focus');
  focus.min = s.lens_min; focus.max = s.lens_max;
  if (s.lens_position !== null) focus.value = s.lens_position;
  document.getElementById('focus-val').textContent = Number(focus.value).toFixed(1);
  document.getElementById('focus-mode-val').textContent = s.af_mode;
  document.getElementById('focus-row').classList.toggle('dimmed', s.af_mode !== 'manual');

  document.querySelectorAll('#af-range-tabs .chip-tab').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.range === s.af_range);
  });

  const brightness = document.getElementById('brightness');
  brightness.min = s.brightness_min; brightness.max = s.brightness_max; brightness.value = s.brightness;
  document.getElementById('brightness-val').textContent = s.brightness.toFixed(2);

  const contrast = document.getElementById('contrast');
  contrast.value = s.contrast;
  document.getElementById('contrast-val').textContent = s.contrast.toFixed(2);

  const saturation = document.getElementById('saturation');
  saturation.value = s.saturation;
  document.getElementById('saturation-val').textContent = s.saturation.toFixed(2);

  if (document.activeElement.id !== 'weather-lat') document.getElementById('weather-lat').value = s.weather_lat;
  if (document.activeElement.id !== 'weather-lon') document.getElementById('weather-lon').value = s.weather_lon;
}

document.getElementById('capture-btn').addEventListener('click', async () => {
  const status = document.getElementById('capture-status');
  status.textContent = 'saving...';
  try {
    const res = await fetch('/api/capture', { method: 'POST' });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || 'capture failed');
    status.textContent = 'saved ' + data.file;
    toast('Photo saved: ' + data.file);
    if (galleryOverlay.classList.contains('show')) loadGallery();
  } catch (e) {
    status.textContent = 'error';
    toast('Capture failed: ' + e.message);
  }
});

document.getElementById('capture-full-btn').addEventListener('click', async () => {
  const status = document.getElementById('capture-status');
  const btn = document.getElementById('capture-full-btn');
  btn.disabled = true;
  status.textContent = 'focusing + capturing full-res (live view will pause ~2-4s)...';
  try {
    const res = await fetch('/api/capture/full', { method: 'POST' });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || 'capture failed');
    status.textContent = 'saved ' + data.file;
    toast('Full-res photo saved: ' + data.file);
    if (galleryOverlay.classList.contains('show')) loadGallery();
  } catch (e) {
    status.textContent = 'error';
    toast('Full-res capture failed: ' + e.message);
  } finally {
    btn.disabled = false;
  }
});

let recordingActive = false;

document.getElementById('record-btn').addEventListener('click', async () => {
  const btn = document.getElementById('record-btn');
  const status = document.getElementById('record-status');
  btn.disabled = true;
  try {
    if (!recordingActive) {
      const res = await fetch('/api/record/start', { method: 'POST' });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || 'failed to start recording');
      toast('Recording started');
    } else {
      const res = await fetch('/api/record/stop', { method: 'POST' });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || 'failed to stop recording');
      toast('Recording saved: ' + data.file);
      status.textContent = '';
      if (galleryOverlay.classList.contains('show') && currentGalleryTab === 'recordings') loadGallery();
    }
  } catch (e) {
    toast('Error: ' + e.message);
  } finally {
    btn.disabled = false;
  }
});

function formatDuration(sec) {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}

function updateRecordingUI(rec) {
  recordingActive = rec.active;
  const btn = document.getElementById('record-btn');
  const badge = document.getElementById('rec-badge');
  const status = document.getElementById('record-status');
  if (rec.active) {
    btn.innerHTML = '&#9632; Stop Recording (4K)';
    btn.classList.add('recording');
    badge.classList.add('show');
    document.getElementById('rec-time').textContent = 'REC 4K ' + formatDuration(rec.elapsed_seconds || 0);
    status.textContent = formatDuration(rec.elapsed_seconds || 0);
  } else {
    btn.innerHTML = '&#9679; Start Recording (4K)';
    btn.classList.remove('recording');
    badge.classList.remove('show');
  }
}

document.getElementById('reset-image').addEventListener('click', () => {
  postAction('/api/reset-image').then(() => toast('Image settings reset')).catch(e => toast(e.message));
});

document.getElementById('set-location-btn').addEventListener('click', () => {
  const lat = document.getElementById('weather-lat').value;
  const lon = document.getElementById('weather-lon').value;
  postAction(`/api/location?lat=${lat}&lon=${lon}`).then(() => toast('Weather location updated')).catch(e => toast(e.message));
});

document.getElementById('rotate-btn').addEventListener('click', async () => {
  const btn = document.getElementById('rotate-btn');
  btn.disabled = true;
  const next = !rotatedState;
  try {
    await postAction('/api/rotate?value=' + (next ? '1' : '0'));
    toast(next ? 'Rotated 180°' : 'Rotation off');
  } catch (e) {
    toast('Rotate failed: ' + e.message);
  } finally {
    btn.disabled = false;
  }
});

document.getElementById('continuous-btn').addEventListener('click', async () => {
  const btn = document.getElementById('continuous-btn');
  btn.disabled = true;
  const endpoint = continuousEnabled ? '/api/continuous/stop' : '/api/continuous/start';
  try {
    const res = await fetch(endpoint, { method: 'POST' });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || 'request failed');
    toast(data.enabled ? 'Continuous recording started' : 'Continuous recording stopped');
  } catch (e) {
    toast('Failed: ' + e.message);
  } finally {
    btn.disabled = false;
  }
});

document.getElementById('drive-upload-btn').addEventListener('click', async () => {
  const btn = document.getElementById('drive-upload-btn');
  btn.disabled = true;
  try {
    const res = await fetch('/api/drive/toggle?enabled=' + (driveUploadEnabled ? '0' : '1'), { method: 'POST' });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || 'request failed');
    toast(data.enabled ? 'Google Drive uploads resumed' : 'Google Drive uploads paused');
  } catch (e) {
    toast('Failed: ' + e.message);
  } finally {
    btn.disabled = false;
  }
});

const zoomEl = document.getElementById('zoom');
const zoomSend = debounce(v => postAction('/api/zoom?value=' + v).catch(e => toast(e.message)), 150);
zoomEl.addEventListener('input', () => {
  document.getElementById('zoom-val').textContent = Number(zoomEl.value).toFixed(1) + 'x';
  zoomSend(zoomEl.value);
});
document.getElementById('zoom-reset').addEventListener('click', () => {
  postAction('/api/zoom?value=1.0').catch(e => toast(e.message));
});

function pan(dx, dy) {
  postAction(`/api/pan?dx=${dx}&dy=${dy}`).catch(e => toast(e.message));
}
document.getElementById('pan-up').addEventListener('click', () => pan(0, -0.15));
document.getElementById('pan-down').addEventListener('click', () => pan(0, 0.15));
document.getElementById('pan-left').addEventListener('click', () => pan(-0.15, 0));
document.getElementById('pan-right').addEventListener('click', () => pan(0.15, 0));
document.getElementById('pan-center').addEventListener('click', () => {
  postAction('/api/pan?reset=1').catch(e => toast(e.message));
});

document.getElementById('af-auto').addEventListener('click', () => {
  postAction('/api/focus?mode=auto').catch(e => toast(e.message));
});
document.getElementById('af-trigger').addEventListener('click', async () => {
  const label = document.getElementById('focus-mode-val');
  label.textContent = 'focusing...';
  try {
    await postAction('/api/focus?mode=trigger');
  } catch (e) {
    toast(e.message);
  }
  setTimeout(() => {
    fetch('/api/state').then(r => r.json()).then(applyState).catch(() => {});
  }, 1200);
});
document.querySelectorAll('#af-range-tabs .chip-tab').forEach(btn => {
  btn.addEventListener('click', () => {
    postAction('/api/af-range?value=' + btn.dataset.range).catch(e => toast(e.message));
  });
});
const focusEl = document.getElementById('focus');
const focusSend = debounce(v => postAction('/api/focus?mode=manual&value=' + v).catch(e => toast(e.message)), 150);
focusEl.addEventListener('input', () => {
  document.getElementById('focus-val').textContent = Number(focusEl.value).toFixed(1);
  focusSend(focusEl.value);
});

const brightnessEl = document.getElementById('brightness');
const brightnessSend = debounce(v => postAction('/api/brightness?value=' + v).catch(e => toast(e.message)), 150);
brightnessEl.addEventListener('input', () => {
  document.getElementById('brightness-val').textContent = Number(brightnessEl.value).toFixed(2);
  brightnessSend(brightnessEl.value);
});

const contrastEl = document.getElementById('contrast');
const contrastSend = debounce(v => postAction('/api/contrast?value=' + v).catch(e => toast(e.message)), 150);
contrastEl.addEventListener('input', () => {
  document.getElementById('contrast-val').textContent = Number(contrastEl.value).toFixed(2);
  contrastSend(contrastEl.value);
});

const saturationEl = document.getElementById('saturation');
const saturationSend = debounce(v => postAction('/api/saturation?value=' + v).catch(e => toast(e.message)), 150);
saturationEl.addEventListener('input', () => {
  document.getElementById('saturation-val').textContent = Number(saturationEl.value).toFixed(2);
  saturationSend(saturationEl.value);
});

fetch('/api/state').then(r => r.json()).then(applyState).catch(() => {});

function tempClass(c) {
  if (c == null) return 'stat-muted';
  if (c < 60) return 'stat-good';
  if (c < 70) return 'stat-warning';
  if (c < 80) return 'stat-serious';
  return 'stat-critical';
}

function pctClass(p) {
  if (p == null) return 'stat-muted';
  if (p < 50) return 'stat-good';
  if (p < 75) return 'stat-warning';
  if (p < 90) return 'stat-serious';
  return 'stat-critical';
}

const tempHistory = [];
function drawSparkline(canvas, data, color) {
  const ctx = canvas.getContext('2d');
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  if (data.length < 2) return;
  const min = Math.min(...data), max = Math.max(...data);
  const range = (max - min) || 1;
  ctx.beginPath();
  data.forEach((v, i) => {
    const x = (i / (data.length - 1)) * (w - 2) + 1;
    const y = h - 1 - ((v - min) / range) * (h - 2);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.stroke();
}

function updateLiveBadge(ageSeconds) {
  const badge = document.getElementById('live-badge');
  if (ageSeconds != null && ageSeconds > """ + str(STALE_FRAME_SECONDS) + """) {
    badge.textContent = 'STALLED';
    badge.className = 'live-bad';
  } else {
    badge.textContent = 'LIVE';
    badge.className = 'live-ok';
  }
}

function updateSysInfo(s) {
  const tempEl = document.getElementById('stat-temp');
  tempEl.textContent = s.cpu_temp_c != null ? s.cpu_temp_c.toFixed(1) + '°C' : 'n/a';
  tempEl.className = 'stat-value ' + tempClass(s.cpu_temp_c);
  if (s.cpu_temp_c != null) {
    tempHistory.push(s.cpu_temp_c);
    if (tempHistory.length > 30) tempHistory.shift();
    drawSparkline(document.getElementById('temp-spark'), tempHistory, '#3987e5');
  }

  const fanEl = document.getElementById('stat-fan');
  if (s.fan.on === null) {
    fanEl.textContent = 'n/a';
    fanEl.className = 'stat-value stat-muted';
  } else if (s.fan.on) {
    fanEl.textContent = `ON (${s.fan.level}/${s.fan.max_level})`;
    fanEl.className = 'stat-value stat-active';
  } else {
    fanEl.textContent = 'OFF';
    fanEl.className = 'stat-value stat-muted';
  }

  const cpuEl = document.getElementById('stat-cpu');
  cpuEl.textContent = s.cpu_percent.toFixed(0) + '%';
  cpuEl.className = 'stat-value ' + pctClass(s.cpu_percent);

  const ramEl = document.getElementById('stat-ram');
  ramEl.textContent = s.ram_percent.toFixed(0) + '%';
  ramEl.className = 'stat-value ' + pctClass(s.ram_percent);
  document.getElementById('stat-ram-detail').textContent =
    `${s.ram_used_mb} / ${s.ram_total_mb} MB`;

  const diskEl = document.getElementById('stat-disk');
  diskEl.textContent = s.disk_percent.toFixed(0) + '%';
  diskEl.className = 'stat-value ' + pctClass(s.disk_percent);
  document.getElementById('stat-disk-detail').textContent =
    `${s.disk_used_gb} / ${s.disk_total_gb} GB`;

  document.getElementById('stat-uptime').textContent = formatUptime(s.uptime_seconds);

  const bufferEl = document.getElementById('stat-buffer');
  const bufferDetail = document.getElementById('stat-buffer-detail');
  const continuousBtn = document.getElementById('continuous-btn');
  continuousEnabled = s.continuous.enabled;
  if (s.continuous.active) {
    bufferEl.textContent = 'ON';
    bufferEl.className = 'stat-value stat-good';
    const coverageHours = (s.continuous.chunk_count * 10 / 60).toFixed(1);
    const chunkLabel = s.continuous.chunk_count === 1 ? 'chunk' : 'chunks';
    bufferDetail.textContent = `${s.continuous.chunk_count} ${chunkLabel} · ~${coverageHours}h of ${s.continuous.retention_hours}h`;
  } else {
    bufferEl.textContent = 'OFF';
    bufferEl.className = 'stat-value stat-muted';
    bufferDetail.textContent = '';
  }
  continuousBtn.classList.toggle('active', s.continuous.enabled);
  continuousBtn.textContent = s.continuous.enabled ? 'Stop Continuous Recording' : 'Start Continuous Recording';

  const driveBtn = document.getElementById('drive-upload-btn');
  driveUploadEnabled = s.drive.enabled;
  if (!s.drive.configured) {
    driveBtn.disabled = true;
    driveBtn.classList.remove('active');
    driveBtn.textContent = 'Google Drive: Not Configured';
  } else {
    driveBtn.disabled = false;
    driveBtn.classList.toggle('active', s.drive.enabled);
    const queueNote = s.drive.queue_size > 0 ? ` (${s.drive.queue_size} queued)` : '';
    driveBtn.textContent = (s.drive.enabled ? 'Pause Google Drive Uploads' : 'Resume Google Drive Uploads') + queueNote;
  }

  updateLiveBadge(s.frame_age_seconds);
  updateRecordingUI(s.recording);
}

function formatUptime(sec) {
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

let missedPolls = 0;
function pollSysInfo() {
  fetch('/api/sysinfo').then(r => r.json()).then(s => {
    missedPolls = 0;
    document.getElementById('stat-grid').classList.remove('stale');
    document.getElementById('stats-status').classList.remove('show');
    updateSysInfo(s);
  }).catch(() => {
    missedPolls++;
    if (missedPolls >= 2) {
      document.getElementById('stat-grid').classList.add('stale');
      document.getElementById('stats-status').classList.add('show');
    }
  });
}
pollSysInfo();
setInterval(pollSysInfo, 2000);

document.getElementById('fullscreen-btn').addEventListener('click', () => {
  const wrap = document.getElementById('stream-wrap');
  if (!document.fullscreenElement) {
    wrap.requestFullscreen().catch(e => toast('Fullscreen failed: ' + e.message));
  } else {
    document.exitFullscreen();
  }
});

(() => {
  const streamWrap = document.getElementById('stream-wrap');
  const videoControls = document.getElementById('video-controls');
  const handle = document.getElementById('video-controls-handle');
  let dragging = false;
  let offsetX = 0, offsetY = 0;

  function clamp(left, top) {
    const wrapRect = streamWrap.getBoundingClientRect();
    const barRect = videoControls.getBoundingClientRect();
    const maxLeft = Math.max(0, wrapRect.width - barRect.width);
    const maxTop = Math.max(0, wrapRect.height - barRect.height);
    return { left: Math.min(Math.max(left, 0), maxLeft), top: Math.min(Math.max(top, 0), maxTop) };
  }

  function pinToPixelPosition() {
    const wrapRect = streamWrap.getBoundingClientRect();
    const barRect = videoControls.getBoundingClientRect();
    videoControls.style.left = (barRect.left - wrapRect.left) + 'px';
    videoControls.style.top = (barRect.top - wrapRect.top) + 'px';
    videoControls.style.bottom = 'auto';
    videoControls.style.transform = 'none';
  }

  function savePosition() {
    try {
      localStorage.setItem('videoControlsPos', JSON.stringify({
        left: videoControls.style.left, top: videoControls.style.top,
      }));
    } catch (e) { /* localStorage unavailable, ignore */ }
  }

  function restorePosition() {
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem('videoControlsPos')); } catch (e) { /* ignore */ }
    if (!saved || !saved.left || !saved.top) return;
    videoControls.style.left = saved.left;
    videoControls.style.top = saved.top;
    videoControls.style.bottom = 'auto';
    videoControls.style.transform = 'none';
    const clamped = clamp(parseFloat(saved.left), parseFloat(saved.top));
    videoControls.style.left = clamped.left + 'px';
    videoControls.style.top = clamped.top + 'px';
  }

  handle.addEventListener('pointerdown', (e) => {
    dragging = true;
    handle.setPointerCapture(e.pointerId);
    pinToPixelPosition();
    const barRect = videoControls.getBoundingClientRect();
    offsetX = e.clientX - barRect.left;
    offsetY = e.clientY - barRect.top;
    videoControls.classList.add('dragging');
    e.preventDefault();
  });

  handle.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    const wrapRect = streamWrap.getBoundingClientRect();
    const clamped = clamp(e.clientX - wrapRect.left - offsetX, e.clientY - wrapRect.top - offsetY);
    videoControls.style.left = clamped.left + 'px';
    videoControls.style.top = clamped.top + 'px';
  });

  function endDrag(e) {
    if (!dragging) return;
    dragging = false;
    videoControls.classList.remove('dragging');
    savePosition();
  }
  handle.addEventListener('pointerup', endDrag);
  handle.addEventListener('pointercancel', endDrag);

  window.addEventListener('resize', () => {
    if (videoControls.style.left && videoControls.style.bottom === 'auto') {
      const clamped = clamp(parseFloat(videoControls.style.left), parseFloat(videoControls.style.top));
      videoControls.style.left = clamped.left + 'px';
      videoControls.style.top = clamped.top + 'px';
    }
  });
  document.addEventListener('fullscreenchange', () => {
    if (videoControls.style.left && videoControls.style.bottom === 'auto') {
      setTimeout(() => {
        const clamped = clamp(parseFloat(videoControls.style.left), parseFloat(videoControls.style.top));
        videoControls.style.left = clamped.left + 'px';
        videoControls.style.top = clamped.top + 'px';
      }, 50);
    }
  });

  restorePosition();
})();

function formatSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

function escapeHtml(s) {
  const div = document.createElement('div');
  div.textContent = String(s);
  return div.innerHTML;
}

let galleryFiles = [];
let lightboxIndex = -1;

function openLightbox(i) {
  lightboxIndex = i;
  const f = galleryFiles[i];
  document.getElementById('lightbox-img').src = 'snapshots/' + encodeURIComponent(f.name);
  document.getElementById('lightbox-caption').textContent =
    `${f.time_str} · ${formatSize(f.size)} (${i + 1} / ${galleryFiles.length})`;
  document.getElementById('lightbox-overlay').classList.add('show');
}
function closeLightbox() {
  document.getElementById('lightbox-overlay').classList.remove('show');
  lightboxIndex = -1;
}
document.getElementById('lightbox-close').addEventListener('click', closeLightbox);
document.getElementById('lightbox-overlay').addEventListener('click', (e) => {
  if (e.target.id === 'lightbox-overlay') closeLightbox();
});
document.getElementById('lightbox-prev').addEventListener('click', () => {
  if (galleryFiles.length === 0) return;
  openLightbox((lightboxIndex - 1 + galleryFiles.length) % galleryFiles.length);
});
document.getElementById('lightbox-next').addEventListener('click', () => {
  if (galleryFiles.length === 0) return;
  openLightbox((lightboxIndex + 1) % galleryFiles.length);
});
document.addEventListener('keydown', (e) => {
  if (!document.getElementById('lightbox-overlay').classList.contains('show')) return;
  if (e.key === 'Escape') closeLightbox();
  if (e.key === 'ArrowLeft') document.getElementById('lightbox-prev').click();
  if (e.key === 'ArrowRight') document.getElementById('lightbox-next').click();
});

let selectedFiles = new Set();
let currentDeleteEndpoint = '/api/snapshots/delete';

function addCardSelect(card, name) {
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.className = 'card-select';
  cb.checked = selectedFiles.has(name);
  cb.addEventListener('click', (e) => e.stopPropagation());
  cb.addEventListener('change', () => {
    if (cb.checked) selectedFiles.add(name); else selectedFiles.delete(name);
    updateBulkBar();
  });
  card.appendChild(cb);
}

function updateBulkBar() {
  const count = selectedFiles.size;
  document.getElementById('selected-count').textContent = `${count} selected`;
  document.getElementById('delete-selected-btn').disabled = count === 0;
  const total = galleryFiles.length;
  const selectAll = document.getElementById('select-all-checkbox');
  selectAll.checked = total > 0 && count === total;
  selectAll.indeterminate = count > 0 && count < total;
}

function renderGallery(files) {
  galleryFiles = files;
  currentDeleteEndpoint = '/api/snapshots/delete';
  document.getElementById('gallery-title').textContent = `Captured Photos (${files.length})`;
  const grid = document.getElementById('gallery-grid');
  grid.innerHTML = '';
  if (files.length === 0) {
    grid.innerHTML = '<div id="gallery-empty">No photos captured yet.</div>';
    updateBulkBar();
    return;
  }
  files.forEach((f, i) => {
    const card = document.createElement('div');
    card.className = 'photo-card';
    const url = 'snapshots/' + encodeURIComponent(f.name);
    card.innerHTML = `
      <img src="${url}" loading="lazy" alt="${f.name}">
      <div class="photo-meta">${f.time_str}<br><span class="photo-size">${formatSize(f.size)}</span></div>
      <div class="photo-actions">
        <a href="${url}" download="${f.name}">Download</a>
        <button class="photo-delete" type="button">Delete</button>
      </div>
    `;
    card.querySelector('img').addEventListener('click', () => openLightbox(i));
    card.querySelector('.photo-delete').addEventListener('click', (e) => {
      e.stopPropagation();
      if (!confirm(`Delete ${f.name}?`)) return;
      fetch('/api/snapshots/delete?name=' + encodeURIComponent(f.name), { method: 'POST' })
        .then(r => r.json())
        .then(d => {
          if (!d.ok) throw new Error(d.error || 'delete failed');
          toast('Deleted');
          selectedFiles.delete(f.name);
          loadGallery();
        })
        .catch(e => toast('Delete failed: ' + e.message));
    });
    addCardSelect(card, f.name);
    grid.appendChild(card);
  });
  updateBulkBar();
}

function renderVideoList(files, folder, deleteEndpoint, emptyLabel) {
  galleryFiles = files;
  currentDeleteEndpoint = deleteEndpoint;
  document.getElementById('gallery-title').textContent = `${folder === 'recordings' ? 'Recordings' : 'Continuous Buffer'} (${files.length})`;
  const grid = document.getElementById('gallery-grid');
  grid.innerHTML = '';
  if (files.length === 0) {
    grid.innerHTML = `<div id="gallery-empty">${emptyLabel}</div>`;
    updateBulkBar();
    return;
  }
  files.forEach((f) => {
    const card = document.createElement('div');
    card.className = 'photo-card video-card';
    const url = folder + '/' + encodeURIComponent(f.name);
    card.innerHTML = `
      <video src="${url}" preload="metadata" controls></video>
      <div class="photo-meta">${f.time_str}<br><span class="photo-size">${formatSize(f.size)}</span></div>
      <div class="photo-actions">
        <a href="${url}" download="${f.name}">Download</a>
        <button class="photo-delete" type="button">Delete</button>
      </div>
    `;
    card.querySelector('.photo-delete').addEventListener('click', (e) => {
      e.stopPropagation();
      if (!confirm(`Delete ${f.name}?`)) return;
      fetch(deleteEndpoint + '?name=' + encodeURIComponent(f.name), { method: 'POST' })
        .then(r => r.json())
        .then(d => {
          if (!d.ok) throw new Error(d.error || 'delete failed');
          toast('Deleted');
          selectedFiles.delete(f.name);
          loadGallery();
        })
        .catch(e => toast('Delete failed: ' + e.message));
    });
    addCardSelect(card, f.name);
    grid.appendChild(card);
  });
  updateBulkBar();
}

function renderDriveList(files) {
  galleryFiles = files;
  currentDeleteEndpoint = '/api/drive/delete';
  document.getElementById('gallery-title').textContent = `Drive Backups (${files.length})`;
  const grid = document.getElementById('gallery-grid');
  grid.innerHTML = '';
  if (files.length === 0) {
    grid.innerHTML = '<div id="gallery-empty">No files backed up to Drive yet.</div>';
    updateBulkBar();
    return;
  }
  files.forEach((f) => {
    const card = document.createElement('div');
    card.className = 'photo-card drive-card';
    const created = f.createdTime ? new Date(f.createdTime).toLocaleString() : '';
    card.innerHTML = `
      <div class="drive-icon">&#9729;</div>
      <div class="photo-meta">
        <div class="drive-folder-badge">${escapeHtml(f.subfolder)}</div>
        ${escapeHtml(f.name)}<br>${created}<br><span class="photo-size">${formatSize(Number(f.size) || 0)}</span>
      </div>
      <div class="photo-actions">
        <a href="${f.webViewLink}" target="_blank" rel="noopener">View</a>
        <button class="photo-delete" type="button">Delete</button>
      </div>
    `;
    card.querySelector('.photo-delete').addEventListener('click', (e) => {
      e.stopPropagation();
      if (!confirm(`Delete ${f.name} from Google Drive? This cannot be undone.`)) return;
      fetch('/api/drive/delete?name=' + encodeURIComponent(f.id), { method: 'POST' })
        .then(r => r.json())
        .then(d => {
          if (!d.ok) throw new Error(d.error || 'delete failed');
          toast('Deleted from Drive');
          selectedFiles.delete(f.id);
          loadGallery();
        })
        .catch(e => toast('Delete failed: ' + e.message));
    });
    addCardSelect(card, f.id);
    grid.appendChild(card);
  });
  updateBulkBar();
}

let currentGalleryTab = 'photos';

function loadGallery() {
  document.getElementById('gallery-grid').innerHTML = '<div id="gallery-empty">Loading...</div>';
  if (currentGalleryTab === 'photos') {
    fetch('/api/snapshots').then(r => r.json()).then(data => renderGallery(data.files))
      .catch(() => { document.getElementById('gallery-grid').innerHTML = '<div id="gallery-empty">Failed to load photos.</div>'; });
  } else if (currentGalleryTab === 'recordings') {
    fetch('/api/recordings').then(r => r.json()).then(data => renderVideoList(data.files, 'recordings', '/api/recordings/delete', 'No recordings yet.'))
      .catch(() => { document.getElementById('gallery-grid').innerHTML = '<div id="gallery-empty">Failed to load recordings.</div>'; });
  } else if (currentGalleryTab === 'continuous') {
    fetch('/api/continuous').then(r => r.json()).then(data => renderVideoList(data.files, 'continuous', '/api/continuous/delete', 'No buffered footage yet.'))
      .catch(() => { document.getElementById('gallery-grid').innerHTML = '<div id="gallery-empty">Failed to load buffer.</div>'; });
  } else {
    fetch('/api/drive/files').then(r => r.json()).then(data => {
      if (data.error) throw new Error(data.error);
      renderDriveList(data.files);
    }).catch(e => { document.getElementById('gallery-grid').innerHTML = '<div id="gallery-empty">Failed to load Drive files: ' + e.message + '</div>'; });
  }
}

function switchGalleryTab(tab) {
  currentGalleryTab = tab;
  selectedFiles.clear();
  document.getElementById('tab-photos').classList.toggle('active', tab === 'photos');
  document.getElementById('tab-recordings').classList.toggle('active', tab === 'recordings');
  document.getElementById('tab-continuous').classList.toggle('active', tab === 'continuous');
  document.getElementById('tab-drive').classList.toggle('active', tab === 'drive');
  loadGallery();
}
document.getElementById('tab-photos').addEventListener('click', () => switchGalleryTab('photos'));
document.getElementById('tab-recordings').addEventListener('click', () => switchGalleryTab('recordings'));
document.getElementById('tab-continuous').addEventListener('click', () => switchGalleryTab('continuous'));
document.getElementById('tab-drive').addEventListener('click', () => switchGalleryTab('drive'));

document.getElementById('select-all-checkbox').addEventListener('change', (e) => {
  if (e.target.checked) {
    galleryFiles.forEach(f => selectedFiles.add(f.name));
  } else {
    selectedFiles.clear();
  }
  document.querySelectorAll('#gallery-grid .card-select').forEach(cb => { cb.checked = e.target.checked; });
  updateBulkBar();
});

document.getElementById('delete-selected-btn').addEventListener('click', async () => {
  const names = [...selectedFiles];
  if (names.length === 0) return;
  if (!confirm(`Delete ${names.length} selected file${names.length === 1 ? '' : 's'}? This cannot be undone.`)) return;
  const btn = document.getElementById('delete-selected-btn');
  btn.disabled = true;
  let succeeded = 0;
  let failed = 0;
  for (const name of names) {
    try {
      const res = await fetch(currentDeleteEndpoint + '?name=' + encodeURIComponent(name), { method: 'POST' });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || 'delete failed');
      selectedFiles.delete(name);
      succeeded++;
    } catch (e) {
      failed++;
    }
  }
  toast(failed === 0 ? `Deleted ${succeeded} file${succeeded === 1 ? '' : 's'}` : `Deleted ${succeeded}, ${failed} failed`);
  loadGallery();
});

const galleryOverlay = document.getElementById('gallery-overlay');
document.getElementById('gallery-btn').addEventListener('click', () => {
  galleryOverlay.classList.add('show');
  switchGalleryTab('photos');
});
document.getElementById('gallery-close').addEventListener('click', () => {
  galleryOverlay.classList.remove('show');
});
galleryOverlay.addEventListener('click', (e) => {
  if (e.target === galleryOverlay) galleryOverlay.classList.remove('show');
});
</script>
</body>
</html>
"""

try:
    TIMESTAMP_FONT = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 24)
except OSError:
    TIMESTAMP_FONT = ImageFont.load_default()

weather_lock = Lock()
weather_state = {"text": None, "updated": None}


def fetch_weather():
    lat, lon = current["weather_lat"], current["weather_lon"]
    with urllib.request.urlopen(weather_url(lat, lon), timeout=10) as resp:
        data = json.load(resp)
    payload_current = data["current"]
    temp = round(payload_current["temperature_2m"])
    condition = WEATHER_CODES.get(payload_current["weather_code"], "")
    text = f"{temp}°F {condition}".strip()
    with weather_lock:
        weather_state["text"] = text
        weather_state["updated"] = time.time()


weather_stop_event = Event()


def weather_loop():
    while True:
        try:
            fetch_weather()
        except Exception:
            logging.exception("weather fetch failed")
        if weather_stop_event.wait(WEATHER_REFRESH_SECONDS):
            break


def _draw_weather_icon(draw, x, y, color):
    draw.ellipse((x + 3, y + 3, x + 13, y + 13), fill=color)
    draw.ellipse((x + 9, y + 1, x + 21, y + 13), fill=color)
    draw.ellipse((x, y + 7, x + 11, y + 17), fill=color)
    draw.rectangle((x + 4, y + 11, x + 18, y + 17), fill=color)


def _draw_calendar_icon(draw, x, y, color):
    draw.rounded_rectangle((x, y + 2, x + 18, y + 18), radius=2, outline=color, width=2)
    draw.line((x, y + 8, x + 18, y + 8), fill=color, width=2)
    draw.line((x + 5, y, x + 5, y + 5), fill=color, width=2)
    draw.line((x + 13, y, x + 13, y + 5), fill=color, width=2)


def _draw_clock_icon(draw, x, y, color):
    cx, cy, r = x + 9, y + 9, 9
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=color, width=2)
    draw.line((cx, cy, cx, cy - 6), fill=color, width=2)
    draw.line((cx, cy, cx + 5, cy + 2), fill=color, width=2)


_OVERLAY_ICON_DRAWERS = {
    "weather": _draw_weather_icon,
    "date": _draw_calendar_icon,
    "time": _draw_clock_icon,
}


def stamp_jpeg(buf):
    img = Image.open(io.BytesIO(buf)).convert("RGB")

    with weather_lock:
        weather_text = weather_state["text"]

    now = time.localtime()
    lines = []
    if weather_text:
        lines.append(("weather", weather_text, (120, 200, 255)))
    lines.append(("date", time.strftime("%b %d %Y", now), (230, 230, 230)))
    lines.append(("time", time.strftime("%I:%M:%S %p", now).lstrip("0"), (255, 255, 0)))

    line_height = 30
    icon_w = 26
    padding = 10
    measure_draw = ImageDraw.Draw(img)
    max_text_w = max(measure_draw.textlength(t, font=TIMESTAMP_FONT) for _, t, _ in lines)
    box_width = int(icon_w + max_text_w + padding * 2)
    box_height = line_height * len(lines) + padding * 2 - 6

    box_x0, box_y0 = 12, img.height - 12 - box_height
    box_x1, box_y1 = box_x0 + box_width, img.height - 12

    overlay = Image.new("RGBA", (box_width, box_height), (0, 0, 0, 150))
    region = img.crop((box_x0, box_y0, box_x1, box_y1)).convert("RGBA")
    blended = Image.alpha_composite(region, overlay).convert("RGB")
    img.paste(blended, (box_x0, box_y0))

    draw = ImageDraw.Draw(img)
    y = box_y0 + padding - 3
    for kind, text, color in lines:
        icon_x = box_x0 + padding
        _OVERLAY_ICON_DRAWERS[kind](draw, icon_x, y, color)
        draw.text((icon_x + icon_w, y - 2), text, font=TIMESTAMP_FONT, fill=color)
        y += line_height

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return out.getvalue()


class StreamingOutput(io.BufferedIOBase):
    def __init__(self):
        self.frame = None
        self.last_write_time = None
        self.condition = Condition()

    def write(self, buf):
        with self.condition:
            self.frame = stamp_jpeg(buf)
            self.last_write_time = time.time()
            self.condition.notify_all()


def load_rotated():
    try:
        with open(ROTATION_STATE_FILE) as f:
            return bool(json.load(f).get("rotated", False))
    except (OSError, ValueError):
        return False


def save_rotated(rotated):
    with open(ROTATION_STATE_FILE, "w") as f:
        json.dump({"rotated": rotated}, f)


def load_location():
    try:
        with open(LOCATION_STATE_FILE) as f:
            data = json.load(f)
            return float(data["lat"]), float(data["lon"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_location(lat, lon):
    with open(LOCATION_STATE_FILE, "w") as f:
        json.dump({"lat": lat, "lon": lon}, f)


def make_video_config(rotated):
    transform = Transform(hflip=1, vflip=1) if rotated else Transform()
    return picam2.create_video_configuration(
        main={"size": LIVE_MAIN_SIZE, "format": "YUV420"},
        lores={"size": LIVE_LORES_SIZE, "format": "YUV420"},
        encode="lores",
        buffer_count=CAMERA_BUFFER_COUNT,
        transform=transform,
    )


picam2 = Picamera2()
ROTATED = load_rotated()
# Dual-stream: "main" stays at full recording resolution at all times so 4K recording
# can start instantly with no mode switch (and thus no live-view interruption); "lores"
# feeds the browser MJPEG preview. Both are produced from the same sensor capture, so
# zoom/pan/focus/image controls apply to both simultaneously.
video_config = make_video_config(ROTATED)
picam2.configure(video_config)
picam2.set_controls({"AfMode": 2, "AfSpeed": 1, "AfRange": 0})  # continuous autofocus, fast, normal range

CONTROL_RANGES = picam2.camera_controls
FULL_CROP = picam2.camera_properties["ScalerCropMaximum"]  # (x, y, w, h)
SENSOR_MODEL = picam2.camera_properties.get("Model", "unknown")
FULL_RES_SIZE = picam2.camera_properties["PixelArraySize"]  # native sensor resolution
LENS_MIN, LENS_MAX, LENS_DEFAULT = CONTROL_RANGES["LensPosition"]
BRIGHT_MIN, BRIGHT_MAX, _ = CONTROL_RANGES["Brightness"]

controls_lock = Lock()
encoder_lock = Lock()
video_encoder = None
recording_file = None
recording_start_time = None
lores_encoder = None
continuous_encoder = None
continuous_file = None
continuous_segment_start_time = None
continuous_enabled = CONTINUOUS_DEFAULT_ENABLED  # state on boot; can be paused/resumed at runtime

# FfmpegOutput spawns ffmpeg with PR_SET_PDEATHSIG tied to the calling thread (not the
# process), so it must be started from a thread that outlives the request - not from a
# per-request ThreadingMixIn handler thread, which exits (and would SIGKILL ffmpeg)
# right after the response is sent.
camera_worker = ThreadPoolExecutor(max_workers=1)

AF_RANGES = {"normal": 0, "macro": 1, "full": 2}

_saved_location = load_location()

current = {
    "zoom": 1.0,
    "af_mode": "auto",
    "af_range": "normal",
    "lens_position": LENS_DEFAULT,
    "brightness": 0.0,
    "contrast": 1.0,
    "saturation": 1.0,
    "pan_x": 0.5,
    "pan_y": 0.5,
    "weather_lat": _saved_location[0] if _saved_location else WEATHER_LAT_DEFAULT,
    "weather_lon": _saved_location[1] if _saved_location else WEATHER_LON_DEFAULT,
}

PAN_STEP = 0.15


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def get_state():
    return {
        "sensor_model": SENSOR_MODEL,
        "full_res_size": list(FULL_RES_SIZE),
        "record_size": list(LIVE_MAIN_SIZE),
        "zoom": current["zoom"],
        "zoom_max": ZOOM_MAX,
        "af_mode": current["af_mode"],
        "af_range": current["af_range"],
        "lens_position": current["lens_position"],
        "lens_min": LENS_MIN,
        "lens_max": LENS_MAX,
        "brightness": current["brightness"],
        "brightness_min": BRIGHT_MIN,
        "brightness_max": BRIGHT_MAX,
        "contrast": current["contrast"],
        "saturation": current["saturation"],
        "pan_x": current["pan_x"],
        "pan_y": current["pan_y"],
        "rotated": ROTATED,
        "weather_lat": current["weather_lat"],
        "weather_lon": current["weather_lon"],
    }


def _reapply_controls():
    """Re-apply persistent camera controls after a configure()/switch_mode(), which
    resets everything to defaults."""
    apply_crop()
    with controls_lock:
        if current["af_mode"] == "manual":
            picam2.set_controls({"AfMode": 0, "LensPosition": current["lens_position"]})
        else:
            picam2.set_controls({"AfMode": 2, "AfSpeed": 1})
        picam2.set_controls({"AfRange": AF_RANGES[current["af_range"]]})
        picam2.set_controls({
            "Brightness": current["brightness"],
            "Contrast": current["contrast"],
            "Saturation": current["saturation"],
        })


def apply_crop():
    fx, fy, fw, fh = FULL_CROP
    w = int(fw / current["zoom"])
    h = int(fh / current["zoom"])
    max_x_off = fw - w
    max_y_off = fh - h
    x = fx + int(max_x_off * current["pan_x"])
    y = fy + int(max_y_off * current["pan_y"])
    with controls_lock:
        picam2.set_controls({"ScalerCrop": (x, y, w, h)})


def set_zoom(factor):
    current["zoom"] = clamp(factor, 1.0, ZOOM_MAX)
    if current["zoom"] == 1.0:
        current["pan_x"] = 0.5
        current["pan_y"] = 0.5
    apply_crop()


def set_pan(dx=0.0, dy=0.0, reset=False):
    if reset:
        current["pan_x"] = 0.5
        current["pan_y"] = 0.5
    else:
        current["pan_x"] = clamp(current["pan_x"] + dx, 0.0, 1.0)
        current["pan_y"] = clamp(current["pan_y"] + dy, 0.0, 1.0)
    apply_crop()


def set_focus(mode, value=None):
    with controls_lock:
        if mode == "auto":
            picam2.set_controls({"AfMode": 2, "AfSpeed": 1})
            current["af_mode"] = "auto"
        elif mode == "manual":
            lp = clamp(value if value is not None else current["lens_position"], LENS_MIN, LENS_MAX)
            picam2.set_controls({"AfMode": 0, "LensPosition": lp})
            current["af_mode"] = "manual"
            current["lens_position"] = lp
        elif mode == "trigger":
            picam2.set_controls({"AfMode": 1})
            picam2.set_controls({"AfTrigger": 0})
            current["af_mode"] = "single-shot"
        else:
            raise ValueError(f"unknown focus mode {mode!r}")


def set_af_range(range_name):
    if range_name not in AF_RANGES:
        raise ValueError(f"unknown af range {range_name!r}")
    with controls_lock:
        picam2.set_controls({"AfRange": AF_RANGES[range_name]})
    current["af_range"] = range_name


def set_brightness(value):
    value = clamp(value, BRIGHT_MIN, BRIGHT_MAX)
    with controls_lock:
        picam2.set_controls({"Brightness": value})
    current["brightness"] = value


def set_contrast(value):
    value = clamp(value, 0.0, CONTRAST_UI_MAX)
    with controls_lock:
        picam2.set_controls({"Contrast": value})
    current["contrast"] = value


def set_saturation(value):
    value = clamp(value, 0.0, SATURATION_UI_MAX)
    with controls_lock:
        picam2.set_controls({"Saturation": value})
    current["saturation"] = value


def reset_image_settings():
    set_brightness(0.0)
    set_contrast(1.0)
    set_saturation(1.0)


def set_location(lat, lon):
    lat = clamp(lat, -90.0, 90.0)
    lon = clamp(lon, -180.0, 180.0)
    current["weather_lat"] = lat
    current["weather_lon"] = lon
    save_location(lat, lon)
    try:
        fetch_weather()
    except Exception:
        logging.exception("weather fetch failed after location update")


def read_cpu_temp():
    try:
        with open(THERMAL_ZONE_PATH) as f:
            return round(int(f.read().strip()) / 1000, 1)
    except OSError:
        return None


def read_fan_status():
    try:
        with open(os.path.join(FAN_COOLING_DEVICE, "cur_state")) as f:
            level = int(f.read().strip())
        with open(os.path.join(FAN_COOLING_DEVICE, "max_state")) as f:
            max_level = int(f.read().strip())
        return {"on": level > 0, "level": level, "max_level": max_level}
    except OSError:
        return {"on": None, "level": None, "max_level": None}


def get_sysinfo():
    vm = psutil.virtual_memory()
    disk = shutil.disk_usage(SNAPSHOT_DIR)
    with output.condition:
        last_write = output.last_write_time
    frame_age = round(time.time() - last_write, 1) if last_write is not None else None
    return {
        "cpu_temp_c": read_cpu_temp(),
        "cpu_percent": psutil.cpu_percent(interval=None),
        "ram_percent": vm.percent,
        "ram_used_mb": round(vm.used / (1024 * 1024)),
        "ram_total_mb": round(vm.total / (1024 * 1024)),
        "fan": read_fan_status(),
        "disk_percent": round(disk.used / disk.total * 100, 1),
        "disk_used_gb": round(disk.used / (1024 ** 3), 1),
        "disk_total_gb": round(disk.total / (1024 ** 3), 1),
        "uptime_seconds": round(time.time() - SERVER_START),
        "frame_age_seconds": frame_age,
        "recording": get_recording_state(),
        "continuous": get_continuous_state(),
        "drive": gdrive_upload.status(),
    }


def get_continuous_state():
    active = continuous_encoder is not None
    elapsed = round(time.time() - continuous_segment_start_time, 1) if active else None
    chunk_count = sum(1 for n in os.listdir(CONTINUOUS_DIR) if CONTINUOUS_NAME_RE.match(n))
    return {
        "active": active,
        "enabled": continuous_enabled,
        "file": continuous_file,
        "elapsed_seconds": elapsed,
        "chunk_count": chunk_count,
        "retention_hours": CONTINUOUS_RETENTION_SECONDS / 3600,
    }


def list_snapshots():
    entries = []
    for name in os.listdir(SNAPSHOT_DIR):
        if not SNAPSHOT_NAME_RE.match(name):
            continue
        path = os.path.join(SNAPSHOT_DIR, name)
        try:
            st = os.stat(path)
        except FileNotFoundError:
            continue  # deleted concurrently (e.g. a racing delete request)
        entries.append({
            "name": name,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "time_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
        })
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return entries


def capture_snapshot():
    with output.condition:
        frame = output.frame
    if frame is None:
        raise RuntimeError("no frame available yet")
    fname = f"snapshot_{time.strftime('%Y%m%d_%H%M%S')}.jpg"
    path = os.path.join(SNAPSHOT_DIR, fname)
    with open(path, "wb") as f:
        f.write(frame)
    gdrive_upload.enqueue(path, "snapshots", delete_after=False)
    return fname


def delete_snapshot(name):
    if not SNAPSHOT_NAME_RE.match(name):
        raise ValueError("invalid filename")
    path = os.path.join(SNAPSHOT_DIR, name)
    if not os.path.isfile(path):
        raise ValueError("not found")
    os.remove(path)


AF_STATE_DONE = {2, 3}  # libcamera AfStateEnum: Focused, Failed


def _autofocus_before_capture(timeout=3.0):
    """Force a fresh AF pass right before a high-quality capture, matching Arducam's
    documented --autofocus-on-capture behavior. Skipped in manual focus mode, where
    the user has deliberately set the lens position."""
    if current["af_mode"] == "manual":
        return
    with controls_lock:
        picam2.set_controls({"AfMode": 1})  # Auto - required for AfTrigger
        picam2.set_controls({"AfTrigger": 0})  # start scan
    deadline = time.time() + timeout
    while time.time() < deadline:
        if picam2.capture_metadata().get("AfState") in AF_STATE_DONE:
            break
        time.sleep(0.1)


def _capture_full_res():
    global lores_encoder
    with encoder_lock:
        if video_encoder is not None:
            raise RuntimeError("stop recording before taking a full-resolution photo")
        fname = f"snapshot_full_{time.strftime('%Y%m%d_%H%M%S')}.jpg"
        path = os.path.join(SNAPSHOT_DIR, fname)
        _autofocus_before_capture()
        # Native sensor resolution requires a different sensor mode than the live
        # main/lores streams, so this briefly switches away and back. The lores
        # encoder doesn't survive a mode switch cleanly, so it's stopped first and
        # replaced with a fresh instance afterwards; the continuous buffer recorder
        # (also on lores) gets the same treatment so it doesn't silently die.
        picam2.stop_encoder(lores_encoder)
        was_continuous = continuous_encoder is not None
        if was_continuous:
            _stop_continuous_segment()
        try:
            transform = Transform(hflip=1, vflip=1) if ROTATED else Transform()
            still_config = picam2.create_still_configuration(transform=transform)
            picam2.switch_mode_and_capture_file(still_config, path, name="main")
            gdrive_upload.enqueue(path, "snapshots", delete_after=False)
        finally:
            new_lores_encoder = JpegEncoder()
            picam2.start_encoder(new_lores_encoder, FileOutput(output), name="lores")
            lores_encoder = new_lores_encoder
            if was_continuous:
                _start_continuous_segment()
            _reapply_controls()
    return fname


def capture_full_res():
    return camera_worker.submit(_capture_full_res).result()


def _set_rotation(rotated):
    global lores_encoder, ROTATED
    with encoder_lock:
        if video_encoder is not None:
            raise RuntimeError("stop recording before changing rotation")
        picam2.stop_encoder(lores_encoder)
        was_continuous = continuous_encoder is not None
        if was_continuous:
            _stop_continuous_segment()
        picam2.stop()
        new_config = make_video_config(rotated)
        picam2.configure(new_config)
        picam2.start()
        new_lores_encoder = JpegEncoder()
        picam2.start_encoder(new_lores_encoder, FileOutput(output), name="lores")
        lores_encoder = new_lores_encoder
        if was_continuous:
            _start_continuous_segment()
        ROTATED = rotated
        _reapply_controls()
        save_rotated(rotated)
    return rotated


def set_rotation(rotated):
    return camera_worker.submit(_set_rotation, rotated).result()


def get_recording_state():
    if video_encoder is None:
        return {"active": False, "file": None, "elapsed_seconds": None}
    return {
        "active": True,
        "file": recording_file,
        "elapsed_seconds": round(time.time() - recording_start_time, 1),
    }


def _start_recording_clip():
    global video_encoder, recording_file, recording_start_time
    with encoder_lock:
        if video_encoder is not None:
            raise RuntimeError("already recording")
        fname = f"recording_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
        path = os.path.join(RECORDING_DIR, fname)
        encoder = H264Encoder(bitrate=RECORDING_BITRATE)
        # "main" stream is always configured at LIVE_MAIN_SIZE (4K), so this starts
        # instantly and the "lores" preview keeps streaming on the browser unaffected.
        picam2.start_encoder(encoder, FfmpegOutput(path), name="main")
        video_encoder = encoder
        recording_file = fname
        recording_start_time = time.time()
    return fname


def _stop_recording_clip():
    global video_encoder, recording_file, recording_start_time
    with encoder_lock:
        if video_encoder is None:
            raise RuntimeError("not recording")
        picam2.stop_encoder(video_encoder)
        fname = recording_file
        video_encoder = None
        recording_file = None
        recording_start_time = None
    gdrive_upload.enqueue(os.path.join(RECORDING_DIR, fname), "recordings", delete_after=False)
    return fname


def start_recording_clip():
    return camera_worker.submit(_start_recording_clip).result()


def stop_recording_clip():
    return camera_worker.submit(_stop_recording_clip).result()


def list_recordings():
    entries = []
    for name in os.listdir(RECORDING_DIR):
        if not RECORDING_NAME_RE.match(name):
            continue
        path = os.path.join(RECORDING_DIR, name)
        try:
            st = os.stat(path)
        except FileNotFoundError:
            continue  # deleted concurrently (e.g. a racing delete request)
        entries.append({
            "name": name,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "time_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
        })
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return entries


def delete_recording(name):
    if not RECORDING_NAME_RE.match(name):
        raise ValueError("invalid filename")
    if name == recording_file:
        raise ValueError("recording in progress")
    path = os.path.join(RECORDING_DIR, name)
    if not os.path.isfile(path):
        raise ValueError("not found")
    os.remove(path)


# --- Always-on rolling buffer: 10-minute chunks on the lores (720p) stream, alongside
# the JPEG live-view encoder on the same stream, oldest chunks deleted once the total
# span exceeds CONTINUOUS_RETENTION_SECONDS. Runs independently of the on-demand 4K
# "main" stream recording above - the two don't interact. Rotation/full-res-photo both
# briefly reconfigure every stream, so this gets paused and resumed around those the
# same way the lores JPEG encoder does.

def _start_continuous_segment():
    global continuous_encoder, continuous_file, continuous_segment_start_time
    fname = f"continuous_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    path = os.path.join(CONTINUOUS_DIR, fname)
    encoder = H264Encoder(bitrate=CONTINUOUS_BITRATE)
    picam2.start_encoder(encoder, FfmpegOutput(path), name="lores")
    continuous_encoder = encoder
    continuous_file = fname
    continuous_segment_start_time = time.time()


def _stop_continuous_segment():
    global continuous_encoder, continuous_file, continuous_segment_start_time
    if continuous_encoder is not None:
        picam2.stop_encoder(continuous_encoder)
        continuous_encoder = None
        continuous_file = None
        continuous_segment_start_time = None


def _cleanup_continuous():
    cutoff = time.time() - CONTINUOUS_RETENTION_SECONDS
    for name in os.listdir(CONTINUOUS_DIR):
        if not CONTINUOUS_NAME_RE.match(name) or name == continuous_file:
            continue
        path = os.path.join(CONTINUOUS_DIR, name)
        try:
            stale = os.stat(path).st_mtime < cutoff
            if stale:
                os.remove(path)
        except FileNotFoundError:
            # Someone else (a concurrent /api/continuous/delete request) already
            # removed it - not an error, just nothing left to clean up here.
            pass


def _rotate_continuous_segment():
    with encoder_lock:
        finished_file = continuous_file
        _stop_continuous_segment()
        _start_continuous_segment()
        _cleanup_continuous()
    if finished_file:
        gdrive_upload.enqueue(
            os.path.join(CONTINUOUS_DIR, finished_file), "continuous", delete_after=True,
        )


continuous_stop_event = Event()


def continuous_recorder_loop():
    while not continuous_stop_event.wait(CONTINUOUS_SEGMENT_SECONDS):
        if continuous_enabled:
            try:
                camera_worker.submit(_rotate_continuous_segment).result()
            except Exception:
                # Same pattern as weather_loop: never let one bad rotation (e.g. a
                # race with a concurrent /api/continuous/delete, a transient encoder
                # error, or a full disk) permanently kill this daemon thread - that
                # would silently disable the rolling buffer's rotation/cleanup until
                # the whole service is restarted.
                logging.exception("continuous segment rotation failed")


def _start_continuous_recording():
    global continuous_enabled
    with encoder_lock:
        continuous_enabled = True
        if continuous_encoder is None:
            _start_continuous_segment()


def _stop_continuous_recording():
    global continuous_enabled
    with encoder_lock:
        continuous_enabled = False
        _stop_continuous_segment()


def start_continuous_recording():
    return camera_worker.submit(_start_continuous_recording).result()


def stop_continuous_recording():
    return camera_worker.submit(_stop_continuous_recording).result()


def list_continuous():
    entries = []
    for name in os.listdir(CONTINUOUS_DIR):
        if not CONTINUOUS_NAME_RE.match(name):
            continue
        path = os.path.join(CONTINUOUS_DIR, name)
        try:
            st = os.stat(path)
        except FileNotFoundError:
            continue  # deleted concurrently (e.g. the rotation cleanup)
        entries.append({
            "name": name,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "time_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
        })
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return entries


def delete_continuous(name):
    if not CONTINUOUS_NAME_RE.match(name):
        raise ValueError("invalid filename")
    if name == continuous_file:
        raise ValueError("recording in progress")
    path = os.path.join(CONTINUOUS_DIR, name)
    if not os.path.isfile(path):
        raise ValueError("not found")
    os.remove(path)


output = StreamingOutput()
picam2.start()
lores_encoder = JpegEncoder()
picam2.start_encoder(lores_encoder, FileOutput(output), name="lores")
psutil.cpu_percent(interval=None)  # prime measurement window

_cleanup_continuous()  # drop any leftover chunks older than the retention window on startup
_start_continuous_segment()
Thread(target=continuous_recorder_loop, daemon=True).start()
Thread(target=weather_loop, daemon=True).start()


class StreamingHandler(server.BaseHTTPRequestHandler):
    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self):
        """Returns True if the request may proceed. Otherwise sends a 401 and
        returns False - callers must return immediately without further writes."""
        if not AUTH_ENABLED:
            return True
        header = self.headers.get("Authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[len("Basic "):]).decode("utf-8")
                user, _, pw = decoded.partition(":")
            except (ValueError, UnicodeDecodeError):
                user, pw = "", ""
            ok = hmac.compare_digest(user, AUTH_USER) and hmac.compare_digest(pw, AUTH_PASS)
        if ok:
            return True
        body = b"Authentication required"
        self.send_response(401)
        self.send_header("WWW-Authenticate", f'Basic realm="{AUTH_REALM}"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def _send_video_file(self, fpath):
        file_size = os.path.getsize(fpath)
        range_header = self.headers.get("Range")
        if range_header:
            m = re.match(r"bytes=(\d+)-(\d*)", range_header)
            start = int(m.group(1)) if m else 0
            end = int(m.group(2)) if m and m.group(2) else file_size - 1
            end = min(end, file_size - 1)
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Type", "video/mp4")
            self.end_headers()
            with open(fpath, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        else:
            self.send_response(200)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(file_size))
            self.send_header("Content-Type", "video/mp4")
            self.end_headers()
            with open(fpath, "rb") as f:
                shutil.copyfileobj(f, self.wfile)

    def do_GET(self):
        if not self._check_auth():
            return
        path = urllib.parse.urlparse(self.path).path

        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        if path == "/":
            self.send_response(301)
            self.send_header("Location", "/index.html")
            self.end_headers()
        elif path == "/index.html":
            content = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        elif path == "/stream.mjpg":
            self.send_response(200)
            self.send_header("Age", "0")
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=FRAME")
            self.end_headers()
            try:
                while True:
                    with output.condition:
                        output.condition.wait()
                        frame = output.frame
                    self.wfile.write(b"--FRAME\r\n")
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(frame)))
                    self.end_headers()
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
            except Exception as e:
                logging.warning("Removed streaming client %s: %s", self.client_address, str(e))
        elif path == "/api/state":
            self._send_json(get_state())
        elif path == "/api/sysinfo":
            self._send_json(get_sysinfo())
        elif path == "/api/snapshots":
            self._send_json({"files": list_snapshots()})
        elif path == "/api/recordings":
            self._send_json({"files": list_recordings()})
        elif path == "/api/continuous":
            self._send_json({"files": list_continuous()})
        elif path == "/api/drive/files":
            try:
                self._send_json({"files": gdrive_upload.list_all_files()})
            except Exception as e:
                self._send_json({"files": [], "error": str(e)}, status=500)
        elif path.startswith("/recordings/"):
            name = urllib.parse.unquote(path[len("/recordings/"):])
            if not RECORDING_NAME_RE.match(name):
                self.send_error(404)
                return
            fpath = os.path.join(RECORDING_DIR, name)
            if not os.path.isfile(fpath):
                self.send_error(404)
                return
            self._send_video_file(fpath)
        elif path.startswith("/continuous/"):
            name = urllib.parse.unquote(path[len("/continuous/"):])
            if not CONTINUOUS_NAME_RE.match(name):
                self.send_error(404)
                return
            fpath = os.path.join(CONTINUOUS_DIR, name)
            if not os.path.isfile(fpath):
                self.send_error(404)
                return
            self._send_video_file(fpath)
        elif path.startswith("/snapshots/"):
            name = urllib.parse.unquote(path[len("/snapshots/"):])
            if not SNAPSHOT_NAME_RE.match(name):
                self.send_error(404)
                return
            fpath = os.path.join(SNAPSHOT_DIR, name)
            if not os.path.isfile(fpath):
                self.send_error(404)
                return
            with open(fpath, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._check_auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        def qf(name, default=None):
            if name not in qs:
                return default
            return float(qs[name][0])

        try:
            if path == "/api/capture":
                fname = capture_snapshot()
                self._send_json({"ok": True, "file": fname})
            elif path == "/api/capture/full":
                fname = capture_full_res()
                self._send_json({"ok": True, "file": fname})
            elif path == "/api/zoom":
                set_zoom(qf("value", 1.0))
                self._send_json({"ok": True, **get_state()})
            elif path == "/api/pan":
                reset = qs.get("reset", ["0"])[0] == "1"
                set_pan(qf("dx", 0.0), qf("dy", 0.0), reset=reset)
                self._send_json({"ok": True, **get_state()})
            elif path == "/api/focus":
                mode = qs.get("mode", ["auto"])[0]
                set_focus(mode, qf("value"))
                self._send_json({"ok": True, **get_state()})
            elif path == "/api/af-range":
                set_af_range(qs.get("value", ["normal"])[0])
                self._send_json({"ok": True, **get_state()})
            elif path == "/api/brightness":
                set_brightness(qf("value", 0.0))
                self._send_json({"ok": True, **get_state()})
            elif path == "/api/contrast":
                set_contrast(qf("value", 1.0))
                self._send_json({"ok": True, **get_state()})
            elif path == "/api/saturation":
                set_saturation(qf("value", 1.0))
                self._send_json({"ok": True, **get_state()})
            elif path == "/api/reset-image":
                reset_image_settings()
                self._send_json({"ok": True, **get_state()})
            elif path == "/api/location":
                set_location(qf("lat", 0.0), qf("lon", 0.0))
                self._send_json({"ok": True, **get_state()})
            elif path == "/api/rotate":
                rotated = qs.get("value", ["0"])[0] == "1"
                set_rotation(rotated)
                self._send_json({"ok": True, **get_state()})
            elif path == "/api/snapshots/delete":
                name = urllib.parse.unquote(qs.get("name", [""])[0])
                delete_snapshot(name)
                self._send_json({"ok": True})
            elif path == "/api/record/start":
                fname = start_recording_clip()
                self._send_json({"ok": True, "file": fname})
            elif path == "/api/record/stop":
                fname = stop_recording_clip()
                self._send_json({"ok": True, "file": fname})
            elif path == "/api/recordings/delete":
                name = urllib.parse.unquote(qs.get("name", [""])[0])
                delete_recording(name)
                self._send_json({"ok": True})
            elif path == "/api/continuous/delete":
                name = urllib.parse.unquote(qs.get("name", [""])[0])
                delete_continuous(name)
                self._send_json({"ok": True})
            elif path == "/api/continuous/start":
                start_continuous_recording()
                self._send_json({"ok": True, **get_continuous_state()})
            elif path == "/api/continuous/stop":
                stop_continuous_recording()
                self._send_json({"ok": True, **get_continuous_state()})
            elif path == "/api/drive/toggle":
                enabled = qs.get("enabled", ["1"])[0] == "1"
                gdrive_upload.set_enabled(enabled)
                self._send_json({"ok": True, **gdrive_upload.status()})
            elif path == "/api/drive/delete":
                # "name" here is the Google Drive file id, not a local filename -
                # kept as "name" so the gallery's generic bulk-delete flow (which
                # builds `<endpoint>?name=<id>`) works unchanged for this tab too.
                file_id = qs.get("name", [""])[0]
                gdrive_upload.delete_file(file_id)
                self._send_json({"ok": True})
            else:
                self.send_error(404)
        except Exception as e:
            logging.exception("control request failed")
            self._send_json({"ok": False, "error": str(e)}, status=400)

    def log_message(self, format, *args):
        pass


class StreamingServer(socketserver.ThreadingMixIn, server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


try:
    address = ("", 8000)
    srv = StreamingServer(address, StreamingHandler)
    print("Streaming on port 8000")
    srv.serve_forever()
finally:
    continuous_stop_event.set()
    weather_stop_event.set()
    picam2.stop_encoder()
    picam2.stop()
