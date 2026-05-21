# Contributing to Warema WMS Home Assistant Integration

Thank you for your interest in contributing! This document provides guidelines and instructions for contributing to the project.

## Code of Conduct

Be respectful, inclusive, and professional in all interactions.

## Getting Started

### Prerequisites
- Python 3.9 or higher
- Home Assistant development environment
- Familiarity with Home Assistant integration development

### Setting Up Development Environment

1. **Clone the repository**
   ```bash
   git clone https://github.com/mike-goldfinger/ha-warema-wms.git
   cd ha-warema-wms
   ```

2. **Install development dependencies**
   ```bash
   pip install black pylint pyserial
   ```

3. **Install Home Assistant (optional, for testing)**
   ```bash
   pip install homeassistant
   ```

## Development Workflow

### 1. Create a Branch
```bash
git checkout -b feature/your-feature-name
# or
git checkout -b fix/your-bug-fix
```

### 2. Make Your Changes
- Follow Home Assistant integration conventions
- Keep changes focused and minimal
- Update relevant documentation

### 3. Code Quality

**Format your code with Black:**
```bash
black custom_components/warema_wms
```

**Check for issues with Pylint:**
```bash
pylint custom_components/warema_wms --disable=all --enable=E,F
```

### 4. Update Changelog
Add an entry to `CHANGELOG.md` under a new version section (or Unreleased):
```
### Added
- Your new feature description

### Fixed
- Bug fix description

### Changed
- Any breaking changes
```

### 5. Commit Your Changes
```bash
git commit -m "Brief description of changes"
```

Use clear, descriptive commit messages:
- ✅ `Fix race condition in entity registration`
- ✅ `Add support for blind position feedback`
- ❌ `fix stuff`
- ❌ `wip`

### 6. Push and Create Pull Request
```bash
git push origin feature/your-feature-name
```

Then create a pull request on GitHub. Use the PR template to describe your changes.

## Testing

### Hardware Testing
If you have access to Warema WMS hardware:
1. Test the integration with your actual blinds
2. Verify all entity types work correctly
3. Test configuration flow with different setup methods
4. Document any hardware-specific issues

### Code Review
- All PRs require code review before merging
- GitHub Actions will automatically check code formatting and validity
- Address reviewer feedback promptly

## Project Structure

```
custom_components/warema_wms/
├── __init__.py           # Integration setup
├── config_flow.py        # Configuration UI
├── coordinator.py        # Data coordination
├── cover.py              # Cover entities (blinds)
├── sensor.py             # Sensor entities
├── binary_sensor.py      # Binary sensor entities
├── const.py              # Constants and defaults
├── manifest.json         # Integration metadata
├── strings.json          # UI strings
├── translations/         # Localized UI strings
└── pywarema/             # WMS protocol library
    ├── protocol.py       # Frame encoding/decoding
    ├── stick.py          # USB stick control
    └── setup.py          # Package metadata
```

## Key Files to Know

- **manifest.json**: Integration metadata (version, requirements, Home Assistant compatibility)
- **coordinator.py**: Manages communication with WMS and updates Home Assistant entities
- **config_flow.py**: Handles user configuration during setup
- **pywarema/protocol.py**: Low-level WMS protocol implementation
- **pywarema/stick.py**: USB stick communication

## Architectural Notes

See `ARCHITECTURE_ISSUES.md` for known limitations and planned improvements.

### Current Issues (v1.0.0)
- Race conditions between dispatcher and entity registration
- Planned refactoring to DataUpdateCoordinator pattern in v1.1.0

## Reporting Issues

When reporting issues:
1. Check if it's already been reported
2. Use the bug report template
3. Include Home Assistant logs
4. Specify your hardware setup
5. Do NOT share network keys or sensitive data

## Feature Requests

Have an idea? Use the feature request template to:
1. Describe the feature clearly
2. Explain why it's useful
3. Discuss any alternatives you've considered

## Documentation

- **README.md**: User-facing documentation
- **ARCHITECTURE_ISSUES.md**: Technical documentation of known issues
- **CONTRIBUTING.md**: This file
- **CHANGELOG.md**: Version history

## Release Process

Releases are tagged with semantic versioning (v1.0.0, v1.1.0, etc.):
1. Update version in `manifest.json`
2. Update `CHANGELOG.md` with release notes
3. Create a git tag: `git tag v1.0.0`
4. Push tag to trigger release

## Questions?

- Check existing issues and discussions
- Open a new discussion if your question isn't answered
- Review Home Assistant developer documentation: https://developers.home-assistant.io/

## License

By contributing, you agree that your contributions will be licensed under the MIT License.

Thank you for helping improve the Warema WMS integration! 🎉
