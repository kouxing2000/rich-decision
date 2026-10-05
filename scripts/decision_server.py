#!/usr/bin/env python3
"""Rich decision UI — serve an HTML decision page, capture the user's choice back.

Zero dependencies (Python stdlib only). Blocks until the user submits in the
browser, then writes the choice to --out, prints it to stdout as JSON, exits 0.

--out defaults to a fresh per-run temp file whose path is printed on stderr; the
result carries a "runId" so a caller can tell its own answer from another run's.

Usage:
  decision_server.py --spec spec.json [--out result.json] [--port 0] [--no-open]

Spec JSON shape — single question (legacy):
  {
    "title": "Pick a database",
    "description": "optional markdown-ish intro text",
    "gloss": "一句话摘要",            # optional one-liner in the user's secondary
                                      # language; also valid on every section / question / option
    "mode": "single",                 # "single" (default) or "multi"
    "allowNotes": true,               # ignored for the shared box - it's always shown now
    "options": [
      {
        "id": "hive",                 # optional; defaults to index
        "label": "Hive",
        "recommended": true,          # adds a Recommended badge, sorts first visually
        "gloss": "纯 Dart 键值库，无原生依赖",
        "summary": "Pure-Dart KV store",
        "pros": ["No native deps", "Fast"],
        "cons": ["No complex queries"],
        "effort": "low",              # optional scores: "low" | "med" | "high"
        "complexity": "med",          #   (qualitative on purpose - never numbers)
        "value": "high",
        "meta": ["$0/mo", "3 files"], # optional free-form fact chips, same row
        "preview": "code or text to show in a monospace box"  # optional
      }
    ]
  }

Spec JSON shape — multiple questions (like AskUserQuestion, 1-N questions on one page):
  {
    "title": "Configure the sync layer",
    "description": "optional intro",
    "allowNotes": true,               # ONE shared notes box at the bottom
    "questions": [
      {
        "id": "store",                # optional; defaults to _qN
        "title": "Pick a local store",
        "gloss": "选本地存储：离线优先的落盘方案",
        "description": "optional per-question intro",
        "mode": "single",             # per-question "single" (default) or "multi"
        "allowNotes": true,           # add a notes box for THIS question (result.answers[i].notes)
        "options": [ {...}, {...} ]    # same option shape as above
      },
      { "id": "sync", "title": "Sync strategy", "mode": "multi", "options": [ ... ] }
    ]
  }

All answers are optional: Confirm is always enabled, the reasoning note is always
shown, and the user can submit with just a note (or even nothing).
"""
import argparse
import atexit
import glob
import hashlib
import hmac
import html
import json
import mimetypes
import os
import plistlib
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# Vendored `marked` (UMD build, pinned) — served from this same local server at /marked.js
# so block markdown (tables, lists, headings, fences) renders with no CDN and no network.
# Kept as a file rather than a CDN import on purpose: a section body is the PRIMARY content,
# so it must not degrade to raw text on a flaky connection the way an optional mermaid
# diagram does. Served as its own route rather than inlined into the page: inlining put 42KB
# of third-party text through the same placeholder substitution as the spec, where a spec
# string could collide with the placeholder (and a future release containing "</script>"
# would silently blank the page).
# To bump: curl -o assets/marked.umd.js https://cdn.jsdelivr.net/npm/marked@<ver>/lib/marked.umd.js
MARKED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "marked.umd.js")

# Identifies THIS run, and rides along in the result file and on stdout. Every session on
# this machine shares /tmp, so a result path reused by two runs is a decision handed to the
# wrong agent -- and a wrong decision is a forged authorization, not a cosmetic mix-up. The
# per-run --out directory below makes that collision impossible; this id is what makes it
# DETECTABLE for a caller that passes an explicit --out anyway. Deliberately not the access
# token: that one is a capability and has no business sitting in a world-readable file.
RUN_ID = secrets.token_hex(8)


def is_shared_dir(path):
    """True if `path` IS a temp root every session on the machine writes into.

    realpath on both sides, and /tmp spelled out: on macOS gettempdir() is the per-user
    $TMPDIR under /var/folders, so comparing against it alone misses the very directory this
    exists to catch -- and /tmp is a symlink to /private/tmp that abspath does not resolve.
    A SUBdirectory of one of these is private enough; only the root itself is shared.
    """
    roots = {os.path.realpath(p) for p in ("/tmp", "/var/tmp", tempfile.gettempdir())}
    return os.path.realpath(path) in roots


def session_scratch_dir():
    """The harness's own per-session scratch root for this session, or None.

    Claude Code keeps one at /private/tmp/claude-<uid>/<mangled-project>/<session-id>/ and
    writes background-task output under it, so a decision's spec and result belong there
    too: they are artifacts OF this session, and that is where anyone debugging one will
    already be looking. The project segment cannot be reconstructed -- Claude Code collapses
    `/`, `.` and `_` all to `-` -- so the session id, which is unique, is what the glob keys
    on. Same trick detect_project() uses on the transcript path.

    This is an UNDOCUMENTED harness internal and a release may move it, so every failure
    path returns None and the caller falls back to an ordinary temp dir. The location is a
    convenience; the per-run directory the caller builds inside it is the part that keeps
    two concurrent decisions apart, and that works either way.
    """
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not SESSION_ID_OK.match(sid):   # validated before it is spliced into a glob pattern
        return None
    hits = glob.glob(f"/private/tmp/claude-*/*/{sid}")
    # Exactly one, or nothing. 0 means no session root was found -- not under Claude Code, or
    # under a build that puts it somewhere else, since this path is hardcoded and macOS-shaped.
    # >1 means the id is ambiguous, and picking arbitrarily would scatter one session's
    # artifacts across two roots -- worse than the plain temp dir, because it looks organised.
    if len(hits) != 1:
        return None
    d = os.path.join(hits[0], "decisions")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return None
    return d if os.access(d, os.W_OK) else None


def new_run_dir():
    """A fresh directory owned by this run alone: spec in, result out.

    mkdtemp with dir=None is exactly the old behaviour, so the session-root lookup failing
    costs nothing. The retry is not belt-and-braces: session_scratch_dir() ends on an
    os.access check, and the directory can be swept between that check and this call -- a
    TOCTOU that would otherwise raise before the server ever binds a port.
    """
    root = session_scratch_dir()
    try:
        return tempfile.mkdtemp(prefix="rich-decision-", dir=root)
    except OSError:
        return tempfile.mkdtemp(prefix="rich-decision-")


def load_marked():
    """Return the vendored marked source, or None if it's missing — /marked.js then 404s and
    the page falls back to the inline-only renderer instead of failing."""
    try:
        with open(MARKED_PATH, encoding="utf-8") as fh:
            src = fh.read()
    except OSError as e:
        print(f"warning: markdown lib unavailable ({e}); block markdown will not render",
              file=sys.stderr)
        return None
    # the .map file isn't vendored; keeping the reference only earns a devtools 404
    return re.sub(r"(?m)^//# sourceMappingURL=.*$", "", src)


# Leads the window title so the popup is identifiable in cmd-Tab / Mission Control /
# the Window menu, where it is otherwise an unlabelled window wearing a Chrome icon.
WINDOW_APP = "Claude"
WINDOW_MARKER = f"[{WINDOW_APP}]"


SESSION_ID_OK = re.compile(r"^[A-Za-z0-9._-]+$")
SESSION_SCAN_LINES = 50     # `cwd` shows up on line 3-4; this is slack, not a search


def _repo_name(start):
    """Repo-root name for `start`, or "" if it is not inside one.

    Repo root, not the raw path -- a decision raised from `<repo>/android` is about the
    repo, not about "android". A leading dot is stripped so `~/.claude` reads as "claude".
    $HOME and / are answers to "where am I", not to "which project".

    Found by walking up for a `.git` entry rather than shelling out to `git rev-parse`.
    Same answer, but `/usr/bin/git` on a Mac with no Command Line Tools is the
    xcode-select shim -- the very trap `native_host.swiftc_path` is built to avoid, where the
    subprocess opens a blocking install dialog that outlives any timeout we set. A
    decorative label must not be able to stall the popup, let alone summon a GUI prompt.
    `.git` is tested with `exists`, not `isdir`, so worktrees and submodules (where it is
    a file) resolve too."""
    if not start:
        return ""
    try:
        root = os.path.realpath(start)
    except OSError:
        return ""  # path deleted underneath us (worktree removed, temp dir reaped)
    while not os.path.exists(os.path.join(root, ".git")):
        parent = os.path.dirname(root)
        if parent == root:
            return ""
        root = parent
    if root in ("/", os.path.realpath(os.path.expanduser("~"))):
        return ""
    name = os.path.basename(root)
    return name[1:] if name.startswith(".") else name


# (browser binary, AppleScript app name) — first one found wins for --app window mode.
def _session_cwd():
    """The working directory of the Claude Code session that launched this server, read
    from that session's own transcript.

    This is what rescues the label when the popup is raised from the session scratchpad
    (`/private/tmp/claude-<uid>/<mangled-project>/<session-id>/scratchpad`) or any other
    non-repo directory -- the cwd walk finds nothing there, but the session knows.

    It reads the transcript rather than DECODING that mangled path segment, because the
    segment cannot be decoded: Claude Code collapses `/`, `.` AND `_` all to `-`, so
    `-Users-me-Downloads-chat-images` is `Downloads/chat-images` and
    `-Users-me-workspaces-git-learn-mahjong` is `git/learn_mahjong`, and nothing in the
    name says which. The transcript's `cwd` is the exact original string.

    Costs ~0.5 ms (a glob over ~75 dirs plus a few lines of one file), and every failure
    is "" -- the caption is never worth an exception or a stall."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID") or ""
    if not SESSION_ID_OK.match(sid):
        return ""   # unset, or something that has no business being spliced into a path
    try:
        hits = glob.glob(os.path.join(os.path.expanduser("~/.claude/projects"), "*", sid + ".jsonl"))
        if not hits:
            return ""
        # Line-by-line and bail early: transcripts run to megabytes, and the field is at
        # the top. The FIRST cwd is the session's opening directory, which is the project
        # even if a later tool call wandered off into a temp dir -- exactly what's wanted.
        with open(hits[0], encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= SESSION_SCAN_LINES:
                    break
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                cwd = rec.get("cwd") if isinstance(rec, dict) else None
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        return ""
    return ""


def detect_project():
    """Which project this decision is about, for the window title.

    Derived, never asked for: a spec field would be forgotten in exactly the distracted
    moment the label is worth having.

    Three sources, most authoritative first, each resolved to a repo root. NO REPO MEANS
    NO LABEL -- the cwd's basename is deliberately not a fallback, because the cases where
    the walk finds nothing are precisely the ones where the cwd is a scratch or temp
    directory that says nothing about the project, and captioning from it produces a
    confident lie (`[Claude · scratchpad]`, `· tmp`, `· folders` -- all observed). A repo
    found *under* a temp root is still fine: a worktree in /tmp has a real `.git`.

    Every failure returns "" rather than raising: this runs before the server is up, so
    an exception here means no page at all -- catastrophic, in service of a caption."""
    # 1. The harness's own answer (hooks set it), when it is plumbed through at all.
    name = _repo_name(os.environ.get("CLAUDE_PROJECT_DIR"))
    if name:
        return name
    # 2. The cwd -- correct whenever the server was launched from inside the project, and
    #    more specific than the session when an agent has moved between repos.
    try:
        name = _repo_name(os.getcwd())
    except OSError:
        name = ""
    if name:
        return name
    # 3. The session transcript -- the rescue for a scratchpad/temp cwd.
    return _repo_name(_session_cwd())


CHROMIUM_BROWSERS = [
    ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "Google Chrome"),
    ("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge", "Microsoft Edge"),
    ("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser", "Brave Browser"),
    ("/Applications/Chromium.app/Contents/MacOS/Chromium", "Chromium"),
]


def _osascript(script, warn=None):
    """Run an AppleScript. Silent by default; pass `warn` to surface a one-line reason
    on stderr, for steps whose silent failure would look like the feature is broken
    (osascript fails with a TCC error when Automation access isn't granted).

    The timeout is NOT belt-and-braces: if Automation access for System Events has never
    been granted, macOS puts up a consent dialog and osascript blocks until a human
    answers it. Without a bound, open_page never returns -- the popup is on screen and
    the user can even submit, but the main thread never resumes to print the result or
    close the window."""
    try:
        r = subprocess.run(["osascript", "-e", script], check=False, timeout=20,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if r.returncode != 0 and warn:
            err = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            print(f"warning: {warn}: {err[-1] if err else 'osascript failed'}",
                  file=sys.stderr)
    except Exception:
        pass


def _raise_pid(pid, attempts=5, delay=0.3):
    """Bring the popup's own process to the front, and CONFIRM it landed.

    One fire-and-forget `set frontmost` is not enough. Raising a process that has no
    window yet is a silent no-op that still exits 0, and a cold-started Chrome on a
    fresh profile often has not drawn its window by the time we ask -- so the popup
    ends up behind whatever the user was looking at, with nothing to signal that it
    exists. Poll instead: re-issue until System Events agrees OUR pid is frontmost.

    Warns once, only after every attempt has actually failed -- warning on the last
    attempt before checking it would report failure for a raise that just succeeded."""
    check = ('tell application "System Events" to get unix id of '
             'first process whose frontmost is true')
    for i in range(attempts):
        if i:
            time.sleep(delay)     # between attempts only; never after the last one
        _osascript("tell application \"System Events\" to set frontmost of "
                   f"(first process whose unix id is {pid}) to true")
        try:
            r = subprocess.run(["osascript", "-e", check], check=False, timeout=20,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            if r.stdout.decode("utf-8", "replace").strip() == str(pid):
                return True
        except Exception:   # incl. TimeoutExpired on a TCC consent prompt
            return False
    print("warning: could not raise the decision popup to the front", file=sys.stderr)
    return False


def close_popup(handle):
    """Tear down the standalone --app popup after the choice is made. Chrome blocks JS
    window.close() for CLI-launched --app windows, so we kill the browser process we
    own outright -- deterministic, unlike AppleScript title matching (see open_page).

    Idempotent: safe to call twice (the normal path and the finally backstop race).
    The guard is cleared if teardown is itself interrupted -- a second SIGTERM landing
    during the wait below must not leave the flag set, or the atexit backstop would
    skip the retry and leak the very process this function exists to reap."""
    if not handle or handle.get("closed"):
        return
    handle["closed"] = True
    try:
        proc = handle.get("proc")
        if proc is not None:
            time.sleep(0.4)
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=2)   # reap before rmtree, or we race a dying Chrome
                except Exception:
                    pass
            except Exception:
                pass
        _discard_profile(handle.get("profile"))
    except BaseException:
        handle["closed"] = False
        raise


def _discard_profile(profile):
    if profile:
        shutil.rmtree(profile, ignore_errors=True)


NATIVE_HOST_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "decision_host.swift")
NATIVE_BUNDLE = "Rich Decision.app"
NATIVE_EXEC = "RichDecision"
NATIVE_CACHE = os.path.expanduser("~/Library/Caches/claude-rich-decision")
# NSAllowsLocalNetworking is load-bearing: App Transport Security blocks plain http by
# default, and this page is only ever served over http on 127.0.0.1 — without it the
# window opens blank. CFBundleName is what macOS shows in the Dock, cmd-Tab, Mission
# Control and the menu bar; it is the entire reason for wrapping the binary in a bundle
# instead of exec'ing it bare.
INFO_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Rich Decision</string>
  <key>CFBundleDisplayName</key><string>Rich Decision</string>
  <key>CFBundleExecutable</key><string>RichDecision</string>
  <key>CFBundleIdentifier</key><string>io.github.kouxing2000.rich-decision</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict>
</plist>
"""


# The bundle builder is `native_host.py`, beside this file; another local tool can reuse it
# (and decision_host.swift) to get a window of its own by passing a different Info.plist.
# The import is fail-soft on purpose: a missing or broken module must cost this skill the
# native TIER, never the decision itself, so it degrades to the Chrome popup exactly as an
# absent toolchain does.
try:
    from native_host import NativeHost as _NativeHost
except Exception:                                        # pragma: no cover - defensive
    _NativeHost = None

_HOST = _NativeHost(
    src=NATIVE_HOST_SRC, bundle=NATIVE_BUNDLE, exec_name=NATIVE_EXEC,
    plist=INFO_PLIST, cache_root=NATIVE_CACHE, label="native decision window",
) if _NativeHost else None


def _native_host():
    """Path to the compiled "Rich Decision.app" executable, building it on first use.

    None -- never an exception, never an indefinite block -- when this machine can't
    produce one, so the caller falls back to the Chrome popup. A decision that cannot be
    shown is far worse than one shown in the wrong window."""
    return _HOST.build() if _HOST else None


def _evict_native_cache(exe):
    """Drop a bundle that compiled fine but will not run, so the next decision rebuilds."""
    if _HOST:
        _HOST.evict(exe)


def lan_ip():
    """This machine's address on the local network, or None.

    A UDP `connect` sends no packets -- it only makes the kernel run a route lookup and
    bind a source address -- so this costs nothing and needs no network to be up. The
    destination is TEST-NET-1 (RFC 5737, never routable) precisely because nothing is
    ever meant to receive it; only the route decision matters.

    KNOWN LIMIT: this reports whatever the DEFAULT route uses, so under a full-tunnel VPN
    (Tailscale, a corporate client) it returns the utun address, not the Wi-Fi one. That
    address is right if the phone is on the same tunnel and wrong otherwise, and nothing
    here can tell those apart -- enumerating interfaces instead would face the same
    question from the other side. The URL simply times out; SKILL.md says to check.

    127.0.0.1 and 0.0.0.0 are both reported as failure: neither is an address a phone can
    open, and printing one as though it were would be a lie."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))
            ip = s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None
    return None if ip.startswith("127.") or ip == "0.0.0.0" else ip


def _probe_url(url):
    """(launch url, token) — the page URL tagged with a per-tier token.

    A plain boolean "was the page served" cannot say WHO served it, and every cheap
    variant of it is wrong in a way that was reproduced: as a never-cleared latch, a
    late-rendering tier 1 satisfies tier 2's proof; cleared-per-tier, the same late
    render still lands in the window between arming and checking; and the URL is printed
    to stderr before any tier launches, so anything that opens or curls it pre-satisfies
    every tier at once. Each of those ends with open_page reporting success, the chime
    playing, the remaining tiers skipped, and NO window on screen.

    Tagging the launch URL removes the ambiguity instead of narrowing it: the handler
    echoes back whichever token was fetched, so a tier can only ever be satisfied by its
    own page load. `_w` is inert beyond that -- the page's own requests (/marked.js,
    /asset, /submit) carry only the access token `k`, never `_w`, so nothing the page
    does after loading can re-satisfy a tier."""
    token = os.urandom(6).hex()
    sep = "&" if urlparse(url).query else "?"
    return f"{url}{sep}_w={token}", token


def _await_window(ctx, proc, token, timeout=6.0):
    """Wait until THIS tier's tagged page is fetched, proving its window rendered.

    Returns False if the process dies first or the token never arrives, which is the
    caller's cue to try the next tier. The signal is the GET handler echoing the token
    into ctx["served"]: permission-free, unlike an AppleScript probe, and incapable of
    mistaking "Automation access denied" for "no window" -- a distinction that matters,
    because falling through on the wrong one opens two windows instead of none.

    A dead process fails immediately regardless of the token: it owns no window, and for
    Chrome it is also the "forwarded to a running instance and exited" case, where some
    other process may hold a window but not one whose pid we could ever close."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        if ctx is not None and ctx.get("served") == token:
            return True
        time.sleep(0.1)
    return bool(ctx is not None and ctx.get("served") == token)


def open_page(url, app_mode=True, sound=True, ctx=None):
    """Open the decision page and make it hard to miss, then hard to lose.

    Three tiers, best available wins: the native "Rich Decision" app (its own
    Dock/cmd-Tab identity and a floating window nothing can cover), a Chromium browser
    as a standalone --app window, then a plain browser tab. Returns a handle for
    close_popup(), or None in fallback mode.

    Each popup tier must PROVE it drew a window (see _await_window) before it counts.
    "The process is still alive" does not prove it, and a tier that silently shows
    nothing while suppressing the fallbacks is the worst outcome available here.

    The alert sound is played only once a tier has succeeded -- ringing before a
    possible first-run compile would send the user looking for a window that does not
    exist yet.

    Publishes that handle into ctx["popup"] AS IT IS BUILT, not on return: the caller's
    teardown can only reap what it can see, and a SIGTERM during the launch window
    would otherwise unwind past a browser whose handle was still a local here.

    The popup gets its own throwaway --user-data-dir. Without it Chrome's singleton
    lock forwards the launch to an already-running instance and our Popen pid is a
    stub that exits immediately -- leaving us to address the window by app NAME, which
    AppleScript resolves by bundle id. With two Chrome instances running (e.g. a
    browser-automation instance alongside the user's own), that name resolves to an
    arbitrary one: `activate` yanks the WRONG Chrome to the front a beat after the
    popup appears, and the close is delivered to a browser that never had the window.
    A private profile makes the launch a real, separate instance we hold the pid of,
    so both activate and close target exactly our window."""
    is_mac = sys.platform == "darwin"

    def chime():
        if is_mac and sound:
            subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if is_mac and app_mode:
        exe = _native_host()
        if exe:
            # No throwaway profile to reap — the bundle IS its own app, so there is no
            # Chrome singleton lock to dodge and nothing on disk to clean up.
            handle = {"proc": None, "profile": None, "app_name": "Rich Decision"}
            if ctx is not None:
                ctx["popup"] = handle
            try:
                # stderr inherited on purpose: a WKWebView load failure is the one thing
                # that would leave a blank window with no other trace.
                launch_url, token = _probe_url(url)
                launched = time.time()
                proc = subprocess.Popen([exe, launch_url], stdout=subprocess.DEVNULL)
            except Exception:
                if ctx is not None:
                    ctx["popup"] = None
            else:
                handle["proc"] = proc
                if _await_window(ctx, proc, token):
                    # Deliberately NO _raise_pid here. The host already called
                    # NSApp.activate(ignoringOtherApps:) synchronously at launch -- a real
                    # API needing no permission, which is half the reason this tier exists.
                    # Adding the AppleScript raise on top would hand back the exact TCC
                    # dependency the tier removes: on a machine where Automation was never
                    # granted it prompts against an already-frontmost window, and on a
                    # denied one it prints a false failure warning every single decision.
                    chime()
                    return handle
                # Launched but never drew a window (no window server, or it died).
                # Reap it before falling through, or it lingers as an invisible process.
                print("warning: native decision window did not appear, "
                      "falling back to a browser popup", file=sys.stderr)
                # A binary that builds but cannot exec (an SDK newer than the running OS
                # links symbols that are absent at runtime) would otherwise be cached
                # forever: os.path.exists(exe) is the only validity test, so every popup
                # from here on pays a doomed launch. Evict it so the next run rebuilds.
                # Narrow discriminator on purpose -- a user closing the window inside the
                # deadline also exits without serving, but not within a second of launch.
                if proc.poll() is not None and time.time() - launched < 2.0:
                    _evict_native_cache(exe)
                close_popup(handle)
                if ctx is not None:
                    ctx["popup"] = None
        for path, app_name in CHROMIUM_BROWSERS:
            if not os.path.exists(path):
                continue
            # mkdtemp, not a pid/port-derived name: both of those recycle, and it
            # creates 0700 + O_EXCL so a predictable path in a world-writable /tmp
            # (any context without TMPDIR) can't be pre-staged as a symlink.
            profile = tempfile.mkdtemp(prefix="claude-rich-decision-")
            handle = {"proc": None, "profile": profile, "app_name": app_name}
            if ctx is not None:
                ctx["popup"] = handle          # reapable from here on, proc or not
            try:
                launch_url, token = _probe_url(url)
                proc = subprocess.Popen(
                    [path, f"--app={launch_url}", f"--user-data-dir={profile}",
                     "--no-first-run", "--no-default-browser-check",
                     "--window-size=1120,820", "--window-position=200,100"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                _discard_profile(profile)
                if ctx is not None:
                    ctx["popup"] = None
                continue   # this browser won't exec -- try the next one installed
            handle["proc"] = proc
            if not _await_window(ctx, proc, token):  # died/forwarded -- fall back to a tab
                close_popup(handle)
                if ctx is not None:
                    ctx["popup"] = None
                break
            _raise_pid(proc.pid)
            chime()
            return handle
    try:
        import webbrowser
        webbrowser.open(url)
        chime()
    except Exception:
        pass
    return None

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root {
    --bg: #0f1117; --panel: #181b24; --panel-2: #1f2330; --border: #2a2f3d;
    --text: #e6e8ef; --muted: #9aa3b2; --accent: #6ea8fe; --accent-2: #3b82f6;
    --good: #3fb950; --bad: #f0883e; --sel: #1d2b4a;
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg: #f6f7f9; --panel: #ffffff; --panel-2: #f0f2f6; --border: #dce0e8;
      --text: #1b1f27; --muted: #5b6472; --accent: #2563eb; --accent-2: #2563eb;
      --good: #1a7f37; --bad: #bc4c00; --sel: #e6efff;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    padding: 32px 20px 140px;
  }
  .wrap { max-width: clamp(1100px, 88vw, 1760px); margin: 0 auto; }
  /* Prose keeps a readable measure while the option grid stretches to the window. */
  .intro, .q-intro, .q-hint { max-width: 1100px; }
  h1 { font-size: 24px; margin: 0 0 8px; }
  .intro { color: var(--muted); margin: 0 0 24px; }
  .question { margin: 0 0 30px; }
  .q-head { display: flex; align-items: baseline; gap: 10px; margin: 0 0 4px; }
  .q-title { font-size: 18px; margin: 0; }
  .q-num {
    font-size: 11px; font-weight: 700; color: var(--muted); background: var(--panel-2);
    border: 1px solid var(--border); border-radius: 999px; padding: 1px 9px; white-space: nowrap;
  }
  .q-intro { color: var(--muted); margin: 0 0 6px; font-size: 14px; }
  .q-hint { color: var(--muted); font-size: 12.5px; margin: 0 0 12px; }
  textarea.q-notes {
    width: 100%; margin: 14px 0 0; background: var(--panel-2); color: var(--text);
    border: 1px solid var(--border); border-radius: 8px; padding: 9px 12px; resize: vertical;
    min-height: 44px; font: inherit; font-size: 14px;
  }
  .grid { display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); }
  .card {
    background: var(--panel); border: 1.5px solid var(--border); border-radius: 14px;
    padding: 18px; cursor: pointer; position: relative; transition: all .12s ease;
    display: flex; flex-direction: column; gap: 10px;
  }
  .card:hover { border-color: var(--accent); transform: translateY(-1px); }
  .card.sel { border-color: var(--accent-2); background: var(--sel); box-shadow: 0 0 0 1.5px var(--accent-2); }
  .card .top { display: flex; align-items: center; gap: 10px; }
  .card h3 { margin: 0; font-size: 17px; flex: 1; }
  .badge {
    font-size: 11px; font-weight: 600; color: #fff; background: var(--good);
    padding: 2px 8px; border-radius: 999px; white-space: nowrap; letter-spacing: .02em;
  }
  .summary { color: var(--muted); margin: 0; font-size: 14px; }
  /* `gloss`: one short line per page / section / question / option in the reader's
     secondary language, so the whole page is skimmable in it. Muted + rule so it reads as a
     gloss, not content. */
  .gloss {
    color: var(--muted); font-size: 13px; line-height: 1.55; margin: 0; max-width: 1100px;
    padding-left: 9px; border-left: 2px solid var(--border);
  }
  .gloss-page { margin: -14px 0 22px; font-size: 13.5px; }
  .question > .gloss { margin: 2px 0 8px; }
  .explain .gloss { margin: 0 0 10px; }
  ul { margin: 4px 0 0; padding-left: 18px; }
  li { margin: 2px 0; }
  .pros li { color: var(--good); }
  .cons li { color: var(--bad); }
  .pc-label { font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin-top: 4px; }
  /* Score chips (effort/complexity/value: low|med|high) + free-form `meta` fact chips.
     Colour encodes DIRECTION, not magnitude: effort/complexity are costs (low = good),
     value is a benefit (high = good); med and fact chips stay neutral. */
  .scores { display: flex; flex-wrap: wrap; gap: 6px; margin: 0; }
  .score {
    display: inline-flex; align-items: baseline; gap: 5px; font-size: 11px;
    background: var(--panel-2); border: 1px solid var(--border);
    border-radius: 999px; padding: 2px 9px; white-space: nowrap;
  }
  .score .s-k { color: var(--muted); text-transform: uppercase; letter-spacing: .05em; font-size: 10px; }
  .score .s-v { font-weight: 600; color: var(--muted); }
  .score .s-good { color: var(--good); }
  .score .s-bad { color: var(--bad); }
  /* WRAP, do not scroll. `<pre>`'s default `white-space: pre` plus a narrow option
     card hides every long line past the card's width, and the macOS overlay scrollbar
     shows no affordance that anything is missing -- so the box silently truncates.
     That defeats `preview`'s whole job: callers put the EXACT text being approved in
     here (a verbatim memory rule is one long line), and text you cannot see cannot be
     approved. pre-wrap keeps real indentation for code; anywhere breaks the long
     unbroken tokens (paths, URLs) that overflow-wrap:break-word leaves sticking out. */
  pre {
    background: var(--panel-2); border: 1px solid var(--border); border-radius: 8px;
    padding: 10px 12px; overflow-x: auto; font-size: 12.5px; margin: 4px 0 0;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    white-space: pre-wrap; overflow-wrap: anywhere;
  }
  /* Explainer sections (spec.sections): full-width content blocks above the questions.
     Prose keeps the 1100px reading measure; visuals may use the whole page width and
     get a taller cap than option-card visuals (they ARE the content here). */
  .explain { margin: 0 0 28px; }
  .explain .s-title { font-size: 19px; margin: 0 0 6px; }
  .explain .s-body { margin: 0 0 10px; max-width: 1100px; }
  .explain .visual { max-height: clamp(320px, 52vh, 680px); }
  .explain .visual img, .explain .visual svg, .explain .visual pre { max-height: clamp(300px, 50vh, 650px); }
  .visual {
    margin: 2px 0 4px; border-radius: 8px; overflow: hidden; max-height: clamp(260px, 34vh, 440px);
    display: flex; justify-content: center; align-items: center;
    background: var(--panel-2); border: 1px solid var(--border); padding: 8px;
  }
  .visual img, .visual svg { max-width: 100%; max-height: clamp(244px, 32vh, 420px); height: auto; display: block; }
  .visual pre { margin: 0; max-height: clamp(244px, 32vh, 420px); }
  .visual video { max-width: 100%; max-height: clamp(244px, 32vh, 420px); display: block; background: #000; border-radius: 6px; }
  .explain .visual video { max-height: clamp(300px, 50vh, 650px); }
  /* An `html` visual: a self-contained HTML/CSS mockup in its OWN document (iframe
     srcdoc) so its styles never collide with the page. Native-UI mockups need width
     AND height, so the preview keeps a readable min-width and SCROLLS (styled bars)
     when the card is narrower, plus an Enlarge button opens it full-size. */
  .visual.visual-html { max-height: none; padding: 0; display: block; background: transparent; position: relative; }
  .hv-scroll { max-height: 620px; overflow: auto; border-radius: 8px; }
  .hv-scroll::-webkit-scrollbar { height: 9px; width: 9px; }
  .hv-scroll::-webkit-scrollbar-thumb { background: var(--border); border-radius: 5px; }
  .hv-scroll::-webkit-scrollbar-track { background: transparent; }
  .html-preview {
    width: 100%; min-width: 520px; height: 220px; border: 0; display: block;
    background: var(--panel-2); color-scheme: light dark;
  }
  .hv-expand {
    position: absolute; top: 6px; right: 6px; z-index: 3; display: inline-flex;
    align-items: center; gap: 4px; padding: 3px 8px; font-size: 11px; border-radius: 6px;
    border: 1px solid var(--border); background: var(--panel); color: var(--text);
    cursor: pointer; opacity: .92;
  }
  .hv-expand:hover { opacity: 1; border-color: var(--accent); }
  .hv-overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,.55); z-index: 1000; padding: 24px;
    display: none; align-items: center; justify-content: center;
  }
  .hv-overlay.show { display: flex; }
  .hv-overlay-inner {
    position: relative; width: min(1040px, 96vw); height: min(88vh, 900px);
    background: var(--panel); border-radius: 12px; box-shadow: 0 24px 70px rgba(0,0,0,.55);
    overflow: hidden;
  }
  .hv-overlay-frame { width: 100%; height: 100%; border: 0; background: var(--panel-2); color-scheme: light dark; }
  .hv-overlay-close {
    position: absolute; top: 8px; right: 10px; z-index: 2; width: 28px; height: 28px;
    padding: 0; border-radius: 50%; border: 0; background: rgba(0,0,0,.55); color: #fff;
    font-size: 15px; line-height: 1; cursor: pointer; display: grid; place-items: center;
  }
  .hv-overlay-close:hover { background: rgba(0,0,0,.78); }
  /* Inline-rendered fields only. `.intro` / `.q-intro` are block-rendered (`.md`) and are
     styled by the `.md` rules below — listing them here too would leave two equal-specificity
     rules whose winner is decided by source order alone. */
  .summary code, li code, .gloss code {
    background: var(--panel-2); border: 1px solid var(--border); border-radius: 4px;
    padding: 0 4px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .9em;
  }
  .summary a, li a, .gloss a { color: var(--accent); }
  .mark {
    flex: 0 0 auto; width: 22px; height: 22px; border-radius: 50%;
    border: 2px solid var(--border); display: grid; place-items: center; font-size: 13px; color: transparent;
  }
  .card.sel .mark { background: var(--accent-2); border-color: var(--accent-2); }
  .bar {
    position: fixed; left: 0; right: 0; bottom: 0; background: var(--panel);
    border-top: 1px solid var(--border); padding: 14px 20px; display: flex; gap: 14px;
    align-items: center; backdrop-filter: blur(8px);
  }
  .bar .inner { max-width: clamp(1100px, 88vw, 1760px); margin: 0 auto; width: 100%; display: flex; gap: 14px; align-items: center; }
  textarea {
    flex: 1; background: var(--panel-2); color: var(--text); border: 1px solid var(--border);
    border-radius: 8px; padding: 9px 12px; resize: none; height: 42px; font: inherit; font-size: 14px;
  }
  button {
    background: var(--accent-2); color: #fff; border: 0; border-radius: 9px; padding: 11px 22px;
    font-size: 15px; font-weight: 600; cursor: pointer; white-space: nowrap;
  }
  button:disabled { opacity: .45; cursor: not-allowed; }
  .hint { color: var(--muted); font-size: 13px; }
  .overlay {
    position: fixed; inset: 0; z-index: 2000; background: var(--bg); display: none; place-items: center; text-align: center;
  }
  .overlay.show { display: grid; }
  .overlay .check { font-size: 56px; color: var(--good); }
  /* Block markdown (`.md`): the fields rendered through the vendored `marked` — the page
     `description`, each question `description`, and every `sections[].body`. Everything
     here is scoped under `.md` so option-card prose (inline-only) is untouched. */
  .md > :first-child { margin-top: 0; }
  .md > :last-child { margin-bottom: 0; }
  .md p { margin: 0 0 10px; }
  .md h1, .md h2, .md h3, .md h4, .md h5, .md h6 {
    margin: 18px 0 8px; line-height: 1.3; color: var(--text); font-weight: 650;
  }
  .md h1 { font-size: 20px; } .md h2 { font-size: 18px; } .md h3 { font-size: 16px; }
  .md h4, .md h5, .md h6 { font-size: 14.5px; }
  .md ul, .md ol { margin: 0 0 10px; padding-left: 22px; }
  .md li { margin: 3px 0; }
  .md li > p { margin: 0 0 4px; }
  .md blockquote {
    margin: 0 0 10px; padding: 2px 0 2px 12px;
    border-left: 3px solid var(--border); color: var(--muted);
  }
  .md hr { border: 0; border-top: 1px solid var(--border); margin: 16px 0; }
  .md code {
    background: var(--panel-2); border: 1px solid var(--border); border-radius: 5px;
    padding: 0 4px; font-size: .9em;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .md pre { margin: 0 0 10px; }
  .md pre code { background: none; border: 0; padding: 0; font-size: inherit; }
  .md a { color: var(--accent); }
  /* Tables scroll inside their own box rather than widening the page. */
  .md .md-table-scroll { overflow-x: auto; margin: 0 0 12px; }
  .md table { border-collapse: collapse; font-size: 13.5px; background: var(--panel); }
  .md th, .md td {
    border: 1px solid var(--border); padding: 6px 11px; text-align: left; vertical-align: top;
  }
  .md thead th { background: var(--panel-2); font-weight: 650; color: var(--text); white-space: nowrap; }
  .md tbody tr:nth-child(even) { background: color-mix(in srgb, var(--panel-2) 45%, transparent); }
  .md img { max-width: 100%; height: auto; border-radius: 6px; }
  /* Language toggle. Sits in the HEADER, never in the bottom .bar: that bar is the commit
     zone, and a toggle one tab-stop from Confirm invites a mis-click on a popup whose whole
     job is capturing a deliberate choice. Styled as a quiet pill for the same reason -- it
     must not compete with Confirm for the eye. */
  .head-row { display: flex; align-items: baseline; gap: 14px; margin: 0 0 8px; }
  .head-row h1 { flex: 1; margin: 0; }
  #lang {
    flex: none; font: inherit; font-size: 13px; color: var(--muted); cursor: pointer;
    background: var(--panel-2); border: 1px solid var(--border); border-radius: 999px;
    padding: 4px 14px; transition: all .12s ease; white-space: nowrap;
  }
  #lang:hover:not(:disabled) { color: var(--text); border-color: var(--accent); }
  #lang:disabled { cursor: progress; opacity: .65; }
  #lang-err { font-size: 12px; color: var(--bad); max-width: 340px; text-align: right; }
</style>
</head>
<body>
  <div class="wrap">
    <div class="head-row">
      <h1 id="title"></h1>
      <span id="lang-err" hidden></span>
      <button id="lang" type="button" hidden></button>
    </div>
    <div class="intro md" id="intro"></div>
    <p class="gloss gloss-page" id="gloss-page" hidden></p>
    <div id="sections"></div>
    <div id="questions"></div>
  </div>
  <div class="bar">
    <div class="inner">
      <textarea id="notes" placeholder="Optional notes / reasoning - you can confirm with just a note..."></textarea>
      <span class="hint" id="hint"></span>
      <button id="submit">Confirm</button>
    </div>
  </div>
  <div class="overlay" id="overlay">
    <div>
      <div class="check" id="ov-mark">&#10003;</div>
      <h2 id="ov-title">Choice recorded</h2>
      <p class="hint" id="ov-hint">You can return to your terminal.</p>
    </div>
  </div>
<script src="/marked.js?k=__TOKEN__"></script>
<script>
  // Every route requires the per-run token, so the page's own fetches carry it too. It
  // rides the query string and not a cookie on purpose: cookies ignore the port, so a
  // cookie set here would also be sent to every other service on this host.
  const TOK = "__TOKEN__";
  const SPEC = __SPEC_JSON__;
  const LANGS = __LANGS_JSON__;
  const SECTIONS = Array.isArray(SPEC.sections) ? SPEC.sections : [];
  // Normalize to a list of questions. A legacy spec (top-level options) becomes one
  // headerless question, so the rest of the page treats both shapes identically.
  // A sections-only spec (explainer mode, no questions/options) gets NO synthetic
  // question — the page is content + the shared notes box.
  const QUESTIONS = (Array.isArray(SPEC.questions) && SPEC.questions.length)
    ? SPEC.questions
    : ((SPEC.options && SPEC.options.length) || !SECTIONS.length)
      ? [{ id: "_q0", title: null, mode: SPEC.mode, options: SPEC.options || [] }]
      : [];
  const qid = (q, qi) => (q.id != null ? String(q.id) : "_q" + qi);

  // Quotes are escaped too, NOT optional: esc() is interpolated into double-quoted
  // attributes (the `image` visual's src=), and a spec field containing a `"` would
  // otherwise close the attribute and inject `onerror=` into the MAIN document -- not a
  // sandboxed iframe. Demonstrated live, so do not trim this character class back to
  // &<> on the grounds that text nodes do not need it.
  const esc = s => String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  // Escape a string for a DOUBLE-QUOTED HTML attribute (iframe srcdoc): only & and "
  // need encoding; < and > stay literal so srcdoc parses back into real HTML.
  const attrEsc = s => String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;");
  // Minimal, safe inline markdown: escape first, then **bold**, *italic*/_italic_, `code`, [text](url).
  function md(s) {
    if (s == null) return "";
    let t = esc(String(s));
    const codes = [];
    t = t.replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return "@@C" + (codes.length - 1) + "@@"; });
    t = t.replace(/\[([^\]]+)\]\((https?:[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    t = t.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    t = t.replace(/(^|[^_\w])_([^_\n]+)_/g, "$1<em>$2</em>");
    t = t.replace(/@@C(\d+)@@/g, (_, i) => "<code>" + codes[+i] + "</code>");
    return t;
  }

  // ---- Block markdown -------------------------------------------------------
  // `md()` above is inline-only. The three genuinely multi-line fields (page
  // description, question description, section body) go through the FULL GFM parser
  // instead, using the `marked` copy vendored at skills/rich-decision/assets/ and served
  // by this same server at /marked.js — so tables, lists, headings, and fences render,
  // offline, with no CDN. If that file is missing the page degrades to `md()`.
  const MD = (typeof marked === "object" && marked && typeof marked.parse === "function") ? marked : null;
  // breaks:true keeps the old `white-space: pre-wrap` feel — a lone newline stays a
  // line break, so specs written against the previous renderer still look right.
  if (MD) MD.setOptions({ gfm: true, breaks: true });

  // marked emits raw HTML for any HTML in the source, which drops the escape-first
  // property the inline renderer had. This puts a floor back under it: unknown tags are
  // unwrapped (their text kept), executable/embedding tags are dropped whole, and only
  // a known-safe attribute set on a known-safe URL scheme survives.
  const MD_TAGS = new Set(("p br hr h1 h2 h3 h4 h5 h6 ul ol li blockquote pre code em strong " +
    "del ins sup sub a img table thead tbody tfoot tr th td span div small mark abbr kbd").split(" "));
  const MD_DROP = new Set(("script style iframe object embed form input button select textarea " +
    "link meta base noscript template applet frame frameset").split(" "));
  const MD_ATTRS = new Set(["href", "src", "alt", "title", "colspan", "rowspan", "start", "type", "align"]);
  // `id` is deliberately NOT allowed: raw HTML in a body could carry id="submit" /
  // id="intro" / id="notes" and shadow the page's own getElementById lookups. And since
  // marked emits no ids of its own (v9 moved heading ids out to the marked-gfm-heading-id
  // extension, which is not installed), an in-page `#anchor` has nothing to reach either
  // way — so `#` is not an allowed scheme, and such a link unwraps to plain text rather
  // than sitting there looking clickable.
  // data: is image-only, and accepts both the `;base64,` and the bare `,` payload forms.
  const MD_URL_OK = /^(https?:\/\/|mailto:|\/asset|data:image\/(png|jpe?g|gif|webp|svg\+xml)[;,])/i;

  function mdSanitize(dirty) {
    // A <template>'s content is an inert document fragment: parsing it runs nothing
    // (no scripts, no image/iframe fetches). Everything below happens before insertion.
    const tpl = document.createElement("template");
    tpl.innerHTML = dirty;
    for (const el of Array.from(tpl.content.querySelectorAll("*"))) {
      if (!tpl.content.contains(el)) continue;   // dropped with an ancestor already
      const tag = el.tagName.toLowerCase();
      if (MD_DROP.has(tag)) { el.remove(); continue; }
      if (!MD_TAGS.has(tag)) { el.replaceWith(...el.childNodes); continue; }
      for (const attr of Array.from(el.attributes)) {
        const name = attr.name.toLowerCase();
        const bad = name.startsWith("on") || !MD_ATTRS.has(name) ||
          ((name === "href" || name === "src") && !MD_URL_OK.test(attr.value.trim()));
        if (bad) el.removeAttribute(attr.name);
        // /asset is a whitelisted scheme above but a token-gated route on the server, so
        // a markdown-authored one has to be tagged here -- the spec rewrite only reaches
        // `visual` fields. Untagged it 403s, and because the URL SURVIVED the sanitizer
        // the `[image: alt]` fallback never fires: the author sees nothing at all.
        else if ((name === "href" || name === "src") &&
                 /^\/asset/i.test(attr.value.trim()) && !/[?&]k=/.test(attr.value)) {
          el.setAttribute(attr.name, attr.value.trim() + "&k=" + TOK);
        }
      }
      if (tag === "a") {
        // href survived the scheme check -> make it a real external link; href was
        // stripped (javascript:, etc.) -> unwrap, so it can't LOOK like a live link.
        if (el.getAttribute("href")) {
          el.setAttribute("target", "_blank");
          el.setAttribute("rel", "noopener noreferrer");
        } else {
          el.replaceWith(...el.childNodes);
          continue;
        }
      }
      // An <img> whose src didn't survive would render as an invisible nothing. Show a
      // marker instead, so a bad image URL is visible to the author rather than silent.
      // `![](x.png)` — the most common form — has alt="", so the marker must NOT be
      // conditional on alt being non-empty.
      if (tag === "img" && !el.getAttribute("src")) {
        const alt = el.getAttribute("alt");
        el.replaceWith(document.createTextNode("[image: " + (alt || "no alt") + "]"));
        continue;
      }
      // Wide tables scroll in their own box instead of stretching the page.
      if (tag === "table") {
        const box = document.createElement("div");
        box.className = "md-table-scroll";
        el.replaceWith(box);
        box.appendChild(el);
      }
    }
    return tpl.innerHTML;
  }

  // Fallback when the block renderer is unavailable. These containers no longer carry
  // `white-space: pre-wrap` (the block renderer owns line breaks), so the inline output
  // has to materialize newlines itself or the prose collapses into one run-on line.
  const mdInlineFallback = s => md(s).replace(/\n/g, "<br>");

  function mdBlock(s) {
    if (s == null || s === "") return "";
    if (!MD) return mdInlineFallback(s);            // vendored lib absent -> inline-only
    try { return mdSanitize(MD.parse(String(s))); }
    catch (e) { console.warn("markdown render failed, using inline fallback:", e); return mdInlineFallback(s); }
  }

  // A short gloss in the secondary language, rendered wherever the spec carries one (page,
  // section, question, option card). Returns "" when absent, so it's always safe to concatenate.
  const glossHtml = (s, cls) => s ? '<p class="gloss' + (cls ? " " + cls : "") + '">' + md(s) + "</p>" : "";

  // ---- Translation registry -------------------------------------------------------
  // Every user-visible string registers itself HERE as it renders and gets a sequential
  // key spliced into its element as data-t="tN". The translate button POSTs {key, text}
  // to /translate and paints the translation back into those same nodes.
  //
  // The CLIENT minting the keys is the whole point. The obvious alternative -- Python
  // walks the spec, JS walks it the same way -- needs two walkers agreeing on a path
  // scheme, and they drift silently the moment either side gains a field. Here the server
  // never sees the spec's shape at all: the code that RENDERS a string is the single
  // source of truth for whether that string is translatable.
  //
  // Painting into the existing nodes rather than re-rendering is equally deliberate: a
  // re-render would destroy the html-visual iframes and mermaid SVGs, force mermaid to
  // run again, and put `selections` / `qNotes` at risk. Swapping text in place leaves
  // visuals, selection highlights, and typed notes untouched.
  //
  // Deliberately NOT registered: `preview` (SKILL.md's contract is that it is the VERBATIM
  // text being approved -- translating it would change what the user is consenting to),
  // every `visual` (svg / mermaid / image / html srcdoc is code and markup), the glosses
  // (already in the secondary language), and all ids and urls.
  const T = [];
  const byKey = {};   // k -> item, so a string arriving mid-stream finds its node in O(1)
  let tSeq = 0;
  // mode: "" inline md | "b" block md | "p" plain text | "a:<attr>" attribute value
  // en: the text as rendered -- the spec's own words for content, English for chrome.
  // fixed: hand-written Chinese for FIXED UI chrome (Pros / Confirm / ...). A language with
  // hand-written chrome never sends it to the model: it costs nothing and flips instantly
  // while the content is still in flight. Any other target gets chrome from the model.
  function tReg(text, mode, fixed) {
    if (text == null || text === "") return null;
    const k = "t" + (tSeq++);
    const it = { k, en: String(text), mode: mode || "", fixed: fixed || null };
    T.push(it);
    byKey[k] = it;
    return k;
  }
  const t = (text, mode, fixed) => {          // -> ' data-t="tN"', to splice into a tag
    const k = tReg(text, mode, fixed);
    return k ? ' data-t="' + k + '"' : "";
  };
  const tEl = (el, text, mode, fixed) => {    // for nodes that already exist in the document
    const k = tReg(text, mode, fixed);
    if (k) el.setAttribute("data-t", k);
  };
  // Paints ONE node, so a string arriving mid-stream costs a single lookup instead of a
  // full pass over T for each of forty arrivals.
  function paintItem(it, v) {
    if (v == null) return;
    const el = document.querySelector('[data-t="' + it.k + '"]');
    if (!el) return;
    if (it.mode === "p") el.textContent = v;
    else if (it.mode.indexOf("a:") === 0) el.setAttribute(it.mode.slice(2), v);
    else el.innerHTML = it.mode === "b" ? mdBlock(v) : md(v);
  }
  function paint(pick) { for (const it of T) paintItem(it, pick(it)); }
  const paintOne = (k, v) => { if (byKey[k]) paintItem(byKey[k], v); };

  const titleEl = document.getElementById("title");
  const fallbackTitle = QUESTIONS.length ? "Make a choice" : "Walkthrough";
  titleEl.textContent = SPEC.title || fallbackTitle;
  // A spec-supplied title is content (translate it); the fallback is chrome (fixed string).
  tEl(titleEl, titleEl.textContent, "p",
      SPEC.title ? null : (QUESTIONS.length ? "做个选择" : "说明"));
  const introEl = document.getElementById("intro");
  introEl.innerHTML = mdBlock(SPEC.description || "");
  tEl(introEl, SPEC.description || "", "b");
  if (SPEC.gloss) {
    const gp = document.getElementById("gloss-page");
    gp.innerHTML = md(SPEC.gloss);
    gp.hidden = false;
  }
  // The reasoning note is ALWAYS available and every answer is optional, so Confirm is
  // never disabled — the user can submit a selection, just a note, or nothing.
  // (`allowNotes:false` no longer hides the shared note box.)
  const hintEl = document.getElementById("hint");
  const notesEl = document.getElementById("notes");
  hintEl.textContent =
    QUESTIONS.length > 1 ? "All optional - pick what you like or just leave a note"
    : QUESTIONS.length === 1 ? "Optional - pick an option or just leave a note"
    : "Read through, then Confirm - questions/comments welcome";
  // Chinese chrome uses full-width punctuation, matching what the translator emits for the
  // content around it -- ASCII commas and parens next to full-width prose read as a bug.
  tEl(hintEl, hintEl.textContent, "p",
      QUESTIONS.length > 1 ? "全部可选 - 想选就选，也可以只留一条备注"
      : QUESTIONS.length === 1 ? "可选 - 选一个方案，或者只留一条备注"
      : "读完后点确认 - 欢迎提问或评论");
  if (!QUESTIONS.length) {
    notesEl.placeholder = "Questions / comments (optional)...";
    document.querySelector("#overlay h2").textContent = "Response recorded";
  }
  // Fixed chrome: registered with a hardcoded gloss so the whole frame of the page flips
  // the instant the button is pressed, with no model call behind it.
  tEl(notesEl, notesEl.placeholder, "a:placeholder",
      QUESTIONS.length ? "备注 / 理由（可选）- 也可以只写备注就确认……" : "提问 / 评论（可选）……");
  tEl(document.getElementById("submit"), "Confirm", "p", "确认");
  tEl(document.querySelector("#overlay h2"), QUESTIONS.length ? "Choice recorded" : "Response recorded",
      "p", QUESTIONS.length ? "已记录选择" : "已记录回复");
  tEl(document.querySelector("#overlay .hint"), "You can return to your terminal.", "p", "可以返回终端了。");

  const root = document.getElementById("questions");
  const mermaidJobs = [];
  const htmlJobs = [];     // ids of html-preview iframes to auto-size after render
  const selections = {};   // qid -> Set of chosen option ids
  const qNotes = {};       // qid -> per-question notes <textarea> (only when allowNotes)
  const showNumbers = QUESTIONS.length > 1;

  // Prepended to every `html` visual's srcdoc. An iframe is its OWN document and does NOT
  // inherit the page's theme: `color-scheme` set on the iframe ELEMENT stays `normal`
  // inside (measured), so the embedded canvas is painted white in dark mode -- and that
  // white canvas paints OVER the `background: var(--panel-2)` the stylesheet puts on the
  // element, which is why that rule looked right and did nothing. A mockup written for a
  // dark page then showed light text on white and was unreadable.
  //
  // Canvas/CanvasText are the system pair, so they follow whatever the viewer is using.
  // Consequence worth knowing: a mockup that hardcodes a text colour must hardcode its
  // background too, or it is asserting half a colour scheme (SKILL.md says so).
  const IFRAME_THEME =
    '<meta name="color-scheme" content="light dark">' +
    "<style>:root{color-scheme:light dark}html,body{background:Canvas;color:CanvasText}</style>";

  function visualHtml(v, id) {
    if (!v) return "";
    // shorthand: a bare string starting with "<svg" is treated as inline SVG
    if (typeof v === "string") v = { type: v.trim().startsWith("<svg") ? "svg" : "image", code: v, src: v };
    if (v.type === "svg") return '<div class="visual">' + (v.code || "") + "</div>";
    if (v.type === "image") return '<div class="visual"><img alt="" src="' + esc(v.src || v.code) + '"></div>';
    // In the page itself, not in an html visual's iframe: only the spec-level `src` can
    // reach a local file (through the /asset whitelist), and the native controls need
    // nothing from the page. preload=metadata fetches the first frame, not the file.
    if (v.type === "video") return '<div class="visual visual-video"><video controls playsinline preload="metadata" src="' + esc(v.src || v.code) + '"></video></div>';
    if (v.type === "mermaid") {
      const mid = "mm-" + id;
      mermaidJobs.push({ id: mid, code: v.code || "" });
      return '<div class="visual" id="' + mid + '"><pre>' + esc(v.code || "") + "</pre></div>";
    }
    if (v.type === "html") {
      const fid = "hv-" + id;
      htmlJobs.push(fid);
      // sandbox WITHOUT allow-scripts -> the mockup renders but stays inert (no JS,
      // forms, or navigation); allow-same-origin lets the page read its size. The
      // preview keeps a readable min-width and scrolls; Enlarge opens it full-size.
      return '<div class="visual visual-html">' +
               '<button class="hv-expand" type="button" data-fid="' + fid + '" title="Enlarge">&#10530; ' +
                 "<span" + t("Enlarge", "", "放大") + ">Enlarge</span></button>" +
               '<div class="hv-scroll"><iframe class="html-preview" id="' + fid +
               '" sandbox="allow-same-origin" srcdoc="' + attrEsc(IFRAME_THEME + (v.code || "")) + '"></iframe></div>' +
             "</div>";
    }
    return "";
  }

  // Explainer sections render above the questions, full page width.
  const sroot = document.getElementById("sections");
  SECTIONS.forEach((s, si) => {
    const el = document.createElement("section");
    el.className = "explain";
    let h = "";
    if (s.title) h += '<h2 class="s-title"' + t(s.title) + ">" + md(s.title) + "</h2>";
    h += glossHtml(s.gloss);
    if (s.body) h += '<div class="s-body md"' + t(s.body, "b") + ">" + mdBlock(s.body) + "</div>";
    h += visualHtml(s.visual, "s" + si);
    el.innerHTML = h;
    sroot.appendChild(el);
  });

  // Score chips ("Effort low") + free-form `meta` fact chips ("$0/mo"). Scores are
  // qualitative on purpose -- low|med|high, never numbers: a number fakes measurement
  // and invites summing across options. Chip label + value carry SEPARATE data-t keys
  // because paintItem replaces the tagged element's whole content -- one key on the
  // chip would weld "Effort" and "low" into a single translation unit. An unknown
  // score value renders as a neutral chip with the literal text, never vanishes --
  // a typo must stay visible on the page it was written for.
  const SCORES = [
    ["effort", "Effort", "工作量", "low"],        // last column: which end is GOOD
    ["complexity", "Complexity", "复杂度", "low"],
    ["value", "Value", "价值", "high"],
  ];
  const SCORE_VAL_ZH = { low: "低", med: "中", high: "高" };
  const scoreChips = o => {
    let h = "";
    for (const [key, en, zhK, goodEnd] of SCORES) {
      if (o[key] == null) continue;
      let v = String(o[key]).toLowerCase();
      if (v === "medium" || v === "mid") v = "med";
      const known = Object.hasOwn(SCORE_VAL_ZH, v);   // not [v]: "constructor" must stay unknown
      const cls = (!known || v === "med") ? "" : (v === goodEnd ? " s-good" : " s-bad");
      h += '<span class="score"><span class="s-k"' + t(en, "", zhK) + ">" + en +
           '</span><span class="s-v' + cls + '"' + (known ? t(v, "", SCORE_VAL_ZH[v]) : t(v)) +
           ">" + esc(v) + "</span></span>";
    }
    // A bare-string meta is wrapped, not swallowed -- the one shape error that would
    // otherwise vanish, and every chip mistake here must stay visible on the page.
    for (const m of (Array.isArray(o.meta) ? o.meta : (o.meta != null ? [o.meta] : []))) {
      h += '<span class="score"><span class="s-v"' + t(String(m)) + ">" + md(String(m)) + "</span></span>";
    }
    return h ? '<div class="scores">' + h + "</div>" : "";
  };

  QUESTIONS.forEach((q, qi) => {
    const id = qid(q, qi);
    const multi = q.mode === "multi";
    const sel = new Set();
    selections[id] = sel;

    const section = document.createElement("section");
    section.className = "question";

    let head = "";
    if (q.title || showNumbers) {
      head += '<div class="q-head">';
      if (showNumbers) head += '<span class="q-num">' + (qi + 1) + " / " + QUESTIONS.length + "</span>";
      if (q.title) head += '<h2 class="q-title"' + t(q.title) + ">" + md(q.title) + "</h2>";
      head += "</div>";
    }
    head += glossHtml(q.gloss);
    if (q.description) head += '<div class="q-intro md"' + t(q.description, "b") + ">" + mdBlock(q.description) + "</div>";
    const qHint = multi ? "Select one or more (optional)" : "Select one (optional)";
    head += '<p class="q-hint"' + t(qHint, "", multi ? "可多选（均为可选）" : "选择一项（可选）") + ">" + qHint + "</p>";
    section.innerHTML = head;

    const grid = document.createElement("div");
    grid.className = "grid";

    const opts = (q.options || []).map((o, i) => ({ id: o.id != null ? String(o.id) : String(i), ...o }));
    opts.sort((a, b) => (b.recommended ? 1 : 0) - (a.recommended ? 1 : 0));   // recommended first
    for (const o of opts) {
      const card = document.createElement("div");
      card.className = "card";
      card.dataset.id = o.id;
      let html = '<div class="top"><h3' + t(o.label || o.id) + ">" + md(o.label || o.id) + "</h3>";
      if (o.recommended) html += '<span class="badge"' + t("Recommended", "", "推荐") + ">Recommended</span>";
      html += '<div class="mark">' + (multi ? "&#10003;" : "&#9679;") + "</div></div>";
      html += glossHtml(o.gloss);
      html += scoreChips(o);
      html += visualHtml(o.visual, id + "-" + o.id);
      if (o.summary) html += '<p class="summary"' + t(o.summary) + ">" + md(o.summary) + "</p>";
      if (o.pros && o.pros.length) html += '<div class="pc-label"' + t("Pros", "", "优点") + '>Pros</div><ul class="pros">' + o.pros.map(p => "<li" + t(p) + ">" + md(p) + "</li>").join("") + "</ul>";
      if (o.cons && o.cons.length) html += '<div class="pc-label"' + t("Cons", "", "缺点") + '>Cons</div><ul class="cons">' + o.cons.map(p => "<li" + t(p) + ">" + md(p) + "</li>").join("") + "</ul>";
      // `preview` is NEVER registered: SKILL.md's contract is that this box holds the exact
      // text being approved, so translating it would change the thing being consented to.
      if (o.preview) html += "<pre>" + esc(o.preview.replace(/^```[a-z]*\n?/i, "").replace(/```$/, "")) + "</pre>";
      card.innerHTML = html;
      card.onclick = (e) => {
        // Playing a video or enlarging a mockup inside the card is not a vote for it.
        if (e.target.closest("video, .hv-expand")) return;
        if (!multi) {                                       // single-select: clear only THIS question
          sel.clear();
          grid.querySelectorAll(".card").forEach(c => c.classList.remove("sel"));
        }
        if (sel.has(o.id)) { sel.delete(o.id); card.classList.remove("sel"); }
        else { sel.add(o.id); card.classList.add("sel"); }
      };
      grid.appendChild(card);
    }
    section.appendChild(grid);
    if (q.allowNotes) {
      const ta = document.createElement("textarea");
      ta.className = "q-notes";
      ta.placeholder = "Notes for this question (optional)...";
      tEl(ta, ta.placeholder, "a:placeholder", "针对本题的备注（可选）……");
      qNotes[id] = ta;
      section.appendChild(ta);
    }
    root.appendChild(section);
  });

  // Render any mermaid diagrams; if the lib can't load, the <pre> fallback stays.
  if (mermaidJobs.length) {
    const dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    import("https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs")
      .then(async ({ default: mermaid }) => {
        mermaid.initialize({ startOnLoad: false, theme: dark ? "dark" : "default" });
        for (const job of mermaidJobs) {
          try {
            const { svg } = await mermaid.render(job.id + "-svg", job.code);
            document.getElementById(job.id).innerHTML = svg;
          } catch (e) { /* keep code fallback */ }
        }
      })
      .catch(() => { /* offline: keep code fallback */ });
  }

  // html-preview iframes: auto-size to content height (capped, then it scrolls),
  // wire each Enlarge button to a full-size overlay, and build that overlay once.
  if (htmlJobs.length) {
    const sizeFrame = f => {
      try {
        const d = f.contentDocument;
        if (d && d.body) f.style.height = Math.min(d.body.scrollHeight + 2, 600) + "px";
      } catch (e) { /* cross-origin or not ready: keep the default height */ }
    };
    for (const fid of htmlJobs) {
      const f = document.getElementById(fid);
      if (!f) continue;
      f.addEventListener("load", () => sizeFrame(f));
      sizeFrame(f);
      setTimeout(() => sizeFrame(f), 80);
    }
    // Full-size overlay (lightbox), created once and reused by every Enlarge button.
    const overlay = document.createElement("div");
    overlay.className = "hv-overlay";
    overlay.innerHTML = '<div class="hv-overlay-inner"><button class="hv-overlay-close" type="button" aria-label="Close">&times;</button><iframe class="hv-overlay-frame" sandbox="allow-same-origin"></iframe></div>';
    document.body.appendChild(overlay);
    const oFrame = overlay.querySelector(".hv-overlay-frame");
    const closeOverlay = () => overlay.classList.remove("show");
    overlay.addEventListener("click", e => { if (e.target === overlay) closeOverlay(); });
    overlay.querySelector(".hv-overlay-close").addEventListener("click", closeOverlay);
    document.addEventListener("keydown", e => { if (e.key === "Escape") closeOverlay(); });
    document.querySelectorAll(".hv-expand").forEach(btn => {
      btn.addEventListener("click", () => {
        const f = document.getElementById(btn.dataset.fid);
        if (!f) return;
        oFrame.srcdoc = f.getAttribute("srcdoc") || "";
        overlay.classList.add("show");
      });
    });
  }

  // ---- Translate toggle -----------------------------------------------------------
  // On demand only: nothing is translated until this is clicked, so a popup nobody
  // translates costs zero tokens. The strings then STREAM in and paint one at a time as
  // the model finishes each: first text at ~3s rather than ~17s of a page that looks
  // like the button did nothing. After a complete run the map is cached in the page and
  // every later toggle is local -- no second request in either direction.
  //
  // PRIMARY is the language of the fixed labels; SECONDARY (optional) is the translate
  // target. Both come from the server (config file, else the OS list) because the page
  // cannot see them: navigator.languages reports only the browser's own UI language.
  const PRIMARY = LANGS.primary || "en";
  const SECONDARY = LANGS.secondary || null;
  const base = tag => String(tag || "").split("-")[0].toLowerCase();
  // The tag's script, inferred the way the server's _variant() does it: an explicit
  // four-letter subtag, else for Chinese the region (TW / HK / MO Traditional, the rest
  // Simplified), else none. The two sides must agree or a pair the server keeps apart
  // would reach the page looking like one language.
  const scriptOf = tag => {
    const p = String(tag || "").split("-");
    const s = p.slice(1).find(x => /^[A-Za-z]{4}$/.test(x));
    if (s) return s.charAt(0).toUpperCase() + s.slice(1).toLowerCase();
    if (p[0].toLowerCase() !== "zh") return "";
    return p.slice(1).some(x => ["tw", "hk", "mo"].includes(x.toLowerCase())) ? "Hant" : "Hans";
  };
  // The hand-written Chinese is Simplified, so it serves only a Simplified tag; zh-Hant gets
  // its labels from the model.
  const isHans = tag => base(tag) === "zh" && scriptOf(tag) === "Hans";
  // Fixed labels exist hand-written in English (`en`) and Simplified Chinese (`fixed`). Any
  // other primary shows the English ones; any other target gets them from the model.
  const chromeIn = (it, tag) => isHans(tag) ? it.fixed : base(tag) === "en" ? it.en : null;
  const original = it => it.fixed ? (chromeIn(it, PRIMARY) || it.en) : it.en;
  // Each language is named in itself (中文, 日本語, Español), the way a language picker
  // does it: a reader of that language finds it without knowing the other one. The script
  // joins the name only when both languages share a base, where it is the whole
  // difference (简体中文 / 繁體中文).
  const sameBase = !!SECONDARY && base(PRIMARY) === base(SECONDARY);
  const langName = tag => {
    const script = scriptOf(tag);
    const code = sameBase && script ? base(tag) + "-" + script : base(tag);
    try {
      const n = new Intl.DisplayNames([code], { type: "language" }).of(code);
      if (n) return n.charAt(0).toLocaleUpperCase(base(tag)) + n.slice(1);
    } catch (e) { /* Intl.DisplayNames needs Safari 14.1+; fall through */ }
    return code.toUpperCase();
  };
  // Status lines are hand-written in English and Simplified Chinese; any other target gets
  // the English ones.
  const MSGS = {
    en: { busy: (g, n) => "Translating " + g + "/" + n,
          cut: m => "Translation interrupted: " + m + " left in the original",
          partial: m => m + " left in the original",
          fail: w => "Translation failed: " + w, closed: "connection closed", timeout: "timed out" },
    zh: { busy: (g, n) => "翻译中 " + g + "/" + n,
          cut: m => "翻译中断：" + m + " 段未译出，保留原文",
          partial: m => m + " 段未译出，保留原文",
          fail: w => "翻译失败：" + w, closed: "连接中断", timeout: "超时" },
  };
  const msg = isHans(SECONDARY) ? MSGS.zh : MSGS.en;

  document.documentElement.lang = PRIMARY;
  if (base(PRIMARY) !== "en") paint(it => (it.fixed ? original(it) : null));

  const langBtn = document.getElementById("lang");
  // Offered only when a secondary language is configured: nobody else has a use for it,
  // and an unasked-for model call is the last thing it should be.
  langBtn.hidden = !SECONDARY;
  const fwdLabel = SECONDARY ? langName(SECONDARY) : "";
  const backLabel = langName(PRIMARY);
  langBtn.textContent = fwdLabel;
  const langErr = document.getElementById("lang-err");
  let trMap = null;
  let showingTr = false;
  // Distinct from `trMap != null`: a run that broke halfway leaves a usable PARTIAL map
  // that must not be mistaken for the finished one, or the button would never retry the rest.
  let trComplete = false;

  // Fixed chrome only. Painted the moment the button is pressed so the page frame flips
  // instantly while the content request is still in flight. A target with no hand-written
  // labels gets them with the content instead, so nothing paints here for it.
  const paintChrome = on => paint(it => (it.fixed ? (on ? chromeIn(it, SECONDARY) : original(it)) : null));
  const setLang = on => {
    showingTr = on;
    document.documentElement.lang = on ? SECONDARY : PRIMARY;
    langBtn.textContent = on ? backLabel : fwdLabel;
    // The partial-translation warning describes the translated view only -- leaving it up
    // over the restored original states a problem the user is no longer looking at.
    if (!on) langErr.hidden = true;
    paint(on ? (it => (it.fixed && chromeIn(it, SECONDARY)) || trMap[it.k]) : original);
  };

  langBtn.onclick = async () => {
    if (showingTr) { setLang(false); return; }
    if (trComplete) { setLang(true); return; }          // cached -> instant, no network
    // Content always goes to the model; a fixed label only when the target has no
    // hand-written text for it.
    const items = T.filter(it => !it.fixed || !chromeIn(it, SECONDARY)).map(it => ({ k: it.k, t: it.en }));
    if (!items.length) { trMap = {}; trComplete = true; setLang(true); return; }
    langErr.hidden = true;
    langBtn.disabled = true;
    paintChrome(true);
    // Translations start landing before the run finishes, so the page IS translated from
    // here on -- showingTr has to say so, or the back button would have nothing to restore.
    showingTr = true;
    document.documentElement.lang = SECONDARY;
    trMap = trMap || {};
    const total = items.length;
    // Counted per RUN, not against trMap: a retry after a partial failure re-delivers keys
    // trMap already holds, and counting those as "not new" would leave got at 0 while the
    // page visibly repaints -- which then sends a failure down the nothing-landed branch
    // and strands the page half translated under original chrome.
    const arrived = new Set();
    let got = 0;
    const progress = () => { langBtn.textContent = msg.busy(got, total); };
    progress();
    // IDLE, not total. A close-delimited stream gives the browser no other way to tell
    // "still generating" from "stalled", and a fixed wall clock would abort a long page
    // mid-flight for no reason. Every record pushes it back. The window is derived from
    // the server's own timeout so the server always speaks first.
    const abort = new AbortController();
    let idle = null;
    const bump = () => { clearTimeout(idle); idle = setTimeout(() => abort.abort(), __IDLE_MS__); };
    try {
      bump();
      const r = await fetch("/translate?k=" + TOK, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items }), signal: abort.signal,
      });
      // Rejections still arrive as one ordinary JSON body with a real status -- the
      // server picks the code before it commits to streaming, precisely so this works.
      if (!r.ok || !r.body) {
        const d = await r.json().catch(() => ({}));
        throw new Error(d.error || ("HTTP " + r.status));
      }
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "", closed = null;
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        bump();
        buf += dec.decode(value, { stream: true });
        // A record is only whole once its newline is in hand: a chunk boundary can land
        // anywhere, including the middle of a multi-byte character (which is what
        // stream:true above handles) or the middle of a line.
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line) continue;
          let rec;
          try { rec = JSON.parse(line); } catch (e) { continue; }
          if (rec.t === "k") {
            // Guarded by the per-run set, so a reconcile repaint of the same key is not
            // counted as new progress but a retry's first delivery still is.
            if (!arrived.has(rec.k)) { arrived.add(rec.k); got++; }
            trMap[rec.k] = rec.v;
            paintOne(rec.k, rec.v);
            progress();
          } else if (rec.t === "done") closed = rec;
          else if (rec.t === "error") throw new Error(rec.error || "translation failed");
        }
      }
      if (!closed) throw new Error(msg.closed);
      trComplete = true;
      setLang(true);
      if (closed.partial) {
        langErr.textContent = msg.partial(closed.partial);
        langErr.hidden = false;
      }
    } catch (e) {
      const why = e && e.name === "AbortError" ? msg.timeout : ((e && e.message) || e);
      if (got) {
        // KEEP WHAT LANDED. Tearing painted translations back off to show an error helps
        // nobody, and a half-translated page is the same shape as a partial result, which
        // this page has always tolerated. trComplete stays false, so pressing the button
        // again retries -- instantly, if the run actually finished server-side and cached.
        langBtn.textContent = backLabel;
        // Deliberately NOT the underlying error: "translation produced nothing" is true of
        // the validated result but flatly contradicts the translation the user is looking
        // at. The count is the part they can act on; the cause is on the server's stderr.
        langErr.textContent = msg.cut(total - got);
      } else {
        // Nothing landed, so there is no work to protect: go all the way back. setLang
        // rather than paintChrome, because chrome is not the only thing that may have been
        // painted -- an earlier run can have left content in trMap, and reverting only the
        // chrome would leave translated content under original chrome. setLang(false) also
        // clears langErr, so the message is set after it, not before.
        setLang(false);
        langErr.textContent = msg.fail(why);
      }
      langErr.hidden = false;
    } finally {
      clearTimeout(idle);
      langBtn.disabled = false;
    }
  };

  document.getElementById("submit").onclick = async () => {
    const payload = {
      answers: QUESTIONS.map((q, qi) => {
        const id = qid(q, qi);
        const a = { id, choice: [...selections[id]] };
        if (qNotes[id]) a.notes = qNotes[id].value.trim();
        return a;
      }),
      notes: document.getElementById("notes").value.trim(),
    };
    document.getElementById("submit").disabled = true;
    try {
      const r = await fetch("/submit?k=" + TOK, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      // 409 = another device confirmed first. Saying "Choice recorded" here would be a
      // lie of exactly the kind --lan makes easy to hit: two windows open, the loser
      // walks away believing their pick is the one the agent got.
      if (r.status === 409) {
        document.getElementById("ov-mark").innerHTML = "&#8212;";
        document.getElementById("ov-title").textContent = "Already answered";
        document.getElementById("ov-hint").textContent =
          "This decision was confirmed on another device. Your pick here was not used.";
      }
    } catch (e) {}
    document.getElementById("overlay").classList.add("show");
    // app-mode popup windows can self-close; a normal tab will just keep the overlay.
    setTimeout(() => { try { window.close(); } catch (e) {} }, 900);
  };
</script>
</body>
</html>"""


# ---- On-demand Chinese translation -----------------------------------------------------
# The page's 中文 button POSTs the strings it rendered to /translate; this runs them through
# `claude -p` and hands back the Chinese. Nothing is translated until that click -- a popup
# nobody translates spawns no subprocess and spends no tokens.
#
# No API key and no exported token: `claude -p` authenticates off the macOS Keychain entry
# the interactive CLI already uses. Do NOT "fix" this by adding --bare (that skips the
# Keychain and fails with "Not logged in") or by plumbing in ANTHROPIC_API_KEY.
# ---- Languages ------------------------------------------------------------------------
# primary: the language of the page's fixed labels. The agent writes the page itself in the
# conversation's language -- the one signal that the reader is reading it right now, which an
# OS list cannot promise.
# secondary (optional): what the translate button translates into; glosses are written in it.
# Resolved by the server because the page cannot see them: the native WKWebView reports
# navigator.languages = ["en-US"] and the Chrome fallback ["en-US", "en"] on a Mac whose
# preferred languages are English and Chinese.
CONFIG_PATH = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                           "rich-decision", "config.json")
_LANG_TAG = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


def _norm_tag(raw):
    """A BCP 47 tag from a config or OS value, or None. `zh_CN.UTF-8@x` -> `zh-CN`.

    The full tag is kept, script and region included: `zh-Hans` and `zh-Hant` are different
    translation targets, and the model reads the tag directly."""
    if not isinstance(raw, str):
        return None
    tag = raw.split(".")[0].split("@")[0].replace("_", "-").strip()
    return tag if _LANG_TAG.match(tag) else None


def _base(tag):
    return (tag or "").split("-")[0].lower()


_HANT_REGIONS = {"tw", "hk", "mo"}


def _variant(tag):
    """(language, script) -- what decides whether two tags read the same.

    Region alone does not (en-US and en-GB are one reader), but script does: zh-Hans and
    zh-Hant are different targets. A Chinese tag with no script is inferred from its region,
    the way the OS does it: zh-TW / zh-HK / zh-MO are Traditional, every other zh Simplified."""
    parts = (tag or "").split("-")
    script = next((p.title() for p in parts[1:] if len(p) == 4 and p.isalpha()), "")
    if parts[0].lower() == "zh" and not script:
        script = "Hant" if any(p.lower() in _HANT_REGIONS for p in parts[1:]) else "Hans"
    return parts[0].lower(), script


def _os_languages():
    """The user's preferred languages, in order, from the OS. [] when unreadable.

    macOS keeps the list in the global preferences plist; plistlib reads it with no
    subprocess, so a missing toolchain or a slow `defaults` can never stall a popup.
    Elsewhere the POSIX locale variables are the only list there is."""
    if sys.platform == "darwin":
        try:
            with open(os.path.expanduser("~/Library/Preferences/.GlobalPreferences.plist"),
                      "rb") as f:
                raw = plistlib.load(f).get("AppleLanguages") or []
        except Exception:                                   # noqa: BLE001 - optional input
            raw = []
    else:
        raw = (os.environ.get("LANGUAGE") or os.environ.get("LC_ALL")
               or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or "").split(":")
    return [t for t in (_norm_tag(x) for x in raw) if t]


def resolve_languages():
    """(primary, secondary or None, source): the config file, else the OS list, else English.

    A config file wins even when it leaves `secondary` out or sets it null -- that is how
    someone with a second OS language turns the translate button off."""
    cfg = None
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            raise ValueError("expected a JSON object")
    except FileNotFoundError:
        cfg = None
    except (OSError, ValueError) as e:
        print(f"warning: ignoring {CONFIG_PATH}: {e}", file=sys.stderr)
        cfg = None
    if cfg is not None:
        for key in ("primary", "secondary"):
            if cfg.get(key) not in (None, "") and not _norm_tag(cfg.get(key)):
                print(f"warning: {CONFIG_PATH}: {key}={cfg.get(key)!r} is not a language tag "
                      f"(e.g. \"en\", \"zh-Hans\")", file=sys.stderr)
        # A file that names only the secondary still means "my OS primary": defaulting the
        # missing key to English would silently relabel a Chinese- or Japanese-first page.
        os_langs = _os_languages() if not _norm_tag(cfg.get("primary")) else []
        primary = _norm_tag(cfg.get("primary")) or (os_langs[0] if os_langs else "en")
        secondary = _norm_tag(cfg.get("secondary"))
        source = CONFIG_PATH
        if secondary and _variant(secondary) == _variant(primary):
            print(f"warning: {CONFIG_PATH}: secondary {secondary} reads the same as primary "
                  f"{primary}; no translate button", file=sys.stderr)
            secondary = None
    else:
        langs = _os_languages()
        primary = langs[0] if langs else "en"
        secondary = next((t for t in langs[1:] if _variant(t) != _variant(primary)), None)
        source = "OS preferences" if langs else "default"
    return primary, secondary, source


def languages_line(langs):
    primary, secondary, source = langs
    return (f"languages: primary={primary} secondary={secondary or 'none'} (from {source}); "
            f"write the page in the conversation's language"
            + (", any glosses in the secondary (none when the conversation is already in it)"
               if secondary else ""))


XLATE_MODEL = "sonnet"          # mechanical transform; opus buys nothing here
XLATE_TIMEOUT = 180
XLATE_MAX_ITEMS = 400           # request caps: a client bug must not spawn an unbounded job
XLATE_MAX_CHARS = 60000
# Per-subprocess caps. The CHAR cap is the real one; the item cap is a backstop for a page
# of many tiny strings. It is deliberately high -- an ordinary 4-option page is ~40 short
# strings totalling ~1.5 KB, and splitting that across subprocesses costs startup latency
# for nothing. --json-schema's `required` turns a dropped key into a hard validation error,
# so chunking no longer has to defend against a silent truncated tail.
XLATE_CHUNK_ITEMS = 120
XLATE_CHUNK_CHARS = 6000
XLATE_WORKERS = 4
# A decision payload is ids, short notes and nothing else; this is orders of magnitude of
# headroom and exists only so a bogus Content-Length cannot ask for an unbounded read.
SUBMIT_MAX_BYTES = 1_000_000

_XLATE_LOCK = threading.Lock()          # single-flight: a double-click must not double-spend
_XLATE_CACHE = {}                       # sha1(target + payload) -> ({key: text}, missing)
_XLATE_CACHE_LOCK = threading.Lock()
# Live `claude -p` children, so shutdown can kill them. See kill_translations().
_XLATE_PROCS = set()
_XLATE_PROCS_LOCK = threading.Lock()
_XLATE_SHUTDOWN = False                 # latched by kill_translations; never cleared


def _claude_bin():
    """Absolute path to the `claude` CLI, or None. PATH can be thin when the server was
    started from a GUI-launched parent, so fall back to the standard install location."""
    exe = shutil.which("claude")
    if exe:
        return exe
    fallback = os.path.expanduser("~/.local/bin/claude")
    return fallback if os.access(fallback, os.X_OK) else None


def kill_translations():
    """Kill every in-flight translation child.

    NOT optional cleanup -- it is what lets the process exit. ThreadPoolExecutor's workers
    are non-daemon and `concurrent.futures.thread._python_exit` joins them at interpreter
    shutdown, so a translation still running when the user hits Confirm keeps the WHOLE
    process alive until its subprocess finishes, even though the popup has closed and the
    result is already written. Measured: main() returning at 0.00s, process exiting at
    6.03s. SKILL.md promises the caller "exits the moment they hit Confirm", and the agent
    blocks on that exit -- so the child has to die for the promise to hold. Killing it
    lets the workers return at once and the join becomes instant.

    LATCHES A FLAG, rather than just sweeping once. A one-shot sweep only kills the chunks
    that happen to be running: ThreadPoolExecutor's queue is FIFO and its workers drain
    pending items before they ever see the shutdown sentinel, so the freed workers
    immediately pull the next queued chunks and spawn FRESH children that nothing will
    ever kill. That resurrects the exact stall this function exists to remove, for any
    page over XLATE_CHUNK_CHARS * XLATE_WORKERS (~24k chars) of translatable text. The
    flag makes _run_claude refuse to spawn at all once shutdown has begun."""
    global _XLATE_SHUTDOWN
    with _XLATE_PROCS_LOCK:
        _XLATE_SHUTDOWN = True
        procs = list(_XLATE_PROCS)
    for proc in procs:
        try:
            proc.kill()
        except OSError:
            pass


class _FlatJsonStreamer:
    r"""Incremental reader for the one JSON shape `--json-schema` allows here: a flat
    object of string values. `feed(text)` returns the pairs that arrived IN FULL since the
    last call, in order.

    A pair is handed over only once its value's CLOSING quote is in the buffer, so a
    half-generated string can never reach the page. The walk is escape-aware -- a `\"` or
    a `\\` inside a value cannot fake a terminator, and a buffer that ends mid-escape is
    left untouched for the next feed().

    An exact cursor walk rather than a regex because the schema pins the shape: one `{`,
    then "key": "value" pairs, nothing nested. There is no depth to track, so the parser
    is both shorter and safer than the regex would have to be.

    `pos` only ever advances past a COMPLETE pair. Every partial return leaves it where it
    was, so the next feed() simply re-reads the same prefix with more bytes behind it."""

    # Shared: raw_decode carries no per-call state, and this is the same scanner the
    # envelope is parsed with.
    _DECODER = json.JSONDecoder()

    def __init__(self):
        self.buf = ""
        self.pos = 0
        self.closed = False

    def feed(self, text):
        self.buf += text
        out = []
        while not self.closed:
            pair = self._next_pair()
            if pair is None:
                break
            out.append(pair)
        return out

    def _skip_ws(self, i):
        while i < len(self.buf) and self.buf[i] in " \t\r\n":
            i += 1
        return i

    def _scan_string(self, i):
        r"""`i` points at an opening quote -> (decoded, index just past the close), or None
        if the literal has not finished arriving.

        raw_decode owns the entire string grammar: it returns (value, end) once the closing
        quote is in the buffer, and raises for every incomplete form -- unterminated, ending
        on a lone backslash, ending inside a half-typed \uXXXX -- which is precisely "not
        yet, come back with more bytes". It also unescapes exactly the way the final
        envelope will, so a preview and its validated value agree character for character.

        Hand-walking the escapes here is what makes a `\"` look like a terminator; the
        stdlib scanner is the same one that parses the envelope, so there is no second
        grammar to keep in sync."""
        try:
            return self._DECODER.raw_decode(self.buf, i)
        except ValueError:
            return None

    def _next_pair(self):
        i = self._skip_ws(self.pos)
        if i < len(self.buf) and self.buf[i] in "{,":
            i = self._skip_ws(i + 1)
        if i >= len(self.buf):
            return None
        if self.buf[i] == "}":
            self.closed = True
            return None
        if self.buf[i] != '"':
            return None
        scanned = self._scan_string(i)
        if scanned is None:
            return None
        key, i = scanned
        i = self._skip_ws(i)
        if i >= len(self.buf) or self.buf[i] != ":":
            return None
        i = self._skip_ws(i + 1)
        if i >= len(self.buf) or self.buf[i] != '"':
            return None
        scanned = self._scan_string(i)
        if scanned is None:
            return None
        value, self.pos = scanned
        return key, value


def _run_claude(prompt, schema, on_key=None):
    """One `claude -p` call, read as a live stream. Returns the parsed {key: translation}
    object from the final result envelope.

    `on_key(key, value)` fires for each value the moment it finishes generating, which is
    what puts the first Chinese on the page at ~3s instead of ~17s. THE STREAM IS A
    PREVIEW: the envelope at the end stays the authority, because it is the payload
    `--json-schema` validated -- so the caller re-emits any key whose final value differs
    from the one streamed here. Passing on_key=None makes this a plain blocking call.

    Flag choices, each load-bearing:
      --safe-mode              disables CLAUDE.md / skills / plugins / hooks / MCP for the
                               child. This is the ROOT FIX for two problems at once: the
                               user-level ASCII-punctuation rule was leaking in and putting
                               half-width commas in Chinese prose (a hand-written normalizer
                               used to fight that, and corrupted CJK URLs doing it), and the
                               session preamble was costing ~46k input tokens PER CALL.
      --json-schema            structured output, so there is no hand-rolled delimiter
                               protocol to frame multi-line GFM and no parser to get wrong.
                               `required` makes a dropped key a validation error instead of
                               a silent truncation.
      --output-format stream-json --include-partial-messages --verbose
                               the incremental view. Structured output arrives as a
                               StructuredOutput *tool_use* block whose input_json_delta
                               events carry the JSON as it is generated, and the closing
                               {"type":"result"} line carries the very same `result`
                               payload a plain `--output-format json` call returns -- so
                               _parse_result and everything downstream are unchanged.
                               --verbose is required by the CLI for stream-json under -p.
      --no-session-persistence otherwise every call leaves a ~35 KB transcript containing
                               the decision text under ~/.claude/projects, forever
                               (cleanupPeriodDays is 3650).
      --allowed-tools ""       nothing here needs a tool; this shrinks what a
                               prompt-injection payload in a spec could reach for.
    Never --bare: it skips the Keychain and fails with "Not logged in"."""
    exe = _claude_bin()
    if not exe:
        raise RuntimeError("`claude` CLI not found (PATH or ~/.local/bin)")
    cmd = [exe, "-p", prompt,
           "--model", XLATE_MODEL, "--effort", "low",
           "--safe-mode", "--no-session-persistence",
           "--allowed-tools", "",
           "--json-schema", json.dumps(schema),
           "--output-format", "stream-json", "--include-partial-messages", "--verbose"]
    # cwd is a neutral temp dir so no PROJECT CLAUDE.md is picked up either, and
    # stdin=DEVNULL skips the CLI's 3s wait for piped input it is never going to get.
    # Spawn INSIDE the lock, with the shutdown check, so there is no window in which a
    # child exists but is not yet registered (kill_translations would miss it) and none in
    # which a sweep has already run but this call spawns anyway. Serializing the spawns
    # costs nothing measurable -- Popen is a fork/exec, and there are at most
    # XLATE_WORKERS of them.
    with _XLATE_PROCS_LOCK:
        if _XLATE_SHUTDOWN:
            raise RuntimeError("server is shutting down")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, text=True,
                                cwd=tempfile.gettempdir())
        _XLATE_PROCS.add(proc)
    # stderr needs its own drain thread, because stdout is read line by line rather than
    # by communicate(): with nobody draining stderr, a chatty child fills the 64 KB stderr
    # buffer and blocks forever with its stdout half-written.
    err = []
    err_drain = threading.Thread(target=err.extend, args=(proc.stderr,), daemon=True)
    err_drain.start()
    # The wall clock is a watchdog thread, since reading stdout incrementally rules out
    # communicate()'s timeout.
    expired = threading.Event()

    def _expire():
        expired.set()
        try:
            proc.kill()
        except OSError:
            pass

    timer = threading.Timer(XLATE_TIMEOUT, _expire)
    # daemon, or it inherits non-daemon from the pool worker that created it and
    # threading._shutdown joins it -- holding the whole process open for up to
    # XLATE_TIMEOUT after Confirm, which is the exact stall kill_translations exists to
    # prevent. The finally below cancels it on every path; this is the backstop for the
    # paths that never reach a finally.
    timer.daemon = True
    timer.start()
    envelope, streamer, block, drained = None, _FlatJsonStreamer(), None, False
    try:
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue                        # a stray non-JSON line is not fatal
            kind = ev.get("type")
            if kind == "result":
                envelope = ev
                continue
            if kind != "stream_event" or not on_key:
                continue
            event = ev.get("event") or {}
            etype = event.get("type")
            # Pin the block index. A turn can carry more than one content block (a text or
            # thinking block alongside the tool call), and feeding another block's deltas
            # into the streamer would corrupt the scan for the whole chunk.
            if etype == "content_block_start":
                if (event.get("content_block") or {}).get("type") == "tool_use":
                    block = event.get("index")
                    # A FRESH streamer per block, because this is a full agentic loop: a
                    # schema-validation failure or a max_tokens stop makes the model open a
                    # SECOND StructuredOutput block, and its deltas would otherwise be
                    # appended to the abandoned attempt's buffer. Both outcomes are silent
                    # -- a truncated first attempt splices its tail onto the retry's `{`
                    # and paints garbage, and a first attempt that closed its `}` latches
                    # `closed`, so the good attempt streams nothing at all and the page
                    # falls back to painting everything at the end.
                    # Previews from the superseded attempt are corrected by the end-of-run
                    # reconcile against the validated envelope.
                    streamer = _FlatJsonStreamer()
            elif etype == "content_block_delta" and event.get("index") == block:
                delta = event.get("delta") or {}
                if delta.get("type") == "input_json_delta":
                    for key, value in streamer.feed(delta.get("partial_json") or ""):
                        on_key(key, value)
        drained = True
    finally:
        # Only on an ABNORMAL exit: at a clean EOF the child has already closed stdout and
        # is on its way out, and killing it there would turn a good run into exit -9.
        if not drained and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.stdout.close()
        except OSError:
            pass
        # The watchdog stays armed ACROSS the wait: stdout reaching EOF does not prove the
        # child exited, and a hang there would otherwise block this pool worker, its
        # handler thread and _XLATE_LOCK with nothing left to interrupt it.
        proc.wait()
        timer.cancel()
        err_drain.join(timeout=1)       # so the diagnostic tail below is not read half-written
        try:
            proc.stderr.close()
        except OSError:
            pass
        with _XLATE_PROCS_LOCK:
            _XLATE_PROCS.discard(proc)
    if expired.is_set():
        raise RuntimeError(f"translation timed out after {XLATE_TIMEOUT}s")
    # Checked explicitly: a non-zero exit can still print plausible-looking text, and
    # trusting stdout alone would splice an error message into the page as a translation.
    if proc.returncode != 0:
        tail = "".join(err).strip().splitlines()
        raise RuntimeError(f"claude exited {proc.returncode}: {tail[-1] if tail else 'no output'}")
    if envelope is None:
        raise RuntimeError("stream ended with no result envelope")
    return _parse_result(envelope)


def _parse_result(env):
    """Unwrap the run's result envelope. Its `result` is the model's payload, which
    arrives either as an object or as a JSON string that still needs a second parse."""
    if isinstance(env, dict) and env.get("is_error"):
        raise RuntimeError(str(env.get("result") or "claude reported an error")[:200])
    result = env.get("result") if isinstance(env, dict) else env
    if isinstance(result, str):
        result = json.loads(result)
    if not isinstance(result, dict):
        raise RuntimeError(f"expected a JSON object, got {type(result).__name__}")
    return result


# Script-specific punctuation rules, keyed on the target's base language. Only CJK needs one:
# the model's commonest defect there is ASCII punctuation inside full-width prose.
_PUNCT_RULES = {
    "zh": ("- Punctuation between Chinese words must be FULL-WIDTH (，。：；？！), never the\n"
           "  ASCII forms (,.:;?!). ASCII punctuation stays only inside code, URLs and\n"
           "  identifiers. This is the single most common defect in this task.\n"),
    "ja": ("- Punctuation in Japanese prose must be the full-width forms (、。！？「」), never\n"
           "  the ASCII forms (,.!?). ASCII punctuation stays only inside code, URLs and\n"
           "  identifiers. This is the single most common defect in this task.\n"),
}


def _build_prompt(group, target):
    """The input goes in as JSON too, not as `key: value` lines.

    A line-oriented framing has to answer "where does this value end", and the two fields
    that matter most (`description`, section `body`) are multi-line GFM -- so any such
    scheme needs a delimiter, and the delimiter needs escaping. JSON already solved that:
    newlines are escaped in, escaped out, and the schema pins the shape of the reply."""
    payload = json.dumps(dict(group), ensure_ascii=False, indent=1)
    return (
        f"Translate every VALUE in this JSON object into the language whose BCP 47 tag\n"
        f"is `{target}` (respect its script subtag: Hans is Simplified, Hant is Traditional).\n"
        "Return an object with the same keys, each mapped to its translation.\n"
        "\n"
        "Rules:\n"
        "- Preserve markdown exactly: **bold**, *italic*, `code`, [text](url), lists,\n"
        "  tables, headings, fenced code blocks.\n"
        "- Do NOT translate anything inside `backticks` or fenced code blocks, nor\n"
        "  identifiers, file paths, CLI flags, or URLs.\n"
        "- Keep each value's structure: a one-line value stays one line; a multi-paragraph\n"
        "  value keeps its paragraph breaks.\n"
        "- Translate meaning, not word-for-word. This is UI text a developer is reading in\n"
        "  order to make a decision.\n"
        + _PUNCT_RULES.get(_base(target), "")
        + "\n" + payload
    )


# A backslash-n that is NOT itself part of an escaped backslash. The leading run has to be
# counted, or `C:\\new\\dir` gets its `\n` eaten and becomes `C:\<LF>ew` -- the escaped
# backslash owns that `\`, and the `n` is just the next letter of the path.
_ESCAPED_NL = re.compile(r"(?<!\\)((?:\\\\)*)\\n")


def _repair_escapes(src, out):
    r"""Undo a double-escaped newline, when and only when the whole value lost its breaks.

    Writing JSON by hand, the model INTERMITTENTLY emits `\\n` where it means `\n`, so the
    value survives json.loads as a literal backslash-plus-n and the page renders "...成本。\n\n|
    候选方案" as one run of text -- the description's table and fenced block silently vanish.
    Seen once end to end and zero times in three direct repeats: the kind of intermittency
    a prompt rule will not close.

    The gate is that the SOURCE had real newlines and the OUTPUT has none. Transport
    double-escaping is uniform over a value -- if it happened, every break in that value
    became literal -- so "source had breaks, translation has none, translation has literal
    \n" is the signature, and a single-line value can never match it.

    An earlier version gated on "the escape is absent from the source" and claimed a
    translation could not legitimately introduce one. That was false, and it corrupted
    real output: rendering the English words "a newline escape" as a concrete `\n` code
    span is CORRECT translation, and the repair turned that span into raw whitespace. The
    gate above is what closes that -- such a value is single-line, so it can never match.

    Code spans are deliberately NOT skipped, even though skipping them looks safer. Under
    this bug the escaping is applied to the whole value, so the fence's OWN newlines were
    eaten too; leaving them literal collapses ```dart\nx();\n``` onto one line, which is
    not a fence at all -- the exact table-and-fence loss this function exists to prevent.
    An escape the model typed on purpose is still safe, because transport escaping would
    have doubled ITS backslash as well: a lost newline arrives as one backslash + n, an
    intentional one as two, and _ESCAPED_NL only matches the first."""
    if "\\n" not in out or "\n" not in src or "\n" in out:
        return out
    return _ESCAPED_NL.sub(lambda m: m.group(1) + "\n", out)


def _translate_chunk(group, target, on_key=None):
    """group: [(key, text)] -> ({key: translation}, [missing keys]).

    Splices whatever ARRIVED rather than failing the batch: one dropped key out of forty
    should not discard thirty-nine good translations. The missing ones stay English and
    are counted as partial.

    `on_key` receives each value as it streams, filtered and repaired by exactly the same
    rules the final pass applies. Running the preview through _repair_escapes too is what
    keeps the caller's reconcile a no-op in the normal case rather than a second full
    repaint of the page."""
    schema = {
        "type": "object",
        "properties": {k: {"type": "string"} for k, _ in group},
        "required": [k for k, _ in group],
        "additionalProperties": False,
    }
    src = dict(group)

    def preview(k, v):
        if k in src and isinstance(v, str) and v.strip():
            on_key(k, _repair_escapes(src[k], v))

    data = _run_claude(_build_prompt(group, target), schema, preview if on_key else None)
    got = {k: _repair_escapes(src[k], v) for k, v in data.items()
           if k in src and isinstance(v, str) and v.strip()}
    missing = [k for k, _ in group if k not in got]
    return got, missing


def _chunk_pairs(pairs):
    groups, cur, cur_chars = [], [], 0
    for key, text in pairs:
        if cur and (len(cur) >= XLATE_CHUNK_ITEMS or cur_chars + len(text) > XLATE_CHUNK_CHARS):
            groups.append(cur)
            cur, cur_chars = [], 0
        cur.append((key, text))
        cur_chars += len(text)
    if cur:
        groups.append(cur)
    return groups


def translate_items(pairs, target, on_key=None):
    """pairs: [(key, text)] -> ({key: translation}, n_untranslated).

    Partial failure NEVER raises: a chunk that errors contributes nothing and those keys
    simply stay English on the page. The worst case is an untranslated card next to
    translated ones, never a corrupted or half-spliced one.

    `on_key` is called from the WORKER threads, so anything it touches must be its own
    business to serialize (stream_translate's emit holds a lock for exactly that)."""
    if not pairs:
        return {}, 0
    out, missing_total = {}, 0
    groups = _chunk_pairs(pairs)
    with ThreadPoolExecutor(max_workers=min(XLATE_WORKERS, len(groups))) as pool:
        for group, future in [(g, pool.submit(_translate_chunk, g, target, on_key)) for g in groups]:
            try:
                got, missing = future.result()
                out.update(got)
                missing_total += len(missing)
                if missing:
                    print(f"warning: {len(missing)}/{len(group)} blocks missing from model output",
                          file=sys.stderr)
            except Exception as e:                          # noqa: BLE001 - degrade, don't crash
                missing_total += len(group)
                print(f"warning: translation chunk of {len(group)} failed: {e}", file=sys.stderr)
    return out, missing_total


def translate_precheck(payload, target):
    """Body: {"items":[{"k":..., "t":...}]} -> (err, job), where err is a (code, resp) pair
    to send instead, or None when the request may proceed.

    EVERY rejection lives here, ahead of the response's first byte. A streamed reply spends
    its status code the moment the headers go out, so a code chosen later cannot be sent --
    which is also why the single-flight lock is ACQUIRED here, while 429 is still
    expressible. On success the job owns the lock and stream_translate gives it back.

    Deliberately isolated from the decision result: nothing on this path touches --out,
    result_holder or done_event, so no translation failure can block or alter a Confirm."""
    if not target:
        return (400, {"ok": False, "error": "no secondary language configured"}), None
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return (400, {"ok": False, "error": "no items"}), None
    pairs = []
    for it in items:
        if not isinstance(it, dict):
            continue
        k, t = it.get("k"), it.get("t")
        if isinstance(k, str) and isinstance(t, str) and t.strip():
            pairs.append((k, t))
    if not pairs:
        return (400, {"ok": False, "error": "no translatable items"}), None
    if len(pairs) > XLATE_MAX_ITEMS or sum(len(t) for _, t in pairs) > XLATE_MAX_CHARS:
        return (413, {"ok": False, "error": "payload too large to translate"}), None

    # Keys are in the digest as well as the texts: the same server can serve a second page
    # (the workflow hands the user the URL, and reopening it is the documented recovery),
    # and an identical text set under different keys must not reuse the other page's map.
    # The target is in it too, so no map can ever be served in the wrong language.
    digest = hashlib.sha1((target + "\x1e" + "\x00".join(
        k + "\x1f" + t for k, t in pairs)).encode("utf-8")).hexdigest()

    # Checked BEFORE the lock, so a reopened page is still served instantly while another
    # window is mid-translation.
    with _XLATE_CACHE_LOCK:
        cached = _XLATE_CACHE.get(digest)
    if cached is not None:
        return None, {"pairs": pairs, "digest": digest, "cached": cached, "locked": False,
                  "target": target}
    # non-blocking: a second page translating at the same time is told to wait rather than
    # queued behind the first, so concurrent windows cannot stack up subprocess fan-outs.
    if not _XLATE_LOCK.acquire(blocking=False):
        return (429, {"ok": False, "error": "another page is translating; retry in a moment"}), None
    # Re-read under the lock: the holder may have filled this very digest while we waited.
    with _XLATE_CACHE_LOCK:
        cached = _XLATE_CACHE.get(digest)
    return None, {"pairs": pairs, "digest": digest, "cached": cached, "locked": True,
                  "target": target}


def stream_translate(job, emit):
    """Run the translation, emitting NDJSON as it happens.

    Records: {"t":"k","k":<key>,"v":<zh>} per string as it finishes generating, then
    exactly one closing {"t":"done","partial":N} or {"t":"error","error":...}. A stream
    that ends with neither is a broken connection, and the page says so.

    A failure AFTER the first record never discards what already landed: the page keeps
    the Chinese it has painted and only the remainder stays English -- the same contract a
    partial result has always had. It also has to work this way: by then the 200 is long
    sent, so an error can only travel as a record."""
    sent = {}
    write_lock = threading.Lock()

    def on_key(k, v):
        # Called from the WORKER threads, so the lock is what keeps NDJSON lines whole.
        with write_lock:
            if sent.get(k) == v:
                return
            sent[k] = v
            emit({"t": "k", "k": k, "v": v})

    try:
        if job["cached"] is not None:
            # The partial count is cached WITH the map. Replaying the map without it would
            # drop the "N 段未译出" warning on exactly the retry path the workflow
            # documents (reopen the URL), leaving the page quietly half-English.
            mapping, missing = job["cached"]
            for k, v in mapping.items():
                on_key(k, v)
            emit({"t": "done", "partial": missing, "cached": True})
            return
        mapping, missing = translate_items(job["pairs"], job["target"], on_key)
        # Reconcile. The streamed values came off an UNVALIDATED delta feed, so every key
        # the validated envelope disagrees with -- or that never streamed at all -- is
        # re-sent now. on_key drops the ones that already match, so this normally emits
        # nothing and the stream stays a pure preview of the same result.
        for k, v in mapping.items():
            on_key(k, v)
        # Gated on `mapping`, NOT on what streamed. A run killed mid-flight leaves keys in
        # `sent` -- previews of a run that then failed -- while `mapping` is empty, and
        # caching that empty map would make every later retry of this digest return
        # instantly with nothing, permanently. The page keeps its painted strings either
        # way; only the cache has to be strict about what actually completed.
        if not mapping:
            emit({"t": "error", "error": "translation produced nothing"})
            return
        with _XLATE_CACHE_LOCK:
            _XLATE_CACHE[job["digest"]] = (mapping, missing)
        emit({"t": "done", "partial": missing})
    except Exception as e:                              # noqa: BLE001 - degrade, don't crash
        emit({"t": "error", "error": f"{type(e).__name__}: {e}"})
    finally:
        if job["locked"]:
            _XLATE_LOCK.release()


def validate_spec(spec):
    """Reject specs that would silently produce wrong results. Duplicate question ids
    collapse in the qid->answer map (last-wins), so abort loudly instead. Empty-option
    questions only warn — the page tolerates them (they don't block Confirm)."""
    questions = spec.get("questions")
    if not (isinstance(questions, list) and questions):
        if not (spec.get("options") or spec.get("sections")):
            print("warning: spec has no questions, options, or sections - page will be empty",
                  file=sys.stderr)
        return
    ids = [str(q["id"]) if q.get("id") is not None else f"_q{i}"
           for i, q in enumerate(questions)]
    dupes = sorted({x for x in ids if ids.count(x) > 1})
    if dupes:
        raise SystemExit(f"error: duplicate question id(s) {dupes} — give each question a unique id")
    for i, q in enumerate(questions):
        if not q.get("options"):
            print(f"warning: question {i + 1} ({ids[i]}) has no options", file=sys.stderr)


def iter_options(spec):
    """Yield every option across all questions (or the legacy top-level options)."""
    questions = spec.get("questions")
    if isinstance(questions, list) and questions:
        for q in questions:
            for o in q.get("options", []):
                yield o
    else:
        for o in spec.get("options", []):
            yield o


def prepare_assets(spec, token):
    """Rewrite local image and video paths in option AND explainer-section visuals to
    /asset?i=N URLs and return the
    ordered whitelist of real filesystem paths. Index-based references mean the server
    can only ever serve files the spec referenced — no path traversal, no arbitrary reads.
    http(s):// and data: sources are left untouched (the browser loads them directly).

    The access token is baked into the rewritten src here rather than appended by the
    page, because these land in `<img src>` attributes built from the spec — the page
    never re-derives them, so a client-side append would have to find every consumer."""
    assets = []
    nodes = list(iter_options(spec)) + list(spec.get("sections") or [])
    for o in nodes:
        v = o.get("visual")
        if v is None:
            continue
        if isinstance(v, str):
            v = {"type": "svg", "code": v} if v.strip().startswith("<svg") else {"type": "image", "src": v}
            o["visual"] = v
        if v.get("type") not in ("image", "video"):
            continue
        src = v.get("src") or v.get("code") or ""
        if src.startswith(("http://", "https://", "data:")):
            continue
        if src.startswith("/asset"):
            # Already an index reference -- a re-run, or a spec that hand-wrote one. It
            # names a file the whitelist carries, so it needs the token appended, not a
            # second slot. Without this it would 403 as an invisible broken image.
            if not parse_qs(urlparse(src).query).get("k"):
                v["src"] = f"{src}&k={token}"
            continue
        path = os.path.abspath(os.path.expanduser(src))
        if not os.path.isfile(path):
            print(f"warning: {v.get('type')} not found: {src}", file=sys.stderr)
        v["src"] = f"/asset?i={len(assets)}&k={token}"
        assets.append(path)
    return assets


def index_questions(spec):
    """Build (qindex, order, legacy) for mapping submitted ids back to labels/titles.

    qindex: qid -> {"title": str|None, "labels": {optid: label}}. The qid/optid
    derivation MUST match the JS (q.id ?? "_qN", o.id ?? index). `legacy` is True for
    the single-question top-level shape, which gets a mirrored choice/chosen in the result."""
    questions = spec.get("questions")
    if isinstance(questions, list) and questions:
        legacy = False
    else:
        questions = [{"id": "_q0", "title": None, "options": spec.get("options", [])}]
        legacy = True
    qindex, order = {}, []
    for qi, q in enumerate(questions):
        q_id = str(q["id"]) if q.get("id") is not None else f"_q{qi}"
        labels = {}
        for oi, o in enumerate(q.get("options", [])):
            o_id = str(o["id"]) if o.get("id") is not None else str(oi)
            labels[o_id] = o.get("label", o_id)
        qindex[q_id] = {"title": q.get("title"), "labels": labels,
                        "allow_notes": bool(q.get("allowNotes"))}
        order.append(q_id)
    return qindex, order, legacy


def effective_title(spec):
    """The page/window title. Mirrors the client's QUESTIONS normalization exactly:
    "Walkthrough" only for a sections-only spec.

    Prefixed with a fixed marker because this string IS the window's only identity.
    The popup is a chromeless Chrome --app window running on a private profile, so in
    Mission Control, cmd-Tab and the Window menu it is an anonymous pane wearing the
    Chrome icon, and NOT findable in the user's own Chrome. A bare subject line ("Pick a
    database") gives no clue who opened it; the marker makes it scannable at a glance
    and searchable. The project rides INSIDE the marker rather than trailing the subject
    so it survives the truncation cmd-Tab and Mission Control apply to long titles --
    which is the one place the label has to work. Window title only -- the page's own
    <h1> comes from SPEC.title."""
    if spec.get("title"):
        subject = str(spec["title"])
    else:
        qs = spec.get("questions")
        has_questions = isinstance(qs, list) and bool(qs)
        walkthrough = ((not has_questions) and bool(spec.get("sections"))
                       and not (spec.get("options") or []))
        subject = "Walkthrough" if walkthrough else "Make a choice"
    proj = detect_project()
    marker = f"[{WINDOW_APP} · {proj}]" if proj else WINDOW_MARKER
    return f"{marker} {subject}"


def build_handler(spec, out_path, result_holder, done_event, assets, ctx, token, langs):
    # html.escape the title (it lands in <title>); `</` -> `<\/` keeps any spec string
    # containing "</script>" from breaking out of the inline <script> block (json.dumps
    # leaves "/" unescaped, and `<\/` is still valid JSON that parses back to "</").
    #
    # ONE pass over the template, not chained .replace() calls: chaining re-scans text a
    # previous step already inserted, so a spec whose own text contains a later placeholder
    # gets that substitution spliced into the JSON string literal — a syntax error that
    # blanks the page and hangs the wait on /submit forever. Single-pass is immune in
    # every direction, whatever a spec happens to say.
    subs = {
        "__TITLE__": html.escape(effective_title(spec)),
        "__SPEC_JSON__": json.dumps(spec).replace("</", "<\\/"),
        # Derived, never hardcoded in the page: the client's idle deadline has to outlast
        # the server's own per-chunk timeout, or a stalled chunk trips the browser first
        # and the user gets a bare 超时 instead of the server's specific error record.
        "__IDLE_MS__": str((XLATE_TIMEOUT + 20) * 1000),
        "__TOKEN__": token,
        # No `claude` CLI means a button that can only fail (a Codex-only machine, say), so
        # the page is told there is no target; glosses still follow langs[1].
        "__LANGS_JSON__": json.dumps({"primary": langs[0],
                                      "secondary": langs[1] if _claude_bin() else None}),
    }
    # longest key first: `re` alternation is leftmost-first, not longest-match, so if a
    # placeholder is ever added that prefixes another, dict order would silently decide
    pattern = "|".join(re.escape(k) for k in sorted(subs, key=len, reverse=True))
    page = re.sub(pattern, lambda m: subs[m.group(0)], PAGE)
    marked_js = load_marked()
    qindex, order, legacy = index_questions(spec)
    submit_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            parsed = urlparse(self.path)
            # Above the dispatch: the page body is the decision itself -- options, code
            # previews, screenshots of the user's screen -- so it is the content this
            # guard exists for, not just the POST routes below.
            if not self._authed(parsed.query):
                self.send_response(403)
                self.end_headers()
                return
            if parsed.path in ("/", "/index.html"):
                # Proof that a real window exists and rendered. open_page waits on this
                # before trusting a popup tier, because "the process is still alive" is
                # not the same thing -- a host that launches with no window server to
                # draw into satisfies that and leaves the user with nothing. Unlike an
                # AppleScript probe this needs no Automation permission and cannot
                # confuse "no window" with "TCC denied".
                #
                # The token is ECHOED, not just flagged: it identifies which tier's
                # launch caused this fetch, so a late render from an already-abandoned
                # tier cannot satisfy the tier that replaced it.
                #
                # An untagged fetch must be IGNORED, not stored: the URL printed to
                # stderr carries no _w, so the user opening it (which the workflow
                # explicitly tells them to do) would otherwise overwrite a live token
                # with "" and un-satisfy the tier that genuinely drew a window -- killing
                # a good popup and opening a second one. Recording nothing means an
                # untagged fetch satisfies nobody AND unsatisfies nobody.
                tok = (parse_qs(parsed.query).get("_w") or [""])[0]
                if tok:
                    ctx["served"] = tok
                body = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif parsed.path == "/marked.js":
                # 404 when the asset is missing -> `marked` stays undefined and the page
                # falls back to the inline renderer, rather than failing to load.
                if marked_js is None:
                    self.send_error(404)
                    return
                body = marked_js.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                # no validator is sent, so a fixed --port + a bumped asset could otherwise
                # serve a stale copy from a reused browser profile
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif parsed.path == "/asset":
                try:
                    i = int(parse_qs(parsed.query).get("i", ["-1"])[0])
                except ValueError:
                    i = -1
                if not (0 <= i < len(assets)):
                    self.send_response(404)
                    self.end_headers()
                    return
                # Byte ranges are what make a `video` visual play: WebKit opens a media
                # source with `Range: bytes=0-1` and gives up on a server that answers 200.
                try:
                    f = open(assets[i], "rb")
                except OSError:
                    self.send_response(404)
                    self.end_headers()
                    return
                with f:
                    size = os.fstat(f.fileno()).st_size
                    start, end, partial = 0, size - 1, False
                    rng = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
                    # An inverted range (`bytes=5-3`) is invalid, and RFC 9110 says to ignore it
                    # and serve the whole file; only a range past the end is unsatisfiable.
                    if rng and (rng.group(1) or rng.group(2)) and not (
                            rng.group(1) and rng.group(2) and int(rng.group(1)) > int(rng.group(2))):
                        partial = True
                        if rng.group(1):
                            start = int(rng.group(1))
                            end = min(int(rng.group(2)), size - 1) if rng.group(2) else size - 1
                        else:  # suffix form: the last N bytes
                            start = max(size - int(rng.group(2)), 0)
                        if start > end:
                            self.send_response(416)
                            self.send_header("Content-Range", f"bytes */{size}")
                            self.end_headers()
                            return
                    ctype = mimetypes.guess_type(assets[i])[0] or "application/octet-stream"
                    self.send_response(206 if partial else 200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(end - start + 1))
                    self.send_header("Accept-Ranges", "bytes")
                    if partial:
                        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                    self.end_headers()
                    # Streamed, never read whole: players ask for open-ended `bytes=N-`
                    # ranges, so one request can span an entire recording.
                    f.seek(start)
                    left = end - start + 1
                    try:
                        while left > 0:
                            chunk = f.read(min(1 << 16, left))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            left -= len(chunk)
                    except ConnectionError:
                        pass  # a player that seeks abandons the request it no longer needs
            else:
                self.send_response(404)
                self.end_headers()

        def _authed(self, query):
            """Constant-time check of the per-run access token on EVERY route.

            The token, not the port, is what makes this server safe to reach: the port is
            ephemeral but not secret (~16k values on macOS, brute-forceable), and with
            --lan the listener is reachable by anything on the subnet. A forged /submit
            hands the calling agent a decision the user never made, and this skill gates
            releases and architecture forks -- a forged choice is a forged authorization.
            The cross-origin guard below cannot cover that case at all: it is a browser
            guard, and curl sends whatever headers it likes.

            No route is exempt, /marked.js included. An exemption list is a second thing
            to keep correct, and the page has the token anyway.

            Encoded before compare_digest: it raises TypeError on a non-ASCII str, and
            the query string is attacker-controlled."""
            got = (parse_qs(query).get("k") or [""])[0]
            return hmac.compare_digest(got.encode("utf-8", "replace"), token.encode())

        def _send_json(self, code, obj):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        _emit_dead = False

        def _emit_ndjson(self, obj):
            """One NDJSON record, flushed. The flush IS the feature -- without it the
            records sit in the socket buffer and arrive together at the end, which is the
            behaviour this whole path exists to remove.

            A vanished client must not read as a failed translation: the write is
            best-effort and latches off after the first failure, so the run still finishes
            and still fills the cache for the reopen-the-URL retry."""
            if self._emit_dead:
                return
            try:
                # errors="replace": a lone surrogate (json.loads happily produces one from
                # a \udXXX escape) makes a strict encode raise ValueError, which is not an
                # OSError and would escape this guard to kill the handler mid-stream. One
                # mangled character beats a dead translation.
                line = json.dumps(obj, ensure_ascii=False) + "\n"
                self.wfile.write(line.encode("utf-8", "replace"))
                self.wfile.flush()
            except OSError:
                self._emit_dead = True

        def _reject_cross_origin(self):
            """CSRF guard for EVERY POST, running second, behind _authed.

            It lives above the route dispatch on purpose. It was first written for /translate
            alone -- reasoning carefully about the attack and then protecting the *lower*
            value endpoint, while /submit, which IS the decision, stayed open one line
            below. Verified live: a cross-origin `Content-Type: text/plain` POST wrote a
            forged answers/notes payload to --out, and the calling agent consumes that as
            the user's choice. This skill gates releases and architecture forks, so a
            forged choice is a forged authorization, and `{"answers":[]}` alone dismisses
            the popup and hands the agent an empty decision.

            Why a content-type check is the guard: a cross-origin
            `<form enctype="text/plain">` POST is a CORS *simple* request, so it needs no
            preflight and no JS, and its body can be shaped to parse as JSON. Requiring
            application/json is what forces a preflight, and OPTIONS is not routed, so the
            preflight fails and the request never arrives. The page's own fetches (both
            routes) already send that header, so there is nothing to change client-side.
            Sec-Fetch-Site is the belt to that braces on browsers that send it.

            This is NOT the thing standing between a stranger and a forged decision --
            _authed above is, and it runs first. This guard exists for the case _authed
            cannot see: a browser that legitimately HAS the token (the user's own open
            decision page) being driven cross-origin by another tab."""
            ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
            site = self.headers.get("Sec-Fetch-Site")
            if ctype != "application/json" or (site and site not in ("same-origin", "none")):
                self._send_json(403, {"ok": False, "error": "cross-origin request refused"})
                return True
            return False

        def _read_body(self, cap):
            """Content-Length parsed by the CALLER's try, and range-checked: a non-numeric
            value would otherwise raise out of do_POST with no response at all, and a
            negative one slips past a `>` cap and makes rfile.read(-1) block until EOF,
            wedging the handler thread."""
            length = int(self.headers.get("Content-Length", 0))
            if not 0 <= length <= cap:
                return None
            return self.rfile.read(length)

        def do_POST(self):
            # urlparse, not self.path: both routes now carry ?k=, so an equality test
            # against the raw path silently 404s every POST.
            parsed = urlparse(self.path)
            if not self._authed(parsed.query):
                self._send_json(403, {"ok": False, "error": "forbidden"})
                return
            if self._reject_cross_origin():
                return
            if parsed.path == "/translate":
                # Served on this same ThreadingHTTPServer, so a slow `claude -p` fan-out
                # does not stall a concurrent /submit -- Confirm stays responsive while
                # the page is mid-translation.
                try:
                    raw = self._read_body(XLATE_MAX_CHARS * 4)
                    if raw is None:
                        self._send_json(413, {"ok": False, "error": "payload too large"})
                        return
                    err, job = translate_precheck(json.loads(raw or b"{}"), langs[1])
                except Exception as e:                      # noqa: BLE001
                    self._send_json(400, {"ok": False, "error": f"{type(e).__name__}: {e}"})
                    return
                if err:
                    self._send_json(*err)
                    return
                # No Content-Length: the body is close-delimited (this server speaks
                # HTTP/1.0, so the connection closes at the end anyway) and its length is
                # not knowable until the last string is translated. Past this point the
                # status is spent -- every later failure rides the stream as a record.
                #
                # The header write is INSIDE the ownership guard. end_headers() is this
                # response's first real socket write, so a peer that already vanished (the
                # popup closed, or Confirm landed, right after the fetch went out) raises
                # BrokenPipeError here -- and stream_translate, the only code that releases
                # the single-flight lock, would never be reached. The lock would then be
                # held for the life of the process and every later 中文 click would get 429
                # telling the user to retry something that can never succeed.
                handed_off = False
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self._emit_dead = False
                    handed_off = True
                    stream_translate(job, self._emit_ndjson)
                finally:
                    if job["locked"] and not handed_off:
                        _XLATE_LOCK.release()
                return
            if parsed.path != "/submit":
                self.send_response(404)
                self.end_headers()
                return
            try:
                raw = self._read_body(SUBMIT_MAX_BYTES)
                if raw is None:
                    self._send_json(413, {"ok": False, "error": "payload too large"})
                    return
            except Exception as e:                          # noqa: BLE001
                self._send_json(400, {"ok": False, "error": f"{type(e).__name__}: {e}"})
                return
            try:
                data = json.loads(raw or b"{}")
                answers_in = data.get("answers")
                if answers_in is None:   # tolerate a very old client that only sends choice
                    answers_in = [{"id": order[0], "choice": data.get("choice", [])}]
                answers = []
                for a in answers_in:
                    q_id = a.get("id")
                    qi = qindex.get(q_id, {})
                    labels = qi.get("labels", {})
                    ids = a.get("choice", [])
                    chosen = [{"id": cid, "label": labels.get(cid, cid)} for cid in ids]
                    ans = {"id": q_id, "title": qi.get("title"), "choice": ids, "chosen": chosen}
                    if qi.get("allow_notes"):   # spec opted this question in — gate server-side, not on the client-sent key
                        ans["notes"] = a.get("notes", "")
                    answers.append(ans)
                result = {"runId": RUN_ID, "answers": answers, "notes": data.get("notes", "")}
                if legacy and answers:   # mirror the single-question shape for back-compat
                    result["choice"] = answers[0]["choice"]
                    result["chosen"] = answers[0]["chosen"]
            except Exception as e:   # malformed body: 400 + retry, never leave done_event unset (would hang)
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode())
                return
            # First submission wins, and the claim is atomic. Two devices holding the
            # page open at once is ORDINARY under --lan, not adversarial -- confirm on the
            # phone, then click the still-open popup on the Mac. "The server exits on
            # Confirm" is not a guard but a race: main()'s teardown runs kill_translations
            # (which waits on subprocess reaping) BEFORE httpd.shutdown(), so the listener
            # keeps accepting for the whole window, and the LATER write is what --out ends
            # up holding. The agent would then act on a choice the user already superseded.
            #
            # result_holder is the claim flag rather than a new one: it already IS the
            # record of "an answer arrived", and a second flag could disagree with it.
            with submit_lock:
                if "result" in result_holder:
                    self._send_json(409, {"ok": False, "error": "already answered"})
                    return
                # Written to a sibling temp and renamed, never in place: ThreadingHTTPServer
                # sets daemon_threads, so interpreter exit kills this thread wherever it
                # stands -- and a half-written out.json is not a file the agent refuses, it
                # is one it parses. os.replace is atomic within a directory.
                tmp = out_path + ".part"
                with open(tmp, "w") as f:
                    json.dump(result, f, indent=2)
                os.replace(tmp, out_path)
                result_holder["result"] = result
            # done_event AFTER the response, still: setting it first lets main() reach
            # close_popup and process exit while this thread is mid-write, and the daemon
            # thread dies with the reply unsent.
            self._send_json(200, {"ok": True})
            done_event.set()

    return Handler


def main():
    ap = argparse.ArgumentParser()
    # Not required at parse time so --new-dir can short-circuit; enforced by hand below, so
    # a plain run still fails with the same "required" complaint it always did.
    ap.add_argument("--spec")
    ap.add_argument("--new-dir", action="store_true",
                    help="print a fresh per-run directory for spec+result, then exit")
    # Optional on purpose. A fixed path like /tmp/decision_result.json is shared by every
    # session on the machine: two concurrent decisions then write one file, and whichever
    # agent reads it second acts on a choice its user never made. Omit --out and the server
    # picks a private per-run path and prints it.
    ap.add_argument("--out", default=None,
                    help="where to write the result; default: a fresh per-run temp file")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-open", action="store_true", help="don't open a browser at all")
    ap.add_argument("--no-app", action="store_true",
                    help="open a normal browser tab instead of a standalone app-window popup (macOS)")
    ap.add_argument("--no-sound", action="store_true", help="don't play an alert sound (macOS)")
    # Accepted and inert: the LAN listener is the default, and this flag is what the
    # habit and the older docs reach for. Refusing it would abort a decision popup
    # mid-turn over a no-op.
    ap.add_argument("--lan", action="store_true", help="no-op; the LAN listener is the default")
    ap.add_argument("--no-lan", action="store_true",
                    help="localhost-only: no listener reachable from the network")
    args = ap.parse_args()

    # Resolved once per run: the page labels, the translate button and every /translate
    # call must agree on one answer even if the config file changes mid-decision.
    langs = resolve_languages()
    if args.new_dir:
        # stderr, so stdout stays the one path the agent copies; the agent reads this line
        # to know which language to write the page in.
        print(languages_line(langs), file=sys.stderr)
        print(new_run_dir())
        return
    if not args.spec:
        ap.error("the following arguments are required: --spec")

    # Read the spec BEFORE minting any directory: new_run_dir() creates one on the spot, so
    # doing it first leaves an empty stray behind on every bad-spec invocation.
    with open(args.spec) as f:
        spec = json.load(f)

    # A private directory per run, so two sessions deciding at once cannot land on one file.
    # mkdtemp, not a name built from the pid or the clock: both repeat, and the failure they
    # produce is silent -- a parsed, well-formed result belonging to somebody else.
    if args.out:
        out_path = args.out
    else:
        # Beside the spec, so one run's spec and result stay together and `ls` tells the whole
        # story -- UNLESS the spec itself sits in a shared root, which is the case this whole
        # change exists for. The test is "is this directory private", asked of the directory;
        # an earlier version asked "is it named rich-decision-*", which is a guess about who
        # made it and was false for any caller using plain mktemp -d.
        spec_dir = os.path.dirname(os.path.abspath(args.spec))
        base = new_run_dir() if is_shared_dir(spec_dir) else spec_dir
        out_path = os.path.join(base, "result.json")

    # Checked on the FINAL path, after both branches: the default can land in a shared root
    # too (a spec written to /tmp), and a guard that inspects only the explicit flag cannot
    # see that. Same check on the spec's own directory -- it is readable by every session
    # during the write-then-launch window, and swapping it shows this user another's
    # questions.
    for label, p in (("--out", out_path), ("--spec", args.spec)):
        if is_shared_dir(os.path.dirname(os.path.abspath(p))):
            print(f"warning: {label} {p} sits directly in a directory every session shares; "
                  "another decision can overwrite it. Use --new-dir for a private path, or "
                  f'check runId == "{RUN_ID}" before acting on the result.', file=sys.stderr)

    validate_spec(spec)
    # Generated before the spec is rewritten: prepare_assets bakes it into every /asset
    # URL. 80 bits, hex so it stays typeable on a phone keypad when the URL is not
    # tappable -- the whole URL IS the capability, and nothing else gates the port.
    token = secrets.token_hex(10)
    assets = prepare_assets(spec, token)
    result_holder = {}
    done_event = threading.Event()
    ctx = {"popup": None}
    handler = build_handler(spec, out_path, result_holder, done_event, assets, ctx, token,
                            langs)
    # The LAN listener is the default, so a phone or tablet can answer without anyone
    # having to think about it beforehand. It widens the bind to every interface, never
    # just the LAN one: the popup on this machine still opens 127.0.0.1, and a
    # single-address bind would refuse it. --no-lan is the way back to localhost.
    lan = not args.no_lan
    httpd = ThreadingHTTPServer(("0.0.0.0" if lan else "127.0.0.1", args.port), handler)
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/?k={token}"

    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    print(f"Decision UI: {url}", file=sys.stderr)
    # Printed every run, not only when defaulted: the caller has to read the file it is
    # actually being written, and a path it merely assumed is the bug this whole block exists
    # to retire.
    print(f"Decision result: {out_path}  (runId {RUN_ID})", file=sys.stderr)
    print(languages_line(langs), file=sys.stderr)
    if lan:
        ip = lan_ip()
        if ip:
            print(f"Decision UI (LAN): http://{ip}:{port}/?k={token}", file=sys.stderr)
        else:
            # Still bound to 0.0.0.0 -- say so rather than implying it was ignored.
            print("warning: LAN listener on, but no LAN address found (no default route?)",
                  file=sys.stderr)
    else:
        print("note: --no-lan: localhost only, no second device can answer", file=sys.stderr)

    # The popup is a browser instance we OWN, so every exit path has to reap it -- it
    # would otherwise reparent to launchd and survive us as a stray chromeless window
    # plus a multi-MB profile dir. The abandon path matters most: the documented way to
    # recover from an unanswered decision is killing this background task, i.e. SIGTERM,
    # whose default action skips finally/atexit entirely. Route it through SystemExit so
    # the finally below runs; atexit backstops anything that bypasses it. close_popup is
    # idempotent, so the overlapping handlers are harmless. (SIGKILL is unreapable.)
    atexit.register(lambda: close_popup(ctx.get("popup")))
    # Deliberately NOT atexit.register(kill_translations): concurrent.futures registers its
    # pool join via threading._register_atexit, which CPython runs BEFORE atexit handlers,
    # so it would always fire after the join it exists to prevent. main()'s finally is the
    # only place it can work, and it runs first there.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    # SIGINT needs installing explicitly, not just leaving to Python: a shell hands
    # background jobs SIGINT as SIG_IGN, and Python then declines to install its own
    # handler over an inherited SIG_IGN -- so in the run_in_background mode this skill
    # actually uses, the KeyboardInterrupt branch below would be unreachable and an
    # interrupt would hang instead of aborting.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    if not args.no_open:
        ctx["popup"] = open_page(url, app_mode=not args.no_app, sound=not args.no_sound,
                                 ctx=ctx)

    try:
        done_event.wait()
    except KeyboardInterrupt:
        print("Aborted (no choice made)", file=sys.stderr)
        sys.exit(2)
    finally:
        # FIRST, before close_popup: that call deliberately blocks (a 0.4s settle plus a
        # proc.wait up to 5s) and re-raises on BaseException, so a second SIGTERM landing
        # in its window -- the documented abandon path -- would propagate straight out of
        # this finally and skip the kill entirely. There is no backstop behind it either:
        # concurrent.futures registers its pool join through threading._register_atexit,
        # and CPython runs those BEFORE atexit handlers, so an atexit-registered kill can
        # never fire in time to prevent the join it would be registered to prevent.
        kill_translations()
        httpd.shutdown()
        close_popup(ctx.get("popup"))
    print(json.dumps(result_holder.get("result", {})))


if __name__ == "__main__":
    main()
