#!/usr/bin/env python3
"""Claude Code PreToolUse hook for AskUserQuestion: send a RICH question to rich-decision.

An instruction to use the rich-decision page is read, and the agent still reaches for the
built-in question tool. This hook refuses that tool when the question carries detail a
terminal list renders poorly, and tells the agent to serve a decision page instead.

DENIES when the question payload has any of:
  - 2+ questions in one call
  - a question with 3+ options
  - multiSelect on any question
  - an option `description` longer than DESC_MAX characters
  - an option carrying a `preview` (a preview IS the per-option detail the page exists for)

ALLOWS one question with at most two options, no preview and no option description over
DESC_MAX characters. That stays in the terminal and is faster there.

Wire it with `|| true` after the command: a missing script makes python exit 2, and Claude
Code reads exit 2 from a PreToolUse hook as "block", which would refuse every question.

Known limit: a hook sees the payload, never the turn. A numbered list of generated
candidates plus "which one?" is a rich decision even as bare one-liners, and a two-option
question of that kind still passes. This narrows the gap; it does not close it.

Fails open by contract: any parse problem exits 0 and the call proceeds.

Optional log: set RICH_DECISION_GUARD_LOG to an absolute file path and every denial is
appended to it as one JSON line (time, reason, question headers).
"""
import json
import os
import sys
from datetime import datetime, timezone

DESC_MAX = 120          # longer than this is per-option detail, not a label gloss
MAX_OPTIONS = 2         # 3+ options is the side-by-side comparison case


def analyse(tool_input):
    """-> a reason string for the first rich signal found, else None."""
    questions = tool_input.get('questions')
    if not isinstance(questions, list) or not questions:
        return None

    if len(questions) >= 2:
        return f'{len(questions)} questions in one call'

    for q in questions:
        if not isinstance(q, dict):
            return None                      # unknown shape -> fail open
        if q.get('multiSelect'):
            return 'multiSelect is on'
        options = q.get('options')
        if not isinstance(options, list):
            continue
        if len(options) >= MAX_OPTIONS + 1:
            return f'{len(options)} options to compare side by side'
        for opt in options:
            if not isinstance(opt, dict):
                continue
            if opt.get('preview'):
                return (f'option "{str(opt.get("label"))[:40]}" carries a `preview` '
                        '(monospace only -- no colour, layout or images)')
            desc = opt.get('description') or ''
            if isinstance(desc, str) and len(desc) > DESC_MAX:
                return (f'option "{str(opt.get("label"))[:40]}" has a {len(desc)}-char '
                        f'description (over {DESC_MAX})')
    return None


def log_denial(reason, tool_input):
    path = os.environ.get('RICH_DECISION_GUARD_LOG')
    if not path:
        return
    try:
        path = os.path.expanduser(path)
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps({
                'ts': datetime.now(timezone.utc).isoformat(),
                'reason': reason,
                'headers': [q.get('header') for q in tool_input.get('questions', [])
                            if isinstance(q, dict)],
            }) + '\n')
    except Exception:
        pass


def deny(reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def main():
    try:
        data = json.load(sys.stdin)
        tool_input = data.get('tool_input') or {}
        if not isinstance(tool_input, dict):
            return
        hit = analyse(tool_input)
    except Exception:
        return                                # fail open, always
    if not hit:
        return
    log_denial(hit, tool_input)
    deny(
        f"This decision is rich ({hit}), so it belongs on a `rich-decision` page, not in a "
        f"terminal list: pros/cons, snippets, diagrams, 3+ options, multi-select and "
        f"several questions at once all render poorly here.\n"
        f"Do this instead:\n"
        f"  1. invoke the `rich-decision` skill\n"
        f"  2. write the spec: `sections` carry the argument, option cards carry pros/cons\n"
        f"  3. serve it in the background, then give the user the printed "
        f"http://127.0.0.1:<port>/?k=<token> URL -- the token is part of the address\n"
        f"The page carries the whole argument; chat carries only the URL.\n"
        f"AskUserQuestion stays right for ONE question with two options and no preview or "
        f"long description."
    )


if __name__ == "__main__":
    main()
