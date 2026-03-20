# Changelog

All notable changes to this project will be documented in this file.

## v1.1.0 - 2026-03-20

### Added

- Added minimize-to-tray support for the main GUI window.
- Added tray menu actions to restore the main window or exit the application.
- Added first-run tray notification when the window is minimized.

### Changed

- Improved Windows tray integration by switching to pystray detached mode.
- Updated PyInstaller packaging configuration to include tray-related hidden imports.
- Updated README with tray behavior, extra dependencies, and packaging notes.

### Fixed

- Prevented the window from disappearing if tray icon initialization fails.

## v1.0.0 - 2026-03-19

### Added

- Initial public release of the RAINBOW3 GUI lighting editor.
- Support for mode light colors, per-LED color editing, multi-color effects, profiles, and program-linked auto switching.