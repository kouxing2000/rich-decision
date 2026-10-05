# Setting up rich-decision

For the agent. Follow this when the user asks to set up, configure or integrate
rich-decision. Walk them through it on a decision page, using this skill, and then apply
exactly what they picked, nothing more.

`SKILL_DIR` below is the directory holding this file. Write its real absolute path
everywhere it appears.

## 1. Look before asking

Collect these facts first. Each one decides a question on the page, or removes it.

- **Which agent you are.** The question-tool guard is a Claude Code hook; skip it
  everywhere else.
- **Where the skill is installed.** Anything under `~/.claude/skills/` (or another agent's
  skills directory in your home) is a user-level install. A `SKILL_DIR` inside a project's
  own `.claude/skills/` is a project install: everything below then goes into that
  project's files, because other projects do not have the skill and must not be told to
  use it, or have a guard that points at it.
- **The instruction files**, and whether each one exists and already mentions
  `rich-decision`:
  - Claude Code: `~/.claude/CLAUDE.md` for every project; `<project>/CLAUDE.md` or
    `<project>/.claude/CLAUDE.md` for one. Look inside files they `@`-import too.
  - Codex: `$CODEX_HOME/AGENTS.md` (default `~/.codex/AGENTS.md`), `<project>/AGENTS.md`.
- **The guard** (Claude Code only): whether any of `~/.claude/settings.json`,
  `~/.claude/settings.local.json`, `<project>/.claude/settings.json` or
  `<project>/.claude/settings.local.json` already mentions `rich_decision_guard.py`.
- **Languages**: run `python3 SKILL_DIR/scripts/decision_server.py --new-dir`. Its stderr
  line gives the primary language, the secondary one (or none) and where they came from.
  Use the directory it prints for the setup page.
- **The translate button** needs the `claude` CLI: `command -v claude`, or an executable
  `~/.local/bin/claude`.
- **The popup** (macOS): the native window needs a Swift compiler. Check with
  `python3 -c 'import sys; sys.path.insert(0, "SKILL_DIR/scripts"); import native_host; print(native_host.swiftc_path())'`;
  anything but `None` means the native window. Without one, the page opens in Chrome or a
  browser tab. Report this; there is nothing to choose.

## 2. Serve one setup page

Use the workflow in `SKILL.md`. Put what step 1 found in the page `description`, so the
user sees the facts behind each question. Drop a question whose answer is already in
place, and say so in the description.

1. **The instructions.** Show the exact block in the question's `description`, copied
   verbatim from the fenced `markdown` block under "Make it the usual choice" in
   `README.md`. Never paraphrase it. Options: this agent's global file (recommended for a
   user-level install: it covers every project), this project's file (the only target
   for a project install), or skip.
2. **The question-tool guard** (Claude Code only). `SKILL_DIR/hooks/rich_decision_guard.py`
   denies the built-in `AskUserQuestion` tool when a call has 2+ questions, 3+ options,
   multi-select, an option `preview`, or an option description over 120 characters, and
   tells the agent to serve a page instead. One question with two options and no preview
   or long description still goes through, and any parse problem lets the call through.
   It takes effect from the next session. Show the settings entry from section 3 as the
   option's `preview`. Options: install (recommended when the user wants the page used
   consistently), or skip.
3. **Languages.** Options: keep what was detected (recommended when it matches how the
   user talks to you); English only, with no translate button; or something else, named
   in the notes as BCP 47 tags (`ja`, `zh-Hant`, `pt-BR`). When the `claude` CLI is
   missing, say the translate button stays hidden whatever is picked.

A starting spec. Fill in the angle-bracket parts, and drop questions that do not apply.
For a project install, drop the `global` option and recommend `project`:

```json
{
  "title": "Set up rich-decision",
  "description": "What I found:\n- <agent, install scope, instruction files, guard, languages, claude CLI, popup tier>\n\nPick what to set up; I change only what you pick.",
  "questions": [
    { "id": "instructions", "title": "Where should the instructions go?",
      "description": "This block is added unchanged:\n\n```markdown\n<the block from README.md>\n```",
      "options": [
        { "id": "global", "label": "<global file path>", "recommended": true, "summary": "Every project" },
        { "id": "project", "label": "<project file path>", "summary": "This project only" },
        { "id": "skip", "label": "Skip" } ] },
    { "id": "guard", "title": "Block the built-in question tool for rich questions?",
      "description": "<what it denies and allows; takes effect from the next session>",
      "options": [
        { "id": "install", "label": "Install the guard", "recommended": true,
          "preview": "<the settings entry from section 3>" },
        { "id": "skip", "label": "Skip" } ] },
    { "id": "languages", "title": "Which languages?",
      "options": [
        { "id": "keep", "label": "Keep <primary>, secondary <tag or none>", "recommended": true,
          "summary": "Detected from <source>; nothing is written" },
        { "id": "english", "label": "English only", "summary": "No translate button" },
        { "id": "custom", "label": "Something else", "summary": "Name the tags in the notes" } ] }
  ]
}
```

## 3. Apply exactly what was picked

- **Instructions:** append the block unchanged to the chosen file, after a blank line.
  Create the file when it does not exist. Leave the rest of the file alone.
- **Guard:** add this entry to the `hooks.PreToolUse` array of `~/.claude/settings.json`,
  or of `<project>/.claude/settings.json` for a project install, creating the array or
  the file when missing. Keep every existing entry. Insert the text at an anchor rather
  than loading and re-dumping the JSON: a serializer rewrites the whole file's formatting.

  ```json
  { "matcher": "AskUserQuestion",
    "hooks": [ { "type": "command", "command": "python3 \"SKILL_DIR/hooks/rich_decision_guard.py\" || true", "timeout": 15 } ] }
  ```

  Keep the quotes, which survive a space in the path, and keep the `|| true`: if the
  script ever goes missing, python exits 2, and Claude Code reads exit 2 from this hook as
  "block", which would refuse every question. For a project install, use
  `"$CLAUDE_PROJECT_DIR/.claude/skills/rich-decision/hooks/rich_decision_guard.py"` in
  place of the absolute path, so the entry's command reads
  `"python3 \"$CLAUDE_PROJECT_DIR/.claude/skills/rich-decision/hooks/rich_decision_guard.py\" || true"`:
  the project's settings file is usually shared with the team, and Claude Code sets
  `CLAUDE_PROJECT_DIR` for every hook command. To keep a
  record of each refusal, prefix the command with
  `RICH_DECISION_GUARD_LOG=<absolute path to a .jsonl file>` (in the user's own settings
  file, not a shared one).
- **Languages:** "keep" writes nothing. Otherwise write
  `$XDG_CONFIG_HOME/rich-decision/config.json` (`~/.config/rich-decision/config.json` when
  `XDG_CONFIG_HOME` is unset), creating its directory first: `{"primary": "en"}` for
  English only (no secondary turns the translate button off), or
  `{"primary": "<tag>", "secondary": "<tag>"}`.

## 4. Verify, then report

- The instruction file now contains the block's `## Asking me to choose` heading.
- The settings file still parses, and the command it now holds refuses a rich question.
  This prints `deny` when both are true:

  ```bash
  python3 - <<'PY'
  import json, os, subprocess
  path = os.path.expanduser("~/.claude/settings.json")   # or the project's settings file
  entries = json.load(open(path))["hooks"]["PreToolUse"]
  cmd = next(h["command"] for e in entries if e.get("matcher") == "AskUserQuestion"
             for h in e["hooks"] if "rich_decision_guard" in h["command"])
  payload = '{"tool_input":{"questions":[{"question":"Q","header":"H","options":[{"label":"A"},{"label":"B"},{"label":"C"}]}]}}'
  env = {**os.environ, "CLAUDE_PROJECT_DIR": os.getcwd()}  # run from the project root
  out = subprocess.run(["sh", "-c", cmd], input=payload, capture_output=True, text=True, env=env).stdout
  print(json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out.strip()
        else "NOT FIRED - check the command's path")
  PY
  ```

  The running session will not use the guard yet, because Claude Code reads hooks when
  a session starts. With `RICH_DECISION_GUARD_LOG` set, this check adds one test row to
  that log; say so in the report.
- `--new-dir` prints the languages that were picked.

Report each file you changed, with its path, and tell the user the guard applies from
their next session.
