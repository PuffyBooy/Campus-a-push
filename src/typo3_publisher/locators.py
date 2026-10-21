"""Resolve locator specs from config/typo3_selectors.yaml into Playwright locators.

A spec is a string (CSS), a mapping ({css|role|label|text|placeholder|test_id|xpath}) or a
list of specs forming a fallback chain. :func:`find` waits explicitly (polling all
candidates) instead of sleeping for fixed times, and never uses coordinates.
"""

from __future__ import annotations

import re
import time
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Frame, Locator, Page


class LocatorNotFoundError(RuntimeError):
    pass


Scope = Page | Frame | Locator


def page_of(scope: Scope) -> Page:
    if isinstance(scope, Page):
        return scope
    return scope.page


def as_list(spec: Any) -> list[Any]:
    if spec is None:
        return []
    return list(spec) if isinstance(spec, list | tuple) else [spec]


def format_spec(spec: Any, variables: dict[str, Any] | None) -> Any:
    if not variables:
        return spec
    if isinstance(spec, str):
        try:
            return spec.format(**variables)
        except (KeyError, IndexError, ValueError):
            return spec
    if isinstance(spec, dict):
        return {k: format_spec(v, variables) for k, v in spec.items()}
    if isinstance(spec, list):
        return [format_spec(v, variables) for v in spec]
    return spec


def _name(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("re:"):
        return re.compile(value[3:], re.I)
    return value


def build(scope: Scope, spec: Any) -> Locator:
    if isinstance(spec, str):
        return scope.locator(spec)
    if not isinstance(spec, dict):
        raise ValueError(f"invalid locator spec: {spec!r}")
    exact = bool(spec.get("exact", False))
    if "css" in spec:
        loc = scope.locator(spec["css"])
    elif "role" in spec:
        kwargs: dict[str, Any] = {}
        if spec.get("name"):
            kwargs["name"] = _name(spec["name"])
            if not isinstance(kwargs["name"], re.Pattern):
                kwargs["exact"] = exact
        loc = scope.get_by_role(spec["role"], **kwargs)
    elif "label" in spec:
        loc = scope.get_by_label(_name(spec["label"]), exact=exact)
    elif "text" in spec:
        loc = scope.get_by_text(_name(spec["text"]), exact=exact)
    elif "placeholder" in spec:
        loc = scope.get_by_placeholder(_name(spec["placeholder"]), exact=exact)
    elif "test_id" in spec:
        loc = scope.get_by_test_id(spec["test_id"])
    elif "xpath" in spec:
        loc = scope.locator(f"xpath={spec['xpath']}")
    else:
        raise ValueError(f"locator spec needs css/role/label/text/placeholder/test_id/xpath: {spec!r}")
    if "nth" in spec:
        loc = loc.nth(int(spec["nth"]))
    return loc


def describe(spec: Any) -> str:
    return " | ".join(str(s) for s in as_list(spec))


def try_find(scope: Scope, spec: Any, *, visible: bool = True, variables: dict | None = None) -> Locator | None:
    """Single, non-waiting pass over the fallback chain."""
    for candidate in as_list(format_spec(spec, variables)):
        try:
            loc = build(scope, candidate)
            count = loc.count()
        except PlaywrightError:
            continue  # frame detached / navigating
        if count == 0:
            continue
        if not visible:
            return loc.first
        for i in range(min(count, 20)):
            try:
                if loc.nth(i).is_visible():
                    return loc.nth(i)
            except PlaywrightError:
                break
    return None


def find(
    scope: Scope,
    spec: Any,
    *,
    timeout_ms: int,
    visible: bool = True,
    variables: dict | None = None,
    what: str = "element",
) -> Locator:
    deadline = time.monotonic() + timeout_ms / 1000
    page = page_of(scope)
    while True:
        loc = try_find(scope, spec, visible=visible, variables=variables)
        if loc is not None:
            return loc
        if time.monotonic() >= deadline:
            raise LocatorNotFoundError(f"{what} not found within {timeout_ms} ms (tried: {describe(spec)})")
        page.wait_for_timeout(200)


def exists(scope: Scope, spec: Any, *, visible: bool = False) -> bool:
    return try_find(scope, spec, visible=visible) is not None
