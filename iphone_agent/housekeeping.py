"""Keep the state directory bounded: old runner builds, old recordings and files no release reads.

Builds: every ``setup`` derives into its own ``build/<id>``. The one ``runner.json``
names is in use, and a directory younger than a day may be a setup still running;
the rest are removed. Recordings are the owner's files, so only finished
``recording-*.mp4`` files past an age the owner can change are removed.
"""
import json
import logging
import os
from pathlib import Path
import shutil
import stat
import time

BUILD_GRACE_SECONDS = 24 * 3600.0
DEFAULT_KEEP_RECORDINGS_DAYS = 30.0

_log = logging.getLogger(__name__)


def _tree_size(path: Path) -> int:
    total = 0
    for root, directories, files in os.walk(path):
        for name in (*directories, *files):
            try:
                info = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total


def _open_up(path: Path) -> None:
    """Give the owner access to every directory under ``path``, so that its contents can be listed and removed."""
    for root, directories, _files in os.walk(path, onerror=lambda error: _open_one(error.filename)):
        for name in directories:
            child = os.path.join(root, name)
            if not os.path.islink(child):
                _open_one(child)


def _open_one(directory) -> None:
    try:
        os.chmod(directory, 0o700, follow_symlinks=False)
    except (OSError, NotImplementedError):
        pass


def _remove_tree(path: Path) -> int | None:
    # Products can contain directories without owner access; neither their size nor their files are reachable otherwise.
    _open_up(path)
    _open_up(path)  # A second pass reaches what the first opened.
    size = _tree_size(path)
    try:
        shutil.rmtree(path)
    except OSError as exc:
        _log.warning("Could not remove old runner build %s: %s", path, exc)
        return None
    return size


def _saved_build(home: Path, builds: Path) -> Path | None:
    """The ``build/<id>`` directory that ``runner.json`` names, or ``None``; raise ValueError if it cannot be read."""
    config = home / "runner.json"
    if not config.exists():
        return None
    try:
        app_path = json.loads(config.read_text()).get("appPath")
    except (OSError, ValueError, AttributeError) as exc:
        raise ValueError(f"{config} cannot be read: {exc}") from exc
    return _build_of(app_path, builds)


def _build_of(app_path, builds: Path) -> Path | None:
    if not isinstance(app_path, (str, os.PathLike)):
        return None
    # Setup saves resolved paths and a controller can be given a linked --home, so both sides are resolved.
    try:
        relative = Path(os.path.realpath(app_path)).relative_to(os.path.realpath(builds))
    except ValueError:
        return None
    return Path(os.path.realpath(builds)) / relative.parts[0] if relative.parts else None


def prune_builds(home: Path, keep=()) -> list[tuple[Path, int]]:
    """Remove runner builds that are neither saved in ``runner.json``, nor in ``keep``, nor under a day old."""
    home = Path(home)
    builds = home / "build"
    if not builds.is_dir():
        return []
    try:
        kept = {_saved_build(home, builds)}
    except ValueError as exc:
        _log.warning("Kept every runner build: %s", exc)
        return []
    kept |= {_build_of(path, builds) for path in keep}
    cutoff = time.time() - BUILD_GRACE_SECONDS
    removed = []
    for entry in sorted(builds.iterdir()):
        try:
            info = entry.lstat()
        except OSError:
            continue
        resolved = Path(os.path.realpath(builds)) / entry.name
        if not stat.S_ISDIR(info.st_mode) or resolved in kept or info.st_mtime >= cutoff:
            continue  # Files and links are not builds; a link is never followed.
        size = _remove_tree(entry)
        if size is not None:
            _log.warning("Removed old runner build %s (%d bytes); runner.json uses another", entry, size)
            removed.append((entry, size))
    return removed


def prune_recordings(directory: Path, keep_days: float) -> list[tuple[Path, int]]:
    """Remove finished ``recording-*.mp4`` files older than ``keep_days``; 0 keeps every recording."""
    directory = Path(directory)
    if not keep_days or not directory.is_dir():
        return []
    cutoff = time.time() - keep_days * 24 * 3600.0
    removed = []
    for entry in sorted(directory.glob("recording-*.mp4")):
        try:
            info = entry.lstat()
            # A recording being written changes with every frame, so it is never this old.
            if not stat.S_ISREG(info.st_mode) or info.st_mtime >= cutoff:
                continue
            entry.unlink()
        except OSError as exc:
            _log.warning("Could not remove old recording %s: %s", entry, exc)
            continue
        _log.warning("Removed recording %s (%d bytes), older than %g days (--keep-recordings-days)",
                     entry, info.st_size, keep_days)
        removed.append((entry, info.st_size))
    return removed


def _total(removed):
    return {"count": len(removed), "bytes": sum(size for _, size in removed)}


def tidy_state_directory(home: Path, keep_recordings_days: float, audit=None) -> None:
    """Run at controller start, once this controller holds the state directory. Never raises."""
    home = Path(home)
    found = {}
    for kind, prune in (("builds", lambda: prune_builds(home)),
                        ("recordings", lambda: prune_recordings(home / "recordings", keep_recordings_days))):
        try:
            found[kind] = prune()
        except Exception as exc:  # Housekeeping must never keep a controller from starting.
            _log.warning("State directory housekeeping (%s) failed: %s", kind, exc)
            found[kind] = []
    if audit is not None and any(found.values()):
        audit.append("housekeeping", keepRecordingsDays=keep_recordings_days,
                     **{kind: _total(removed) for kind, removed in found.items()})
