// Native shell around memwatch.py: starts the server on launch, restarts it if it dies, stops it on quit.
// Runs as a normal Dock app or as a menu bar app (Settings).
import Cocoa
import ServiceManagement
import WebKit

let logURL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/Memwatch.log")
let defaults = UserDefaults.standard
enum Key {
    static let menuBar = "menuBarMode", showUsage = "showUsageInMenuBar", refresh = "refreshSeconds"
}

// Explicit drag region over the blank top bar. Needed because the webview sits under the transparent titlebar
// and swallows its clicks. Only this strip drags; the page header under it holds no controls, and the traffic
// lights live in the titlebar layer above it, so they keep working.
final class DragStrip: NSView {
    weak var web: WKWebView?
    override func mouseDown(with e: NSEvent) {
        guard e.clickCount == 2 else { return window?.performDrag(with: e) ?? () }
        // honor System Settings > Desktop & Dock > "Double-click a window's title bar to"
        switch defaults.string(forKey: "AppleActionOnDoubleClick") {
        case "Minimize": window?.performMiniaturize(nil)
        case "None": break
        default: window?.performZoom(nil)
        }
    }
    override func scrollWheel(with e: NSEvent) { web?.scrollWheel(with: e) }  // scrolling over the bar still scrolls
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKScriptMessageHandler {
    var window: NSWindow!
    var strip: DragStrip!
    var web: WKWebView!
    var statusItem: NSStatusItem?
    let popover = NSPopover()
    var settings: NSWindow?
    var usageBox: NSButton!
    var lastUsed: Double?
    var usageTimer: Timer?
    var server: Process?
    var port: String?
    var quitting = false
    var crashes = 0
    var startedAt = Date()
    var menuBarMode: Bool { defaults.bool(forKey: Key.menuBar) }

    func applicationDidFinishLaunching(_ n: Notification) {
        defaults.register(defaults: [Key.showUsage: true, Key.refresh: 3])
        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "mem")  // page -> native: Settings / Quit buttons
        web = WKWebView(frame: .zero, configuration: config)
        web.setValue(false, forKey: "drawsBackground")  // no white flash before the page paints in dark mode
        buildMenu()
        buildWindow()
        let vc = NSViewController()
        vc.view = NSView(frame: NSRect(x: 0, y: 0, width: 520, height: 660))
        popover.contentViewController = vc
        popover.behavior = .transient
        applyMode()
        startServer()
    }

    func buildWindow() {
        // Native chrome kept (traffic lights, resize, fullscreen); title text hidden; page runs under the titlebar.
        // The empty unified toolbar makes the top strip taller with the traffic lights centered in it.
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1040, height: 780),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        window.title = "Memwatch"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.toolbar = NSToolbar()
        window.toolbarStyle = .unified
        window.titlebarSeparatorStyle = .none
        window.minSize = NSSize(width: 420, height: 320)
        window.isReleasedWhenClosed = false
        window.delegate = self
        let root = NSView()
        window.contentView = root
        let tb = window.frame.height - window.contentLayoutRect.height
        strip = DragStrip(frame: NSRect(x: 0, y: root.bounds.height - tb, width: root.bounds.width, height: tb))
        strip.autoresizingMask = [.width, .minYMargin]  // stays pinned to the top on resize
        strip.web = web
        root.addSubview(strip)
        window.center()
        window.setFrameAutosaveName("main")
    }

    // One webview, moved between the window and the menu bar popover.
    func mount(in view: NSView, below: NSView? = nil) {
        web.removeFromSuperview()
        web.frame = view.bounds
        web.autoresizingMask = [.width, .height]
        view.addSubview(web, positioned: .below, relativeTo: below)
    }

    func applyMode() {
        if menuBarMode {
            if statusItem == nil {
                let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
                item.button?.image = NSImage(systemSymbolName: "memorychip", accessibilityDescription: "Memwatch")
                item.button?.imagePosition = .imageLeading
                item.button?.font = .monospacedDigitSystemFont(ofSize: NSFont.systemFontSize, weight: .regular)
                item.button?.target = self
                item.button?.action = #selector(statusClicked)
                item.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])
                statusItem = item
            }
            window.orderOut(nil)
            mount(in: popover.contentViewController!.view)
            NSApp.setActivationPolicy(.accessory)
        } else {
            popover.performClose(nil)
            if let s = statusItem { NSStatusBar.system.removeStatusItem(s) }
            statusItem = nil
            mount(in: window.contentView!, below: strip)
            NSApp.setActivationPolicy(.regular)
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        }
        updateStatusTitle()
        startUsageTimer()
        loadPage()
    }

    // The menu bar number is fetched natively from /sys (cheap: no top). The page can't feed it: macOS
    // suspends timers in a webview that isn't on screen, which is the popover's normal state.
    func startUsageTimer() {
        usageTimer?.invalidate()
        usageTimer = nil
        guard menuBarMode else { return }
        usageTimer = Timer.scheduledTimer(withTimeInterval: Double(defaults.integer(forKey: Key.refresh)), repeats: true) { [weak self] _ in
            self?.fetchUsage()
        }
        fetchUsage()
    }

    func fetchUsage() {
        guard let port, defaults.bool(forKey: Key.showUsage), let url = URL(string: "http://127.0.0.1:\(port)/sys") else { return }
        URLSession.shared.dataTask(with: url) { [weak self] data, _, _ in
            guard let data, let j = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let used = j["used"] as? Double else { return }
            DispatchQueue.main.async {
                self?.lastUsed = used
                self?.updateStatusTitle()
            }
        }.resume()
    }

    @objc func statusClicked() {
        guard let item = statusItem, let button = item.button else { return }
        if NSApp.currentEvent?.type == .rightMouseUp {
            let m = NSMenu()
            m.addItem(withTitle: "Open Memwatch", action: #selector(togglePopover), keyEquivalent: "").target = self
            m.addItem(withTitle: "Settings…", action: #selector(openSettings), keyEquivalent: ",").target = self
            m.addItem(.separator())
            m.addItem(withTitle: "Quit Memwatch", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
            item.menu = m
            button.performClick(nil)  // shows the menu; cleared after so left click keeps opening the popover
            item.menu = nil
        } else {
            togglePopover()
        }
    }

    @objc func togglePopover() {
        guard let button = statusItem?.button else { return }
        if popover.isShown { return popover.performClose(nil) }
        NSApp.activate(ignoringOtherApps: true)
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        popover.contentViewController?.view.window?.makeKey()
        web.evaluateJavaScript("window.refreshNow && refreshNow()")  // timers were paused while hidden
    }

    func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) {
        switch (m.body as? [String: Any])?["action"] as? String {
        case "settings": popover.performClose(nil); openSettings()
        case "quit": NSApp.terminate(nil)
        default: break
        }
    }

    func updateStatusTitle() {
        guard let button = statusItem?.button else { return }
        if defaults.bool(forKey: Key.showUsage), let u = lastUsed {
            button.title = String(format: " %.1f GB", u / 1_073_741_824)
        } else {
            button.title = ""
        }
    }

    // MARK: Settings

    @objc func openSettings() {
        if settings == nil {
            let w = NSWindow(contentRect: .zero, styleMask: [.titled, .closable], backing: .buffered, defer: false)
            w.title = "Memwatch Settings"
            w.isReleasedWhenClosed = false
            let menuBar = NSButton(checkboxWithTitle: "Run in the menu bar instead of the Dock", target: self, action: #selector(setMenuBar(_:)))
            menuBar.state = menuBarMode ? .on : .off
            usageBox = NSButton(checkboxWithTitle: "Show memory used next to the menu bar icon", target: self, action: #selector(setUsage(_:)))
            usageBox.state = defaults.bool(forKey: Key.showUsage) ? .on : .off
            let login = NSButton(checkboxWithTitle: "Open at login", target: self, action: #selector(setLogin(_:)))
            login.state = SMAppService.mainApp.status == .enabled ? .on : .off
            let refresh = NSPopUpButton()
            for s in [1, 3, 5, 10] {
                refresh.addItem(withTitle: s == 1 ? "1 second" : "\(s) seconds")
                refresh.lastItem?.tag = s
            }
            refresh.selectItem(withTag: defaults.integer(forKey: Key.refresh))
            refresh.target = self
            refresh.action = #selector(setRefresh(_:))
            let row = NSStackView(views: [NSTextField(labelWithString: "Refresh every"), refresh])
            let stack = NSStackView(views: [menuBar, usageBox, login, row])
            stack.orientation = .vertical
            stack.alignment = .leading
            stack.spacing = 12
            stack.edgeInsets = NSEdgeInsets(top: 20, left: 24, bottom: 24, right: 24)
            w.contentView = stack
            w.setContentSize(stack.fittingSize)
            w.center()
            settings = w
        }
        usageBox.isEnabled = menuBarMode
        NSApp.activate(ignoringOtherApps: true)
        settings?.makeKeyAndOrderFront(nil)
    }

    @objc func setMenuBar(_ b: NSButton) {
        defaults.set(b.state == .on, forKey: Key.menuBar)
        usageBox.isEnabled = menuBarMode
        applyMode()
        NSApp.activate(ignoringOtherApps: true)
        settings?.makeKeyAndOrderFront(nil)  // keep Settings in front across the mode switch
    }

    @objc func setUsage(_ b: NSButton) {
        defaults.set(b.state == .on, forKey: Key.showUsage)
        updateStatusTitle()
        fetchUsage()
    }

    @objc func setLogin(_ b: NSButton) {
        do {
            if b.state == .on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
        } catch {
            b.state = b.state == .on ? .off : .on
            alert("Couldn't change Open at login: \(error.localizedDescription)")
        }
    }

    @objc func setRefresh(_ p: NSPopUpButton) {
        defaults.set(p.selectedTag(), forKey: Key.refresh)
        startUsageTimer()
        loadPage()
    }

    // MARK: Server

    func startServer() {
        // ponytail: brew python first; /usr/bin/python3 is a stub that prompts for Xcode CLT if missing
        guard let script = Bundle.main.path(forResource: "memwatch", ofType: "py"),
              let python = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]
                  .first(where: FileManager.default.isExecutableFile) else {
            return fail("Python 3 not found. Install it with: xcode-select --install")
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = ["-I", script, "--app"]
        // server exits on stdin EOF, so it dies with us even if we crash or get force-quit
        p.standardInput = Pipe()
        let out = Pipe()
        p.standardOutput = out
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        p.standardError = try? FileHandle(forWritingTo: logURL)

        port = nil
        out.fileHandleForReading.readabilityHandler = { [weak self] h in
            let d = h.availableData
            guard !d.isEmpty else { h.readabilityHandler = nil; return }
            guard let s = String(data: d, encoding: .utf8),
                  let r = s.range(of: #"PORT \d+"#, options: .regularExpression) else { return }
            let port = String(s[r].dropFirst(5))
            DispatchQueue.main.async {
                self?.port = port
                self?.loadPage()
                self?.fetchUsage()
            }
        }
        p.terminationHandler = { [weak self] _ in DispatchQueue.main.async { self?.serverDied() } }
        do { try p.run() } catch { return fail("Couldn't start the server: \(error.localizedDescription)") }
        server = p
        startedAt = Date()

        DispatchQueue.main.asyncAfter(deadline: .now() + 15) { [weak self] in
            guard let self, self.server === p, self.port == nil, !self.quitting else { return }
            self.fail("The server didn't start. Details: ~/Library/Logs/Memwatch.log")
        }
    }

    func serverDied() {
        guard !quitting else { return }
        if Date().timeIntervalSince(startedAt) > 60 { crashes = 0 }
        crashes += 1
        if crashes > 3 { return fail("The server keeps crashing. Details: ~/Library/Logs/Memwatch.log") }
        startServer()
    }

    // The page learns its layout from the URL: titlebar metrics (tb = strip height, tl = where the traffic
    // lights end, read from AppKit), fullscreen, menu bar mode and refresh interval.
    func loadPage() {
        guard let port else { return }
        var q = "r=\(defaults.integer(forKey: Key.refresh))"
        if menuBarMode {
            q += "&menubar&tb=44"
        } else {
            let tb = Int(window.frame.height - window.contentLayoutRect.height)
            let tl = Int((window.standardWindowButton(.zoomButton)?.frame.maxX ?? 70) + 14)
            q += "&tb=\(tb)&tl=\(tl)" + (window.styleMask.contains(.fullScreen) ? "&fs" : "")
        }
        web.load(URLRequest(url: URL(string: "http://127.0.0.1:\(port)/?\(q)")!))
    }

    @objc func reload() { loadPage() }

    // no traffic lights in fullscreen, so the page drops their inset
    func windowWillEnterFullScreen(_ n: Notification) { setFullscreen(true) }
    func windowWillExitFullScreen(_ n: Notification) { setFullscreen(false) }
    func setFullscreen(_ on: Bool) {
        web.evaluateJavaScript("document.documentElement.classList.toggle('fs', \(on))")
    }

    func alert(_ msg: String) {
        let a = NSAlert()
        a.messageText = "Memwatch"
        a.informativeText = msg
        a.runModal()
    }

    func fail(_ msg: String) {
        quitting = true
        alert(msg)
        NSApp.terminate(nil)
    }

    func applicationWillTerminate(_ n: Notification) {
        quitting = true
        server?.terminate()
    }

    // Dock mode quits with its window; menu bar mode lives in the status item.
    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { !menuBarMode }

    func applicationShouldHandleReopen(_ app: NSApplication, hasVisibleWindows: Bool) -> Bool {
        if menuBarMode { togglePopover() } else { window.makeKeyAndOrderFront(nil) }
        return false
    }

    func buildMenu() {
        let main = NSMenu()
        func sub(_ title: String, _ items: [NSMenuItem]) {
            let m = NSMenu(title: title)
            items.forEach(m.addItem)
            let i = NSMenuItem()
            i.submenu = m
            main.addItem(i)
        }
        func item(_ title: String, _ action: Selector, _ key: String, target: AnyObject? = nil) -> NSMenuItem {
            let i = NSMenuItem(title: title, action: action, keyEquivalent: key)
            i.target = target
            return i
        }
        sub("Memwatch", [
            item("About Memwatch", #selector(NSApplication.orderFrontStandardAboutPanel(_:)), ""),
            .separator(),
            item("Settings…", #selector(openSettings), ",", target: self),
            .separator(),
            item("Hide Memwatch", #selector(NSApplication.hide(_:)), "h"),
            item("Quit Memwatch", #selector(NSApplication.terminate(_:)), "q"),
        ])
        sub("Edit", [
            item("Cut", #selector(NSText.cut(_:)), "x"),
            item("Copy", #selector(NSText.copy(_:)), "c"),
            item("Paste", #selector(NSText.paste(_:)), "v"),
            item("Select All", #selector(NSText.selectAll(_:)), "a"),
        ])
        sub("View", [item("Reload", #selector(reload), "r", target: self)])
        sub("Window", [
            item("Minimize", #selector(NSWindow.performMiniaturize(_:)), "m"),
            item("Close", #selector(NSWindow.performClose(_:)), "w"),
        ])
        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
