# iPhone Agent

Let an AI agent use a real iPhone from your Mac.

iPhone Agent is a local controller that drives a physical iPhone over USB or
Apple's paired network connection. It gives agents an
[MCP](https://modelcontextprotocol.io) server, and gives scripts an
authenticated HTTP, JSON-RPC and WebSocket API. Underneath, it uses
[WebDriverAgent](https://github.com/droidrun/WebDriverAgent) for input,
accessibility and screen capture, and
[pymobiledevice3](https://github.com/doronz88/pymobiledevice3) for device
services.

- **Runs on your Mac only.** No cloud account. The API listens on `127.0.0.1`
  and needs a bearer token.
- **Agent tools.** Tap, swipe, type, press buttons, open apps and links, read the
  accessibility tree, and take screenshots in screen points.
- **Device services.** App install, files, clipboard, simulated location, a SOCKS
  proxy, read-only Messages snapshots, H.264 video, app audio, WebRTC and MP4
  recording.
- **Safe defaults.** One controller per phone. A hash-chained audit journal of
  every action. A failed action is never replayed. Erase is off unless you
  enable it. Keep-awake changes no settings on the phone.

> **Status: alpha.** The interfaces may change. See
> [hardware support](docs/hardware-support.md) for what has been tested on real
> phones.

## Requirements

- macOS with Xcode, and an Apple developer account signed in to Xcode (the
  optional VPN proxy runner needs a paid team)
- Python 3.13+ and [uv](https://docs.astral.sh/uv/)
- An iPhone that trusts the Mac, with Developer Mode turned on

## Quick start

```sh
git clone https://github.com/edigeh/iphone-agent.git
git clone https://github.com/droidrun/WebDriverAgent.git
git -C WebDriverAgent checkout 8ce9d6f
cd iphone-agent
uv sync
uv run iphone-agent doctor                       # check USB and pairing
uv run iphone-agent setup --source ../WebDriverAgent --team YOUR_TEAM_ID
uv run iphone-agent serve                        # start the controller
uv run iphone-agent call tap '{"x":100,"y":200}'
```

`setup` builds and signs the WebDriverAgent runner from a private copy of the
source and installs it on the phone. Your checkout stays unchanged.

## Use it from an agent

Point your MCP client at the installed command, using an absolute path:

```json
{
  "mcpServers": {
    "iphone": {
      "command": "/absolute/path/to/iphone-agent/.venv/bin/iphone-agent",
      "args": ["mcp"]
    }
  }
}
```

For Claude Code: `claude mcp add iphone -- /absolute/path/to/iphone-agent/.venv/bin/iphone-agent mcp`.

The server starts without a phone and connects on the first tool call, so set
your client's tool timeout above two minutes. Tools: `iphone_capabilities`,
`iphone_state`, `iphone_screenshot`, `iphone_tap`, `iphone_swipe`,
`iphone_type`, `iphone_press`, `iphone_open`, `iphone_action` and
`iphone_disconnect`.

The phone must be unlocked. The controller cannot enter Face ID or a passcode,
and input to a locked phone returns a `PhoneLocked` error.

## Without a cable

If Xcode can see the phone over Wi-Fi, use its hardware UDID:

```sh
uv run iphone-agent setup --paired-network --udid YOUR_UDID --team YOUR_TEAM_ID --headless-assets
uv run iphone-agent serve --network-fallback --udid YOUR_UDID
```

`--network-fallback` uses USB while the cable is attached and the network
otherwise.

## Documentation

- [Reference](docs/reference.md): CLI options, MCP tools, the HTTP API and every
  action, media, Messages, the audit journal
- [Hardware support](docs/hardware-support.md): what has been tested on real
  phones
- [Contributing](CONTRIBUTING.md) and [Security](SECURITY.md)

## How it works

```
agent ──MCP/HTTP──▶ iphone-agent (Mac) ──USB or paired network──▶ WebDriverAgent (XCTest on the phone)
                         │                                           └─ input, accessibility, screen and audio
                         └─ pymobiledevice3 ──▶ lockdown, AFC, install, backup, developer disk image
```

The controller launches the signed runner through Xcode's test plan, holds a
per-phone lock, sends a keep-awake key event every 10 seconds while an agent is
active, and writes every request and result to `~/.iphone-agent/audit.jsonl`
before and after it runs.

## License

[GPL-3.0-or-later](LICENSE). WebDriverAgent is a separate BSD-3-Clause project
built from source on your Mac. Dependency versions and licenses are in
[THIRD_PARTY.md](THIRD_PARTY.md).

iPhone is a trademark of Apple Inc. This project is not affiliated with or
endorsed by Apple.
