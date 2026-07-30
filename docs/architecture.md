# Architecture

A self-hosted camera system built around a Raspberry Pi 5 and an Arducam 64MP
Hawkeye sensor: live view, on-demand and rolling recording, optional cloud
backup, and private remote access — with no cloud subscription and no vendor
lock-in.

`Raspberry Pi 5 / 4 / Zero 2 W` · `Arducam 64MP Hawkeye (CSI)` · `picamera2 + libcamera` ·
`Python http.server` · `systemd user service` · `HTTP Basic Auth (optional)` ·
`Google Drive backup (optional)` · `Tailscale (private remote access)`

## System diagram

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {
  'background': '#0a0f1a',
  'primaryColor': '#121a2b',
  'primaryTextColor': '#e8edf7',
  'primaryBorderColor': '#324061',
  'lineColor': '#4b5978',
  'secondaryColor': '#121a2b',
  'tertiaryColor': '#121a2b',
  'clusterBkg': '#0d1424',
  'clusterBorder': '#26314a',
  'fontFamily': 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
  'fontSize': '13px'
}}}%%
flowchart TB
    subgraph HW["HARDWARE"]
        direction LR
        CAM["Arducam 64MP Hawkeye\nautofocus · CSI"]
        SD["microSD / USB storage"]
        CAM -->|"CSI ribbon"| PI["Raspberry Pi 5\nactive cooling"]
        PI --- SD
    end

    subgraph STACK["CAMERA STACK — libcamera / picamera2"]
        DUAL["Dual-stream pipeline\nmain 3840x2160 (always on) · lores 1280x720 (live)"]
    end

    subgraph APP["camera-stream.service — Python, systemd"]
        direction LR
        HTTPS["HTTP server :8000\nweb UI + REST API + MJPEG"]
        GATE{"Basic Auth gate\noptional"}
        WEATHER["weather thread\nOpen-Meteo, 15 min poll"]
        CONT["continuous recorder thread\n10-min segments, 3h retention"]
        UPLOAD["gdrive_upload thread + queue\npausable / resumable"]
        HTTPS --> GATE
    end

    subgraph STORE["LOCAL STORAGE"]
        direction LR
        SNAP["snapshots/"]
        REC["recordings/"]
        CBUF["continuous/"]
    end

    subgraph CFG["CONFIG — ~/.config/camera-stream/"]
        direction LR
        ENVF["*.env\ngdrive · location · performance · auth"]
        STATEF["*_state.json\nrotation · location · drive toggle"]
    end

    subgraph CLOUD["EXTERNAL SERVICES"]
        direction LR
        GDRIVE[("Google Drive API\nOAuth2 refresh token")]
        METEO[("Open-Meteo API\npublic, no auth")]
    end

    subgraph NET["NETWORK ACCESS"]
        direction LR
        LAN["LAN\nhttp://pi-ip:8000"]
        TS["tailscaled\ntailscale serve"]
        TSURL["https://*.ts.net\ntailnet-private, real TLS"]
        TS --> TSURL
    end

    subgraph CLIENTS["CLIENTS"]
        direction LR
        BROWSER["Browser — web UI"]
        VLC["VLC — MJPEG player"]
    end

    PI --> DUAL --> HTTPS
    ENVF -. configures .-> HTTPS
    ENVF -. configures .-> UPLOAD
    ENVF -. configures .-> WEATHER
    STATEF -. persists .-> HTTPS
    WEATHER --> METEO
    HTTPS --> SNAP
    HTTPS --> REC
    CONT --> CBUF
    SNAP -. uploads .-> UPLOAD
    REC -. uploads .-> UPLOAD
    CBUF -. uploads .-> UPLOAD
    UPLOAD --> GDRIVE
    GATE --> LAN
    GATE --> TS
    LAN --> BROWSER
    LAN --> VLC
    TSURL --> BROWSER
    TSURL --> VLC

    classDef hw fill:#182236,stroke:#5b6f96,color:#c3cee2;
    classDef core fill:#0f2b28,stroke:#2dd4bf,color:#bdfaef;
    classDef store fill:#161d2e,stroke:#3a4766,color:#c7d0e2;
    classDef cfg fill:#161d2e,stroke:#3a4766,color:#c7d0e2;
    classDef cloud fill:#2c2411,stroke:#c99a2e,color:#f2dc9e;
    classDef net fill:#221c38,stroke:#8a6fd6,color:#dccdfb;
    class CAM,PI,SD hw;
    class DUAL,HTTPS,GATE,WEATHER,CONT,UPLOAD core;
    class SNAP,REC,CBUF store;
    class ENVF,STATEF cfg;
    class GDRIVE,METEO cloud;
    class LAN,TS,TSURL,BROWSER,VLC net;
```

**Legend**: 🔷 Hardware · 🟢 Core application · ⬛ Storage & config · 🟡 External services · 🟣 Network & clients

## Layer notes

### Hardware
Arducam 64MP Hawkeye over CSI — autofocus, 9248×6944 native. Board choice is read
from the sensor at runtime, not hardcoded, so a Camera Module 3, Pi 4, or Zero 2 W
all work without code changes (see [Camera Compatibility](../SETUP.md#camera-compatibility-switching-away-from-the-arducam-64mp)
and [Low-Power Boards](../SETUP.md#low-power-boards-raspberry-pi-zero-2-w) in
SETUP.md). Active cooling matters: sustained encode load holds the Pi in the
55-65°C range.

### Application
`server.py` runs one dual-stream picamera2 pipeline: **main** stays at recording
resolution at all times so 4K capture starts instantly with no mode switch;
**lores** feeds the live MJPEG view and the rolling buffer.

The Basic Auth gate, when enabled, sits in front of every route - the web UI, the
REST API, and the stream itself - checked once per request with a constant-time
comparison.

### Storage & config
Media lives in `snapshots/`, `recordings/`, `continuous/` next to the app. All
four `.env` files are optional and independently togglable - absent means "off,"
not "broken."

Live-adjustable settings (rotation, weather location, Drive pause state) persist
to small JSON files alongside the code, separate from the deploy-time `.env`
config.

### External services
Google Drive backup uploads in the background via a resumable-upload queue,
browsable and deletable from the app itself - no need to open Drive separately.
Weather is a public, unauthenticated Open-Meteo lookup, refreshed every 15
minutes.

### Network access
On the LAN, the stream is reachable directly at `http://pi-ip:8000`. Off the LAN,
`tailscale serve` reverse-proxies the same port to a private `*.ts.net` address
with a real, trusted certificate - reachable only by devices signed into the
tailnet, never the public internet.

Tailscale Funnel (public internet exposure) is supported by the platform but
intentionally not enabled here.
