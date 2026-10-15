import AppKit
import Foundation

enum ResultLayout: String {
    case phrase
    case single
    case paragraphs
    case multi
}

final class ResultWindowDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private let horizontalTextInset: CGFloat = 22
    private let verticalTextInset: CGFloat = 20
    private let lineFragmentPadding: CGFloat = 5
    private let layoutSlack: CGFloat = 8
    private let translationTextColor = NSColor(name: nil) { appearance in
        appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            ? .white : .textColor
    }
    private var mode: String
    private var payloadPath: String
    private var resultLayout: ResultLayout
    private let statusPath: String
    private let streamPath: String?
    private var streamReader: FileHandle?
    private var streamSource: DispatchSourceFileSystemObject?
    private var streamBuffer = Data()
    private var hasActivated = false
    private var window: NSWindow?
    private var statusTimer: Timer?
    private weak var progressLabel: NSTextField?
    private weak var progressIndicator: NSProgressIndicator?
    private weak var translationView: NSTextView?
    private var hasReceivedTranslation = false
    private var isFittingHeight = false

    init(mode: String, payloadPath: String, resultLayout: ResultLayout, streamPath: String? = nil) {
        self.mode = mode
        self.payloadPath = payloadPath
        self.statusPath = payloadPath
        self.streamPath = streamPath
        self.resultLayout = resultLayout
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        configureMainMenu()
        if mode == "loading" {
            showLoadingWindow()
        } else {
            finishTranslation()
        }
    }

    private func makeWindow(frame: NSRect, styleMask: NSWindow.StyleMask) -> NSWindow {
        if let window = self.window { return window }
        let window = NSWindow(
            contentRect: frame,
            styleMask: styleMask,
            backing: .buffered,
            defer: false
        )
        window.center()
        window.delegate = self
        window.isReleasedWhenClosed = false
        window.level = .normal
        window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        self.window = window
        return window
    }

    private func prepareSurface(frame: NSRect = NSRect(x: 0, y: 0, width: 560, height: 68)) {
        guard window == nil else { return }
        let window = makeWindow(
            frame: frame,
            styleMask: [.titled, .closable, .miniaturizable, .resizable]
        )
        window.title = "翻译"
        window.contentMinSize = NSSize(width: 320, height: 68)
        let contentView = NSView(frame: frame)
        let label = NSTextField(labelWithString: "正在启动翻译…")
        label.frame = NSRect(x: 22, y: frame.height - 34, width: frame.width - 44, height: 24)
        label.alignment = .center
        label.font = NSFont.systemFont(ofSize: 13)
        label.lineBreakMode = .byTruncatingTail
        label.autoresizingMask = [.width, .minYMargin]
        let indicator = NSProgressIndicator(
            frame: NSRect(x: 22, y: frame.height - 45, width: frame.width - 44, height: 8)
        )
        indicator.isIndeterminate = false
        indicator.minValue = 0
        indicator.maxValue = 100
        indicator.doubleValue = 2
        indicator.autoresizingMask = [.width, .minYMargin]
        contentView.addSubview(label)
        contentView.addSubview(indicator)
        progressLabel = label
        progressIndicator = indicator

        let scrollFrame = NSRect(x: 0, y: 0, width: frame.width, height: frame.height - 56)
        let scrollView = NSScrollView(frame: scrollFrame)
        scrollView.hasVerticalScroller = true
        scrollView.autohidesScrollers = true
        scrollView.scrollerStyle = .overlay
        scrollView.autoresizingMask = [.width, .height]
        scrollView.isHidden = true
        let textView = NSTextView(frame: scrollFrame)
        textView.isEditable = false
        textView.isSelectable = true
        textView.isRichText = false
        textView.usesFindPanel = true
        textView.isAutomaticQuoteSubstitutionEnabled = false
        textView.isAutomaticDashSubstitutionEnabled = false
        textView.font = NSFont.systemFont(ofSize: 16)
        textView.textColor = translationTextColor
        textView.textContainerInset = NSSize(width: horizontalTextInset, height: verticalTextInset)
        textView.minSize = NSSize(width: 0, height: scrollFrame.height)
        textView.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        textView.isVerticallyResizable = true
        textView.isHorizontallyResizable = false
        textView.textContainer?.containerSize = NSSize(width: frame.width, height: CGFloat.greatestFiniteMagnitude)
        textView.textContainer?.widthTracksTextView = true
        textView.textContainer?.lineFragmentPadding = lineFragmentPadding
        textView.autoresizingMask = [.width]
        scrollView.documentView = textView
        contentView.addSubview(scrollView)
        window.contentView = contentView
        translationView = textView
    }

    private func showLoadingWindow() {
        prepareSurface()
        statusTimer = Timer.scheduledTimer(
            timeInterval: 0.05,
            target: self,
            selector: #selector(pollStatus),
            userInfo: nil,
            repeats: true
        )
        // Keep the source app focused until selection/copy capture finishes.
        window?.orderFrontRegardless()
        pollStatus()
        startStreamUpdates()
    }

    private func applyMode(_ label: String) {
        window?.title = label
        if label.hasPrefix("词组") {
            resultLayout = .phrase
        } else if label.hasPrefix("单段落") {
            resultLayout = .single
        } else if label.hasPrefix("多段落") {
            resultLayout = label.contains("保留原段落") ? .paragraphs : .multi
        }
        configureTextLayout(wrapsLines: resultLayout != .multi)
    }

    private func configureTextLayout(wrapsLines: Bool) {
        guard let textView = translationView,
              textView.textContainer?.widthTracksTextView != wrapsLines else { return }
        textView.enclosingScrollView?.hasHorizontalScroller = !wrapsLines
        textView.isHorizontallyResizable = !wrapsLines
        textView.textContainer?.widthTracksTextView = wrapsLines
        textView.textContainer?.containerSize = NSSize(
            width: wrapsLines ? (textView.enclosingScrollView?.bounds.width ?? 560) : CGFloat.greatestFiniteMagnitude,
            height: CGFloat.greatestFiniteMagnitude
        )
    }

    private func startStreamUpdates() {
        guard mode == "loading", let streamPath,
              let reader = try? FileHandle(forReadingFrom: URL(fileURLWithPath: streamPath)) else {
            return
        }
        streamReader = reader
        let source = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: reader.fileDescriptor, eventMask: .write, queue: .main
        )
        source.setEventHandler { [weak self] in self?.consumeStreamUpdates() }
        source.setCancelHandler { try? reader.close() }
        streamSource = source
        source.resume()
        consumeStreamUpdates()
    }

    private func consumeStreamUpdates() {
        guard let streamReader else { return }
        streamBuffer.append(streamReader.availableData)
        let newline = Data([10])
        while let boundary = streamBuffer.range(of: newline) {
            let line = streamBuffer.subdata(in: 0..<boundary.lowerBound)
            streamBuffer.removeSubrange(0..<boundary.upperBound)
            guard let payload = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
                  let translation = payload["translation"] as? String else { continue }
            if let mode = payload["mode"] as? String {
                applyMode(mode)
            }
            if let message = payload["message"] as? String {
                progressLabel?.stringValue = message
            }
            if !hasReceivedTranslation { progressIndicator?.doubleValue = 60 }
            updateStreamingTranslation(translation)
            window?.displayIfNeeded()
        }
    }

    @objc private func pollStatus() {
        let url = URL(fileURLWithPath: statusPath)
        if !FileManager.default.fileExists(atPath: statusPath) {
            statusTimer?.invalidate()
            streamSource?.cancel()
            NSApp.terminate(nil)
            return
        }
        guard
            let data = try? Data(contentsOf: url),
            !data.isEmpty,
            let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            return
        }
        if let completed = payload["completed"] as? String,
           ["result", "error"].contains(completed),
           let path = payload["path"] as? String {
            statusTimer?.invalidate()
            consumeStreamUpdates()
            streamSource?.cancel()
            mode = completed
            payloadPath = path
            resultLayout = ResultLayout(rawValue: payload["layout"] as? String ?? "multi") ?? .multi
            try? FileManager.default.removeItem(at: url)
            finishTranslation()
            return
        }
        if !hasActivated && payload["activate"] as? Bool != false {
            hasActivated = true
            window?.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        }
        if let mode = payload["mode"] as? String {
            applyMode(mode)
        }
        if let message = payload["message"] as? String {
            progressLabel?.stringValue = message
        }
        if !hasReceivedTranslation, let percent = payload["percent"] as? NSNumber {
            progressIndicator?.doubleValue = percent.doubleValue
        }
        if let translation = payload["translation"] as? String {
            updateStreamingTranslation(translation)
        }
    }

    private func updateStreamingTranslation(_ translation: String, followStream: Bool = true) {
        guard let textView = translationView, textView.string != translation else { return }
        if !translation.isEmpty && !hasReceivedTranslation {
            hasReceivedTranslation = true
            progressIndicator?.doubleValue = 100
            progressLabel?.isHidden = true
            progressIndicator?.isHidden = true
            if let bounds = window?.contentView?.bounds,
               let scrollView = textView.enclosingScrollView {
                scrollView.isHidden = false
                scrollView.frame = bounds
                textView.minSize = NSSize(width: 0, height: bounds.height)
                textView.setFrameSize(NSSize(
                    width: bounds.width,
                    height: max(textView.frame.height, bounds.height)
                ))
            }
        }
        let current = textView.string
        let selection = textView.selectedRange()
        let scrollOrigin = textView.enclosingScrollView?.contentView.bounds.origin
        let scrollView = textView.enclosingScrollView
        let visibleBottom = scrollView?.contentView.bounds.maxY ?? 0
        let followsEnd = visibleBottom >= textView.bounds.maxY - 30
            && textView.selectedRange().length == 0
        if translation.hasPrefix(current) {
            let suffix = String(translation.dropFirst(current.count))
            textView.textStorage?.append(NSAttributedString(
                string: suffix,
                attributes: [
                    .font: NSFont.systemFont(ofSize: 16),
                    .foregroundColor: translationTextColor,
                ]
            ))
        } else {
            // A transport or schema retry replaces the previous preview.
            textView.string = translation
            let length = textView.string.utf16.count
            let location = min(selection.location, length)
            textView.setSelectedRange(NSRange(location: location, length: min(selection.length, length - location)))
        }
        fitWindowHeightToText()
        if followStream && followsEnd {
            textView.scrollRangeToVisible(NSRange(location: textView.string.utf16.count, length: 0))
        } else if let scrollOrigin {
            scrollView?.contentView.scroll(to: scrollOrigin)
        }
    }

    private func fitWindowHeightToText() {
        guard !isFittingHeight, hasReceivedTranslation,
              let window, let textView = translationView,
              let container = textView.textContainer,
              let manager = textView.layoutManager else { return }
        isFittingHeight = true
        defer { isFittingHeight = false }
        manager.ensureLayout(for: container)
        let textHeight = ceil(manager.usedRect(for: container).height)
            + 2 * verticalTextInset + layoutSlack
        let visibleFrame = (window.screen ?? NSScreen.main)?.visibleFrame
            ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let currentFrame = window.frame
        let contentSize = window.contentView?.bounds.size ?? NSSize(width: 560, height: 68)
        let titleHeight = currentFrame.height - contentSize.height
        let maximumHeight = max(68, visibleFrame.height - titleHeight - 40)
        let desiredHeight = min(max(68, textHeight), maximumHeight)
        if desiredHeight > contentSize.height + 0.5 {
            var fittedFrame = window.frameRect(forContentRect: NSRect(
                x: 0, y: 0, width: contentSize.width, height: desiredHeight
            ))
            fittedFrame.origin.x = currentFrame.minX
            fittedFrame.origin.y = max(
                visibleFrame.minY + 20,
                min(currentFrame.maxY - fittedFrame.height, visibleFrame.maxY - 20 - fittedFrame.height)
            )
            window.setFrame(fittedFrame, display: true, animate: false)
        }
        if let scrollView = textView.enclosingScrollView {
            textView.minSize = NSSize(width: 0, height: scrollView.contentSize.height)
        }
    }

    func windowDidResize(_ notification: Notification) {
        fitWindowHeightToText()
    }

    private func textWindowFrame(for payload: String, wrapsLines: Bool) -> NSRect {
        let visibleSize = (window?.screen ?? NSScreen.main)?.visibleFrame.size
            ?? NSSize(width: 1440, height: 900)
        let availableWidth = max(360, visibleSize.width - 80)
        let availableHeight = max(240, visibleSize.height - 100)
        let horizontalChrome = 2 * (horizontalTextInset + lineFragmentPadding)
            + layoutSlack
        let verticalChrome = 2 * verticalTextInset + layoutSlack

        if !wrapsLines {
            let font = NSFont.systemFont(ofSize: 16)
            let lineBounds = (payload as NSString).boundingRect(
                with: NSSize(
                    width: CGFloat.greatestFiniteMagnitude,
                    height: CGFloat.greatestFiniteMagnitude
                ),
                options: [.usesLineFragmentOrigin, .usesFontLeading],
                attributes: [.font: font]
            )
            let maximumWidth = min(1000, availableWidth)
            let minimumWidth = min(560, maximumWidth)
            let width = min(
                max(minimumWidth, ceil(lineBounds.width) + horizontalChrome),
                maximumWidth
            )
            let height = min(
                max(180, ceil(lineBounds.height) + verticalChrome),
                availableHeight
            )
            return NSRect(x: 0, y: 0, width: width, height: height)
        }

        let font = NSFont.systemFont(ofSize: 16)
        let attributes: [NSAttributedString.Key: Any] = [.font: font]
        let naturalBounds = (payload as NSString).boundingRect(
            with: NSSize(
                width: CGFloat.greatestFiniteMagnitude,
                height: CGFloat.greatestFiniteMagnitude
            ),
            options: [.usesLineFragmentOrigin, .usesFontLeading],
            attributes: attributes
        )
        let maximumWidth = min(760, availableWidth)
        let minimumWidth = min(380, maximumWidth)
        let width = min(
            max(minimumWidth, ceil(naturalBounds.width) + horizontalChrome),
            maximumWidth
        )
        let wrappedBounds = (payload as NSString).boundingRect(
            with: NSSize(
                width: max(1, width - horizontalChrome),
                height: CGFloat.greatestFiniteMagnitude
            ),
            options: [.usesLineFragmentOrigin, .usesFontLeading],
            attributes: attributes
        )
        let height = min(
            max(100, ceil(wrappedBounds.height) + verticalChrome),
            availableHeight
        )
        return NSRect(x: 0, y: 0, width: width, height: height)
    }

    private func finishTranslation() {
        let payloadURL = URL(fileURLWithPath: payloadPath)
        let payload = (try? String(contentsOf: payloadURL, encoding: .utf8))
            ?? "无法读取翻译结果。"
        try? FileManager.default.removeItem(at: payloadURL)

        if window == nil {
            var frame = textWindowFrame(for: payload, wrapsLines: mode == "error" || resultLayout != .multi)
            frame.size.height += 56
            prepareSurface(frame: frame)
            let label = resultLayout == .phrase ? "词组极速翻译"
                : resultLayout == .single ? "单段落快速翻译"
                : resultLayout == .paragraphs ? "多段落综合翻译（保留原段落）" : "多段落综合翻译"
            applyMode(label)
        }
        configureTextLayout(wrapsLines: mode == "error" || resultLayout != .multi)
        // Keep the same view hierarchy, text view, frame, selection and scroll.
        updateStreamingTranslation(payload, followStream: false)
        if mode == "result" {
            let pasteboard = NSPasteboard.general
            pasteboard.clearContents()
            pasteboard.setString(payload, forType: .string)
            progressLabel?.stringValue = "翻译完成，已复制到剪贴板。"
            progressIndicator?.doubleValue = 100
        } else {
            progressLabel?.stringValue = "翻译失败。"
            progressLabel?.textColor = .systemRed
        }
        if !hasActivated {
            hasActivated = true
            window?.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        }
    }

    private func configureMainMenu() {
        let mainMenu = NSMenu()
        let editMenuItem = NSMenuItem()
        mainMenu.addItem(editMenuItem)
        let editMenu = NSMenu(title: "编辑")
        editMenu.addItem(
            withTitle: "全选",
            action: #selector(NSText.selectAll(_:)),
            keyEquivalent: "a"
        )
        editMenu.addItem(
            withTitle: "复制",
            action: #selector(NSText.copy(_:)),
            keyEquivalent: "c"
        )
        editMenuItem.submenu = editMenu
        NSApp.mainMenu = mainMenu
    }

    func windowWillClose(_ notification: Notification) {
        statusTimer?.invalidate()
        streamSource?.cancel()
        NSApp.terminate(nil)
    }
}

let arguments = CommandLine.arguments
let hasStreamPath = arguments.count == 5 && arguments[1] == "loading"
    && arguments[3] == "--stream-file"
let hasValidCount = arguments.count == 3 || arguments.count == 4 || hasStreamPath
let hasValidMode = arguments.count >= 2
    && ["loading", "result", "error"].contains(arguments[1])
let requestedLayout = arguments.count == 4
    ? ResultLayout(rawValue: arguments[3])
    : .multi
guard hasValidCount, hasValidMode, requestedLayout != nil else {
    FileHandle.standardError.write(
        Data("usage: translation_result_window loading|result|error FILE [phrase|single|paragraphs|multi] [--stream-file PATH]\n".utf8)
    )
    exit(2)
}

let application = NSApplication.shared
application.setActivationPolicy(.accessory)
let delegate = ResultWindowDelegate(
    mode: arguments[1],
    payloadPath: arguments[2],
    resultLayout: requestedLayout ?? .multi,
    streamPath: hasStreamPath ? arguments[4] : nil
)
application.delegate = delegate
application.run()
