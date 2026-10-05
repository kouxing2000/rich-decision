# rich-decision

**Let your coding agent ask you questions with a real UI instead of a numbered list in the
terminal.**

An [agent skill](https://code.claude.com/docs/en/skills) for Claude Code (and Codex). When
the agent has a choice for you to make, it writes a small JSON spec. The skill serves a
local page with comparison cards, pros and cons, code previews, diagrams and mockups, opens
it in a popup window, and hands your pick back to the agent as JSON.

![A decision page: three option cards with score chips, pros and cons, a code preview and a diagram](docs/screenshot.png)

## Why

A terminal is a poor place to compare three architectures, judge a UI mockup or triage ten
ideas. The built-in question tool shows labels and a monospace preview. This shows the
actual mockup, side by side, and lets you answer with a note as well as a click.

- **Comparison cards**: summary, pros/cons, effort/complexity/value chips, fact chips, a
  "Recommended" badge.
- **Visuals on any card**: inline SVG, Mermaid, sandboxed HTML/CSS mockups, images, video.
- **Several questions on one page**, single- or multi-select, with per-question notes.
- **Explainer mode**: sections of markdown and diagrams with no options, for "here is how
  this works"; questions come back as notes.
- **Two languages**: set a primary and an optional secondary language, or let your OS
  preference list decide. The page offers one-click translation into the secondary, and
  options can carry a one-line gloss in it.
- **Answer from your phone**: the page is also served on your LAN behind a per-run token,
  so a phone mockup can be judged on a phone.
- **No dependencies**: Python standard library, one vendored copy of `marked`, no build
  step. Works offline; only Mermaid diagrams load from a CDN, and fall back to their source
  without one.
- **A real window on macOS**: a native WebKit popup with its own Dock icon, compiled on
  first use when Xcode or the Command Line Tools are installed. Elsewhere it opens a browser
  tab.

## Install

Claude Code, for every project:

```bash
git clone https://github.com/kouxing2000/rich-decision ~/.claude/skills/rich-decision
```

For one project only, clone it into that project's `.claude/skills/rich-decision`
instead. For another agent that loads `SKILL.md` folders, clone it into that agent's skills
directory; the skill needs only the ability to run a background shell command.

Nothing needs configuring. The agent decides when to use it from the skill description,
or you can ask for it: *"show me the options as a rich decision"*.

Languages come from your OS preference list. To choose them yourself, write
`~/.config/rich-decision/config.json`:

```json
{ "primary": "en", "secondary": "zh-Hans" }
```

Leave `secondary` out to turn the translate button off.

## How it works

```
agent writes spec.json ──> decision_server.py ──> popup window (or browser tab)
                                  │                        │
agent reads JSON  <── stdout <────┴──── you click Confirm ─┘
```

1. `decision_server.py --new-dir` hands the agent a private directory for the run.
2. The agent writes `spec.json` there and starts the server in the background.
3. The server opens the page and waits; every route requires a per-run token.
4. When you confirm, it prints the result JSON to stdout and exits.

A minimal spec:

```json
{
  "title": "Pick a local store",
  "options": [
    { "id": "hive", "label": "Hive", "recommended": true,
      "pros": ["No native deps"], "cons": ["No queries"] },
    { "id": "sqlite", "label": "SQLite", "pros": ["Real SQL"], "cons": ["Native deps"] }
  ]
}
```

The result (abridged):

```json
{ "runId": "5a0b1e0ea8ba4c04",
  "answers": [{ "id": "_q0", "choice": ["hive"], "chosen": [{ "id": "hive", "label": "Hive" }] }],
  "notes": "went with no native deps" }
```

Try it yourself:

```bash
python3 scripts/decision_server.py --spec examples/pick-a-store.json
```

The full spec format, the visual types and the CLI flags are in [SKILL.md](SKILL.md),
which is also what the agent reads.

## Requirements

- Python 3, standard library only.
- Optional, macOS: Xcode or the Command Line Tools for the native popup window, or Google
  Chrome for a standalone app window. Without either, a browser tab opens.
- Optional: the `claude` CLI, for the on-demand translate button (shown when a secondary
  language is set).

## Security

The page is plain HTTP on your machine and, by default, your LAN. Every route needs an
80-bit token that appears only in the printed URL, so nothing that merely finds the port
can read the page or submit an answer. Pass `--no-lan` on a network you don't own. The
choice itself is advisory: the agent still has to follow its own rules before doing
anything irreversible. Details in [docs/INTERNALS.md](docs/INTERNALS.md).

## Contributing

Read [docs/INTERNALS.md](docs/INTERNALS.md) before changing the scripts. Most of the
non-obvious code exists because the obvious version was tried and failed, and that file
records how.

## License

MIT. `assets/marked.umd.js` is [marked](https://github.com/markedjs/marked), also MIT.
