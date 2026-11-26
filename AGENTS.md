# Agent instructions

- Run `uv run pytest -q` and `uv run ruff check .` after changes. Both must pass.
- Follow the rules in [CONTRIBUTING.md](CONTRIBUTING.md#making-a-change).
- Tests use fakes and do not prove phone behaviour. State in the pull request
  whether a change was run on a real iPhone, with model and iOS version.
- Never run device erase. Preserve the user's data on any phone you use.
- Keep UDIDs, phone names, team IDs and personal paths out of commits, tests and
  logs.
