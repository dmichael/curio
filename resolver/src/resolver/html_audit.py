"""Static self-containment audit for HTML artifacts.

Markup-level only: the attributes and inline CSS that make a browser load a
subresource. A clean audit therefore claims "every declared reference is
relative, so it lives inside the stored artifact" — not "the work never
touches the network". Script bodies are deliberately not scanned: inert
strings (XML namespaces, library credits) would flag clean works, while a
URL a script assembles at runtime would still slip through. Same-bundle
stylesheets are not fetched either; an external url() inside one is not seen.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

# Attributes that make the browser fetch, on any element.
_FETCH_ATTRS = {"src", "poster", "xlink:href"}
# Elements whose href is a subresource fetch rather than a navigation link.
_HREF_FETCH_TAGS = {"link", "use", "image"}

_CSS_URL_RE = re.compile(r"""url\(\s*['"]?([^'")]+)""", re.IGNORECASE)
_CSS_IMPORT_RE = re.compile(r"""@import\s+['"]([^'"]+)""", re.IGNORECASE)
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")

# Schemes that never leave the document's own storage.
_LOCAL_SCHEMES = ("data:", "blob:", "about:", "javascript:")


def _is_external(ref: str) -> bool:
    ref = ref.strip()
    if not ref or ref.startswith("#"):
        return False
    if ref.startswith("//"):
        return True
    if ref.lower().startswith(_LOCAL_SCHEMES):
        return False
    return _SCHEME_RE.match(ref) is not None


def _srcset_urls(value: str) -> list[str]:
    return [part.strip().split()[0] for part in value.split(",") if part.strip()]


class _RefScanner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.external: list[str] = []
        self._in_style = False

    def _record(self, ref: str) -> None:
        if _is_external(ref):
            self.external.append(ref.strip())

    def _scan_css(self, text: str) -> None:
        for pattern in (_CSS_URL_RE, _CSS_IMPORT_RE):
            for match in pattern.finditer(text):
                self._record(match.group(1))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "style":
            self._in_style = True
        for name, value in attrs:
            if value is None:
                continue
            if name in _FETCH_ATTRS or (name == "href" and tag in _HREF_FETCH_TAGS):
                self._record(value)
            elif name == "srcset":
                for url in _srcset_urls(value):
                    self._record(url)
            elif name == "data" and tag == "object":
                self._record(value)
            elif name == "style":
                self._scan_css(value)

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self._in_style = False

    def handle_data(self, data: str) -> None:
        if self._in_style:
            self._scan_css(data)


def external_markup_refs(text: str) -> list[str]:
    """Absolute subresource references declared in the markup, in order."""
    scanner = _RefScanner()
    scanner.feed(text)
    scanner.close()
    return scanner.external
