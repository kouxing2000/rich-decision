// Native host for the rich-decision popup: a WKWebView in a window that is a real,
// separately-identifiable macOS app.
//
// Why this exists instead of a Chrome --app window: the Chrome popup runs on a throwaway
// profile, so it is not a tab in the user's own browser AND it wears the Chrome icon in
// the Dock, cmd-Tab and Mission Control -- an anonymous window with no owner. Users went
// hunting for it. This binary is compiled into a real .app bundle, so it has its own name
// and icon everywhere macOS lists apps -- THAT is what makes it findable again, not window
// level. ONE source can serve SEVERAL apps (the decision popup, or another local tool
// that wants a window of its own), differing only in their Info.plist and an optional
// AppIcon.png, so every user-visible label -- the icon, the window title, the menu items --
// is derived from the bundle and never hardcoded. The window
// is an ordinary one and can be covered; pinning is opt-in on cmd-shift-T and persists
// (see toggleAlwaysOnTop).
//
// Compiled on demand and cached by native_host.py (beside this file). Built as
// main.swift so top-level code runs -- do not rename the copy in the build dir.
//
// Usage: RichDecision <url>

import Cocoa
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    private let url: URL
    private var window: NSWindow!
    private var web: WKWebView!
    private var titleObs: NSKeyValueObservation?
    /// Set while our own about:blank error page is being loaded, so the origin guard
    /// lets it through. Never reset: once the load has failed there is nothing to protect.
    private var showingFailure = false

    init(url: URL) {
        self.url = url
        super.init()
    }

    func applicationDidFinishLaunching(_ note: Notification) {
        NSApp.applicationIconImage = AppDelegate.appIcon()
        buildMenu()

        let config = WKWebViewConfiguration()
        // A `video` visual's fullscreen button does nothing without this.
        if #available(macOS 12.3, *) { config.preferences.isElementFullscreenEnabled = true }
        web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.allowsBackForwardNavigationGestures = false
        // Carry the last zoom over from the previous decision. `object(forKey:)` rather
        // than `double(forKey:)` so an absent key stays 100% instead of reading back 0.
        if let z = UserDefaults.standard.object(forKey: AppDelegate.zoomKey) as? Double,
           z > 0 { web.pageZoom = CGFloat(z) }

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1120, height: 820),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = AppDelegate.appName
        // NOT pinned by default. It used to be `.floating` unconditionally, on the theory
        // that a decision must not get lost behind another window -- but that theory is
        // already served by this tier existing at all: the app has its own name, icon,
        // Dock tile, cmd-Tab entry and Mission Control card, which is what actually made
        // the window findable again. Forcing it above everything else only stops the user
        // from reading the code or docs they need in order to DECIDE, which is the one
        // thing the popup is asking them to do. Pinning stays one keystroke away
        // (cmd-shift-T) and now persists, so anyone who wants it sets it once.
        window.level = AppDelegate.pinned ? .floating : .normal
        window.contentView = web
        window.center()
        window.makeKeyAndOrderFront(nil)

        // The page <title> already carries the decision's subject, so mirror it into the
        // window title rather than leaving a generic one in the Window menu.
        titleObs = web.observe(\.title, options: [.new]) { [weak self] view, _ in
            if let t = view.title, !t.isEmpty { self?.window.title = t }
        }

        web.load(URLRequest(url: url))
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return true
    }

    // MARK: - Navigation

    /// Hand a URL to the user's real browser -- but only a URL that belongs in a browser.
    ///
    /// NSWorkspace.open is a privileged sink: it will just as happily launch file://,
    /// shortcuts://run-shortcut, vscode:// or itms-services://. The page's own JS
    /// sanitizer already restricts hrefs, but that is the RENDERER, the least privileged
    /// component in this design, and the page content comes from an agent-authored spec
    /// (whose `html` visuals are injected unsanitized into a sandboxed iframe). One new
    /// unsanitized field there should not turn a click into an unconfirmed app launch, so
    /// the allowlist is enforced here too, next to the sink. Chrome -- the fallback tier --
    /// shows an "Open in <app>?" confirmation for foreign schemes; without this guard the
    /// native host would be a strict downgrade on exactly that point.
    private func openExternally(_ target: URL?) {
        guard let t = target,
              let scheme = t.scheme?.lowercased(),
              ["http", "https", "mailto"].contains(scheme) else {
            FileHandle.standardError.write(
                "decision host: refused to open \(target?.absoluteString ?? "nil")\n"
                    .data(using: .utf8)!)
            return
        }
        NSWorkspace.shared.open(t)
    }

    /// Keep the decision page put. A markdown link in a spec would otherwise navigate the
    /// main frame away from the form -- and this window has no back button, so the pending
    /// decision would be unreachable and the server would wait forever. Only the localhost
    /// origin we launched with may drive the main frame; everything else goes to the user's
    /// real browser. Sub-frames (the sandboxed srcdoc iframes used by `html` visuals) are
    /// not main-frame navigations and never reach the redirect branch.
    ///
    /// Scheme, host AND port must all match -- that is what an origin is. Host alone would
    /// wave through any scheme on the loopback address; scheme+host still waves through
    /// every OTHER port on it, and a dev server, a debug endpoint or :9222 DevTools is a
    /// realistic thing to be listening on one. Getting navigated away is unrecoverable
    /// here: no back button, no reload, and the server blocks on done_event forever.
    func webView(_ webView: WKWebView,
                 decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard navigationAction.targetFrame?.isMainFrame ?? false else {
            decisionHandler(.allow)
            return
        }
        // Our own error page is loaded with baseURL: nil, i.e. a main-frame navigation to
        // about:blank -- which fails the origin test below. Without this the recovery UI
        // silently cancels itself and the blank window it exists to replace stays blank.
        // Scoped to that one URL rather than an allow-all latch: once set the flag never
        // clears, and a blanket allow would also admit file:// (WKWebView navigates on a
        // file drop) for the rest of the process's life.
        if showingFailure && navigationAction.request.url?.absoluteString == "about:blank" {
            decisionHandler(.allow)
            return
        }
        let target = navigationAction.request.url
        let sameOrigin = target?.scheme == url.scheme
            && target?.host == url.host
            && target?.port == url.port
        if target == nil || sameOrigin {
            decisionHandler(.allow)
        } else {
            openExternally(target)
            decisionHandler(.cancel)
        }
    }

    /// target="_blank" and window.open() have no window to land in here -- hand them to
    /// the default browser instead of silently dropping the click. This is the sink that
    /// actually fires in practice: the page forces target="_blank" on every link it
    /// renders, and a _blank navigation has a nil targetFrame.
    func webView(_ webView: WKWebView,
                 createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        openExternally(navigationAction.request.url)
        return nil
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        showLoadFailure(error)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) {
        showLoadFailure(error)
    }

    /// A failed load would otherwise leave a blank floating window with no back button, no
    /// reload, and a diagnostic buried in a background task's log the user never opens.
    /// Put the reason and the URL on screen instead -- the URL is also the recovery path,
    /// since the server is still waiting and the page can be reopened in any browser.
    private func showLoadFailure(_ error: Error) {
        if showingFailure { return }   // a second failure must not re-enter
        showingFailure = true
        FileHandle.standardError.write("decision host: load failed: \(error.localizedDescription)\n"
                                        .data(using: .utf8)!)
        // Show the UNTAGGED url: our launch url carries the server's per-tier ?_w= probe
        // token, and a human opening that copy would stamp a stale token onto the
        // server's window-proof slot. The bare url is also what the user was told to use.
        var bare = URLComponents(url: url, resolvingAgainstBaseURL: false)
        let kept = bare?.queryItems?.filter { $0.name != "_w" } ?? []
        bare?.queryItems = kept.isEmpty ? nil : kept
        let escaped = (bare?.url?.absoluteString ?? url.absoluteString)
            .replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
        let reason = error.localizedDescription
            .replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
        web.loadHTMLString("""
            <meta name="viewport" content="width=device-width,initial-scale=1">
            <body style="font:15px -apple-system,sans-serif;padding:40px;line-height:1.6;
                         background:#1b1f27;color:#e6e8ef">
              <h2 style="margin:0 0 10px">Could not load the decision page</h2>
              <p style="color:#9aa3b2;margin:0 0 18px">\(reason)</p>
              <p style="margin:0">The server is still waiting. Open this in any browser:</p>
              <p><code style="font-size:14px">\(escaped)</code></p>
            </body>
            """, baseURL: nil)
    }

    // MARK: - Zoom

    /// WKWebView has no zoom affordance of its own. The Chrome tier got cmd-+/- free from
    /// the browser, so making the native window the default tier silently took it away --
    /// a regression, not a simplification. Rebuilt here on `pageZoom`.
    ///
    /// Persisted, because someone who needs larger text needs it on EVERY decision, and
    /// each popup is a fresh process that would otherwise open back at 100%.
    private static let zoomKey = "pageZoom"
    private static let zoomSteps: [CGFloat] = [0.5, 0.67, 0.75, 0.9, 1.0, 1.1, 1.25, 1.5,
                                               1.75, 2.0, 2.5, 3.0]

    private func applyZoom(_ z: CGFloat) {
        web.pageZoom = z
        UserDefaults.standard.set(Double(z), forKey: AppDelegate.zoomKey)
    }

    /// Compared with a tolerance: pageZoom is a CGFloat round-tripped through a plist, so
    /// an exact `>` against a step it is nominally equal to can skip a step or stick.
    @objc func zoomIn(_ sender: Any?) {
        applyZoom(AppDelegate.zoomSteps.first { $0 > web.pageZoom + 0.001 }
                  ?? AppDelegate.zoomSteps.last!)
    }

    @objc func zoomOut(_ sender: Any?) {
        applyZoom(AppDelegate.zoomSteps.last { $0 < web.pageZoom - 0.001 }
                  ?? AppDelegate.zoomSteps.first!)
    }

    @objc func zoomReset(_ sender: Any?) { applyZoom(1.0) }

    // MARK: - Menu

    /// Persisted like `pageZoom`, and for the same reason: a per-popup window preference
    /// the user has to re-set on every decision is not a preference, it is a chore.
    private static let pinKey = "alwaysOnTop"
    static var pinned: Bool { UserDefaults.standard.bool(forKey: pinKey) }   // defaults false

    @objc func toggleAlwaysOnTop(_ sender: NSMenuItem) {
        let nowPinned = window.level != .floating
        window.level = nowPinned ? .floating : .normal
        sender.state = nowPinned ? .on : .off
        UserDefaults.standard.set(nowPinned, forKey: AppDelegate.pinKey)
    }

    /// A programmatic menu bar, because an app with no main menu loses the standard Edit
    /// responders -- and cmd-C / cmd-V in the reasoning-notes textarea would stop working.
    /// String selectors for Edit: `copy:` etc. resolve on the first responder at runtime,
    /// while #selector(NSText.copy(_:)) collides with NSObject.copy().
    private func buildMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Hide \(AppDelegate.appName)",
                        action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "Quit \(AppDelegate.appName)",
                        action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)

        let editItem = NSMenuItem()
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        let redo = edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        edit.addItem(NSMenuItem.separator())
        edit.addItem(withTitle: "Cut", action: Selector(("cut:")), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: Selector(("copy:")), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: Selector(("paste:")), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: Selector(("selectAll:")), keyEquivalent: "a")
        editItem.submenu = edit
        main.addItem(editItem)

        let viewItem = NSMenuItem()
        let view = NSMenu(title: "View")
        for (title, key, sel) in [("Zoom In", "+", #selector(zoomIn(_:))),
                                  ("Zoom Out", "-", #selector(zoomOut(_:))),
                                  ("Actual Size", "0", #selector(zoomReset(_:)))] {
            let it = view.addItem(withTitle: title, action: sel, keyEquivalent: key)
            it.target = self   // the action is ours, not a responder-chain method
        }
        // cmd-= is the chord people actually press for "zoom in", but a key equivalent of
        // "+" only matches the shifted character. A hidden duplicate is the Cocoa idiom
        // for a second chord on one command: performKeyEquivalent still dispatches it,
        // so the menu shows one entry and both chords work.
        let alias = NSMenuItem(title: "Zoom In", action: #selector(zoomIn(_:)),
                               keyEquivalent: "=")
        alias.target = self
        alias.isHidden = true
        view.addItem(alias)
        viewItem.submenu = view
        main.addItem(viewItem)

        let winItem = NSMenuItem()
        let win = NSMenu(title: "Window")
        win.addItem(withTitle: "Minimize",
                    action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        win.addItem(withTitle: "Close",
                    action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        win.addItem(NSMenuItem.separator())
        let pin = NSMenuItem(title: "Always on Top",
                             action: #selector(toggleAlwaysOnTop(_:)), keyEquivalent: "t")
        pin.keyEquivalentModifierMask = [.command, .shift]
        pin.target = self
        pin.state = AppDelegate.pinned ? .on : .off      // must mirror the window's actual level
        win.addItem(pin)
        winItem.submenu = win
        main.addItem(winItem)

        NSApp.mainMenu = main
        NSApp.windowsMenu = win
    }

    // MARK: - Icon

    /// The bundle's own name, which is what every user-visible label must be built from:
    /// one source can serve several apps, so a hardcoded string ships the WRONG app's name
    /// in the other one's menu bar.
    static let appName = Bundle.main.object(forInfoDictionaryKey: "CFBundleName")
        as? String ?? "Rich Decision"

    /// `Contents/Resources/AppIcon.png` when the bundle carries one, else the default
    /// drawing. That file is the extension point for another tool built from this source:
    /// it passes the PNG through native_host's `resources`, and two tiles drawing the same
    /// glyph -- which at Dock size read as one app -- is the failure it avoids. Read at
    /// runtime rather than compiled in, so this file names no other app.
    private static func appIcon() -> NSImage {
        if let url = Bundle.main.url(forResource: "AppIcon", withExtension: "png"),
           let img = NSImage(contentsOf: url) {
            return img
        }
        return decisionIcon()
    }

    /// Drawn in code rather than shipped as an .icns: it keeps the host to one source file
    /// with nothing binary to regenerate, and the icon only has to be recognisably
    /// NOT-Chrome at Dock and cmd-Tab size.
    private static func decisionIcon() -> NSImage {
        let side: CGFloat = 512
        return NSImage(size: NSSize(width: side, height: side), flipped: false) { rect in
            let body = rect.insetBy(dx: side * 0.05, dy: side * 0.05)
            let path = NSBezierPath(roundedRect: body,
                                    xRadius: side * 0.225, yRadius: side * 0.225)
            let grad = NSGradient(colors: [
                NSColor(srgbRed: 0.87, green: 0.50, blue: 0.36, alpha: 1),
                NSColor(srgbRed: 0.72, green: 0.25, blue: 0.05, alpha: 1),
            ])
            grad?.draw(in: path, angle: -65)

            let glyph = "✳" as NSString
            let attrs: [NSAttributedString.Key: Any] = [
                .font: NSFont.systemFont(ofSize: side * 0.6, weight: .bold),
                .foregroundColor: NSColor.white,
            ]
            let size = glyph.size(withAttributes: attrs)
            glyph.draw(at: NSPoint(x: rect.midX - size.width / 2,
                                   y: rect.midY - size.height / 2),
                       withAttributes: attrs)
            return true
        }
    }
}

let arguments = CommandLine.arguments
guard arguments.count > 1, let target = URL(string: arguments[1]) else {
    FileHandle.standardError.write("usage: RichDecision <url>\n".data(using: .utf8)!)
    exit(2)
}

let application = NSApplication.shared
// Held by a global so the weak NSApplication.delegate does not drop it.
let appDelegate = AppDelegate(url: target)
application.delegate = appDelegate
application.setActivationPolicy(.regular)
// The callers end this process with SIGTERM, whose default action kills it without telling
// LaunchServices: the app stays registered as "exited-with-subordinates" and the Dock keeps
// a blank "Running in Background" tile. So the signal asks the main thread for an ordinary
// NSApp.terminate instead. The handler runs off main so a hung main thread cannot swallow
// it -- SIGTERM must still end the process, so it falls back to a hard exit, inside
// decision_server's 5s wait before its SIGKILL.
signal(SIGTERM, SIG_IGN)
let sigtermSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .global())
sigtermSource.setEventHandler {
    DispatchQueue.main.async { NSApp.terminate(nil) }
    Thread.sleep(forTimeInterval: 3)
    _exit(143)
}
sigtermSource.resume()
application.run()
