"""Enumerate every tenant route (`/api/v1/orgs/<org>/...`) from the live URLconf.

Each leaf URL pattern becomes a `Route` with its kwargs, allowed HTTP methods and a `build(**values)` that
produces a concrete path. Routes that other agents add later are picked up automatically.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

TENANT_PREFIX = "api/v1/orgs/"
SAFE_METHODS = ("get",)
UNSAFE_METHODS = ("post", "put", "patch", "delete")
_ALL_METHODS = SAFE_METHODS + UNSAFE_METHODS


@dataclass(frozen=True)
class Segment:
    kind: str  # "route" | "regex"
    text: str
    converters: tuple[tuple[str, str], ...] = ()  # (kwarg, converter class name) for route segments


@dataclass(frozen=True)
class Route:
    name: str | None
    segments: tuple[Segment, ...]
    callback: Any = field(compare=False, hash=False, repr=False)

    @property
    def pattern(self) -> str:
        return "".join(s.text for s in self.segments)

    @property
    def id(self) -> str:
        return self.name or self.pattern

    @property
    def kwargs(self) -> tuple[str, ...]:
        names: list[str] = []
        for seg in self.segments:
            if seg.kind == "route":
                names.extend(k for k, _ in seg.converters)
            else:
                names.extend(re.compile(seg.text).groupindex)
        return tuple(dict.fromkeys(names))

    @property
    def resource_kwargs(self) -> tuple[str, ...]:
        return tuple(k for k in self.kwargs if k not in ("org", "format"))

    def converter_for(self, kwarg: str) -> str | None:
        for seg in self.segments:
            for name, conv in seg.converters:
                if name == kwarg:
                    return conv
        return None

    @property
    def view_class(self):
        cb = self.callback
        return getattr(cb, "cls", None) or getattr(cb, "view_class", None)

    @property
    def methods(self) -> tuple[str, ...]:
        """HTTP methods (lower-case) the view actually handles."""
        cb = self.callback
        actions = getattr(cb, "actions", None)
        if actions:  # DRF ViewSet route: {'get': 'list', 'post': 'create'}
            return tuple(m for m in _ALL_METHODS if m in actions)
        cls = self.view_class
        if cls is not None:
            allowed = getattr(cls, "http_method_names", _ALL_METHODS)
            return tuple(m for m in _ALL_METHODS if m in allowed and hasattr(cls, m))
        allowed = getattr(cb, "nexora_methods", None)  # function views may declare their methods
        if allowed:
            return tuple(m for m in _ALL_METHODS if m in allowed)
        return ("get",)

    def default_value(self, kwarg: str) -> str:
        conv = (self.converter_for(kwarg) or "").lower()
        if "int" in conv:
            return "1"
        if "uuid" in conv or kwarg in ("pk", "id") or kwarg.endswith("_id") or kwarg.endswith("_pk"):
            return str(uuid.uuid4())
        if "path" in conv:
            return "x/y"
        return "x"

    def build(self, **values: str) -> str:
        """Concrete absolute path; missing kwargs get type-appropriate dummy values."""
        out = []
        for seg in self.segments:
            if seg.kind == "route":
                text = seg.text
                for name, _conv in seg.converters:
                    val = str(values.get(name) or self.default_value(name))
                    text = re.sub(r"<(?:[^>:]+:)?" + re.escape(name) + ">", lambda _m, v=val: v, text)
                out.append(text)
            else:
                out.append(_fill_regex(seg.text, lambda n: str(values.get(n) or self.default_value(n))))
        return "/" + "".join(out)


def _fill_regex(regex: str, value_for) -> str:
    """Turn a (DRF router style) regex into a concrete string by replacing named groups."""
    s = regex
    if s.startswith("^"):
        s = s[1:]
    if s.endswith("$") and not s.endswith("\\$"):
        s = s[:-1]
    out: list[str] = []
    i = 0
    while i < len(s):
        if s.startswith("(?P<", i):
            end_name = s.index(">", i)
            name = s[i + 4 : end_name]
            depth, j = 1, end_name + 1
            while depth and j < len(s):
                if s[j] == "\\":
                    j += 2
                    continue
                if s[j] == "(":
                    depth += 1
                elif s[j] == ")":
                    depth -= 1
                j += 1
            out.append(value_for(name))
            i = j
            if i < len(s) and s[i] == "?":  # optional group, e.g. (?P<format>...)?
                i += 1
            continue
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            out.append(s[i + 1])
            i += 2
            continue
        if ch == "?" and out and out[-1] == "/":  # "/?" -> drop the optional slash
            out.pop()
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _segment(pattern) -> Segment:
    from django.urls.resolvers import RegexPattern, RoutePattern

    if isinstance(pattern, RoutePattern):
        convs = tuple((k, type(v).__name__) for k, v in pattern.converters.items())
        return Segment("route", str(pattern), convs)
    if isinstance(pattern, RegexPattern):
        return Segment("regex", pattern._regex)
    return Segment("route", str(pattern))  # LocalePrefixPattern etc.


def iter_routes(resolver=None, prefix: tuple[Segment, ...] = ()):
    from django.urls import URLPattern, URLResolver, get_resolver

    resolver = resolver or get_resolver()
    for p in resolver.url_patterns:
        segs = (*prefix, _segment(p.pattern))
        if isinstance(p, URLResolver):
            yield from iter_routes(p, segs)
        elif isinstance(p, URLPattern):
            yield Route(name=p.name, segments=segs, callback=p.callback)


def tenant_routes() -> list[Route]:
    """Every route under /api/v1/orgs/<org>/ (deduplicated by pattern, stable order)."""
    seen: dict[str, Route] = {}
    for r in iter_routes():
        if r.pattern.startswith(TENANT_PREFIX) and "org" in r.kwargs:
            if "format" in r.kwargs:  # DRF format-suffix duplicates add no coverage
                continue
            seen.setdefault(r.pattern, r)
    return list(seen.values())


def verify_build(route: Route, path: str) -> bool:
    """True when `path` resolves back to the same view (guards the regex filling above)."""
    from django.urls import Resolver404, resolve

    try:
        match = resolve(path)
    except Resolver404:
        return False
    return match.func is route.callback or getattr(match.func, "__wrapped__", None) is route.callback or (
        getattr(match.func, "cls", object()) is getattr(route.callback, "cls", None)
        and getattr(match.func, "actions", None) == getattr(route.callback, "actions", None)
    )
