// Append to result_window.swift before the command-line entry point.
let heightStatusPath = CommandLine.arguments[1]
let heightStreamPath = CommandLine.arguments[2]
let heightPayloadPath = CommandLine.arguments[3]
let heightLayout = CommandLine.arguments.count > 4
    ? ResultLayout(rawValue: CommandLine.arguments[4])! : .single
let heightMode = heightLayout == .paragraphs
    ? "多段落综合翻译（保留原段落）" : "单段落快速翻译"
let heightApplication = NSApplication.shared
heightApplication.setActivationPolicy(.accessory)
let heightDelegate = ResultWindowDelegate(mode: "loading", payloadPath: heightStatusPath, resultLayout: heightLayout, streamPath: heightStreamPath)
heightApplication.delegate = heightDelegate
var heightStage = 0
var heightWindow: NSWindow?
var heightContentView: NSView?
var heightTextView: NSTextView?
var heightSamples: [CGFloat] = []
let heightLine = "这是一段用于验证流式窗口高度增长的译文，内容会随着返回的片段逐渐增加。"
let heightTexts = ["首个译文。", String(repeating: heightLine, count: 3), String(repeating: heightLine, count: 7) + "\n\n第二段。", String(repeating: heightLine, count: 200) + "\n\n第二段。"]
let heightDeadline = Date().addingTimeInterval(10)
// Completion exercises the result branch; preserve every clipboard format.
let heightSavedClipboard = (NSPasteboard.general.pasteboardItems ?? []).map { item in
    let copy = NSPasteboardItem()
    for type in item.types {
        if let data = item.data(forType: type) { copy.setData(data, forType: type) }
    }
    return copy
}
func heightRestoreClipboard() {
    NSPasteboard.general.clearContents()
    if !heightSavedClipboard.isEmpty { NSPasteboard.general.writeObjects(heightSavedClipboard) }
}

func heightFail(_ message: String) -> Never {
    heightRestoreClipboard()
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}
func heightFindText(_ view: NSView?) -> NSTextView? {
    guard let view else { return nil }
    if let text = view as? NSTextView { return text }
    for child in view.subviews {
        if let text = heightFindText(child) { return text }
    }
    return nil
}
func heightSend(_ text: String) {
    do {
        let handle = try FileHandle(forWritingTo: URL(fileURLWithPath: heightStreamPath))
        try handle.seekToEnd()
        var data = try JSONSerialization.data(withJSONObject: ["translation": text, "mode": heightMode])
        data.append(10)
        try handle.write(contentsOf: data)
        try handle.close()
    } catch { heightFail("Cannot append stream: \(error)") }
}
let heightTimer = Timer.scheduledTimer(withTimeInterval: 0.12, repeats: true) { _ in
    if Date() > heightDeadline { heightFail("Adaptive height timed out at stage \(heightStage)") }
    let windows = heightApplication.windows.filter { $0.isVisible }
    guard let window = windows.first, let textView = heightFindText(window.contentView) else { return }
    if windows.count != 1 { heightFail("More than one visible window") }
    if let original = heightWindow {
        if original !== window || window.contentView !== heightContentView || textView !== heightTextView {
            heightFail("Window, page or text view was replaced")
        }
        if abs(window.contentView!.bounds.width - 560) > 0.5 { heightFail("Window width changed") }
    }
    if heightStage == 0 {
        heightWindow = window
        heightContentView = window.contentView
        heightTextView = textView
        let initial = window.contentView!.bounds.height
        if abs(initial - 68) > 0.5 { heightFail("Waiting view was not compact: \(initial)") }
        if textView.enclosingScrollView?.isHidden != true { heightFail("Waiting view shows an empty text area") }
        heightSamples.append(initial)
        heightSend(heightTexts[0])
        heightStage = 1
        return
    }
    if heightStage <= 4 {
        guard textView.string == heightTexts[heightStage - 1] else { return }
        if textView.textContainer?.widthTracksTextView != true
            || textView.isHorizontallyResizable
            || textView.enclosingScrollView?.hasHorizontalScroller != false {
            heightFail("Streamed prose does not wrap inside the viewport")
        }
        let container = textView.textContainer!
        textView.layoutManager!.ensureLayout(for: container)
        if textView.layoutManager!.usedRect(for: container).width > textView.enclosingScrollView!.contentSize.width {
            heightFail("Streamed prose extends beyond the viewport")
        }
        let current = window.contentView!.bounds.height
        if current < heightSamples.last! { heightFail("Height shrank during streaming") }
        if heightStage == 2 || heightStage == 3 {
            if current <= heightSamples.last! { heightFail("Height did not grow with text") }
        }
        heightSamples.append(current)
        if heightStage == 3 && CommandLine.arguments.count > 5 {
            let content = window.contentView!
            if let bitmap = content.bitmapImageRepForCachingDisplay(in: content.bounds) {
                content.cacheDisplay(in: content.bounds, to: bitmap)
                if let png = bitmap.representation(using: .png, properties: [:]) {
                    try? png.write(to: URL(fileURLWithPath: CommandLine.arguments[5]))
                }
            }
        }
        if heightStage < 4 {
            heightSend(heightTexts[heightStage])
            heightStage += 1
            return
        }
        let screen = window.screen!.visibleFrame
        if window.frame.minY < screen.minY + 19 || window.frame.maxY > screen.maxY - 19 {
            heightFail("Window exceeded screen bounds")
        }
        let scrollView = textView.enclosingScrollView!
        if textView.bounds.height <= scrollView.contentSize.height { heightFail("Long text did not overflow into scrolling") }
        do {
            try heightTexts[3].write(toFile: heightPayloadPath, atomically: true, encoding: .utf8)
            let data = try JSONSerialization.data(withJSONObject: ["completed": "result", "path": heightPayloadPath, "layout": heightLayout.rawValue])
            try data.write(to: URL(fileURLWithPath: heightStatusPath), options: .atomic)
        } catch { heightFail("Cannot complete fixture") }
        heightStage = 5
        return
    }
    guard !FileManager.default.fileExists(atPath: heightStatusPath) else { return }
    if abs(window.contentView!.bounds.height - heightSamples.last!) > 0.5 { heightFail("Completion changed height") }
    if textView.textContainer?.widthTracksTextView != true { heightFail("Completion disabled line wrapping") }
    print("layout=\(heightLayout.rawValue) adaptive_content_heights=\(heightSamples) same_window=true same_page=true same_text_view=true wraps_lines=true screen_cap=true scroll_overflow=true")
    heightRestoreClipboard()
    heightApplication.terminate(nil)
}
heightApplication.run()
