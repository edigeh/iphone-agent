# Contributing

Thanks for helping. Bug reports, hardware reports, docs fixes and code are all
welcome.

## Set up

```sh
uv sync
uv run pytest -q
uv run ruff check .
```

The tests use fakes for the phone, Xcode and WebDriverAgent, so they run
without a device. They take a few minutes.

## Making a change

1. Open an issue first for anything bigger than a small fix, so we can agree on
   the approach.
2. Add or update a test that fails without your change.
3. Keep the pull request to one change. Describe what you changed, why, and how
   you checked it.
4. If the change affects the phone, say which iPhone model and iOS version you
   ran it on, or that you did not run it on a phone.

Rules the code keeps, which reviews will check:

- A failed action is never retried or replayed automatically.
- The controller cleans up only resources it created.
- Nothing that changes the phone runs without an explicit request. Erase stays
  behind `--allow-erase` and an exact UDID confirmation.
- The audit journal stores digests, never typed text, clipboard contents or
  credentials.
- Never run device erase in testing.

## Hardware reports

Tests with fakes cannot show that something works on a phone. If you run a
feature on your own iPhone, open a **Hardware report** issue with the model, iOS
version, transport (USB or paired network), the commands you ran and what
happened. Remove your UDID, phone name and any personal data from logs first.

## Developer Certificate of Origin

By contributing you certify the [DCO](https://developercertificate.org/): you
wrote the change or have the right to submit it under the project license. Sign
each commit with `git commit -s`.

## License

Contributions are licensed under [GPL-3.0-or-later](LICENSE).
