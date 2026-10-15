// Append this harness to result_window.swift before its command-line entry point.
let smokeStatusPath = CommandLine.arguments[1]
let smokePayloadPath = CommandLine.arguments[2]
let smokeStreamPath = CommandLine.arguments.count > 3 ? CommandLine.arguments[3] : nil
let smokeApplication = NSApplication.shared
smokeApplication.setActivationPolicy(.accessory)
let smokeDelegate = ResultWindowDelegate(mode: "loading", payloadPath: smokeStatusPath, resultLayout: .single, streamPath: smokeStreamPath)
smokeApplication.delegate = smokeDelegate
var smokeWindow: NSWindow?
var smokeTopLeft: NSPoint?
var smokeFrame: NSRect?
var smokeContentView: NSView?
var smokeTranslationView: NSTextView?
var smokeStage = 0
let smokeDeadline = Date().addingTimeInterval(8)

func smokeFail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

func smokeTextView(_ view: NSView?) -> NSTextView? {
    guard let view else { return nil }
    if let text = view as? NSTextView { return text }
    for child in view.subviews {
        if let text = smokeTextView(child) { return text }
    }
    return nil
}

func smokeProgress(_ view: NSView?) -> NSProgressIndicator? {
    guard let view else { return nil }
    if let progress = view as? NSProgressIndicator { return progress }
    for child in view.subviews {
        if let progress = smokeProgress(child) { return progress }
    }
    return nil
}

func smokeWrite(_ payload: [String: Any]) {
    do {
        if let smokeStreamPath, payload["translation"] != nil {
            let stream = try FileHandle(forWritingTo: URL(fileURLWithPath: smokeStreamPath))
            try stream.seekToEnd()
            var line = try JSONSerialization.data(withJSONObject: payload)
            line.append(10)
            try stream.write(contentsOf: line)
            try stream.close()
            return
        }
        try JSONSerialization.data(withJSONObject: payload).write(
            to: URL(fileURLWithPath: smokeStatusPath), options: .atomic
        )
    } catch { smokeFail("Cannot write status: \(error)") }
}

let smokeTimer = Timer.scheduledTimer(withTimeInterval: 0.12, repeats: true) { _ in
    if Date() > smokeDeadline { smokeFail("Window transition timed out at stage \(smokeStage)") }
    let visible = smokeApplication.windows.filter { $0.isVisible }
    guard let window = visible.first else { return }
    if visible.count != 1 { smokeFail("More than one visible window") }
    if let original = smokeWindow {
        if original !== window || original.windowNumber != window.windowNumber {
            smokeFail("Native window identity changed")
        }
        if window.contentView !== smokeContentView
            || smokeTextView(window.contentView) !== smokeTranslationView {
            smokeFail("The page or translation text view was replaced")
        }
        if let frame = smokeFrame, !NSEqualRects(frame, window.frame) {
            smokeFail("Window size or position changed")
        }
        if let anchor = smokeTopLeft,
           abs(window.frame.minX - anchor.x) > 0.5 || abs(window.frame.maxY - anchor.y) > 0.5 {
            smokeFail("Window moved during content replacement")
        }
    }
    switch smokeStage {
    case 0:
        smokeWindow = window
        smokeFrame = window.frame
        smokeContentView = window.contentView
        smokeTranslationView = smokeTextView(window.contentView)
        if smokeTranslationView == nil { smokeFail("Text view was not created at startup") }
        if smokeProgress(window.contentView)?.isHidden != false {
            smokeFail("Initial waiting indicator is missing")
        }
        smokeTopLeft = NSPoint(x: window.frame.minX, y: window.frame.maxY)
        print("progress window_number=\(window.windowNumber)")
        smokeWrite(["translation": "正在逐字显示译文…", "message": "正在接收译文…", "mode": "单段落快速翻译", "percent": 60, "activate": false])
        smokeStage = 1
    case 1:
        guard smokeTextView(window.contentView)?.string == "正在逐字显示译文…" else { return }
        if smokeProgress(window.contentView)?.isHidden != true {
            smokeFail("Progress indicator remained visible after the first translation")
        }
        if smokeProgress(window.contentView)?.doubleValue != 100 {
            smokeFail("First translation did not complete the waiting indicator")
        }
        print("streaming window_number=\(window.windowNumber)")
        smokeWrite(["translation": "", "message": "正在重试…", "percent": 60, "activate": false])
        smokeStage = 2
    case 2:
        guard smokeTextView(window.contentView)?.string == "" else { return }
        if smokeProgress(window.contentView)?.isHidden != true {
            smokeFail("Progress indicator reappeared during retry")
        }
        print("retry_reset window_number=\(window.windowNumber)")
        smokeWrite(["translation": "重新开始的译文。", "message": "正在接收译文…", "percent": 60, "activate": false])
        smokeStage = 3
    case 3:
        guard smokeTextView(window.contentView)?.string == "重新开始的译文。" else { return }
        smokeTranslationView?.setSelectedRange(NSRange(location: 2, length: 3))
        smokeWrite(["completed": "error", "path": smokePayloadPath, "layout": "single"])
        smokeStage = 4
    default:
        guard !FileManager.default.fileExists(atPath: smokeStatusPath) else { return }
        if smokeTextView(window.contentView)?.string != "窗口复用验证完成。" {
            smokeFail("Final text was not replaced")
        }
        if FileManager.default.fileExists(atPath: smokeStatusPath)
            || FileManager.default.fileExists(atPath: smokePayloadPath) {
            smokeFail("Handoff files were not consumed")
        }
        if smokeProgress(window.contentView)?.isHidden != true {
            smokeFail("Progress indicator reappeared at completion")
        }
        if smokeProgress(window.contentView)?.doubleValue != 100 {
            smokeFail("Completed waiting indicator was reset")
        }
        if smokeTranslationView?.selectedRange() != NSRange(location: 2, length: 3) {
            smokeFail("Selection changed at completion")
        }
        print("completed window_number=\(window.windowNumber) same_page=true same_text_view=true same_frame=true progress_hidden=true selection_preserved=true")
        smokeApplication.terminate(nil)
    }
}
smokeApplication.run()
