import AppKit
import ApplicationServices
import Carbon
import Foundation

private let hotKeySignature: OSType = 0x54524E53  // "TRNS"
private let hotKeyIdentifier: UInt32 = 1
private var launcherPath = ""

private func writeLog(_ message: String) {
    FileHandle.standardError.write(Data((message + "\n").utf8))
}

private func requestCaptureAccess() {
    let promptKey = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
    let options = [promptKey: true] as CFDictionary
    let accessibility = AXIsProcessTrustedWithOptions(options)
    let postEvents = CGPreflightPostEventAccess()
        || CGRequestPostEventAccess()
    writeLog(
        "capture permissions: accessibility=\(accessibility) "
        + "post_events=\(postEvents)"
    )
}

private func launchTranslator() {
    FileHandle.standardError.write(Data("hotkey received\n".utf8))
    let process = Process()
    process.executableURL = URL(fileURLWithPath: launcherPath)
    var environment = ProcessInfo.processInfo.environment
    if let target = NSWorkspace.shared.frontmostApplication {
        environment["TRANSLATOR_TARGET_PID"] = String(target.processIdentifier)
        environment["TRANSLATOR_TARGET_BUNDLE_ID"] = target.bundleIdentifier ?? ""
    }
    process.environment = environment
    do {
        try process.run()
    } catch {
        FileHandle.standardError.write(
            Data("failed to launch translator: \(error)\n".utf8)
        )
    }
}

private let hotKeyHandler: EventHandlerUPP = { _, event, _ in
    guard let event else {
        return OSStatus(eventNotHandledErr)
    }
    var identifier = EventHotKeyID()
    let status = GetEventParameter(
        event,
        EventParamName(kEventParamDirectObject),
        EventParamType(typeEventHotKeyID),
        nil,
        MemoryLayout<EventHotKeyID>.size,
        nil,
        &identifier
    )
    guard status == noErr,
          identifier.signature == hotKeySignature,
          identifier.id == hotKeyIdentifier else {
        return OSStatus(eventNotHandledErr)
    }
    launchTranslator()
    return noErr
}

private func main() -> Int32 {
    guard CommandLine.arguments.count == 2 else {
        FileHandle.standardError.write(
            Data("usage: translator_hotkey_daemon /absolute/path/run_quick_action.sh\n".utf8)
        )
        return 64
    }
    launcherPath = CommandLine.arguments[1]
    let application = NSApplication.shared
    application.setActivationPolicy(.prohibited)
    requestCaptureAccess()

    var eventSpec = EventTypeSpec(
        eventClass: OSType(kEventClassKeyboard),
        eventKind: UInt32(kEventHotKeyPressed)
    )
    var handlerReference: EventHandlerRef?
    let handlerStatus = InstallEventHandler(
        GetApplicationEventTarget(),
        hotKeyHandler,
        1,
        &eventSpec,
        nil,
        &handlerReference
    )
    guard handlerStatus == noErr else {
        FileHandle.standardError.write(
            Data("failed to install hotkey handler: \(handlerStatus)\n".utf8)
        )
        return handlerStatus
    }

    var hotKeyReference: EventHotKeyRef?
    let identifier = EventHotKeyID(
        signature: hotKeySignature,
        id: hotKeyIdentifier
    )
    let modifiers = UInt32(controlKey | optionKey | cmdKey)
    let registrationStatus = RegisterEventHotKey(
        UInt32(kVK_ANSI_T),
        modifiers,
        identifier,
        GetApplicationEventTarget(),
        0,
        &hotKeyReference
    )
    guard registrationStatus == noErr else {
        FileHandle.standardError.write(
            Data("failed to register Control+Option+Command+T: \(registrationStatus)\n".utf8)
        )
        return registrationStatus
    }

    application.run()
    return 0
}

exit(main())
