# Changelog

All notable changes are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- `setup` requires `--source`, the path to your WebDriverAgent checkout. The previous default pointed to a path that only existed on the maintainer's machine.

## [0.1.0]

First public release.

- MCP server with tap, swipe, type, buttons, open, state, screenshot, any action and disconnect tools
- Authenticated HTTP, JSON-RPC and WebSocket API on `127.0.0.1`
- USB and Apple paired-network transports, with USB-first network fallback
- WebDriverAgent setup that builds and signs from a private source copy
- Keep-awake by F13 key events, with no phone settings changed
- One controller per phone; hash-chained audit journal
- Video, app audio, WebRTC, MP4 recording, files, install, clipboard, location, SOCKS proxy and read-only Messages snapshots
- Experimental native adapter for iOS 27+
