import SwiftUI
import WatchKit
import WidgetKit

/// The app's copy of the board: the last answer, the settings, and the last error.
@MainActor
final class BoardStore: ObservableObject {
    static let shared = BoardStore()

    @Published private(set) var cached: CachedStatus? = SharedStore.loadCached()
    @Published private(set) var lastError: String?
    @Published private(set) var refreshing = false
    @Published private(set) var config = BoardConfig.load()

    var configured: Bool { config.baseURL != nil }
    var people: [OnSitePerson] { cached?.status.onSite ?? [] }

    func state(at date: Date = .now) -> DisplayState {
        DisplayState.make(cached: cached, configured: configured, lastFetchFailed: lastError != nil, now: date)
    }

    func refresh() async {
        guard configured, !refreshing else { return }
        refreshing = true
        defer { refreshing = false }

        let before = state()
        do {
            let status = try await BoardClient(config: config).fetchStatus()
            let previous = cached?.status
            let fresh = CachedStatus(status: status, fetchedAt: .now)
            cached = fresh
            lastError = nil
            SharedStore.saveCached(fresh)
            if previous?.verdict != status.verdict || previous?.onSite.count != status.onSite.count {
                WidgetCenter.shared.reloadAllTimelines()
            }
        } catch {
            lastError = error.localizedDescription
        }

        // A tap on the wrist when the answer changes while the app is open.
        let after = state()
        if after.kind != before.kind, before.kind != .loading, before.kind != .notSetUp {
            WKInterfaceDevice.current().play(after.kind == .clear ? .success : .notification)
        }
    }

    /// Saves new settings and checks them. Returns an error message, or nil if the board answered.
    func updateConfig(_ new: BoardConfig) async -> String? {
        new.save()
        let moved = new.baseURL != config.baseURL
        config = BoardConfig.load()
        if moved {
            cached = nil
            SharedStore.clearCache()
        }
        lastError = nil
        await refresh()
        WidgetCenter.shared.reloadAllTimelines()
        await PushRegistrar.shared.resend()
        return lastError
    }
}
