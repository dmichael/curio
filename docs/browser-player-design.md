# Curio web interface design

- Status: Implemented (first pass)
- Date: 2026-08-14 (revised 2026-08-15)
- Scope: a minimal appliance homepage and single-work display page

## Decision

Curio is a network appliance and has a small web interface at its public origin.
This is not a standalone player, a DP-1 implementation, or a catalogue UI.
Curio serves the pages as part of the existing resolver service.

The initial interface has two pages:

| Route | Purpose |
|---|---|
| `GET /` | Minimal Curio appliance homepage and resolve form |
| `GET /display?uri=<curio-media-uri>` | Display one URI served by this Curio |

The homepage's web workflow is preview-first. This intentionally inverts the
REST and MCP workflow:

- REST/MCP resolve means resolve and store.
- The homepage's **Resolve** action means resolve and preview.
- Checking **Save to Curio** makes the web action resolve and store before
  displaying the result.

## Homepage

Opening `http://<curio-host>:8090/` should return a simple, useful page rather
than an API response or a blank route. The first version needs only:

- the Curio name and installed version;
- a short statement that this is the Curio appliance on the local network;
- one text field for an artwork URI;
- a **Resolve** submit button;
- an unchecked **Save to Curio** checkbox;
- links to health/status and API documentation.

Use plain server-rendered HTML and a small bundled stylesheet. There is no
JavaScript application, account, navigation system, or build toolchain.

The homepage is not a catalogue browser. It is a small human-facing front door
to Curio's existing resolver.

## Web resolve flow

The homepage submits a form to `POST /display` with these fields:

| Field | Meaning |
|---|---|
| `uri` | Artwork reference to resolve |
| `save` | Optional checkbox; absent by default |

On success, `POST /display` returns `303 See Other` to a GET display URL. This
keeps refreshes and bookmarks on a read-only GET rather than repeating the
resolve request.

### Preview (default)

When `save` is absent:

1. Resolve the submitted artwork URI using the same resolver as REST and MCP.
2. Do not create a stored resolution record, pin IPFS, promote static media to
   stored status, or otherwise admit the work to Curio's durable library.
3. Redirect to `/display?uri=<resolved-curio-media-uri>`.

Resolution can populate ordinary appliance caches. Preview means “not retained
as a saved Curio work,” not “perform no network or disk I/O.” Cached preview
content remains subject to the appliance's cache policy and may disappear.

### Save to Curio

When `save` is present:

1. Resolve and store the submitted URI with the same semantics as REST/MCP
   `POST /resolve`.
2. Record the successful reference for later playback.
3. Redirect to `/display?uri=<stable-curio-media-uri>`.

The stable URI should be the `media_url` from the stored resolution, normally a
same-origin `GET /resolve?ref=...` URL. A failed resolve stays on a useful error
page and does not redirect to an empty display.

The implementation should share one resolve result between preview and save;
checking the box must not fetch the artwork twice merely to reuse the existing
API handler.

## Display contract

The display URL is:

```text
http://<curio-host>:8090/display?uri=<curio-media-uri>
```

Here `uri` is not an arbitrary internet URI and is not resolved by the GET
handler. It must be a URI on this Curio's public origin produced by the web
resolve flow or by Curio's existing resolve response. Accepted targets are the
existing playback routes:

- `/resolve?ref=...`
- `/ipfs/...`
- `/arweave/...`
- `/media/...`

Reject a different origin, credentials in a URI, an unsupported path, or a
missing/unplayable catalogue or static-media target. Native IPFS and Arweave
availability is checked by the display page's same-origin `HEAD` request so a
cold or missing gateway object produces an in-page playback error. Never embed
the submitted URI before validating and normalizing it against Curio's
configured public origin.

`GET /display` is read-only. It adds browser presentation around a Curio-served
media URI; it never discovers, resolves, stores, or proxies an arbitrary source.

This division allows both desired cases:

- a saved work uses a durable `/resolve?ref=...` URI;
- an unsaved preview uses the Curio media URI returned by transient resolution.

A preview display URL is not promised to survive cache eviction or appliance
restart. Saving the work is what creates that durability contract.

## Rendering

Select the renderer from the media response's `Content-Type`, not from the
submitted source URI or filename. A small script may issue a same-origin `HEAD`
request before creating the media element.

| Media type | Rendering |
|---|---|
| `image/*` | `<img>` centered with `object-fit: contain` |
| `video/*` | `<video playsinline autoplay loop controls>` |
| `audio/*` | `<audio autoplay controls>` on a black page |
| `text/html`, `application/xhtml+xml` | sandboxed fullscreen `<iframe>` |
| anything else | clear unsupported-media response |

The page has a black background, fills the viewport, and preserves image and
video aspect ratio without cropping. It may show a small error overlay, but it
does not need custom transport controls beyond native audio/video controls.

Browsers commonly block audible autoplay. Video should begin muted when needed
and expose native controls so the viewer can enable audio. Curio must not claim
that playback started when the browser rejected it.

HTML artwork is untrusted. Render it in a sandboxed iframe, initially with
`allow-scripts` and without `allow-same-origin`, top navigation, or popups.
Capabilities can be reconsidered against real works, but artwork must not run in
the homepage's document context.

## Packaging

Keep the homepage, display document, CSS, and small display script in the
resolver's Python package. Use server-rendered HTML or small static templates
and no third-party browser assets. The routes must be registered before the MCP
sub-application mounted at `/`.

HTML documents should revalidate so an appliance update does not leave an old
page cached indefinitely. Immutable media continues to use the existing Curio
media routes and caching behavior.

## Security

The existing trusted-network model still applies. The web form exposes existing
resolver behavior; it does not make Curio suitable for public-internet hosting.

- Treat submitted values and stored labels as text, never HTML.
- Validate display targets as same-origin Curio playback routes.
- Do not proxy or embed an arbitrary submitted source URI directly.
- Keep GET routes free of resolve and storage side effects.
- Ship no analytics, third-party scripts, fonts, or service worker.
- Apply a restrictive Content Security Policy compatible with the required
  media elements and sandboxed artwork iframe.

## Local iteration

The homepage and display shell run with the Python resolver alone; Kubo, AR.IO
Core, and Compose are not required to inspect the interface:

```bash
cd resolver
content-resolver
```

Media resolution still requires its corresponding appliance backends.

## Testing

Required route and browser tests:

- `/` returns the homepage, URI field, Resolve button, and unchecked Save box;
- default form submission resolves once without creating a durable resolution
  record, then redirects to `/display`;
- checking Save resolves once, stores with REST-equivalent semantics, and
  redirects to a stable display URL;
- failed resolution shows a useful error and creates no saved record;
- display rejects external origins and unsupported Curio paths;
- a known landscape or portrait image fills the display without cropping;
- video loops without a page reload at the loop boundary;
- browser autoplay rejection has a usable fallback;
- HTML renders in the sandboxed iframe;
- submitted text cannot inject markup;
- refreshing the display page does not repeat resolution;
- `/` and `/display` are available through appliance port 8090 after install,
  update, and restart.

## Acceptance criteria

- Visiting the Curio origin produces a recognizable appliance homepage.
- Pasting an artwork URI and pressing Resolve previews it in the browser.
- Preview does not add the artwork to Curio's durable library.
- Selecting Save to Curio gives the work the same storage guarantees as the
  REST and MCP resolve operations.
- Both paths end on a refresh-safe `/display` URL.
- `GET /display` accepts only Curio-served media and has no resolution or storage
  side effects.
- No standalone player, additional service, or JavaScript toolchain is added.

## Deferred

Catalogue browsing, multiple-work playback, remote control, richer diagnostics,
DP-1 playlist consumption, Chromecast, AirPlay, DLNA, and signage integrations
are separate future decisions. They are not requirements of this interface.
