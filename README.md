# Warema WMS Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A Home Assistant custom integration for the **Warema WMS** (WAREMA Mobile System) radio control system, using a **WMS USB Stick** (FTDI FT232R).

## Repository Structure

```
pywarema/                    # Python library (serial protocol)
  __init__.py
  protocol.py                # Frame encoding/decoding
  stick.py                   # WmsStick controller class

custom_components/
  warema_wms/                # Home Assistant custom integration
    __init__.py
    config_flow.py           # UI config flow
    const.py                 # Constants
    coordinator.py           # WMS ↔ HA bridge
    cover.py                 # Cover entities
    manifest.json
    strings.json
    translations/
      en.json

hacs.json                    # HACS compatibility
```

## Prerequisites

- A fully installed Warema WMS network
- Warema WMS USB Stick (FTDI FT232R)
- WMS network parameters: **channel**, **PAN ID**, **network key**
  - Obtain these using the WMS Hand-held transmitter (see JS library README)
- Home Assistant (2023.1.0+)

## Hardware

The WMS USB Stick uses:
- **Baud rate**: 125,000
- **Protocol**: ASCII frames delimited by `}`
- **Typical path**: `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AV0K28M2-if00-port0`

## Installation

### HACS (Recommended)

1. Open Home Assistant and go to **Settings** → **Devices & Services** → **Integrations**
2. Click the **Create Automation** button (or add via HACS)
3. Search for and install **Warema WMS**
4. Restart Home Assistant
5. Add the integration via **Settings** → **Devices & Services** → **Create Automation** and search for "Warema WMS"

### Manual

1. Copy `custom_components/warema_wms/` to your HA `custom_components/` directory
2. Copy `pywarema/` to a location accessible by HA (or install as a package)
3. Restart Home Assistant

### Python Library Dependency

The integration requires the `pywarema` library. Install it:

```bash
pip install pyserial>=3.5
```

The `pywarema` package in this repo must be accessible to Home Assistant. The simplest approach for HA Green is to place the `pywarema/` folder alongside `custom_components/` in your HA config directory.

## Configuration

1. Go to **Settings** → **Devices & Services** → **Add Integration**
2. Search for **Warema WMS**
3. Enter:
   - **Serial Port**: Path to WMS USB Stick (e.g. `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AV0K28M2-if00-port0`)
   - **Channel**: WMS network channel (1-26)
   - **PAN ID**: 4-character hex (e.g. `ABCD`)
   - **Network Key**: 32-character hex
   - **Scan Interval**: Position polling interval in seconds (default: 30)
4. The integration will connect, scan for devices, and let you select which blinds to add

## Getting WMS Network Parameters

If you don't know your network parameters, use the JS library's network parameter discovery:

```bash
cd /path/to/node_modules/warema-wms-venetian-blinds
node lib/wms-vb_test-get-network-params.js
```

Follow the on-screen instructions with your WMS Hand-held transmitter.

## Supported Device Types

| Type | Description |
|------|-------------|
| 20   | Actuator UP |
| 21   | Plug receiver |
| 25   | Radio motor |
| 2E   | Actuator 230V UP |

## Cover Entity Features

Each blind is exposed as a `cover` entity with:

| Feature | Description |
|---------|-------------|
| Open | Move to fully open (position 0) |
| Close | Move to fully closed (position 100) |
| Stop | Stop current movement |
| Set Position | Move to specific position (0-100%) |
| Open Tilt | Set slats to fully outward (+100°) |
| Close Tilt | Set slats to fully inward (-100°) |
| Set Tilt Position | Set slat angle (0-100%) |

### Position Convention

- **HA**: 0 = closed, 100 = open
- **WMS**: 0 = open, 100 = closed
- The integration automatically converts between the two.

### Tilt Convention

- **HA**: 0 = fully down/inward, 50 = horizontal, 100 = fully up/outward
- **WMS**: -100 = fully inward, 0 = horizontal, +100 = fully outward

## Protocol Details

The WMS protocol uses ASCII frames over serial at 125,000 baud:

```
{G}                          → Get stick name
{V}                          → Get stick version
{K401<32-hex-key>}           → Set network key
{M%<channel><panid>}         → Switch channel/PAN
{R04FFFFFF7020<panid>02}     → Scan for devices
{R06<snr>801001000005}       → Get blind position
{R06<snr>707003<pos><ang>FFFF} → Move blind to position
{R06<snr>70700 1FFFFFFFFFF00}  → Stop blind
```

Responses are prefixed with `{r`, `{a}`, `{g`, `{v`, or `{f}`.

## Extra State Attributes

Each cover entity exposes:
- `snr`: Integer serial number
- `snr_hex`: 6-character hex serial number (wire format)
- `device_type`: Device type code
- `device_type_str`: Human-readable device type
- `wms_position`: Raw WMS position (0-100)
- `wms_angle`: Raw WMS angle (-100 to +100)

## Known Issues

This is a **beta release** with known architectural issues:

- **Race Conditions**: Entity position updates may arrive via dispatcher before entity registration completes in Home Assistant, causing occasional delayed or missed initial updates
- **Impact**: Rare edge case when blinds are first controlled; generally does not affect normal operation
- **Planned Fix**: Refactoring to use Home Assistant's `DataUpdateCoordinator` pattern in v1.1.0 (see `ARCHITECTURE_ISSUES.md` for details)

Please report any issues or unexpected behavior in the [GitHub issue tracker](https://github.com/mike-goldfinger/ha-warema-wms/issues).

## Credits

Protocol reverse-engineered from the JavaScript [warema-wms-venetian-blinds](https://www.npmjs.com/package/warema-wms-venetian-blinds) npm package.

Original JS credits: "Pman" and "willjoha" on the ioBroker forum.

## License

MIT
