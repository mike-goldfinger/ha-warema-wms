# Changelog

All notable changes to this project will be documented in this file.

## [1.0.3] - 2026-05-23

### Fixed
- Declared the `usb` integration as a dependency so USB auto-discovery of the
  WMS stick works reliably and the manifest passes Home Assistant's hassfest
  validation.
- Removed an invalid `homeassistant` key from `manifest.json` (the minimum HA
  version belongs in `hacs.json`) and reordered the manifest keys to the
  hassfest convention.

### Added
- Bundled brand icons (`icon.png`, `dark_icon.png`, plus `@2x` hDPI variants) in
  `custom_components/warema_wms/brand/`. From Home Assistant 2026.3 these are
  served through the local Brands Proxy API, so the integration shows its own
  icon in the UI without a separate brands-repository submission.
- Continuous integration: HACS Action and hassfest validation now run on every
  push, plus a daily scheduled check.

## [1.0.2] - 2026-05-22

### Fixed
- Sensors and the moving binary_sensor now appear on first setup, not only after a
  later rescan. If no device is explicitly ticked in the setup wizard, all discovered
  blinds are added, so `CONF_DEVICES` is always populated and every platform creates
  its entities consistently.

### Changed
- Tilt controls are now only exposed for actuator types 20 (Actuator UP) and 2E
  (Actuator 230V UP), which drive slatted blinds (Raffstoren). Awnings and roller
  shutters on plug receivers (21) or radio motors (25) no longer show a meaningless
  tilt control.

## [1.0.1] - 2026-05-22

### Fixed
- Position polling no longer lets the serial queue grow without bound: background
  position queries (pos-update / watch-moving) now skip motors that already have a
  pending `blindGetPos` request (dedup guard).
- Reduced retry count for background position polls (new `POS_POLL_RETRY`), so an
  unreachable motor costs ~1 s instead of ~3 s per cycle and the 5 s poll cycle
  stays ahead of the backlog even with one or two flaky motors. Working blinds keep
  updating reliably. Explicit user commands (stop/move follow-ups) keep full retries.

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
