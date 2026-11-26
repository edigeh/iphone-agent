# Dependency provenance

| Dependency | Pinned version/source | License | Role |
|---|---|---|---|
| [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) | PyPI 11.47.0; hashes in uv.lock | GPL-3.0-or-later | USB/Lockdown/AFC, device services, RemoteXPC and backup |
| [WebDriverAgent](https://github.com/droidrun/WebDriverAgent) | source revision 8ce9d6f | BSD-3-Clause | XCTest runner, accessibility, input and media services |
| [aiohttp](https://github.com/aio-libs/aiohttp) | 3.14.5 | Apache-2.0 AND MIT | HTTP/WebSocket client/server |
| [aiortc](https://github.com/aiortc/aiortc) | 1.15.0 | BSD-3-Clause | WebRTC and recording |
| [PyAV](https://github.com/PyAV-Org/PyAV) | 17.1.0 in uv.lock | BSD-3-Clause | H.264/Opus decoding and MP4 output; bundled codec libraries carry their own licenses |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | 2.3.0 | MIT | MCP stdio tools |

`uv.lock` contains the complete transitive dependency graph and distribution
hashes. Installed distributions include their respective notices. The
controller needs no cloud account or proprietary binary.

WDA's optional SOCKS tunnel uses its pinned hev-socks5-tunnel submodule. Initialize
that source within WDA before choosing the tunnel runner scheme. Its upstream
license and build inputs remain with that source checkout.

Setup applies the packaged `iphone_agent/wda_local.patch` to a private copy of
the pinned WDA source. The patch names the runner iPhoneAgent and binds its raw
video, raw audio and MJPEG listeners to WDA’s selected `USE_IP` interface. The
original dependency checkout remains unchanged. The controller builds that source
with Xcode; it does not bundle a prebuilt WDA binary.
