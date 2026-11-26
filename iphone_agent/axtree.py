"""Read the accessibility tree returned by WDA's ``/mobilerun/state`` route."""

from __future__ import annotations

import math
from typing import Any, Iterator


class TreeError(RuntimeError):
    pass


def walk(node: dict[str, Any], ancestors: tuple[dict[str, Any], ...] = ()) -> Iterator[tuple[dict, tuple]]:
    """Yield every node with its ancestors, root first."""
    yield node, ancestors
    for child in node.get("children", []):
        if isinstance(child, dict):
            yield from walk(child, ancestors + (node,))


def center(node: dict[str, Any]) -> tuple[float, float]:
    bounds = node.get("boundsInScreen")
    if not isinstance(bounds, dict):
        raise TreeError("Element has no screen bounds")
    try:
        left, right, top, bottom = (float(bounds[key]) for key in ("left", "right", "top", "bottom"))
    except (ValueError, TypeError, KeyError) as exc:
        raise TreeError("Element has invalid screen bounds") from exc
    if right <= left or bottom <= top:
        raise TreeError("Element has empty screen bounds")
    return (left + right) / 2, (top + bottom) / 2


def visible(node: dict[str, Any], root: dict[str, Any]) -> bool:
    """Whether the node has a nonempty frame that lies entirely inside the root's frame."""
    try:
        center(node)
        bounds = node["boundsInScreen"]
        viewport = root["boundsInScreen"]
        return (float(viewport["left"]) <= float(bounds["left"]) and
                float(bounds["right"]) <= float(viewport["right"]) and
                float(viewport["top"]) <= float(bounds["top"]) and
                float(bounds["bottom"]) <= float(viewport["bottom"]))
    except (TreeError, KeyError, TypeError, ValueError):
        return False


def home_icons(state: Any) -> frozenset[str] | None:
    """The app icons of a complete, settled Home screen snapshot, or None for anything else."""
    if not isinstance(state, dict) or state.get("truncated") is not False:
        return None
    root, phone = state.get("a11y_tree"), state.get("phone_state")
    package = "com.apple.springboard"
    if (not isinstance(root, dict) or not isinstance(phone, dict)
            or phone.get("packageName") != package or root.get("packageName") != package
            or root.get("className") != "XCUIElementTypeApplication"):
        return None

    def finite_bounds(node):
        try:
            return all(math.isfinite(float(node["boundsInScreen"][key]))
                       for key in ("left", "right", "top", "bottom"))
        except (KeyError, TypeError, ValueError):
            return False

    if not finite_bounds(root) or not visible(root, root):
        return None
    identifiers = set()
    for node, _ in walk(root):
        identifier = node.get("resourceId")
        if (node.get("className") == "XCUIElementTypeIcon" and node.get("packageName") == package
                and node.get("isEnabled") is True and isinstance(identifier, str) and identifier
                and finite_bounds(node) and visible(node, root)):
            identifiers.add(identifier)
    return frozenset(identifiers) if len(identifiers) >= 2 else None
