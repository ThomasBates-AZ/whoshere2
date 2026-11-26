import Foundation

/// What the board's /api/status returns. Only the fields the watch shows are decoded;
/// the Wi-Fi network name is deliberately left out.
struct BoardStatus: Codable, Equatable {
    var allClear: Bool
    var verdict: String            // "clear", "occupied" or "stale"
    var onSite: [OnSitePerson]
    var lastSuccess: Double?       // when UniFi last answered (seconds since 1970)
    var lastError: String?
    var generatedAt: Double?
    var staleAfter: Double?        // seconds without UniFi data before the board says "can't confirm"
}

struct OnSitePerson: Codable, Hashable, Identifiable {
    var name: String
    var location: String?
    var since: Double?
    var lastSeen: Double?
    var leaving: Bool?

    var id: String { "\(name)|\(since ?? 0)" }
    var isLeaving: Bool { leaving ?? false }
    var sinceDate: Date? { Fmt.date(since) }
    var lastSeenDate: Date? { Fmt.date(lastSeen) }

    /// "Since 7:42 AM", or "Not seen for 6 min" while they're in the departure grace period.
    func subtitle(now: Date = .now) -> String {
        if isLeaving, let seen = lastSeenDate {
            return "Not seen for \(max(1, Int(now.timeIntervalSince(seen)) / 60)) min"
        }
        if let since = sinceDate { return "Since \(Fmt.clock(since))" }
        return ""
    }
}

/// A status plus when this watch got it.
struct CachedStatus: Codable, Equatable {
    var status: BoardStatus
    var fetchedAt: Date
}

/// What the home screen and complications show. Built the same way in both, so they never disagree.
struct DisplayState: Equatable {
    enum Kind: Equatable { case notSetUp, loading, clear, occupied, cantConfirm }

    var kind: Kind
    var count: Int = 0
    var headline: String
    var detail: String
    var updated: Date? = nil

    /// - maxAge: how old an answer may be before it no longer counts. Defaults to the board's own
    ///   stale limit; complications pass a longer one because they refresh less often.
    static func make(cached: CachedStatus?, configured: Bool, lastFetchFailed: Bool,
                     now: Date = .now, maxAge: TimeInterval? = nil) -> DisplayState {
        guard configured else {
            return DisplayState(kind: .notSetUp, headline: "Not set up", detail: "Add the board's address in Settings.")
        }
        guard let cached else {
            return lastFetchFailed
                ? DisplayState(kind: .cantConfirm, headline: "Can't confirm", detail: "Can't reach the board.")
                : DisplayState(kind: .loading, headline: "Checking…", detail: "")
        }
        let status = cached.status
        let limit = maxAge ?? max(status.staleAfter ?? 180, 60)
        if status.verdict == "stale" {
            var detail = "UniFi isn't answering."
            if let last = Fmt.date(status.lastSuccess) { detail = "UniFi last answered \(Fmt.clock(last))." }
            return DisplayState(kind: .cantConfirm, headline: "Can't confirm", detail: detail, updated: cached.fetchedAt)
        }
        // Never say "all clear" from an answer that's too old to trust.
        if now.timeIntervalSince(cached.fetchedAt) > limit {
            return DisplayState(kind: .cantConfirm, headline: "Can't confirm",
                                detail: "Can't reach the board. Last answer \(Fmt.ago(cached.fetchedAt, now: now)).",
                                updated: cached.fetchedAt)
        }
        switch status.verdict {
        case "clear":
            return DisplayState(kind: .clear, headline: "All clear to arm", detail: "Everyone has left",
                                updated: cached.fetchedAt)
        case "occupied":
            let n = status.onSite.count
            return DisplayState(kind: .occupied, count: n, headline: "Not clear", detail: "\(n) on campus",
                                updated: cached.fetchedAt)
        default:
            return DisplayState(kind: .cantConfirm, headline: "Can't confirm",
                                detail: "The board gave an answer this app doesn't know.", updated: cached.fetchedAt)
        }
    }
}

enum Fmt {
    static func date(_ seconds: Double?) -> Date? {
        seconds.map { Date(timeIntervalSince1970: $0) }
    }

    /// "7:42 AM" today, "Tue 7:42 AM" otherwise.
    static func clock(_ date: Date) -> String {
        if Calendar.current.isDateInToday(date) {
            return date.formatted(date: .omitted, time: .shortened)
        }
        return date.formatted(.dateTime.weekday(.abbreviated).hour().minute())
    }

    /// "just now", "4 min ago", "2 hr ago", or a clock time.
    static func ago(_ date: Date, now: Date = .now) -> String {
        let seconds = Int(now.timeIntervalSince(date))
        if seconds < 60 { return "just now" }
        if seconds < 3600 { return "\(seconds / 60) min ago" }
        if seconds < 86_400 { return "\(seconds / 3600) hr ago" }
        return clock(date)
    }
}
