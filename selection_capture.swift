import AppKit
import ApplicationServices
import Foundation

private func diagnostic(_ message: String) {
    guard let data = (message + "\n").data(using: .utf8) else {
        return
    }
    FileHandle.standardError.write(data)
}

private func nonempty(_ text: String?) -> String? {
    guard let text,
          !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
        return nil
    }
    return text
}

private func selectedText(processIdentifier: pid_t) -> String? {
    guard AXIsProcessTrusted() else {
        diagnostic("ax_trusted=false")
        return nil
    }
    diagnostic("ax_trusted=true")

    let application = AXUIElementCreateApplication(processIdentifier)
    var focusedValue: CFTypeRef?
    let focusedStatus = AXUIElementCopyAttributeValue(
        application,
        kAXFocusedUIElementAttribute as CFString,
        &focusedValue
    )
    diagnostic("ax_focused_status=\(focusedStatus.rawValue)")
    guard focusedStatus == .success,
    let focusedValue,
    CFGetTypeID(focusedValue) == AXUIElementGetTypeID() else {
        return nil
    }

    let focusedElement = unsafeBitCast(focusedValue, to: AXUIElement.self)
    var selectionValue: CFTypeRef?
    let selectionStatus = AXUIElementCopyAttributeValue(
        focusedElement,
        kAXSelectedTextAttribute as CFString,
        &selectionValue
    )
    diagnostic("ax_selection_status=\(selectionStatus.rawValue)")
    guard selectionStatus == .success else {
        return nil
    }
    let selection = nonempty(selectionValue as? String)
    diagnostic("ax_selection_bytes=\(selection?.utf8.count ?? 0)")
    return selection
}

private func waitForShortcutModifiersToRelease() -> Bool {
    let shortcutModifiers: CGEventFlags = [
        .maskCommand,
        .maskControl,
        .maskAlternate,
    ]
    let deadline = Date().addingTimeInterval(2.0)
    repeat {
        let active = CGEventSource.flagsState(.combinedSessionState)
            .intersection(shortcutModifiers)
        if active.isEmpty {
            diagnostic("shortcut_modifiers_released=true")
            return true
        }
        Thread.sleep(forTimeInterval: 0.02)
    } while Date() < deadline
    let active = CGEventSource.flagsState(.combinedSessionState)
        .intersection(shortcutModifiers)
    diagnostic("shortcut_modifiers_released=false flags=\(active.rawValue)")
    return false
}

private func copiedText(processIdentifier: pid_t) -> String? {
    let trusted = AXIsProcessTrusted()
    let canPostEvents = CGPreflightPostEventAccess()
    diagnostic("copy_ax_trusted=\(trusted) post_events=\(canPostEvents)")
    guard trusted, canPostEvents else {
        return nil
    }

    let frontmostBefore = NSWorkspace.shared.frontmostApplication
    diagnostic(
        "copy_frontmost_before_pid=\(frontmostBefore?.processIdentifier ?? -1)"
    )
    guard frontmostBefore?.processIdentifier == processIdentifier else {
        diagnostic("copy_aborted=frontmost_changed_before_event")
        return nil
    }
    guard waitForShortcutModifiersToRelease() else {
        diagnostic("copy_aborted=shortcut_modifiers_held")
        return nil
    }

    let pasteboard = NSPasteboard.general
    let previousChangeCount = pasteboard.changeCount
    diagnostic("pasteboard_change_before=\(previousChangeCount)")
    let cKeyCode: CGKeyCode = 8
    let eventSource = CGEventSource(stateID: .hidSystemState)
    eventSource?.localEventsSuppressionInterval = 0
    guard let keyDown = CGEvent(
              keyboardEventSource: eventSource,
              virtualKey: cKeyCode,
              keyDown: true
          ),
          let keyUp = CGEvent(
              keyboardEventSource: eventSource,
              virtualKey: cKeyCode,
              keyDown: false
          ) else {
        return nil
    }

    keyDown.flags = .maskCommand
    keyUp.flags = .maskCommand
    // Chromium-based browsers may ignore a keyboard event posted directly to
    // their process. Post at the HID event tap so the normal frontmost-app
    // routing handles Command+C, while checking that focus stayed on the same
    // application before accepting the copied text.
    keyDown.post(tap: .cghidEventTap)
    Thread.sleep(forTimeInterval: 0.01)
    keyUp.post(tap: .cghidEventTap)
    diagnostic("copy_event_posted=true")

    let deadline = Date().addingTimeInterval(1.0)
    repeat {
        if pasteboard.changeCount != previousChangeCount {
            let frontmostAfter = NSWorkspace.shared.frontmostApplication
            diagnostic("pasteboard_change_after=\(pasteboard.changeCount)")
            diagnostic(
                "copy_frontmost_after_pid=\(frontmostAfter?.processIdentifier ?? -1)"
            )
            guard frontmostAfter?.processIdentifier == processIdentifier else {
                diagnostic("copy_aborted=frontmost_changed_after_event")
                return nil
            }
            let copied = nonempty(pasteboard.string(forType: .string))
            diagnostic("copied_bytes=\(copied?.utf8.count ?? 0)")
            return copied
        }
        Thread.sleep(forTimeInterval: 0.02)
    } while Date() < deadline
    diagnostic("copy_timeout=true pasteboard_change_after=\(pasteboard.changeCount)")
    return nil
}

private func requestedProcessIdentifier() -> pid_t? {
    let arguments = Array(CommandLine.arguments.dropFirst())
    guard let index = arguments.firstIndex(of: "--pid"),
          arguments.indices.contains(index + 1),
          let value = Int32(arguments[index + 1]),
          value > 0 else {
        return nil
    }
    return value
}

private func writeStandardOutput(_ text: String) {
    guard let data = text.data(using: .utf8) else {
        return
    }
    FileHandle.standardOutput.write(data)
}

private func main() -> Int32 {
    if CommandLine.arguments.dropFirst().contains("--preflight") {
        let trusted = AXIsProcessTrusted()
        let canPostEvents = CGPreflightPostEventAccess()
        print("accessibility=\(trusted) post_events=\(canPostEvents)")
        return trusted && canPostEvents ? 0 : 1
    }

    guard let currentFrontmostApplication = NSWorkspace.shared.frontmostApplication else {
        diagnostic("capture_aborted=no_frontmost_application")
        return 1
    }
    let processIdentifier = requestedProcessIdentifier()
        ?? currentFrontmostApplication.processIdentifier
    let targetApplication = NSRunningApplication(
        processIdentifier: processIdentifier
    )
    diagnostic("target_pid=\(processIdentifier)")
    diagnostic("target_bundle=\(targetApplication?.bundleIdentifier ?? "")")
    diagnostic(
        "current_frontmost_pid=\(currentFrontmostApplication.processIdentifier)"
    )
    diagnostic(
        "current_frontmost_bundle=\(currentFrontmostApplication.bundleIdentifier ?? "")"
    )
    guard let input = selectedText(processIdentifier: processIdentifier)
        ?? copiedText(processIdentifier: processIdentifier) else {
        diagnostic("capture_result=empty")
        return 1
    }
    diagnostic("capture_result=success bytes=\(input.utf8.count)")
    writeStandardOutput(input)
    return 0
}

exit(main())
