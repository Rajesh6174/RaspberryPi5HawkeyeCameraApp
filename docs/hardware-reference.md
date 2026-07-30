# Raspberry Pi Ports, Cables & Cameras — Hardware Reference

A broader reference than [SETUP.md](../SETUP.md) needs for this app specifically.
This covers the general Raspberry Pi camera/display/PCIe hardware landscape -
useful if you're adapting this project to different hardware, or just want the
full picture beyond the Arducam 64MP Hawkeye / Camera Module 3 combination this
app is actually built and tested against.

**Provenance and confidence levels, read this before relying on anything below**:
this document was adapted from a user-supplied hardware reference covering many
boards, cables, and cameras this project doesn't use. Cable orientation, port
defaults, and lane-count facts that directly affect *this app's* supported
hardware (Pi 4/5, the Arducam 64MP Hawkeye, Camera Module 3) were independently
cross-checked against Raspberry Pi's own documentation and multiple other
sources - see [Connecting the camera](../SETUP.md#connecting-the-camera-cable-and-orientation)
in SETUP.md for that verified subset. Everything else here (GPIO control pin
numbers, exact bus throughput figures, specs for cameras/displays this app
doesn't support) is presented as-sourced and has **not** been independently
re-verified line by line - treat it as a starting point, not gospel, before
making a purchase or wiring decision based on it. One specific correction is
called out explicitly below where the source material conflicted with this
project's own tested, working configuration.

## Board port reference

### Raspberry Pi 5 (BCM2712)

| Port | Bus | Connector | Lanes | Cable needed |
|---|---|---|---|---|
| `CAM/DISP0` | CSI-2 in *or* DSI out (software-selected) | 22-way FPC, 0.5mm pitch | 4 native, 2 via a 15-pin adapter | 22-15 camera or display adapter |
| `CAM/DISP1` | CSI-2 in *or* DSI out (software-selected) | 22-way FPC, 0.5mm pitch | 4 native, 2 via a 15-pin adapter | 22-15 camera or display adapter |
| PCIe ×1 | PCI Express 2.0, single lane | 16-way FPC, 0.5mm pitch | - | Ribbon to an M.2 HAT+ (2230/2242) |

Both CAM/DISP ports are dual-purpose: the same physical connector carries CSI-2
(camera in) or DSI (display out) depending only on which `dtoverlay` you load -
nothing about the port itself is camera-only or display-only, unlike Pi 4. The
OS defaults to connector 1 (`CAM/DISP1`) when a dtoverlay doesn't specify which
port to use; append `,cam0` to target the other one (verified - see
[Connecting the camera](../SETUP.md#connecting-the-camera-cable-and-orientation)
for this app's specific case).

### Raspberry Pi 4B (BCM2711)

| Port | Bus | Connector | Lanes | Cable needed |
|---|---|---|---|---|
| CAMERA | CSI-2 input, fixed role | 15-way FFC, 1mm pitch | 2 | 15-15 camera cable (ships with the camera) |
| DISPLAY | DSI output, fixed role | 15-way FFC, 1mm pitch | 2 | 15-15 display cable (ships with the display) |

Unlike Pi 5, these are two separate, single-purpose connectors - a camera cable
plugged into DISPLAY (or vice versa) simply won't work, even though the physical
connector looks identical. Location: CAMERA sits between the micro-HDMI ports
and the A/V jack; DISPLAY sits near the GPIO/power edge.

### Raspberry Pi Zero 2 W (RP3A0)

| Port | Bus | Connector | Lanes | Cable needed |
|---|---|---|---|---|
| CSI Camera | CSI-2 input (D-PHY v1.1) | 22-way FPC, 0.5mm pitch - same pitch as Pi 5, but a physically smaller board | 2 | 22-15 "Pi Zero camera cable" |

No DSI/display connector exists on this board at all - video out is mini-HDMI
only. The small 65×30mm board is why it needs the narrower connector; the
software stack (`rpicam-apps`, Picamera2, auto-detect) is otherwise identical
to the bigger boards. See this project's own [Low-Power Boards](../SETUP.md#low-power-boards-raspberry-pi-zero-2-w)
section for why the CPU/RAM, not this port, is what actually constrains this
board for a camera-streaming workload like this app's.

## Cable compatibility matrix

| Board | Device | Cable | Notes |
|---|---|---|---|
| Pi 4 | Official camera (v2/v3/HQ/GS/AI) | 15-15 CAMERA (in box) | Auto-detected |
| Pi 4 | Touch Display / Touch Display 2 | 15-15 DISPLAY (in box) | Different pinout from the camera cable despite the identical connector |
| Pi 4 | Arducam 64MP Hawkeye | 15-15 (ships in the Hawkeye's box) | This app's default hardware - see SETUP.md |
| Pi 4 | NVMe SSD / AI accelerator | Not supported | Pi 4 has no PCIe; a USB 3.0 SSD enclosure is the practical alternative |
| Pi 5 | Official camera | 22-15 camera adapter | 200/300/500mm official lengths; the 15-pin cable in the camera's box does **not** fit Pi 5 alone |
| Pi 5 | 7" Touch Display (original) | 22-15 display adapter | Sold separately - the original display predates Pi 5 |
| Pi 5 | Touch Display 2 | 22-15 display FFC (in box) | TD2 ships both a 15-15 and a 22-15 FFC |
| Pi 5 | Arducam 64MP Hawkeye | 15-22 adapter (ships in the Hawkeye's box) | This app's default hardware - see SETUP.md. **No separate driver package needed** - corrected below |
| Pi 5 | Arducam native 22-pin camera | 22-22 straight cable | The only combination that can use the full 4 lanes |
| Pi 5 | NVMe SSD / AI accelerator | PCIe ribbon -> M.2 HAT+ | A completely different bus/cable, not a MIPI camera cable |
| Zero 2 W | Official camera | 22-15 "Pi Zero camera cable" | Short, cheap, often bundled with the official Zero case |
| Zero 2 W | Arducam native 22-pin camera | 22-22 mini-to-mini | Fits, but the Zero only routes 2 lanes regardless |
| Zero 2 W | Touch Display / TD2 | Not possible | No DSI port exists on this board |
| Zero 2 W | NVMe SSD | Not possible | No PCIe; a USB SSD via the single OTG port is the practical ceiling |

## Camera gallery

| Camera | Family | Sensor | Notable spec | Notes |
|---|---|---|---|---|
| Camera Module 2 (v2) | Official | IMX219 | 8MP, rolling shutter, fixed focus | Cheap, universally supported |
| Camera Module 3 (v3) | Official | IMX708 | 12MP, PDAF autofocus, HDR | Standard 75°/Wide 120°, each with a NoIR option. This app's verified-compatible alternative camera - see [Camera Compatibility](../SETUP.md#camera-compatibility-switching-away-from-the-arducam-64mp) |
| HQ Camera | Official | IMX477 | 12.3MP, interchangeable C/CS or M12 lens | Manual focus ring, no motorized `LensPosition` control |
| Global Shutter Camera | Official | IMX296 | 1.6MP, true global shutter, external trigger | No rolling-shutter distortion; built for synced multi-camera rigs |
| AI Camera | Official | Sony IMX500 | 12MP, on-sensor neural network inference | Runs models on the sensor itself, not the Pi's CPU |
| Arducam IMX708 AF | Arducam, libcamera-native | IMX708 | 12MP autofocus | Camera Module 3 equivalent; some SKUs ship native 22-pin |
| Arducam IMX477 line | Arducam, libcamera-native | IMX477 | 12MP | HQ-style modules, with or without a lens mount |
| 64MP OwlSight | Arducam, libcamera-native | OV64A40 | 64MP autofocus | Native libcamera support on Bookworm/Trixie, no separate driver package |
| **64MP Hawkeye** | Arducam | OV64A40-family | 64MP, PDAF/CDAF, 10x digital zoom | **This app's default camera.** The source material for this document claimed Hawkeye needs a separate Arducam driver package and can stop working after a kernel update - that's contradicted by this exact camera running via `dtoverlay=arducam-64mp` alone, verified working reliably throughout this project's development (see `server.py`'s sensor detection, which reports `arducam_64mp` with no driver install step in `install.sh`). Whatever distinction exists upstream between Hawkeye/OwlSight product lines, this project's specific overlay-based setup has not needed a separate driver. |
| Pivariety series | Arducam | Various (IMX519, IMX462 low-light, etc.) | Specialist sensors | Behind Arducam's Pivariety kernel driver - reinstall after kernel updates. Not the camera this app uses. |
| CamArray / multi-cam HATs | Arducam | Multiple sensors | One CSI port, several sensors multiplexed | Via an adapter HAT |
| PiviStation 5 (kit) | Arducam | Pi 5 + 64MP Hawkeye, bundled | All-in-one kit | Case, cooler, SD card, pre-loaded CV/ML libraries |

## Gotchas worth knowing

- **A camera cable and a display cable are not interchangeable**, even where the
  physical connector is identical (Pi 4's separate CAMERA/DISPLAY ports, or
  Pi 5's dual-purpose CAM/DISP ports fed the wrong overlay). Match the cable
  and the `dtoverlay` to the signal you actually want.
- **Cable orientation is mirrored between Pi 4 and Pi 5** - independently
  verified, see [Connecting the camera](../SETUP.md#connecting-the-camera-cable-and-orientation)
  in SETUP.md. Getting it backwards doesn't damage anything, but the camera
  won't be detected until it's flipped.
- **A 15-pin device adapted onto a Pi 5 port runs at 2 lanes**, not the port's
  native 4 - the adapter can't invent lanes the camera-side connector doesn't
  have. Irrelevant for this app's resolutions; a hard ceiling for higher-
  bandwidth sensors.
- **The Zero 2 W has no display port at all** - CSI camera in, mini-HDMI out,
  full stop. DSI touchscreens are not an option on this board.
- **Two-connector boards (Pi 5) default to connector 1.** Append `,cam0` to
  the dtoverlay line to force `CAM/DISP0` instead - verified, see SETUP.md.
- **PCIe Gen 3 on Pi 5 is opt-in and unofficial** (`dtparam=pciex1_gen=3`),
  roughly doubling PCIe throughput. Cheap FPC cables and DRAM-less SSDs are
  the usual sources of instability at Gen 3; drop back to Gen 2 if you see
  errors. Not used by this app - noted here only because Pi 5's PCIe lane is
  physically adjacent to the camera ports and easy to conflate with them.

---

For this app's actual, tested hardware setup, always defer to
[SETUP.md](../SETUP.md) over this document - it's scoped to exactly what this
project supports and has been verified against running hardware, where this
broader reference has not.
