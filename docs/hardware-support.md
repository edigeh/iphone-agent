# Hardware support

The unit tests run against fakes and cannot show that a feature works on a phone.
This table lists what has been seen working on a real iPhone, and what has not.

| Area | Tested on a real phone | Not yet tested on a real phone |
|---|---|---|
| USB and pairing | Exact-UDID selection, device services, app install/uninstall, AFC and House Arrest file transfers (iOS 26) | — |
| Paired network | WDA startup from a cold tunnel, managed reconnect, keep-awake (iOS 26, iOS 27.2) | Pulling the cable in the middle of a `--network-fallback` session |
| Control | Tap, swipe, two-finger pinch, typing, Return, scrolling, App Switcher, Home, lock and unlock | Every key variant; rotation |
| Media | H.264 video over USB and paired network, ReplayKit app audio at 48 kHz, local WebRTC with binary touch input, MP4 recording | Remote ICE, latency, microphone capture |
| Keep-awake | F13 key events keep the screen on during a session; the phone locks normally after it ends (iOS 27.2) | — |
| SOCKS proxy (VPN runner) | Traffic to a synthetic destination through the packet-tunnel extension, and restoring a prior VPN | Unrestricted forwarding; performance |
| Messages (read-only) | Parser and filters against the iOS database schema | A private Messages backup on hardware; encrypted backups |
| Native iOS 27+ adapter | Host tests and static protocol review | Everything on hardware; this adapter is experimental |

Running a feature on your own phone and reporting the iOS version and outcome in
an issue is one of the most useful contributions. See
[CONTRIBUTING.md](../CONTRIBUTING.md#hardware-reports).
