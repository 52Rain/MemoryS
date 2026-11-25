import AppKit
import WebKit
import Security
import CryptoKit

struct AppFault: LocalizedError {
    let message: String
    var errorDescription: String? { message }
    init(_ message: String) { self.message = message }
}

struct Keys {
    static let service = "dev.memorys.desktop.model"
    static func account(_ args: [String:Any]) throws -> String {
        guard let raw = args["url"] as? String,
              let url = URLComponents(string: raw.trimmingCharacters(in: .whitespacesAndNewlines)),
              let host = url.host, !host.isEmpty else {
            throw AppFault("Please enter a valid API base URL.")
        }
        let address = (url.scheme ?? "") + "://" + host.lowercased() + (url.port.map { ":\($0)" } ?? "") + "/" + url.path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        return SHA256.hash(data: Data(address.utf8)).map { String(format: "%02x", $0) }.joined()
    }
    static func lookup(_ account: String) -> [String:Any] {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service, kSecAttrAccount as String: account]
    }
    static func read(_ account: String) throws -> String {
        var q = lookup(account); q[kSecReturnData as String] = true
        var result: CFTypeRef?
        let rc = SecItemCopyMatching(q as CFDictionary, &result)
        if rc == errSecItemNotFound { return "" }
        guard rc == errSecSuccess, let data = result as? Data else {
            throw AppFault("Failed to read Keychain credential.")
        }
        return String(decoding: data, as: UTF8.self)
    }
    static func has(_ account: String) -> Bool {
        SecItemCopyMatching(lookup(account) as CFDictionary, nil) == errSecSuccess
    }
    static func save(_ key: String, _ account: String) throws {
        let q = lookup(account); let data = [kSecValueData as String: Data(key.utf8)]
        var rc = SecItemUpdate(q as CFDictionary, data as CFDictionary)
        if rc == errSecItemNotFound { rc = SecItemAdd(q.merging(data) { _, b in b } as CFDictionary, nil) }
        if rc != errSecSuccess { throw AppFault("Failed to save API Key to macOS Keychain.") }
    }
}

final class Host: NSObject, NSApplicationDelegate, WKScriptMessageHandlerWithReply, WKNavigationDelegate {
    var window: NSWindow!
    var view: WKWebView!
    let queue = DispatchQueue(label: "dev.memorys.desktop.worker", qos: .userInitiated)
    var trusted: URL { Bundle.main.resourceURL!.appendingPathComponent("ui/index.html").standardizedFileURL }

    func applicationDidFinishLaunching(_ note: Notification) {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()
        config.userContentController.addScriptMessageHandler(self, contentWorld: .page, name: "memorys")
        view = WKWebView(frame: .zero, configuration: config)
        view.navigationDelegate = self
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 820),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "MemoryS"
        window.minSize = NSSize(width: 760, height: 560)
        window.setFrameAutosaveName("MemorySWorkspaceOSS")
        window.contentView = view

        let menu = NSMenu()
        let item = NSMenuItem(); menu.addItem(item)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Quit MemoryS", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        item.submenu = appMenu

        let edit = NSMenuItem(); menu.addItem(edit)
        let edits = NSMenu(title: "Edit")
        for (label, selector, key) in [
            ("Undo", "undo:", "z"), ("Cut", "cut:", "x"),
            ("Copy", "copy:", "c"), ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")
        ] {
            edits.addItem(withTitle: label, action: Selector(selector), keyEquivalent: key)
        }
        edit.submenu = edits
        NSApp.mainMenu = menu

        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        view.loadFileURL(trusted, allowingReadAccessTo: trusted.deletingLastPathComponent())
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        let url = navigationAction.request.url
        if navigationAction.navigationType == .linkActivated,
           let target = url,
           let scheme = target.scheme?.lowercased(),
           scheme == "https" || scheme == "http" {
            NSWorkspace.shared.open(target)
            decisionHandler(.cancel)
            return
        }
        let allowed = url?.isFileURL == true && url?.standardizedFileURL.path == trusted.path
        decisionHandler(allowed ? .allow : .cancel)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func run(_ action: String, _ args: [String:Any], _ key: String = "") throws -> Any {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        let base = Bundle.main.resourceURL!
        proc.arguments = ["python3", "-B", base.appendingPathComponent("service/backend.py").path]
        var env = ProcessInfo.processInfo.environment
        let pathPrefix = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        env["PATH"] = pathPrefix + (env["PATH"].map { ":" + $0 } ?? "")
        proc.environment = env

        let input = Pipe(), output = Pipe()
        proc.standardInput = input
        proc.standardOutput = output
        proc.standardError = FileHandle.nullDevice
        try proc.run()

        let body = try JSONSerialization.data(withJSONObject: ["action": action, "args": args, "key": key])
        try input.fileHandleForWriting.write(contentsOf: body)
        try input.fileHandleForWriting.close()
        let result = output.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()

        guard let reply = (try? JSONSerialization.jsonObject(with: result)) as? [String:Any],
              reply["ok"] as? Bool == true else {
            let reply = (try? JSONSerialization.jsonObject(with: result)) as? [String:Any]
            throw AppFault(reply?["error"] as? String ?? "Local MemoryS service failed to respond.")
        }
        return reply["data"] ?? NSNull()
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage, replyHandler: @escaping (Any?, String?) -> Void) {
        guard message.frameInfo.isMainFrame,
              message.frameInfo.request.url?.standardizedFileURL.path == trusted.path,
              let obj = message.body as? [String:Any],
              let action = obj["action"] as? String else {
            replyHandler(nil, "Invalid local request")
            return
        }
        let args = obj["args"] as? [String:Any] ?? [:]
        let actions = [
            "snapshot", "setVaultPath", "test", "saveConfig", "createProposal",
            "editProposal", "reviewProposal", "ignoreProposal", "publish",
            "archiveCard", "saveProjectMeta", "pickDirectory", "openExternal"
        ]
        guard actions.contains(action) else { replyHandler(nil, "Unknown action"); return }

        if action == "pickDirectory" {
            let dialog = NSOpenPanel()
            dialog.canChooseDirectories = true
            dialog.canChooseFiles = false
            dialog.canCreateDirectories = true
            dialog.prompt = (args["prompt"] as? String) ?? "Select Directory"
            if dialog.runModal() != .OK { replyHandler(["cancelled": true], nil); return }
            replyHandler(["path": dialog.url!.path], nil)
            return
        }
        if action == "openExternal" {
            guard let path = args["path"] as? String, !path.isEmpty else {
                replyHandler(nil, "Missing file path"); return
            }
            let mode = (args["mode"] as? String) ?? "obsidian"
            let fileURL = URL(fileURLWithPath: path)
            if mode == "finder" {
                NSWorkspace.shared.activateFileViewerSelecting([fileURL])
                replyHandler(["ok": true], nil); return
            }
            var comps = URLComponents()
            comps.scheme = "obsidian"
            comps.host = "open"
            comps.queryItems = [URLQueryItem(name: "path", value: path)]
            let opened = comps.url.map { NSWorkspace.shared.open($0) } ?? false
            if !opened { NSWorkspace.shared.open(fileURL) }
            replyHandler(["ok": true], nil); return
        }
        if action == "publish" {
            let n = (args["ids"] as? [String])?.count ?? 0
            let alert = NSAlert()
            alert.messageText = "Publish \(n) memory item(s)?"
            alert.informativeText = "Confirmed cards will be written to your MemoryS vault and committed to Git."
            alert.addButton(withTitle: "Publish")
            alert.addButton(withTitle: "Cancel")
            if alert.runModal() != .alertFirstButtonReturn {
                replyHandler(["cancelled": true], nil); return
            }
        }

        let input = args
        queue.async {
            do {
                var work = input
                let entered = work.removeValue(forKey: "key") as? String ?? ""
                var out: Any
                switch action {
                case "saveConfig", "test":
                    let account = try Keys.account(work)
                    _ = try self.run("validateConfig", work)
                    if action == "saveConfig" {
                        if !entered.isEmpty { try Keys.save(entered, account) }
                        out = try self.run(action, work)
                    } else {
                        out = try self.run(action, work, entered.isEmpty ? Keys.read(account) : entered)
                    }
                case "reviewProposal":
                    let c = try self.run("config", [:]) as! [String:Any]
                    out = try self.run(action, work, Keys.read(Keys.account(c)))
                case "snapshot":
                    var snap = try self.run(action, work) as! [String:Any]
                    if let c = snap["config"] as? [String:Any] {
                        snap["hasKey"] = Keys.has(try Keys.account(c))
                    }
                    out = snap
                default:
                    out = try self.run(action, work)
                }
                DispatchQueue.main.async { replyHandler(out, nil) }
            } catch {
                DispatchQueue.main.async { replyHandler(nil, error.localizedDescription) }
            }
        }
    }
}

let app = NSApplication.shared
let host = Host()
app.delegate = host
app.setActivationPolicy(.regular)
app.run()
