from resolver.html_audit import external_markup_refs


def test_relative_references_are_not_external():
    assert external_markup_refs(
        '<link href="./style.css"><script src="bundle.js"></script>'
        '<img src="wait/wait-01.png" srcset="a.png 1x, b.png 2x">'
    ) == []


def test_absolute_subresources_are_flagged():
    refs = external_markup_refs(
        '<script src="https://cdn.example/three.js"></script>'
        '<link href="//fonts.example/font.css" rel="stylesheet">'
        '<img srcset="https://cdn.example/big.png 2x, small.png 1x">'
        '<object data="https://cdn.example/w.wasm"></object>'
    )
    assert refs == [
        "https://cdn.example/three.js",
        "//fonts.example/font.css",
        "https://cdn.example/big.png",
        "https://cdn.example/w.wasm",
    ]


def test_inline_script_strings_are_inert():
    # A URL inside script text is a string, not a declared subresource —
    # exactly the w3.org-namespace shape that a naive grep misflags.
    assert external_markup_refs(
        '<script>const ns = "http://www.w3.org/2000/svg";'
        'fetch("https://api.example/data")</script>'
    ) == []


def test_navigation_links_are_not_subresources():
    assert external_markup_refs('<a href="https://github.com/mrdoob/three.js/">credit</a>') == []


def test_css_url_and_import_are_scanned():
    assert external_markup_refs(
        '<style>@import "https://fonts.example/x.css";'
        "body { background: url('textures/bg.png'); }</style>"
        '<div style="background: url(https://cdn.example/bg.png)"></div>'
    ) == ["https://fonts.example/x.css", "https://cdn.example/bg.png"]


def test_local_schemes_are_not_external():
    assert external_markup_refs(
        '<img src="data:image/png;base64,AAAA">'
        '<video src="blob:abc123"></video>'
        '<a href="#menu"><use href="#icon"/></a>'
    ) == []


def test_svg_fetching_hrefs_are_scanned():
    assert external_markup_refs(
        '<svg><image href="https://cdn.example/tex.png"/>'
        '<use xlink:href="sprites.svg#dot"/></svg>'
    ) == ["https://cdn.example/tex.png"]
