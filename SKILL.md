---
name: rich-decision
description: >-
  Present one or more decisions to the user as a rich HTML page served locally (comparison
  cards, pros/cons, score chips, code previews, SVG/Mermaid/HTML mockups, multi-select,
  notes) and capture the choices back into the conversation. Also explainer mode —
  `sections` of text + diagrams with no options — for visually EXPLAINING an idea; questions
  come back via notes. STRONGLY PREFER this over AskUserQuestion whenever ANY option carries
  detail a terminal list renders poorly (pros/cons, a snippet, a diagram, 3+ options,
  multi-select, several questions at once), AND whenever the turn hands back candidates I
  generated — next steps, proposed rules, ideas to triage, competing readings of an ambiguous
  request — even bare one-liners: a numbered list plus "which do you want?" is this skill's job,
  not chat's. Fall back to AskUserQuestion only for a plain one-line question with label-only
  options, or when no human is watching the turn (a subagent, or an unattended run).
allowed-tools: Bash, Write, Read
---

# Rich Decision UI

Serve a polished HTML decision page on localhost, open it in a popup window, and read the
user's choice back. Python stdlib only; the one bundled asset is a vendored copy of `marked`
for block markdown, so the page needs no network.

Commands below use the default install path `~/.claude/skills/rich-decision`. If this skill
lives somewhere else, substitute the directory this file was loaded from.

## When to use this vs. the built-in question tool

**Reach for this skill** the moment a decision carries detail that a terminal list
renders poorly. Any one of these is enough — don't wait for all of them:

- Options have **pros/cons lists**, a **summary**, or a **code/config snippet** to weigh.
- A **diagram, mockup, or screenshot** would help the choice (SVG / Mermaid / image).
- **3+ options** that benefit from side-by-side comparison cards.
- **Multi-select** (pick several), or a **free-text reasoning note** alongside the choice.
- **Several related questions at once** — this skill puts multiple questions on one page
  (see "Multiple questions" below), the same as `AskUserQuestion` but with rich cards.

**Fall back to the built-in `AskUserQuestion`** only for a genuinely plain question:
one line, a handful of short label-only options, no per-option detail. That's faster and
stays in the terminal. If you're on the fence and you "strongly feel" the richer UI is
warranted, use this skill — that instinct is the signal it's designed for.

## Workflow

1. **Ask the script for a directory, then write the spec into it** — never a fixed path like
   `/tmp/decision_spec.json`:

   ```bash
   python3 ~/.claude/skills/rich-decision/scripts/decision_server.py --new-dir
   ```

   It prints one absolute path and exits, plus a `languages:` line on stderr
   (`primary=en secondary=zh-Hans`) that says which language any glosses take. **Copy the
   path literally into the Write call and into step 2** — do NOT capture it in a shell variable: the Bash tool does not persist
   shell state between calls, and the spec has to be written in between, so a `$d` from
   step 1 expands to the empty string in step 2 and the server dies on `/spec.json`.

   The directory belongs to this run alone — under the session's own scratch root when
   running under Claude Code, an ordinary temp dir otherwise. A fixed name is a path two
   concurrent decisions both write: one user is shown the other's questions, and one agent
   reads back an answer its user never gave. A decision authorizes work, so that is a
   forged authorization, not a cosmetic mix-up.

2. **Launch the server in the background** so the blocking wait doesn't hit the Bash
   timeout — the user may take a while to decide:

   ```bash
   python3 ~/.claude/skills/rich-decision/scripts/decision_server.py \
     --spec /the/exact/path/printed/in/step/1/spec.json
   ```

   **Omit `--out`.** It defaults to `result.json` beside the spec, and prints the path on
   stderr as `Decision result: <path>  (runId <id>)`.

   Run it in the background. In Claude Code, use `run_in_background: true`;
   its task notifies you on exit. In Codex, use **one `functions.exec` call**:
   run `tools.exec_command` with the exact server command and a short
   `yield_time_ms`; print its launch output so the URL is available. If it returns
   a `session_id`, call `await yield_control()`, then await
   `tools.write_stdin({session_id, chars:"", yield_time_ms:300000})` in a loop until
   it returns an exit code; call `notify()` with the final exit code and stdout.
   This delivers completion as a new tool output without model-level polling.
   If the one-call listener API is unavailable, poll the session directly.
3. **Tell the user** a popup window opened and you're waiting for their pick — and
   **include the `http://127.0.0.1:<port>/?k=<token>` URL** the server printed on stderr,
   token and all — it is an access token, not decoration, and the URL 403s without it (read it
   from the background task's output). The popup is a separate window, not a tab in
   their own browser: if it ends up behind something, or they close it by accident, that
   URL is their only way back. Cmd-clicking it in the terminal reopens the same page
   against the same still-waiting server.
4. **When the command exits, take the choice from the background task's own output** — the
   server prints the whole result JSON to stdout, and that output is per-task by
   construction, so nothing else on the machine can write it.

   **Codex completion:** Keep the turn active until the listener reports the
   answer or the user explicitly cancels or defers the decision; elapsed time is
   not a reason to end it. If stdout is missing, read the printed result path:
   Confirm atomically writes `result.json` before server shutdown. Match its
   `runId` to the launch banner before using a file result.

The file at the printed path holds the same JSON and is the debugging artifact: it sits
beside the spec, so one directory shows both what was asked and what came back.

## Spec format

```json
{
  "title": "Pick a local store for the sync layer",
  "description": "Optional intro. Full markdown - tables, lists, headings.",
  "mode": "single",
  "options": [
    {
      "id": "hive",
      "label": "Hive",
      "recommended": true,
      "summary": "Pure-Dart KV store",
      "pros": ["No native deps", "Works on web"],
      "cons": ["No complex queries"],
      "effort": "low", "complexity": "med", "value": "high",
      "meta": ["$0/mo", "3 files"],
      "preview": "final box = await Hive.openBox('items');\nbox.put('k', v);",
      "visual": { "type": "svg", "code": "<svg ...>...</svg>" }
    },
    { "id": "sqlite", "label": "SQLite (drift)", "summary": "Relational, queryable",
      "pros": ["SQL queries"], "cons": ["Native deps"] }
  ]
}
```

Field notes:
- `mode`: `"single"` (default) or `"multi"` for checkbox-style multi-select.
- `recommended`: adds a green badge and sorts that card first. Put your recommendation first.
- `id`: optional; defaults to the array index. The result echoes these ids.
- `summary` / `pros` / `cons` / `preview`: all optional. `preview` renders in a
  monospace box; fenced ```` ``` ```` wrappers are stripped automatically.
- `effort` / `complexity` / `value`: optional per-option scores, each `"low"` | `"med"` |
  `"high"` (`"medium"`/`"mid"` and any casing accepted), rendered as a chip row under the
  label. **Qualitative on purpose — never numbers**: `value: 8` fakes measurement and
  invites summing across options; words read as the judgment they are. Colour encodes
  *direction*: effort and complexity are costs (low = green), value is a benefit
  (high = green), `med` is neutral. They are three distinct axes — effort is one-time
  cost, complexity is *standing* cost (a one-day hack that adds a daemon is low effort,
  high complexity) — so don't mirror one into another. **Use them when the page is a
  triage of homogeneous generated candidates** (ideas, next steps, proposed rules) where a
  consistent rubric beats reading N pros lists; **skip them on a 2-3-way design fork**
  where pros/cons already carry the argument. Score all options on the page or none — a
  lone scored card can't be compared with anything.
- `meta`: optional array of short free-form **fact** chips on the same row — `"$0/mo"`,
  `"3 files"`, `"reversible"`. Facts you can defend, not judgments; inline markdown works.
- `gloss`: optional one-line gloss in the user's secondary language — see "Languages" below.
- `allowNotes` (top level): **ignored** — the shared reasoning box is ALWAYS shown, and its
  text comes back as `notes` (`""` if empty). Only the per-question `allowNotes` does
  anything (see "Multiple questions" below).

### Markdown

Which renderer a field gets depends on whether it is a prose *block* or a *line*:

- **Full GFM (block)** in the three multi-line fields: the page `description`, each
  question's `description`, and every `sections[].body`. Tables, lists (nested too),
  headings, fenced code, blockquotes, rules, links. A lone newline stays a line break.
- **Inline only** everywhere else — `label`, `summary`, `pros`, `cons`, `gloss`:
  `**bold**`, `*italic*`, `` `inline code` ``, `[text](https://url)`.
- `title` and `preview` stay literal (`title` is also the page `<title>`; `preview` is
  a code box).
- **Sanitized before insertion.** Script, style, iframe, form and other executable or
  embedding tags are dropped, `on*` handlers and `id` are stripped, and `href`/`src` must
  start with `http://`, `https://`, `mailto:`, `/asset`, or `data:image/…`.
- **Markdown images need a URL, not a local path.** `![](https://…)` and
  `![](data:image/png;base64,…)` work; `![](./diagram.png)` does NOT — use `visual` for
  local files. In-page `#anchor` links are not supported.

### `visual` — a rich graphic at the top of a card

Prefer the inline forms (`svg`, `mermaid`, `html`) when they can say it: they need no file
serving. `image` and `video` with a local path are served from a whitelist of the paths the
spec names, so there is no path-traversal surface.

- `{ "type": "svg", "code": "<svg ...>...</svg>" }` — author the SVG yourself for layouts,
  flows, or simple mockups. Renders inline, works offline. **Injected exactly as written —
  not sanitized, not sandboxed**, so `code` is trusted input: never paste it in from a
  source you would not equally trust with the rest of the spec.
- `{ "type": "mermaid", "code": "graph LR; A-->B" }` — a Mermaid diagram, rendered
  client-side from a CDN; offline, it falls back to showing the diagram source.
- `{ "type": "image", "src": "/abs/path/to/img.png" }` — a real raster: a local file path
  (a generated illustration, a screenshot) or an `http(s)://` URL.
- `{ "type": "html", "code": "<div>…</div>" }` — a self-contained **HTML/CSS mockup** in a
  sandboxed `iframe srcdoc`, auto-sized, with an **Enlarge** button for a full-size view.
  Use it for a faithful UI mockup — native-app chrome, a web component, a settings pane —
  where real CSS layout, typography and light/dark theming matter. The preview is INERT
  (no scripts, forms, or navigation), so keep it presentational with all CSS inline. Write
  just the fragment; no `<!doctype>`/`<html>`/`<head>` needed.

  **Colours: set BOTH or set NEITHER.** The iframe follows the viewer's light/dark theme
  when the mockup declares no colours, which is the default to prefer. If you hardcode a
  text colour you MUST hardcode its background too, or one theme renders invisible text.
  Prefer the system pair `Canvas`/`CanvasText`, or an explicit pair like
  `background:#eef;color:#123`.
- `{ "type": "video", "src": "/abs/path/to/clip.mp4" }` — a playable video with native
  controls, for reviewing a render, a screen recording, or an animation. Same `src` rules as
  `image`. H.264 in `.mp4` is the safe choice. Put video here, never as a `<video>` inside an
  `html` visual — that iframe has no route to a local file.
- Shorthand: a bare string is treated as SVG if it starts with `<svg`, otherwise as an
  image `src`.

Which to reach for: **SVG** for diagrams/layouts you can describe in markup; **Mermaid** for
flow/sequence/graph diagrams that are tedious in raw SVG; **HTML** for a faithful UI
mockup; **image** only for a real raster; **video** when the thing being judged moves or
makes sound.

### Languages and `gloss`

Write the page in the language of the conversation. The user also has a **primary**
language and, optionally, a **secondary** one; `--new-dir` prints both. The primary sets the
page's fixed labels; the secondary is the translate button's target and the gloss language.

- **`gloss`** — every page, section, question, and option takes an optional one-line gloss
  in the secondary language, rendered as a muted line under its heading so the decision can
  be skimmed in that language. Never required; leave it out when there is no secondary, or
  when the conversation (and so the page) is already in the secondary, where it would only
  repeat the text. Keep it to one short line — a gloss, not a translation. For an option: what it *is* + the
  one thing that decides for or against it. Same inline markdown as the other text fields.
- **The translate button** appears only when a secondary language is set and the `claude`
  CLI is installed. It is labelled in that language (中文, 日本語, Español) and translates
  the page on demand (see Notes).
- **The fixed labels** (Pros, Cons, Confirm, hints) follow the primary: hand-written for
  English and Simplified Chinese, English for any other language.

The user sets both in `~/.config/rich-decision/config.json` (`$XDG_CONFIG_HOME` is
honoured):

```json
{ "primary": "en", "secondary": "zh-Hans" }
```

Without that file the server takes the first two distinct languages of the OS preference
list (macOS Language & Region; `LANGUAGE` / `LANG` elsewhere). A config file without
`secondary` turns the button off; one without `primary` keeps the OS primary. Tags are
BCP 47: `zh-Hans` is Simplified Chinese, `zh-Hant` Traditional, and the two count as
different languages.

## Multiple questions on one page

To ask several related decisions at once (like `AskUserQuestion`'s 1-4 questions),
replace the top-level `options` with a `questions` array. Each question is its own
labelled section with an independent grid of option cards, its own `mode`, and its own
optional `description`. A shared notes box always sits at the bottom. For a note attached to a
*specific* question (not the shared box), set `allowNotes: true` on that question — it gets
its own notes field, returned as `answers[i].notes`.

```json
{
  "title": "Configure the sync layer",
  "description": "Two related decisions.",
  "questions": [
    {
      "id": "store",
      "title": "Pick a local store",
      "description": "Optional per-question intro.",
      "mode": "single",
      "allowNotes": true,
      "options": [
        { "id": "hive", "label": "Hive", "recommended": true, "pros": ["No native deps"] },
        { "id": "drift", "label": "Drift", "summary": "Relational, queryable" }
      ]
    },
    {
      "id": "sync",
      "title": "Sync strategy (pick all that apply)",
      "mode": "multi",
      "options": [
        { "id": "push", "label": "Push on write" },
        { "id": "pull", "label": "Pull on open" }
      ]
    }
  ]
}
```

- Each question takes the same fields as a single-question spec: `id` (optional, defaults
  to `_qN`), `title`, `gloss`, `description`, `mode`, an optional `allowNotes`, and an
  `options` array with the identical option shape.
- `mode` is **per question**; mix `single` and `multi` freely.
- The page numbers the sections (`1 / N`).
- If `questions` is present, the top-level `options`/`mode` are ignored.

## Explainer mode (`sections`) — show an idea, not just a choice

To visually EXPLAIN something — an architecture, a flow, a design, "here's how X works" —
add a top-level `sections` array. Each section is a full-width content block rendered ABOVE
any questions: optional `title`, optional `gloss`, optional `body` (full GFM markdown), optional
`visual` (same five types as option cards, with a taller height cap since the visual IS the
content).

```json
{
  "title": "How the sync layer works",
  "sections": [
    { "title": "1. The write path",
      "body": "Every write lands locally first...",
      "visual": { "type": "mermaid", "code": "graph LR; App-->Hive-->Sync-->Firestore" } },
    { "body": "Cloud sync is opt-in per user." }
  ]
}
```

- `sections` composes with `questions`/`options`: sections render first, then the option
  grids — use this for "here's the context, now pick".
- **Sections-only spec (no questions, no options)**: the page becomes a pure walkthrough —
  the bottom bar reads "Read through, then Confirm", and the notes box becomes
  "Questions / comments". The result is `{"answers": [], "notes": "<their text>"}` —
  treat a non-empty `notes` as the user's questions and answer them in the conversation.
- Use this instead of terminal prose whenever a diagram or mockup would say it better, and
  instead of a hosted page when the explanation is ephemeral and private.

### Layered answers

A branching, multi-layer answer maps straight onto a spec; no template is needed:

- **Layer 1**, the verdict → the page `description`.
- **Layer 2**, the branches → one `sections[]` entry each, ordered by importance.
- **Layer 3**, the detail under a branch → that section's `body`.

**Collapse/expand needs an `html` visual, not a `body`.** `<details>` / `<summary>` written
into a `body` are unwrapped by the sanitizer and render permanently expanded. Inside
`visual: {"type": "html"}` they survive, and toggling works despite the sandbox because
`<details>` is native browser behaviour rather than script.

## Result format

The result always carries a `runId`, an `answers` array — one entry per question, in spec
order — and the shared `notes` (`""` if empty):

```json
{
  "runId": "5a0b1e0ea8ba4c04",
  "answers": [
    { "id": "store", "title": "Pick a local store",
      "choice": ["hive"], "chosen": [{ "id": "hive", "label": "Hive" }],
      "notes": "fine to revisit once web ships" },
    { "id": "sync", "title": "Sync strategy (pick all that apply)",
      "choice": ["push", "pull"],
      "chosen": [{ "id": "push", "label": "Push on write" }, { "id": "pull", "label": "Pull on open" }] }
  ],
  "notes": "web support is the decider"
}
```

- `runId` identifies the run that produced the result and is printed on stderr at launch.
  It matters only when reading the result FILE: equal means the answer is yours, a mismatch
  means another run wrote over it. It is not the `?k=` access token, which never lands on
  disk.
- `choice` is always an array (one element for `single`, zero-or-more for `multi`), and may
  be **empty** — answers are optional, so the user can confirm with just a `notes` value.
- `chosen` pairs each chosen id with its label, for convenience.
- `answers[i].notes` is present only for questions with `allowNotes: true`. The top-level
  `notes` is always the shared bottom box.
- Option ids and labels stay in the spec's language even when the user read the page
  translated, so a decision made in translation reads back identically.
- **Back-compat:** for a single-question (top-level `options`) spec, the result ALSO
  mirrors the lone answer at the top level as `choice` / `chosen`.

## CLI flags

| Flag | Default | Purpose |
|------|---------|---------|
| `--new-dir` | — | Print a fresh per-run directory for spec+result, then exit |
| `--spec` | (required) | Path to the spec JSON |
| `--out` | `result.json` beside the spec | Where to write the result JSON. Leave it unset; an explicit path in a shared temp root warns |
| `--port` | `0` (auto free port) | Fix the port if needed |
| `--no-open` | off | Don't open any window (print the URL only) |
| `--no-app` | off | Open a normal browser tab instead of the popup window (macOS) |
| `--no-sound` | off | Don't play the alert sound (macOS) |
| `--no-lan` | off | Localhost-only — nothing on the network can reach the page |
| `--lan` | — | No-op, accepted so an old invocation still runs: the LAN listener is the default |

### Answering from a phone or tablet — on by default

The server binds every interface and prints a second line,
`Decision UI (LAN): http://<lan-ip>:<port>/?k=<token>`. Hand that URL over whenever the
user wants to judge a mockup or screenshot on the device it targets — a phone renders a
phone mockup and a laptop does not. Either device can Confirm; the first submission wins
and the server exits.

- **Pass `--no-lan` on a network the user doesn't own.** Every route requires the per-run
  80-bit token, which defeats scanning, but this is plain HTTP: anyone on the same open or
  guest Wi-Fi can read the token off the phone's first request.
- **Check the printed IP is the Wi-Fi one.** Under a full-tunnel VPN the lookup reports the
  tunnel address, which works only if the phone is on the same tunnel.

## Notes

- **The popup (macOS): three tiers, best available wins.** (1) A native **Rich Decision**
  app — a WKWebView window compiled on first use from `scripts/decision_host.swift` and
  cached, with its own Dock tile and cmd-Tab entry; it needs Xcode or the Command Line
  Tools, and is skipped silently without them. (2) A chromeless Chrome `--app` window.
  (3) A plain browser tab — also the only tier on Linux and Windows. The native window is
  an ordinary one and can be covered; **Always on Top** is `cmd-shift-T` and **Zoom** is
  `cmd +` / `cmd -` / `cmd 0`, both persisted across popups.
- **The translate button** appears when a secondary language is set (see "Languages").
  Clicking it translates the page by shelling out to `claude -p` (the Claude Code CLI, using
  its existing login), with the strings streaming in as they are generated; clicking again
  restores the original. Nothing is translated until it is clicked. `preview` and every
  `visual` are never translated — `preview` is the verbatim text being approved. A failed
  translation leaves the original in place and never blocks Confirm.
- If the user closes the window without choosing, the command keeps waiting. Reopen the
  printed URL, or kill the background task — the kill is clean and reaps the popup.
- Every route requires the per-run access token (`?k=`), so nothing that merely finds the
  port can read the page or submit a choice the user never made.
- This is advisory — it only collects a choice. Acting on a destructive choice still
  follows the normal confirmation rules.
- **Changing the scripts?** Read `docs/INTERNALS.md` first: it records the traps each
  design choice exists to avoid.
