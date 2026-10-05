# Internals

Read this before changing anything under `scripts/`. Almost every non-obvious line in the
server exists because the obvious version was tried and broke; each section below names the
failure, so a "simplification" can be checked against it first. The agent-facing usage doc
is `SKILL.md`; this file is for whoever maintains the scripts.

## Per-run directory and `runId`

- `--new-dir` returns a directory owned by one run, under the Claude Code session's scratch
  root (`/private/tmp/claude-<uid>/<project>/<session-id>/decisions/`) when it can find
  one, and an ordinary temp dir otherwise. A *session* directory alone does not isolate two
  decisions, since every turn and every subagent of a session shares it; the per-run
  subdirectory is what does. Locating the session root relies on an undocumented harness
  layout, so every failure there falls back to a temp dir.
- An explicit `--out` or `--spec` directly in `/tmp`, `/var/tmp` or `$TMPDIR` prints a
  warning. The spec's directory is checked too, because it is exposed during the
  write-then-launch window.
- The result's `runId` is printed on stderr at launch. Comparing it matters only when
  reading the result FILE; a caller reading the task's stdout is reading per-task output
  that nothing else can write, so a ritual comparison there is a check that can never fire.
  `runId` is not the `?k=` token: the token is a capability and never lands on disk.

## Markdown rendering

- Block fields are rendered by a **vendored** `marked` (`assets/marked.umd.js`, served at
  `/marked.js`), so the page works offline. If the file is missing, `/marked.js` 404s and
  the page degrades to the inline renderer rather than failing. The pinned version is in
  the file's banner (`head -5 assets/marked.umd.js`). To bump it:
  `curl -o assets/marked.umd.js https://cdn.jsdelivr.net/npm/marked@<ver>/lib/marked.umd.js`
- `marked` passes raw HTML through, so its output is sanitized before insertion. Dropped
  with their text: `<script>` `<style>` `<iframe>` `<object>` `<embed>` `<form>` and other
  executable or embedding tags. Stripped: `on*` handlers, `id`, and every attribute outside
  a small allowlist. A link that loses its `href` is unwrapped to plain text so it cannot
  look clickable; a stripped image renders as `[image: <alt>]` rather than vanishing.
- `id` is stripped everywhere so raw HTML in a body cannot mint an `id` that shadows the
  page's own elements. `marked` emits no heading ids either, so in-page anchors have
  nothing to point at.
- The inline renderer HTML-escapes first and has no dependency. A `|` in a `pros` bullet
  stays a literal pipe.
- `<details>` / `<summary>` are not in `MD_TAGS`, so `mdSanitize` unwraps them in a `body`.
  An `html` visual is attribute-escaped into `srcdoc` and never reaches the sanitizer,
  which is why collapse/expand works there.

## Visuals

- `svg` is injected into the main document unsanitized; that is what makes arbitrary SVG
  work, and it is why the field is documented as trusted input.
- `html` visuals get a scriptless sandboxed iframe. The iframe is its own document and
  inherits none of the page's theme, so the server prepends `color-scheme: light dark` plus
  `background: Canvas; color: CanvasText`. Without that prelude the embedded canvas was
  painted white in dark mode, and the white covered the `background` the page set on the
  iframe element, which is why that rule looked correct and did nothing.
- `image` and `video` local paths are served through `/asset` by index: only files the spec
  references can be served. Video is served with byte ranges, which WebKit requires before
  it will play anything. `.mov` with PCM audio is verified only in the native window; the
  Chrome tiers may not play it.

## Languages and translation

- **The page's own text follows the conversation, not the settings.** The agent writes in
  the language it is chatting in; primary sets only the fixed labels, secondary the
  translate target and the gloss language. An OS-derived primary is a guess about the
  reader, and the conversation is not. Known edge: when the conversation is in the
  secondary, the button still offers that same language and its back label names the
  primary; the agent skips glosses there, and nothing else adapts, because the page does not
  declare its own language.
- **The server resolves the languages; the page cannot.** On a Mac whose preferred
  languages are English then Chinese, the native WKWebView reports
  `navigator.languages = ["en-US"]` and the Chrome fallback `["en-US", "en"]`. So
  `resolve_languages()` reads `~/.config/rich-decision/config.json`, else the OS list
  (macOS `AppleLanguages` from the global preferences plist via `plistlib`, no subprocess;
  `LANGUAGE` / `LC_*` / `LANG` elsewhere), else English. It runs once per server, so the
  labels, the button and every `/translate` call agree even if the file changes mid-decision.
- A config file wins even without `secondary`: that is the off switch for someone whose OS
  lists a second language. A file without `primary` keeps the OS primary rather than English,
  or naming only a secondary would silently relabel a non-English page. A malformed tag, or
  a secondary that reads the same as the primary, is warned about on stderr, never silently
  used.
- "Reads the same" is `_variant()`: language plus script, never region. en-US and en-GB are
  one reader; zh-Hans and zh-Hant are two, and a Chinese tag with no script takes it from
  its region (TW / HK / MO Traditional, the rest Simplified).
- Tags stay whole (`zh-Hans-US`), because script and region pick the variant: the prompt
  passes the BCP 47 tag and tells the model Hans is Simplified, Hant Traditional.
- The button shows only when a secondary is set AND `_claude_bin()` finds the CLI: without
  it the button could only fail, so the page is told there is no target. Glosses still follow
  the secondary. It is labelled with the language's own name from
  `Intl.DisplayNames` (falling back to the upper-cased code on WebKit older than Safari
  14.1). `/translate` refuses with 400 when no secondary is set, and always uses the
  server's target, never one the page names.
- Nothing is translated until the button is clicked. The click POSTs the rendered strings
  to `/translate`, which shells out to `claude -p` and pushes each string back as an NDJSON
  record the moment the model finishes generating it; the page paints them top-down into
  the same nodes. Streaming makes the wait visible, not shorter, and costs the same tokens.
- **No API key, no exported token.** `claude -p` authenticates the way the interactive CLI
  already does. Don't add `--bare` (it skips that login and fails "Not logged in") and
  don't plumb in `ANTHROPIC_API_KEY`.
- **`--safe-mode` is load-bearing, not hygiene.** It disables CLAUDE.md / skills /
  plugins / hooks / MCP for the child. Without it a user-level `~/.claude/CLAUDE.md` follows
  the subprocess from any cwd, and any style rule in it (half-width punctuation, say) bleeds
  into the translation. It also removes tens of thousands of input tokens of session
  preamble per call. Paired with `--no-session-persistence`, or every call leaves a
  transcript of the decision text on disk forever.
- **Structured output, not a hand-rolled protocol.** `--json-schema` carries multi-line GFM
  losslessly and makes a dropped key a validation error rather than a silent truncated
  tail. One repair survives on top: the model intermittently double-escapes, emitting `\n`
  as two characters, which would eat a table or a fence. `_repair_escapes` undoes that,
  gated on *the source having had real newlines and the output having none* — a
  single-line value can never match, which keeps it off legitimate `` `\n` `` code spans.
- **ASCII punctuation occasionally survives in Chinese prose** (`队列,以保证`). Do not fix
  this with a post-processor. Two have been tried and both corrupted real content — one
  widened the `?` in a CJK URL into `？` and produced dead links, the other blanked
  `` `\n` `` code spans. A half-width comma is cosmetic; a dead link in the text being
  decided on is a defect. The prompt asks for full-width, which is as far as this goes.
  `_PUNCT_RULES` carries that rule for Chinese and Japanese targets only.
- **The translation cache digest includes the target**, so a map can never be served in
  the wrong language.
- `preview` and every `visual` are never translated (`preview` is the verbatim text being
  approved; visuals are code). Glosses are left alone (already in the secondary). Option ids
  and labels in the result stay untranslated.
- **Fixed chrome (Pros / Cons / Recommended / Confirm / hints) is hand-written in English
  and Simplified Chinese** in the page — deterministic, free, and it flips instantly while
  the content is in flight. Only a Simplified tag (`isHans`) uses the Chinese table, so
  zh-Hant never gets Simplified labels. A primary with no hand-written chrome shows English;
  a target with none gets its chrome from the model, in the same request as the content.
  Status lines (progress, partial, failure) are hand-written only: Simplified Chinese for a
  Simplified target, English for every other.
- **Failure degrades, never blocks, and never un-paints.** A failed chunk leaves its items
  English; partial results are spliced, never discarded. Once streaming has started the
  page keeps every string already painted and reports how many strings stayed in the
  original; only a run that delivered nothing reverts fully, with a failure line. Confirm is untouched on every path.
- **A streamed value is a preview; the envelope is the authority.** Incremental records come
  off `--include-partial-messages` deltas, which nothing has validated. The closing
  `{"type":"result"}` line carries the schema-checked payload, and any key it disagrees
  with is re-sent and repainted. A value is emitted only once its closing quote arrives,
  and **only a run that produced a validated map is cached** — caching what merely
  streamed would poison the cache with an empty map and make every retry return nothing.
- **All rejections happen before the first byte.** A streamed reply spends its status code
  when the headers go out, so `400` / `413` / `429` — including the non-blocking
  single-flight `acquire` — live in `translate_precheck`. After that a failure can only
  travel as an `{"t":"error"}` record.
- **`kill_translations()` on every exit path is what lets the process exit.**
  `ThreadPoolExecutor` workers are non-daemon and are joined at interpreter shutdown, so a
  translation still running at Confirm would keep the process alive after the popup closed
  and the result was written. Killing the children makes that join instant.
- **`/translate` refuses anything that isn't same-origin JSON.** It spawns an LLM
  subprocess under the user's login, so a page in another tab that guessed the port could
  otherwise use it as an oracle: a cross-origin `<form enctype="text/plain">` POST is a CORS
  *simple* request whose body can be shaped to parse as JSON. Requiring
  `application/json` forces a preflight, and `OPTIONS` is not routed.

## The popup

Three tiers, best available wins. The alert sound plays only once a tier has proven itself,
and the server closes the window once the choice is in.

1. **The native `Rich Decision` app** — `scripts/decision_host.swift`, a WKWebView in an
   `NSWindow`, compiled on first use into a real `.app` bundle by `scripts/native_host.py`.
   Its own name and icon in the Dock, cmd-Tab, Mission Control and the menu bar.
2. **A chromeless Chrome `--app` window** — used when the native host can't be built.
3. **A plain browser tab** — non-macOS, or `--no-app`.

### Why the native tier exists

"It opens behind things" was a bug (see `_raise_pid`) and is fixed on the Chrome path too.
"I can't tell which app it belongs to, and can't find it again once it's behind something"
is *structural*: a Chrome `--app` window on a private profile is an anonymous window wearing
someone else's icon. Confirmed-raise plus a title marker was tried first and was not enough
in practice. Owning the app fixes it, and brings `NSApp.activate(ignoringOtherApps:)`, a real
API rather than AppleScript that needs Automation permission and can silently no-op.

**It is an ordinary window and can be covered — deliberately; don't make it `.floating`.**
Findability is served by the app existing at all, and pinning it above everything only
stops the user reading the code or docs they need in order to decide. **Always on Top** is
opt-in on `cmd-shift-T` and persists. **Zoom** (`cmd +` / `cmd -` / `cmd 0`, `cmd =` as the
unshifted alias) is hand-built on `pageZoom` and persists: a WKWebView has no zoom of its
own, and anything Chrome gave for free has to be re-supplied here or it silently regresses
when the native tier wins.

### Every tier must PROVE it drew a window, with a token — not a flag

`_probe_url` appends `?_w=<hex>` to the URL each tier is launched with, the `GET /` handler
echoes that token into `ctx["served"]`, and `_await_window` waits for *its own* token. The
naive versions all broke, each ending with the chime playing, `open_page` reporting success,
the remaining tiers skipped, and **no window on screen**:

- a plain boolean latch — a late-rendering tier 1 satisfies tier 2's proof;
- cleared per tier — the same late render still lands between the clear and the check;
- either of the above — the URL is printed to stderr *before* any tier launches, so anything
  that opens or curls it pre-satisfies every tier at once.

**Never drop `?_w=`** — it looks like an inert query param and is the entire proof. **Never
store an untagged fetch**: recording `""` would let the user opening the printed URL
*un*-satisfy the tier that genuinely drew a window. Do not substitute `_raise_pid`'s bool,
which returns False when Automation is merely denied — falling through on that opens two
windows instead of none.

### Reusing the host for another tool

`native_host.py` and `decision_host.swift` are reusable by any local tool that wants a real
window for a local web page: pass a different `Info.plist` (its own `CFBundleName` and
bundle id) and, optionally, `resources={"AppIcon.png": png_bytes}`. Both are part of the
cache key, so each caller gets its own cached bundle and Dock identity out of the one Swift
file — **never copy that source to give a tool a window**. The host shows
`Contents/Resources/AppIcon.png` when present and its own drawing otherwise; two apps
drawing the same glyph are one app at Dock size.

### The build cache

`NativeHost.build()` compiles into `<cache_root>/<hash>/` (the popup uses
`~/Library/Caches/claude-rich-decision/`), keyed on the Swift source + `Info.plist` +
resources + macOS major version + `platform.machine()` + `BUILD_RECIPE` — every term free to
read, so **a cache hit runs no subprocess at all**. Keying on the `swiftc` version instead
would force a toolchain probe on every popup. The arch and the compiler flags are in the key
because `os.path.exists(exe)` is the only validity test: an arm64 bundle restored onto an
Intel Mac, or a flag change, would otherwise be served forever. `-sdk` is deliberately
**not** in the key: computing it needs a subprocess. Traps already paid for:

- **Never gate on `shutil.which("swiftc")`, and never trust `xcode-select -p`'s exit code.**
  `/usr/bin/swiftc` is the xcode-select shim, present on every Mac including ones with no
  toolchain, and invoking it without a usable developer dir opens a blocking GUI install
  dialog. `xcode-select -p` prints whatever `DEVELOPER_DIR` points at and **exits 0 even
  when that path does not exist**. `swiftc_path()` therefore stats its way to an absolute
  `swiftc` *and* an SDK, so the shim is never what gets exec'd — stronger than a timeout,
  since killing swiftc would not dismiss a dialog owned by the installer.
- **Pass `-sdk` explicitly.** The shim silently supplies one, so code that works through
  `/usr/bin/swiftc` fails the moment it calls the real binary: *"unable to load standard
  library for target arm64-apple-macosxNN"*. Xcode keeps its SDK under `Platforms/`, a
  Command-Line-Tools-only install under `SDKs/`.
- **`-target` is load-bearing.** With no deployment target swiftc stamps the minimum OS
  from the SDK, so an Xcode newer than the running macOS produces a binary that machine
  cannot open, and LaunchServices refuses it with -10825.
- **Build inside the cache dir, not `$TMPDIR`.** The install is an `os.rename`, which fails
  `EXDEV` across filesystems.
- **No `exists()` pre-check before the rename.** Two servers building at once both pass it,
  and the loser's rename fails `ENOTEMPTY` and silently degrades. Let it throw, catch
  `OSError`, and use the winner's bundle.
- **Failure handling is graded.** No toolchain writes a permanent `.unbuildable` marker
  (delete it to retry; its path is printed every time). A build *timeout* does not — that is
  transient. A binary that builds but will not launch is evicted so the next run rebuilds.
- `NSAllowsLocalNetworking` in the plist (ATS blocks plain http, and this page is only ever
  served over http on a local address — without it the window opens blank). The source is
  copied to a file named `main.swift` to compile, because it has top-level code.
- **A rebuilt bundle does not replace a running one.** A re-key moves the bundle's path,
  and LaunchServices launches a SECOND instance despite the identical bundle id. A caller
  that keeps a long-lived window should quit instances from older cache keys before
  opening.

### Window title and project label

The window title carries the decision's subject, prefixed `[Claude · <project>]`, set from
the page `<title>` via KVO on `WKWebView.title`. The prefix is what makes the *Chrome
fallback* findable in cmd-Tab. The project rides **inside** the bracket because cmd-Tab and
Mission Control truncate the tail.

`detect_project()` walks up to the nearest `.git` from three candidates, first repo found
wins: `$CLAUDE_PROJECT_DIR` → the cwd → **the session's own transcript `cwd`**. A leading dot
is dropped so `~/.claude` reads as `claude`; `$HOME` and `/` yield no label; no repo found
means no label.

- The transcript source rescues a popup launched from the session scratchpad or any
  non-repo cwd. It reads `~/.claude/projects/*/<CLAUDE_CODE_SESSION_ID>.jsonl` (first `cwd`,
  within the first ~50 lines) rather than decoding the mangled project segment of the
  scratchpad path, because that segment cannot be decoded: Claude Code collapses `/`, `.`
  and `_` all to `-`. The session id is regex-validated before it is spliced into a glob.
- It never shells out to `git`: `/usr/bin/git` is the same xcode-select shim on a Mac
  without Command Line Tools, and a caption must never be able to stall the popup. Every
  failure path returns `""`, since it runs before the server is up.

### `_raise_pid` is for the Chrome tier only

It re-issues the AppleScript raise until System Events reports our own pid frontmost,
because raising a process whose window Chrome has not drawn yet is a silent no-op that still
exits 0. Do **not** add it to the native tier: the host already calls
`NSApp.activate(ignoringOtherApps:)`, and layering AppleScript on top hands back the very TCC
dependency that tier removes.

### Links

`openExternally` in the Swift host allowlists `http`/`https`/`mailto` before
`NSWorkspace.open`. The page's sanitizer already restricts hrefs, but that is the renderer —
the least privileged component — while `NSWorkspace.open` is the privileged sink, and Chrome
confirms foreign schemes where a bare `open` would not. Main-frame navigation is pinned to
the launch origin by scheme, host **and port**; host alone admits any scheme on the loopback
address, and scheme+host still admits every other port on it (a dev server, `:9222`
DevTools). Sub-frames are exempt so `srcdoc` visuals work.

### The Chrome fallback's throwaway profile

It runs in a throwaway `--user-data-dir`, by necessity: a real, separate browser instance
whose pid the server owns, so focus and auto-close target exactly that window. Without it
Chrome's singleton lock forwards the launch to a running instance, leaving only the app
*name* to address, and AppleScript resolves a name by bundle id — with two Chromes running
(an automation browser beside the user's own) it picks an arbitrary one. Consequence: the
page loads with no cookies, logins, or extensions, so an auth-gated remote image will not
render there (the native host has no such limitation).

## Network exposure

- The server binds every interface by default so a phone on the same Wi-Fi can answer;
  `--no-lan` narrows it to `127.0.0.1`. There is no network allowlist.
- **Every route requires the per-run access token** (`?k=`, 80 bits) — `/`, `/asset`,
  `/marked.js`, `/submit`, `/translate`, no exemptions. The port alone never guarded
  anything: it is ephemeral but not secret, and a bare `curl` POST to `/submit` would hand
  the calling agent a decision the user never made.
- **The token guards against guessing, not reading.** Plain HTTP carries it in the clear on
  every request, so an on-path observer on the same L2 can forge a `/submit`. That is a
  real gap on open Wi-Fi and a negligible one on a home router, which is why the default is
  on and `--no-lan` exists. The blast radius is bounded: a forged pick is advisory, and
  every irreversible step downstream should have its own human gate.
- The LAN address is whatever the *default route* uses: under a full-tunnel VPN that is the
  tunnel address. `no LAN address found` means no default route; localhost still works.
- **The token is visible to other local processes**: it is in the popup browser's argv. A
  local process could always reach the port anyway, so "the URL is the capability" holds
  against the network, not against this machine.
- The cross-origin content-type guard on `/translate` is a second, narrower layer for a
  browser that legitimately holds the token being driven from another tab.

## Exit

If the user closes the window without choosing, the server keeps waiting. Killing it is
clean: on SIGTERM it reaps its popup and the Chrome fallback's temp profile. The Swift host
turns SIGTERM into an ordinary `NSApp.terminate`, because the default action kills it
without telling LaunchServices and leaves a blank "Running in Background" Dock tile.

## README images

`node docs/images/make.mjs` regenerates every image in `docs/` from the specs in
`examples/` and the layout pages in `docs/images/`. Run it after any change to the page's
look; nothing else notices that the README's pictures have gone stale.

- It needs Node 22+ (it uses the global `WebSocket`, so there is nothing to install),
  Google Chrome, ffmpeg 5.1+ (for `-fps_mode`) and the `claude` CLI. Set `CHROME` when the
  browser is not at its default path.
- It drives headless Chrome over the DevTools protocol rather than `--screenshot`,
  because the GIF and the gallery need clicks, typed notes and the translate button.
- Languages are pinned to `en` + `zh-Hans` through a temporary config, so the output does
  not depend on the maintainer's OS language list.
- Every image is built in a temp dir and copied into `docs/` only once all six exist, so a
  failed run leaves the committed set whole. The `claude` check runs first, because without
  the CLI the server hides the translate button on every page.
- `gallery-phone.png` comes from a live model call, so its Chinese wording can change on
  every run. The other five come out byte-identical from run to run on one machine; fonts
  differ between systems, so another machine's run will differ slightly. In `demo.gif`
  only the page frames are captures: the terminal and the cursor are drawn by
  `docs/images/stage.html`.
