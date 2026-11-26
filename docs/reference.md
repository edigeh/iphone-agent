# Reference

Full behaviour of the CLI, MCP server, HTTP API and device services. Start with the [README](../README.md).
A local Python controller that lets an AI agent operate a physical iPhone from a
Mac through USB or an explicitly selected Apple paired network connection.
It exposes MCP tools, an authenticated HTTP API, JSON-RPC,
WebSocket RPC and WebRTC media/control. It uses the public WebDriverAgent fork
for XCTest services and pymobiledevice3 for USB, pairing and device services.

## Install

Python 3.13+, `uv`, Xcode and a trusted iPhone with Developer Mode enabled are
required. Initial setup requires an unlocked, trusted phone connected through
USB or the explicitly selected Apple paired network path described below.

```sh
cd /path/to/controller
uv sync
uv run iphone-agent doctor
uv run iphone-agent devices
```

`doctor` reports USB/pairing readiness. `serve` additionally probes WDA before
opening the controller API. With multiple phones, select their hardware UDID
using `--udid`; a CoreDevice UUID is a different identifier.

Sign in to Xcode → Settings → Accounts. Build and install WDA using your
development team ID:

```sh
uv run iphone-agent setup --team YOUR_TEAM_ID
```

The default source checkout is `../src/github/WebDriverAgent`, revision
`8ce9d6f`. Another checkout can be selected with `--source`. For a standalone
checkout of this controller, clone the BSD-licensed WDA fork:

```sh
git clone https://github.com/droidrun/WebDriverAgent.git ../WebDriverAgent
git -C ../WebDriverAgent checkout 8ce9d6f
uv run iphone-agent setup --source ../WebDriverAgent --team YOUR_TEAM_ID
```

Setup saves the installed runner identity and signed product. Start the
controller; it launches and owns XCTest through Xcode's saved test plan, with
control and media forwarded through USB. No administrator privileges are
required. Stopping the controller closes its owned host connections.

```sh
uv run iphone-agent serve
uv run iphone-agent call state
uv run iphone-agent call tap '{"x":100,"y":200}'
uv run iphone-agent call type_text '{"text":"Hello"}'
uv run iphone-agent call global_action '{"action":2}'
```

An App Store Connect API key can provision the runner without an Xcode account
login. Pass `--authentication-key-path`, `--authentication-key-id`, and
`--authentication-key-issuer-id` together to `setup`. Apple may require the
account holder to accept an updated developer agreement before provisioning.

`setup --headless-assets` builds from a temporary source copy that omits the
optional runner icon catalog, using the installed iPhoneOS SDK. This supports
Macs whose asset compiler requires an unavailable simulator runtime. The WDA
source checkout stays unchanged. Each build uses fresh derived data to avoid
stale provisioning references. `--build-only` verifies the signed product
without installing it.

Each setup derives into its own directory under `build/` in the state directory.
After a successful setup, and when a controller has connected (`serve` once
at startup, `mcp` at the first tool call of each connection; a reconnect does
not count), the builds that
`runner.json` does not name and that are more than a day old are removed; a
younger one may be a setup still running. Setup also keeps the build the previous
`runner.json` named, since a session started before it may still run that build;
the next connection removes it. A `--build-only` product is therefore kept
for a day. Each removal is logged, and a connection that removes anything
writes one `housekeeping` entry to `audit.jsonl` with the count and bytes of each
kind.

For an independently installed runner, select its bundle with
`serve --runner-bundle-id org.iphoneagent.WebDriverAgentRunner.xctrunner`.
Use a separate `--home` when the saved product belongs to another runner.
Adoption requires a ready WDA response with the selected bundle identity. The
same options work with `mcp`. With an explicit `--runner-bundle-id` and no saved
signed product, the USB launcher uses pymobiledevice3's in-process XCTest path;
that alternative currently lacks
physical acceptance on the tested iOS version. Launch failures never silently
switch launchers or replay an action.

If Apple sees the phone through its paired network tunnel, use its hardware UDID:

```sh
uv run iphone-agent setup --paired-network --udid YOUR_UDID --team YOUR_TEAM_ID --headless-assets
uv run iphone-agent serve --paired-network --udid YOUR_UDID
```

The controller resolves the current CoreDevice tunnel address, verifies the exact
hardware UDID, and launches the saved signed XCTest product itself. It validates
the original test plan and signature, then uses a private launch plan bound to
that tunnel IP. The USB path binds WDA to the phone's loopback address. HTTP,
MJPEG, raw video and raw audio sockets inherit that binding. Stopping an owned
runner terminates its Xcode process group; an adopted runner stays with its owner.

Launching the saved product needs Xcode's developer disk image, which iOS
unmounts at every reboot and mounts only while the phone is unlocked. Startup
reads the lock state and mounts the image before starting Xcode, because Xcode
waits on a locked phone without failing. When the lock state cannot be read the
launch goes ahead; a launch that Xcode reports as waiting for an unlock is
stopped unless the phone reads unlocked by then. While the phone is locked,
startup retries for about 20 seconds and then ends with a `PhoneLocked` error; while the
network link fails the mount transiently, it retries within `--startup-timeout`.
A runner that is already running is adopted without this check, so a session can
connect to a locked phone then. iOS asks for Face ID or the passcode before it
lets XCTest drive the screen. If that prompt is cancelled or the phone locks, the
error says so; unlock the phone, approve the prompt and retry.

`--network-fallback --udid YOUR_UDID` uses USB while the cable is attached and
the paired network otherwise. It chooses again at every startup and reconnect.
While a USB startup waits for WDA it rechecks the cable every 2 seconds. If two
checks in a row find the phone not listed or fail, the attempt stops its
`xcodebuild`, logs the reason and starts again, which chooses the link afresh
from a single reading.
When the cable comes out during a USB session, the failed action starts a
managed reconnect, which moves the session to the paired network. A session
already on the network stays there until its next reconnect, even after the
cable is plugged back in. Fallback needs the saved signed product from
`setup`.

For an already running WDA, `--wda-url http://[TUNNEL_IPV6]:8100 --udid YOUR_UDID`
selects its existing endpoint. The literal IP must match that phone's live
CoreDevice inventory record, rechecked before requests and media connections. In network
mode, `capabilities` lists absent USB services as conditional: AFC, packages,
install/uninstall, time/timezone, device info and Messages backup refresh still
use the USB adapter. Cached Messages reads work independently.

Keep-awake is enabled during controller sessions and changes no setting on the
phone. While an agent is using the phone, the controller sends an F13 key event
every 10 seconds. iOS restarts its Auto-Lock countdown on any input event, and no
app gives F13 a function, so the screen stays on and nothing else happens. The
phone's own Auto-Lock and Low Power Mode values stay as the owner set them.

The events stop when the agent calls `iphone_disconnect`, which the server's MCP
instructions ask it to do once its task is done; 30 minutes after the agent's last call
(`--idle-timeout SECONDS` changes that); when the session closes; and when the
controller dies for any reason. An MCP session also disconnects at that point,
which frees the phone for other sessions; its next tool call connects again.
A recording, stream or WebRTC peer that is still running counts as use, so
neither happens until it ends. `--idle-timeout 0` never times out. From then on the phone locks after its normal
Auto-Lock time; there is nothing to restore. No event is sent while the phone is
locked, so a locked screen stays dark. `--no-keep-awake` sends no events at all.
`capabilities.keepAwake` reports whether events are being sent, the time since
the last one, the lock state it saw and its last error. A failed event never ends
a session.

A locked phone needs its owner: the controller cannot enter Face ID or the
passcode. `capabilities.locked` reports the lock state. `screenshot`, `state` and
`global_action` with `lock` or `unlock` run while the phone is locked; `tap`,
`swipe`, `long_press`, `touch_actions`, `tap_element`, `type_text`, `clear_text`,
`key`, `launch_app`, `deep_link` and `global_action` with `home`, `back` or
`app_switch` return a `PhoneLocked` error that says to unlock it, since their
input would land on the lock screen. An input action reuses an unlocked reading up to
12 seconds old, which keep-awake refreshes every 10, so input sent within that
window after the owner locks the phone reaches the lock screen. A locked reading
is never reused.

`global_action` with `lock` presses the side button for 50 ms and returns once
the phone reports locked; input after it always reads the lock again. On a phone
that is already locked it presses nothing, since a press would wake the screen.
`unlock` wakes the screen so Face ID can run; when the phone stays locked it
returns `PhoneLocked`, since only the owner can enter the passcode. `home`
presses the Home button, which also leaves Spotlight, Control Center, the App
Library and later Home pages; in the app switcher it taps the empty gap above
the app cards instead.

Stop a `serve` controller with `uv run iphone-agent call shutdown` or Ctrl-C.

Coordinates use iOS screen points. WDA state and input explicitly use `scale=1`.
The foreground accessibility root supplies geometry; an incomplete snapshot
cannot authorize a gesture that needs screen dimensions. Screenshots retain
their encoded image resolution.

The HTTP endpoint and audit journal stay open across a managed reconnect.
In paired-network mode, a lost transport or changed tunnel address records the
failed action, then starts a new session for the same hardware UDID. The failed
action is never repeated. `iphone-agent call reconnect` explicitly requests
the same recovery; `capabilities.connection` reports its generation and any
unresolved cleanup. Startup and reconnect use `--startup-timeout` (120 seconds
by default). Identity failures are fatal. Shutdown cancels recovery and closes owned resources.

## One controller per phone

WebDriverAgent keeps one session, and a new session ends the previous one, so two
controllers on one phone would end each other's session at every request. A
controller, `serve` or an `mcp` session, therefore holds its phone from the start
of its connect until its connection is closed, through any reconnects. The lock
is per hardware UDID in `~/.iphone-agent/phones/`, whatever `--home` each
controller uses, and it covers USB, `--paired-network`, `--network-fallback`,
`--wda-url` and an adopted runner alike. A second controller of the same phone
is refused at once with a `PhoneBusy` error that names the holder's mode, PID
and state directory and says how to free it: for `serve`, Ctrl-C or
`iphone-agent --home HOLDER_HOME call shutdown --url HOLDER_URL` with the
holder's own values; for `mcp`, its agent calling `iphone_disconnect`, closing its client session, or waiting until it
has made no call for its `--idle-timeout`, when it disconnects (a session started
with `--idle-timeout 0` never does). An idle MCP
session that is still connected keeps the phone until then, so a `serve` started
next to it exits with that error. A holder that is cleaning up is waited for
within `--startup-timeout`. The kernel drops the lock when its holder dies, so a
killed controller never blocks the next one. `IPHONE_AGENT_LOCK_DIR` moves the
lock directory; every controller of a phone must use the same one.

Other WDA clients, such as Appium or Xcode tools, take no such lock. When one of
them, or a runner restart, ends the controller's session, the controller starts
a new one, logs a warning naming the ended session and writes a
`wda_session_replaced` entry to `audit.jsonl`. Element IDs from the ended session
no longer resolve.

## MCP

The `mcp` command starts its own controller; it does not attach to an existing
HTTP server. To control a running `serve` session, use `iphone-agent call` or its
authenticated HTTP, JSON-RPC or WebSocket API.

Configure an MCP client with an absolute path to the installed command:

```json
{
  "mcpServers": {
    "iphone": {
      "command": "/absolute/path/to/controller/.venv/bin/iphone-agent",
      "args": ["mcp"]
    }
  }
}
```

| Tool | Parameters | Use |
|---|---|---|
| `iphone_capabilities` | none | Connects if needed; reports the link, lock state, keep-awake and the action names available |
| `iphone_state` | none | Accessibility tree of the foreground app, with element bounds in screen points |
| `iphone_screenshot` | `scale` (default 1) | PNG in which one pixel is one screen point; `scale` 2 gives more detail, up to the screen's native pixels |
| `iphone_tap` | `x`, `y`, optional `hold_ms` | Tap, or long press with `hold_ms` |
| `iphone_swipe` | `x1`, `y1`, `x2`, `y2`, optional `duration_ms` | Drag between two points |
| `iphone_type` | `text`, optional `clear` | Type into the focused field |
| `iphone_press` | `button`: `home`, `back`, `app_switch`, `lock`, `unlock`, `volumeup`, `volumedown` | System buttons |
| `iphone_open` | `bundle_id`, `url` or both | Launch an app or open a URL or deep link |
| `iphone_action` | `action`, `params` | Every other action in the table under API and actions |
| `iphone_disconnect` | none | Closes the connection when the task is done: stops the runner and keep-awake and frees the phone; the next call connects again. Refused while a recording or stream runs |

Coordinates are screen points everywhere. The MCP process uses the same
transport, runner startup, control and journal implementation as HTTP. Add
`--paired-network --udid YOUR_UDID` to its arguments for the paired network path,
or `--network-fallback --udid YOUR_UDID` for USB with a paired-network fallback.

The MCP server answers its client without a phone attached and connects on the
first tool call, so a session can start while the phone is unplugged and idle
sessions leave the phone alone. A call made while the phone is unavailable
returns the connection error, such as `USB device … is not connected`; the next
call tries again from a fresh connection. That first call can take up to
`--startup-timeout`, so set the client's tool timeout above it. A phone that is
still locked about 20 seconds into a connect ends it with a `PhoneLocked` error that
says to unlock it, since only its owner can. Tool errors carry the same type and
message as the HTTP API. A missing parameter is reported by name with the
parameters the action takes, and an action that exceeds its 120-second limit says
which action and what to try. After a failed reconnect, the next call reconnects
before it runs; `sms_watch` does not, and reports the lost connection.

The process a client starts only relays bytes to a background worker, which runs
the MCP server and owns the phone. When the client signals or kills that process,
or dies itself, the process exits at once. When the client closes stdin, it first
passes on what the worker has written, which takes at most 15 seconds if the
worker's event loop is stuck or the client has stopped reading. The worker carries
on until the runner's `xcodebuild` is stopped and owned resources are closed, then
exits on its own. It runs in its own session under launchd, so process group kills
and kills that walk parent PIDs do not reach it. A session that never connected
has nothing to clean up and its worker exits immediately. Cleanup stops at 5
minutes, ending whatever the session started. Worker output and failures go to
the private log `mcp-session.log` in the state directory, which moves to
`mcp-session.log.1` past 1 MiB, checked every 30 seconds. The USB forwarder's
repeated lines, such as a refused connection while WDA starts, appear once a minute
with a count of the dropped ones. `audit.jsonl` records the same keep-awake and
connection steps as `serve`.

Killing the worker itself by PID or by name, or shutting the Mac down, can leave
its `xcodebuild` running: the next session uses that WDA and does not stop it, so
stop that `xcodebuild` by PID if it is unwanted. The phone needs nothing: keep-awake
ends with the worker.

One MCP session holds the phone, and its state directory, from its first tool
call until its cleanup ends or until it has made no call for `--idle-timeout`. A
first tool call that finds another session cleaning up waits for it within
`--startup-timeout` and returns a retryable error if the cleanup outlasts that. A
call that finds another session or `serve` using the phone, from this or any
other state directory, returns the `PhoneBusy` error described under One
controller per phone at once.

## API and actions

HTTP listens on `127.0.0.1:8742`. A random bearer token is stored privately in
`~/.iphone-agent/token`; `IPHONE_AGENT_TOKEN` overrides it. Every endpoint except
`GET /ping` requires `Authorization: Bearer TOKEN`. POST bodies use
`application/json`. `--home` selects another private state directory.

`POST /execute` accepts `{"action":"tap","params":{"x":100,"y":200}}` and
returns `{"result":...}`. Named routes such as `POST /tap`, `POST /app`,
`GET /state`, `GET /a11y_tree`, `GET /phone_state`, `GET /screenshot` and
`/action/...` aliases use that same result envelope. These are this controller's
API responses. JSON-RPC over `POST /rpc` or WebSocket `/ws` accepts Portal-style
method names, for example:

```json
{"jsonrpc":"2.0","id":1,"method":"portal.Tap","params":{"displayId":0,"x":100,"y":200}}
```

| Actions | Parameters |
|---|---|
| `state`, `a11y_tree`, `phone_state`, `screen_geometry` | `{}` |
| `screenshot` | `{}` for the screen's native pixels; `scale` (0.25 or more) for that many pixels per screen point, up to the native pixels |
| `tap`, `long_press` | `x`, `y`; optional `durationMs` for long press |
| `tap_element` | unique accessibility `identifier`, optional `expectedValue` guard and `scrollToVisible` boolean (default `true`) |
| `swipe` | `startX`, `startY`, `endX`, `endY`, `duration` in seconds; or `x1,y1,x2,y2,durationMs` |
| `touch_actions` | `actions`: WDA pointerDown/pointerMove/pause/pointerUp items, supporting multiple pointer IDs |
| `type_text`, `clear_text`, `key` | `text` or `base64_text`, optional `clear`; `elementId`; Android-style supported `key_code`, optional `metaState` |
| `global_action` | `action`: 1 back gesture, 2 Home, 3 app switcher; or `name`: home/back/app_switch/lock/unlock |
| `launch_app`, `stop_app`, `uninstall` | `package` or `bundleId` |
| `deep_link` | `url` or `deepLink`, optional `bundleId` or `package` |
| `packages`, `info`, `time`, `version`, `timezone_get` | `{}` |
| `install` | local IPA/app `path`, or `urls` containing up to 10 HTTP(S) IPA URLs |
| `files_list`, `files_read`, `files_delete` | device `path`, optional app `bundleId` |
| `files_write` | device `path`, base64 `data`, optional `bundleId` |
| `files_fetch`, `files_push` | device `path`, HTTP(S) `url`, optional `bundleId`; fetch downloads, push uses HTTP PUT |
| `timezone_set` | IANA `timezone` |
| `clipboard_get`, `clipboard_set` | `{}`; `text` or `text_base64` |
| `location_get`, `location_set`, `location_reset` | `{}`; `latitude`, `longitude`; `{}` |
| `proxy_connect`, `proxy_disconnect`, `proxy_stats` | SOCKS5 `uri`, optional `timeout`; `{}`; `{}` |
| `reconnect` | `{}`; optional diagnostic `reason` string |

File access uses the AFC media root or an application's House Arrest container.
Paths are absolute within that device namespace; the host filesystem is separate.
HTTP transfers reject redirects and enforce download size limits. Downloads and
app installation can fail after an effect; failed mutations are never replayed
automatically.

## Media

`stream_video_start` starts a WDA H.264/H.265 stream and returns `host` and
`localPort`. USB uses a loopback forward; paired network mode returns the selected
phone’s validated tunnel address and device port. `stream_audio_start` uses
scrcpy-framed Opus. Stream operations include `*_list`, `*_get`, `*_stop`, and
`stream_video_keyframe`, with the stream's numeric `id`.

`webrtc_offer` accepts an SDP `sdp` and returns `sessionId`, `sdp`, and `type`.
`webrtc_ice` accepts `sessionId`, `candidate`, `sdpMid`, `sdpMLineIndex`;
`webrtc_keepalive`, `webrtc_requestframe`, and `webrtc_close` take `sessionId`.
Peers use local ICE candidates and a 120-second idle timeout. Their data channels
accept JSON-RPC actions and the modeled iOS key/text/touch/scroll/clipboard packets.
Binary keys, text lengths, touch coordinates and dimensions, pressure, button
masks, and scroll fields use little-endian encoding. Touch pointer IDs are opaque
eight-byte values that must stay unchanged through down/move/up. Clipboard lengths
and sequence IDs use big-endian encoding. Key events support
printable characters, Shift/Caps state and App Switcher. Scroll events use the
modeled bounded swipe fallback. Touch gestures support multiple fingers.
The existing volume/camera key forwarding is a local extension and remains
subject to the runner's supported button names.
Unsupported binary packet types return explicit errors.

`recording_start` and `recording_stop` create MP4 files in `recordings/` under
the private state directory; `recording_status` reports source errors and frame
counts. When a controller has connected, as for builds above, it removes `recording-*.mp4` files in that
directory last changed more than 30 days ago, logging each one; other files are
left alone, and a recording being written changes with every frame, so it is
never that old. `--keep-recordings-days DAYS` on `serve` or `mcp` changes the
age, and `0` keeps every recording. The removals go into the one `housekeeping`
audit entry with the build removals. Default
WebRTC and recording decode the WDA H.264 stream. `videoSource:"screenshot"`
selects a screenshot fallback. Options include `width`, `height`, `fps`, and
`audio:true`. Audio requires an active ReplayKit app-audio broadcast.

`stream_broadcast_start`, `stream_broadcast_status`, and `stream_broadcast_stop`
control ReplayKit. Full-device audio/screen capture requires its device consent
flow. Microphone capture is outside the WDA app-audio service. The controller
stops only media resources it created.

## Additional device services

For VPN proxy operations, initialize the WDA tunnel submodule and use
`setup --tunnel` with a paid development team. The runner and packet-tunnel
extension need Network Extension provisioning; the ordinary runner reports an
unsupported-operation error for proxy connection. Proxy configuration is
device-wide. Setup builds and validates the pinned tunnel engine in its private
source copy before building the signed runner. The first VPN configuration may
require the owner's passcode on the phone.

An experimental iOS 27+ adapter can be selected with `--native-rsd-host`,
`--native-rsd-port`, `--native-width`, and `--native-height`. The caller supplies
an already reachable RSD endpoint. This CLI mode routes tap, swipe and screenshot
through the native adapter; WDA supplies the remaining actions. The adapter
validates the phone's UDID, OS version and advertised services. Its remote
accessibility protocol and orientation transforms have independent static and
host-test review, but lack iOS 27 hardware acceptance.

Native HID currently refuses input without a verified, owned capture lease;
the CLI does not supply one. Its targeted native capture-stop protocol remains
unverified. Touch geometry must come from a fresh AX tree with a named interface
orientation; supplied dimensions and `native_geometry` cannot override this
check. `capabilities.native` reports these limits. Use the default WDA backend
for the verified iOS 26 control path.

`--sms-backup-dir /private/path` enables read-only Messages snapshots.
`sms_refresh` explicitly runs a USB MobileBackup2 backup retaining the SMS
database; it can require the phone passcode. `sms_list` and `sms_get` query the
last snapshot. Service, kind, junk and date filters, ascending/descending pagination, and
first-seen `storedAt`/`storedFrom` are supported. Pagination tokens bind the
filters and successful refresh generation. `sms_subscriptions` returns the
backup's verified receiving number when its private `Info.plist` identifies the
selected phone and contains an unambiguous international phone number. Encrypted
backups and iOS SMS sending return capability errors. Construction and
ordinary state reads do not start a backup.

`sms_list` defaults to `kinds:["message"]`; choose any combination of `message`,
`reaction` and `system`. `junk` accepts `include` (default), `exclude`, `only`,
or `unknownSender`. Responses include `kind`, `junk` and `unknownSender` using
the modeled iOS database rules. Unknown senders exclude messages classified as
junk. Optional columns use the parser's verified zero/default behavior.

Before changing a cached backup, refresh persists a private incomplete marker.
Failed or cancelled refreshes block snapshot reads, including existing cursors,
until a successful explicit `sms_refresh` completes backup, indexing and
validation. The marker survives controller restarts.

`sms_watch` delivers new Messages through a bounded long poll over the same
authenticated action API. It refreshes only while that explicit request is
pending, seeds the first snapshot without reporting history, and returns
`messages`, `cursor` and `timedOut`. Pass the returned cursor to the next call to
drain later arrivals in batches of up to `limit` (1–200). `timeout` includes backup
time and is at most 90 seconds; `interval` is 10–300 seconds. Cancellation stops
the pending operation. Keep the cursor private; an expired snapshot anchor
requires a fresh subscription.

```sh
uv run iphone-agent call sms_watch '{"timeout":90,"interval":60}'
uv run iphone-agent call sms_watch '{"cursor":"RETURNED_CURSOR","timeout":90,"interval":60}'
```

Each pending watch explicitly authorizes USB backups. Message contents are
returned to the authenticated caller; the audit journal stores payload digests.

Device erase is disabled by default. Enabling `serve --allow-erase` additionally
requires a reset request's `confirmEraseUDID` to equal the selected phone UDID.
An erase response means the device accepted the request; subsequent device state
must establish completion. App-data reset has no supported iOS service here.

## Verification and audit

```sh
uv run pytest -q
uv run iphone-agent audit
```

The hash-chained JSONL journal records each action's request/result, UDID,
timestamps and payload digests. It fsyncs before dispatch and after completion.
One writer holds the journal lock. Plaintext input, clipboard contents and
credentials are excluded from journal records. Hashes detect chain modification;
they are not external signatures. Requests without completion receipts have
unknown effects and require inspection before retrying. A journal past 8 MiB, or
one whose last record was cut off by a killed writer, is renamed
`audit-full-…jsonl` or `audit-damaged-…jsonl` at the next connection and a new
chain starts in `audit.jsonl`. The state directory keeps the 20 newest
`wda-xcodebuild-*.log` runner logs.

Hardware coverage and known limits are listed in [hardware-support.md](hardware-support.md).

## License

GPL-3.0-or-later. WebDriverAgent is a separate BSD-licensed dependency.
Dependency sources, versions and licenses are recorded in
[THIRD_PARTY.md](../THIRD_PARTY.md) and `uv.lock`.
