# Security

iPhone Agent can control a phone and read data from it, so security reports
matter to us.

## Reporting a vulnerability

Report it privately through GitHub: **Security → Report a vulnerability** on this
repository. Please do not open a public issue. We aim to reply within 7 days.

Include the version or commit, the steps to reproduce, and what an attacker
could do.

## Scope

In scope:

- Reaching the controller API without the bearer token, or from another host
- Actions running without a request, or replayed after a failure
- Secrets, typed text or clipboard contents written to the audit journal or logs
- Escaping the per-phone lock, or one session acting on another's phone
- Path or URL handling in file transfer, install and fetch actions

Out of scope: issues in WebDriverAgent, pymobiledevice3 or iOS itself (report
those upstream), and attacks that need an already compromised Mac user account.

## Supported versions

Only the latest release receives fixes during the alpha.
