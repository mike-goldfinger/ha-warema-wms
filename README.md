# Warema WMS Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://github.com/hacs/default)
[![GitHub Release](https://img.shields.io/github/release/mike-goldfinger/ha-warema-wms.svg?style=flat-square&label=Release)](https://github.com/mike-goldfinger/ha-warema-wms/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Maintenance](https://img.shields.io/badge/Maintenance-Active-green.svg)](https://github.com/mike-goldfinger/ha-warema-wms)

<p align="center">
  <a href="https://github.com/mike-goldfinger/ha-warema-wms" target="_blank">
    Control your Warema WMS venetian blinds and shades through Home Assistant — directly via the Warema WMS USB Stick
  </a>
</p>

---

> [!IMPORTANT]
> This integration talks **directly to the Warema WMS radio network using the Warema WMS USB Stick** (FTDI FT232R).
> It does **not** use and does **not** require the **WMS WebControl pro** gateway or any cloud service.
> If you control your blinds through a *WMS WebControl pro*, this integration is **not** the right one for you.

---

## ✨ Features

- 🪟 **Full blind control** — Open, close, stop, set position and tilt angle
- 🎪 **Valance control** — Awning valances as their own cover entities (open, close, stop, position)
- 💡 **Light control** — WMS dimming actuators as dimmable light entities (brightness + on/off)
- 📊 **Real-time monitoring** — Position, angle, and motion detection sensors
- 🌦️ **Weather station support** — Temperature, wind, brightness and rain sensors, auto-discovered from WMS weather-station broadcasts
- 🔦 **Identify button** — Briefly waves a blind so you can match an entity to the physical device
- 🔌 **USB auto-detection** — Plug in your Warema WMS Stick and go
- ⚙️ **Easy setup wizard** — Multiple discovery methods (manual, Wandsender pairing, new network)
- 🌍 **Multiple device support** — Works with Type 20, 21, 25, 2A and 2E cover actuators plus Type 26, 28 and 31 dimmers
- 🎯 **Native Home Assistant integration** — Full cover entity support with all features
- 🐍 **Native Python integration** — Pure Python, no external bridge, add-on or MQTT broker required
- 🔐 **Local control only** — No cloud dependency, all communication is local

---

## 📷 Screenshots

**Configuration Flow — Wandsender Pairing**
![Wandsender Pairing](docs/screenshots/wandsender-pairing.png)

**Device Control — Blind in Home Assistant**
![Device Control](docs/screenshots/device-control.png)

---

## 🚀 Installation

### Via HACS

This integration is in the **HACS default store** — no custom repository needed:

1. Open **HACS** in Home Assistant
2. Search for **Warema WMS**
3. Click **Download**
4. Restart Home Assistant
5. Add the integration via **Settings** → **Devices & Services** → **Add Integration**

### Manual Installation

1. Copy `custom_components/warema_wms/` to your HA `custom_components/` directory
2. Restart Home Assistant
3. Add the integration via **Settings** → **Devices & Services** → **Add Integration**

> **Note:** The `pywarema` library is bundled inside — no separate installation needed.

---

## ⚙️ Configuration

The integration uses a **configuration flow** with auto-detection and multiple setup options.

### Automatic Detection
Plug in your Warema WMS USB Stick (FTDI FT232R) and Home Assistant will notify you to start the setup wizard.

### Setup Wizard

**Step 1: Serial Port Selection**
- Auto-scans for connected USB sticks
- Or enter port path manually (e.g., `/dev/ttyUSB0`, `COM5`)

**Step 2: Network Discovery Method**

| Method | Use When | How It Works |
|--------|----------|-------------|
| **Enter manually** | You know your channel, PAN ID & key | Type in parameters directly |
| **Wandsender pairing** | You have an existing network | Wizard captures parameters from transmitter |
| **Create new network** | You want a fresh network | Wizard generates new credentials |

**Step 3: Select Devices**
- Auto-scans for blinds on the network
- Multi-select which ones to add

> **No network parameters needed upfront** — the wizard handles everything!

---

## 🎯 Supported Devices

### Actuator hardware

Any motor reachable from one of these WMS actuators:

| Type | Description |
|------|-------------|
| **20** | Actuator UP |
| **21** | Plug receiver |
| **25** | Radio motor |
| **2A** | Radio motor (Lamellendach L60/L70) |
| **2E** | Actuator 230V UP |

### Dimming actuators (light entities)

WMS dimming actuators are stand-alone devices with their own serial number —
a light is never a sub-channel of a motor. They are discovered by the same
scan as the cover actuators and exposed as dimmable light entities:

| Type | Description |
|------|-------------|
| **26** | Dimmer |
| **28** | Dimmer (smart) |
| **31** | Dimmer 0-10 V |

### Weather stations (read-only)

A WMS weather station (type **06**) is supported as a monitoring device: the
integration listens for its broadcasts and exposes temperature, wind,
brightness and rain sensors. Weather stations are not controllable and are not
part of the device-selection wizard — see *Weather Station Entities* below.

### Product types (auto-detected per device)

The integration reads the motor's `productType` (Block 37) on first connect
and exposes it as the right Home Assistant cover device class. Slat tilt
controls are enabled automatically when the motor reports that it has slats.

| WMS product | HA device class | Tilt |
|-------------|-----------------|------|
| ExternalVenetianBlind (Raffstore) | `blind` | ✓ |
| InternalVenetianBlind | `blind` | ✓ |
| VerticalLouvreBlind (Vertikallamellen) | `blind` | ✓ |
| RollerShutter (Rollladen) | `shutter` | – |
| SlatRoofL60 / SlatRoofL70 / SlatRoofL70Tilting (Lamellendach) | `awning` | ✓ |
| Awning, FacadeAwning, DroparmAwning, VerticalAwning, ConservatoryAwning, PergolaAwning, AwningOneValance, AwningTwoValances, Markisolette, SunSail, Valance | `awning` | – |
| PleatedBlindInside (Plissee), RollerBlindInside | `shade` | – |
| Window | `window` | – |

Unknown or unrecognised product IDs fall through to `blind`. If the motor is
asleep when first read, the integration falls back to the previous
hardware-based tilt heuristic (tilt only for Actuator UP / 230V UP) and
retries the detection on the next reload.

## 🏠 Hardware Requirements

- **Warema WMS USB Stick** (FTDI FT232R, USB VID: 0403, PID: 6001)
- **Supported blinds:** any motor driven by an Actuator UP (20), Plug Receiver
  (21), Radio Motor (25), Slat-Roof Motor L60/L70 (2A) or Actuator 230V UP (2E)
  — covering the full Warema range from Raffstoren and roller shutters to all
  awning families and Lamellendach (slat-roof) systems
- **Home Assistant:** 2024.6.0 or later
- **Python:** 3.9+

---

## 📊 Entities & Features

### Cover Entity
Each blind appears as a `cover` entity with:

| Control | Range | Notes |
|---------|-------|-------|
| **Position** | 0–100% | 0 = closed, 100 = open |
| **Tilt** | 0–100% | 0 = fully closed, 50 = horizontal, 100 = open. *Only for slatted blinds — see note below.* |
| **Open** | — | Move to fully open |
| **Close** | — | Move to fully closed |
| **Stop** | — | Stop current movement |

> **Tilt availability:** Tilt (slat angle) is auto-detected per device from
> the motor's own `isWithBlinds` flag (Block 37). Raffstoren / venetian blinds
> get tilt controls; awnings and roller shutters do not — regardless of which
> actuator hardware drives them. See *Supported Devices* above for the full
> product-type table.

### Valance Entities

An awning's valance (the fabric drop at the front) is a second motorised axis
on the *same* motor as the cover. Each valance channel that reports a position
appears as its own `cover` entity, *Valance 1* / *Valance 2*, on the cover's
device:

| Control | Range | Notes |
|---------|-------|-------|
| **Position** | 0–100% | 0 = fully lowered, 100 = fully raised |
| **Open** | — | Raise the valance fully |
| **Close** | — | Lower the valance fully |
| **Stop** | — | Stops the motor — including the cover, if it is moving |

> **Auto-detection:** there is no flag that announces a valance. The position
> frame simply reports `0xFF` for a channel that is not there, so the entity is
> created the first time a channel reports a real value — within one position
> poll of startup. Hardware without a valance never gets the entity.

> **Position convention:** the valance is not affected by the per-device
> *invert position* option. That option describes which end of the *cover's*
> travel you consider closed; a valance only ever drops downwards, so lowered
> is always "closed".

> **The two axes move independently.** A valance command carries the position
> the cover is already at, so only the valance moves, and a cover command
> leaves the valance untouched. The motor handles the awkward cases itself: it
> never drags a lowered valance while the cover travels — it raises it, moves,
> and lowers it again at the destination — and it accepts a lowered valance at
> any cover position, which is what makes a valance useful at a partial
> extension when the sun is low.

### Sensor Entities
- **Motor SNR** — Serial number (6-digit hex)
- **WMS Position** — Raw position from device
- **WMS Angle** — Raw tilt angle from device
- **Product type** *(diagnostic)* — The product type the device reports
  (e.g. `28 SlatRoofL70`). This is the field to quote in a support request,
  since it decides how the device is driven.

### Binary Sensor Entities
- **Moving** — `on` if blind is moving, `off` if stopped

### Button Entities
- **Identify** — Sends a wave/beckon so the blind briefly moves, to help you
  match the entity to the physical device

### Light Entities

WMS dimming actuators (types 26, 28, 31) are exposed as dimmable lights:

| Control | Range | Notes |
|---------|-------|-------|
| **On / Off** | — | Off sends level 0 (a real off, not a minimum glow) |
| **Brightness** | 0–100% | Turning on without a brightness restores the last level |

Brightness only — the WMS protocol carries no colour information, so there is
no colour control.

On an existing setup, dimmers are picked up via
**Configure → Add new devices** (a fresh scan is needed; existing devices and
entities are unaffected).

### Weather Station Entities

If a WMS weather station (device type `06`) is on the network, its periodic
broadcasts are decoded into a dedicated *Weather station &lt;SNR&gt;* device:

| Entity | Unit | Device class |
|--------|------|--------------|
| **Temperature** | °C | `temperature` |
| **Wind speed** | m/s | `wind_speed` |
| **Brightness** | lx | `illuminance` |
| **Rain** | on/off | `moisture` |

> Weather stations transmit unsolicited and are **not** part of the setup
> wizard. Their entities appear automatically once the first broadcast arrives
> (usually within a few minutes of startup).

> All blind entities are auto-discovered and named from the device's internal name.

---

## 🔄 Adding More Blinds Later

Go to the integration's **Configure** dialog to:
- Re-scan the network
- Add blinds that joined after initial setup
- Existing entities keep their history

---

## 🛠️ Configuring Motor Parameters

The **Configure** dialog → *Configure motor firmware parameters* lets you read and
write the persistent settings stored in the motor itself (Block 38). These survive
power cycles and also apply when you operate the blind via the handheld remote.

**How it works:**
1. Pick the **device** you want to configure.
2. Optionally pick a **source** under *Load values from* to copy another blind's
   settings — leave it on *current values of the device* to edit the device's own
   values. Copying is optional.
3. A short **loading** step reads the parameters over the radio network (this can
   take a few seconds; the source motor must be awake).
4. The form opens **pre-filled**. Numeric fields are input boxes (with %/s/° units),
   grouped into collapsible sections (Manual operation, Comfort, Away status, Run
   times & calibration, Slats, Other).
5. **Submit** writes only the fields you changed — unchanged parameters are never
   touched, using targeted single-byte writes that are read back and verified.

> Only Actuator UP (20), Plug Receiver (21) and Actuator 230V UP (2E) expose these
> parameters. Radio motors (25) use a different layout and are not listed.

---

## 💡 Position & Tilt Conventions

Home Assistant uses different conventions than WMS:

**Position**
- Home Assistant: 0 = closed, 100 = open
- WMS: 0 = open, 100 = closed
- → Automatically converted by the integration

**Tilt**
- Home Assistant: 0 = fully closed, 50 = horizontal, 100 = fully open
- WMS: −100 = fully closed, 0 = horizontal, +100 = fully open
- → Formula: `HA_tilt = (100 − WMS_angle) / 2`

---

## 🤝 Support & Contribution

### 🐞 Bugs

Found a problem? [Open a bug report](https://github.com/mike-goldfinger/ha-warema-wms/issues/new?template=bug_report.yml). The form asks for:

- Home Assistant and integration version
- Your hardware setup (device types)
- Steps to reproduce and relevant logs

### 💡 Feature Requests

Have an idea? [Open a feature request](https://github.com/mike-goldfinger/ha-warema-wms/issues/new?template=feature_request.yml) so it can be tracked and labelled.

### 💬 Questions & Help

For setup questions, usage help or general discussion, head to [GitHub Discussions](https://github.com/mike-goldfinger/ha-warema-wms/discussions).

### Want to Help?

Contributions are welcome! See `CONTRIBUTING.md` for:

- Development setup
- Code quality standards (Black, Pylint)
- Testing requirements
- Pull request guidelines

---

## 📚 Advanced: Protocol Details

The WMS protocol uses ASCII frames over serial at 125,000 baud:

```
{G}                          → Get stick name
{V}                          → Get stick version  
{K401<32-hex-key>}           → Set network key
{M%<channel><panid>}         → Switch channel/PAN
{R04FFFFFF7020<panid>02}     → Scan for devices
{R06<snr>801001000005}       → Get blind position
{R06<snr>707003<pos><ang>FFFF} → Move blind to position
{R06<snr>707001FFFFFFFFFF00}   → Stop blind
{R06<snr>7050...}            → Wave / beckon (identify)
```

Weather stations push their readings unsolicited as `7080` broadcasts, which the
integration decodes into temperature, wind, brightness and rain.

Responses use prefixes: `{r`, `{a}`, `{g`, `{v`, `{f}`

---

## 📜 License

This project is licensed under the **MIT License** — see `LICENSE` for details.

## 🙏 Credits

- **Protocol** derived from the open-source [warema-wms-venetian-blinds](https://www.npmjs.com/package/warema-wms-venetian-blinds) package
- **Original research** by "Pman" and "willjoha" on the ioBroker forum
- **Home Assistant community** for the excellent integration framework

---

**Made with ❤️ for Home Assistant**

[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.6+-blue?logo=home-assistant&logoColor=white)](https://www.home-assistant.io/)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9+-blue?logo=python&logoColor=white)](https://www.python.org/)
[![HACS](https://img.shields.io/badge/HACS-Community-orange)](https://hacs.xyz/)
