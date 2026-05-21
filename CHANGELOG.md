# Changelog

All notable changes to this project will be documented in this file.

## [1.0.0] - 2026-05-21

### Added
- Initial Home Assistant integration for Warema WMS venetian blinds control
- Config flow UI for easy setup with multiple discovery methods:
  - Manual configuration with IP/port
  - Wandsender auto-pairing
  - New network creation
- Cover entities with full blind control:
  - Open, close, stop, set position and tilt
  - Position and angle feedback
- Sensor entities for position and angle monitoring
- Binary sensor entities for motion detection (moving/stopped)
- USB auto-detection for FTDI FT232R (Warema WMS USB Stick)
- Support for 4 blind device types: Type 20, Type 21, Type 25, Type 2E
- Localized UI strings with English translation
- Brand assets (logos and icons) for Home Assistant UI

### Requirements
- Python 3.9+
- Home Assistant 2023.1.0+
- pyserial >= 3.5
- Warema WMS compatible hardware (venetian blinds/shades)
