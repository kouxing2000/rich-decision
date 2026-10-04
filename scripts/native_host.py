"""Build a tiny macOS .app bundle around a Swift source file, and cache it.

For any local tool that wants a REAL window -- its own name, icon, Dock tile, cmd-Tab
entry and Mission Control card -- instead of an anonymous browser window wearing someone
else's identity. The rich-decision popup is built with it, from `decision_host.swift` beside
this file: that host takes a URL on argv and mirrors the page title into the window title.

Another tool can reuse the SAME Swift source and differ only in its Info.plist and an
optional `AppIcon.png` passed as a resource. Both are part of the cache key, so each caller
gets an independently cached bundle with its own identity out of one file -- a copy of the
source would have to be kept in sync by instruction, which is the thing that always goes
stale.

    host = NativeHost(src=..., bundle="Viewer.app", exec_name="Viewer",
                      plist=INFO_PLIST, cache_root=..., label="viewer window",
                      resources={"AppIcon.png": png_bytes})
    exe = host.build()          # absolute path, or None -- never raises, never blocks
    host.evict(exe)             # built fine but will not run: drop it, rebuild next time

Every failure path returns None so the caller can degrade to a browser. A window that
cannot be shown at all is worse than one shown in the wrong frame.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile


def swiftc_path():
    """(swiftc, sdk) absolute paths, or None. Never the PATH name.

    Two traps stacked here, and the fix for the first is not enough on its own:

    1. `shutil.which("swiftc")` cannot detect a missing toolchain: /usr/bin/swiftc is
       the xcode-select shim (one binary hardlinked as swiftc, git, clang and dozens
       more), present on every Mac including ones with no toolchain. Invoking that shim
       with no usable developer directory opens the blocking "Install the command line
       developer tools?" GUI dialog and waits for a human.
    2. `xcode-select -p` exiting 0 does NOT mean a toolchain exists. It prints whatever
       DEVELOPER_DIR or the xcode-select record points at and exits 0 even when that
       path does not exist -- verified: `DEVELOPER_DIR=/nonexistent xcode-select -p`
       returns rc=0. So the answer has to be stat'ed, not trusted.

    Resolving to an absolute path under the developer dir means the shim is never the
    thing we exec, so the install dialog cannot be reached at all -- which is stronger
    than bounding it with a timeout, since killing swiftc would not dismiss a dialog
    owned by the system installer.

    The SDK has to be resolved here too, and that is not optional: the shim silently
    supplies one, so a bare `swiftc` that works through /usr/bin fails as soon as it is
    called directly -- "unable to load standard library for target arm64-apple-macosxNN".
    Both layouts are handled: Xcode keeps its SDK under Platforms/, a Command-Line-Tools-
    only install under SDKs/."""
    try:
        r = subprocess.run(["xcode-select", "-p"], check=False, timeout=5,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if r.returncode != 0:
            return None
        devdir = r.stdout.decode("utf-8", "replace").strip()
    except Exception:
        return None
    if not devdir:
        return None
    swiftc = next(
        (c for c in (
            os.path.join(devdir, "Toolchains", "XcodeDefault.xctoolchain", "usr", "bin",
                         "swiftc"),
            os.path.join(devdir, "usr", "bin", "swiftc"),
        ) if os.path.isfile(c) and os.access(c, os.X_OK)), None)
    if not swiftc:
        return None
    sdk = next(
        (d for d in (
            os.path.join(devdir, "Platforms", "MacOSX.platform", "Developer", "SDKs",
                         "MacOSX.sdk"),
            os.path.join(devdir, "SDKs", "MacOSX.sdk"),
        ) if os.path.isdir(d)), None)
    if not sdk:
        return None
    return swiftc, sdk


class NativeHost:
    # The OS floor the built binary declares. Each caller's Info.plist states the same
    # number in LSMinimumSystemVersion and nothing checks that they agree, so grep
    # LSMinimumSystemVersion before changing this -- only the Mach-O load command is
    # enforced, and a bump here that misses a plist leaves that plist overclaiming.
    DEPLOYMENT_TARGET = "11.0"

    # Compiler flags, kept as data so they can be folded into the cache key -- they are an
    # input to the binary, so a change here has to invalidate a cached build.
    #
    # `-target` is load-bearing, not tuning. With no deployment target swiftc stamps
    # LC_BUILD_VERSION minos from the SDK, so an Xcode newer than the running macOS
    # produces a binary that machine cannot open. It bites through LaunchServices, which
    # reads the load command and refuses with -10825: a caller that runs the bundle with
    # `open -a` loses its window, while one that execs the binary directly is unaffected.
    # Nothing inside the bundle explains it -- LSMinimumSystemVersion is ignored once the
    # load command disagrees.
    BUILD_RECIPE = ("-O", "-target",
                    f"{platform.machine()}-apple-macosx{DEPLOYMENT_TARGET}")

    def __init__(self, src, bundle, exec_name, plist, cache_root, label, resources=None):
        self.src = src
        self.bundle = bundle
        self.exec_name = exec_name
        self.plist = plist
        self.cache_root = cache_root
        self.label = label          # what to call this thing in a warning
        # {file name: bytes} copied into Contents/Resources. Bare names only: a name that
        # carries a separator would write outside the bundle.
        self.resources = dict(resources or {})
        for name in self.resources:
            if not name or os.path.basename(name) != name or name in (".", ".."):
                raise ValueError(f"resource name must be a bare file name: {name!r}")

    def build(self):
        """Path to the compiled executable inside the bundle, building it on first use.

        Returns None -- never raises, never blocks indefinitely -- when this machine
        can't produce one, so the caller falls back to a browser.

        The cache key covers the Swift source, the Info.plist, the resources and the macOS
        major version, all of which are free to read. That matters: the cache hit is checked
        BEFORE any toolchain probe, so a warm cache never runs a subprocess at all.
        Keying on the swiftc version instead would invert that -- the probe would have to
        run on every single launch just to compute the key.

        A machine that cannot build writes a negative marker and stops retrying, rather
        than paying a failed compile every time."""
        if sys.platform != "darwin":
            return None
        if not os.path.exists(self.src):
            # Loud, because every OTHER failure path here prints and this is the one that
            # fires when the shared Swift source moves -- a path every caller other than
            # rich-decision itself has to hardcode. Silent, it degrades a window to a browser tab with no clue
            # why, on a machine that is perfectly capable of building one.
            print(f"warning: {self.label} has no source at {self.src}", file=sys.stderr)
            return None
        try:
            with open(self.src, "rb") as f:
                src = f.read()
            # platform.machine() is in the key because os.path.exists(exe) is the ONLY
            # cache-validity test: an arm64 bundle restored onto an Intel Mac (Migration
            # Assistant copies ~/Library/Caches) would fail to exec every time and never
            # be rebuilt. BUILD_RECIPE covers the compiler flags for the same reason --
            # they are an input to the binary, so changing them must re-key it.
            h = hashlib.sha256(src + b"\0" + self.plist.encode() + b"\0"
                               + platform.mac_ver()[0].split(".")[0].encode() + b"\0"
                               + platform.machine().encode() + b"\0"
                               + " ".join(self.BUILD_RECIPE).encode())
            for name in sorted(self.resources):
                h.update(b"\0" + name.encode() + b"\0" + self.resources[name])
            key = h.hexdigest()[:16]
            root = os.path.join(self.cache_root, key)
            exe = os.path.join(root, self.bundle, "Contents", "MacOS", self.exec_name)
            if os.path.exists(exe):
                return exe
            marker = root + ".unbuildable"
            if os.path.exists(marker):
                # Say so every time rather than degrading silently: this machine would
                # otherwise use the fallback forever, and someone who later installs a
                # toolchain has no way to discover why nothing changed.
                print(f"warning: {self.label} disabled by {marker} (delete it to retry)",
                      file=sys.stderr)
                return None
            toolchain = swiftc_path()
            if not toolchain:
                self._mark_unbuildable(
                    root, "no Swift toolchain/SDK under the active developer dir")
                return None
            swiftc, sdk = toolchain

            os.makedirs(self.cache_root, exist_ok=True)
            # Built inside the cache dir, not $TMPDIR: the install below is an os.rename,
            # which fails with EXDEV across filesystems. A TMPDIR on a RAM disk or a
            # separate volume would otherwise make every build link fine and then fail to
            # install, silently, forever.
            build = tempfile.mkdtemp(dir=self.cache_root, prefix=".build-")
            try:
                contents = os.path.join(build, self.bundle, "Contents")
                os.makedirs(os.path.join(contents, "MacOS"))
                with open(os.path.join(contents, "Info.plist"), "w") as f:
                    f.write(self.plist)
                if self.resources:
                    os.makedirs(os.path.join(contents, "Resources"))
                    for name, data in self.resources.items():
                        with open(os.path.join(contents, "Resources", name), "wb") as f:
                            f.write(data)
                # Top-level statements only compile from a file literally named main.swift.
                main_swift = os.path.join(build, "main.swift")
                with open(main_swift, "wb") as f:
                    f.write(src)
                try:
                    r = subprocess.run(
                        [swiftc, "-sdk", sdk, *self.BUILD_RECIPE,
                         "-o", os.path.join(contents, "MacOS", self.exec_name), main_swift],
                        check=False, timeout=180,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                except subprocess.TimeoutExpired:
                    # Deliberately NOT marked unbuildable: a timeout is transient (a
                    # loaded machine, three builds at once), unlike a missing toolchain
                    # or a compile error. A permanent opt-out for a transient cause would
                    # need clearing by hand.
                    print(f"warning: {self.label} build timed out, using the fallback "
                          "this time", file=sys.stderr)
                    return None
                if r.returncode != 0:
                    out = (r.stdout or b"").decode("utf-8", "replace")
                    # Only the error lines: the build emits pages of warning context, and
                    # a blind tail of the combined stream can be entirely warnings with
                    # the actual error scrolled off.
                    errs = [ln for ln in out.splitlines() if ": error:" in ln] or \
                           out.strip().splitlines()[-5:]
                    self._mark_unbuildable(root, "\n".join(errs[:10]))
                    return None
                os.makedirs(root, exist_ok=True)
                try:
                    os.rename(os.path.join(build, self.bundle),
                              os.path.join(root, self.bundle))
                except OSError:
                    # Another process built the same key concurrently and won the race.
                    # Its bundle is already atomically in place, so use it -- do NOT
                    # pre-check with os.path.exists and skip the rename, which just turns
                    # the race into a silent downgrade for whoever loses.
                    pass
                if os.path.exists(exe):
                    return exe
                # The destination exists but holds no executable: a partially purged or
                # otherwise corrupt cache entry. Without this the rename fails forever,
                # the exe never appears, and EVERY launch pays a compile it throws away.
                shutil.rmtree(os.path.join(root, self.bundle), ignore_errors=True)
                print(f"warning: {self.label} cache entry was corrupt, cleared it; "
                      "using the fallback this time", file=sys.stderr)
                return None
            finally:
                shutil.rmtree(build, ignore_errors=True)
        except Exception:
            return None

    def app_path(self):
        """The .app directory for a bundle already built, or None. Cheap: no compile.

        `open -a` addresses the BUNDLE, not the executable, and that is what makes
        launch-or-raise one command: LaunchServices refuses to start a second instance of
        a bundle already running and activates the live one instead.
        """
        exe = self.build()
        if not exe:
            return None
        # .../<key>/<bundle>/Contents/MacOS/<exec>  ->  .../<key>/<bundle>
        return os.path.dirname(os.path.dirname(os.path.dirname(exe)))

    def evict(self, exe):
        """Delete the cached bundle whose executable is `exe`, and say where it was.

        Deliberately NOT a `.unbuildable` marker: this fires for a binary that compiled
        fine but cannot run, which a rebuild may well fix (a reinstalled toolchain, a
        matching SDK). A marker would make a recoverable condition permanent."""
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(exe))))
        # Refuse to walk outside the cache if the layout ever changes underneath us.
        if os.path.dirname(root) != self.cache_root:
            return
        shutil.rmtree(root, ignore_errors=True)
        print(f"warning: {self.label} built but would not run; removed {root} so the "
              f"next run rebuilds it", file=sys.stderr)

    def _mark_unbuildable(self, root, why):
        """Record that this machine cannot build the host, so it stops trying.

        Delete the marker (or the whole cache dir) to force a retry after installing a
        toolchain -- the path is printed so the user can act on it."""
        print(f"warning: {self.label} unavailable, using the fallback: {why}",
              file=sys.stderr)
        try:
            os.makedirs(self.cache_root, exist_ok=True)
            with open(root + ".unbuildable", "w") as f:
                f.write(why + "\n")
        except Exception:
            pass
