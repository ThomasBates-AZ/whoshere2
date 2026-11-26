import SwiftUI
import WidgetKit

@main
struct WhosHereComplications: WidgetBundle {
    var body: some Widget {
        VerdictWidget()
    }
}

struct VerdictWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "WhosHereVerdict", provider: VerdictProvider()) { entry in
            VerdictWidgetView(entry: entry)
                .containerBackground(.fill.tertiary, for: .widget)
        }
        .configurationDisplayName("Who's Here")
        .description("All clear to arm, or how many are on campus.")
        .supportedFamilies([.accessoryRectangular, .accessoryCircular, .accessoryCorner, .accessoryInline])
    }
}

struct VerdictEntry: TimelineEntry {
    let date: Date
    let state: DisplayState
}

struct VerdictProvider: TimelineProvider {
    func placeholder(in context: Context) -> VerdictEntry {
        VerdictEntry(date: .now, state: DisplayState(kind: .occupied, count: 3, headline: "Not clear",
                                                     detail: "3 on campus", updated: .now))
    }

    func getSnapshot(in context: Context, completion: @escaping (VerdictEntry) -> Void) {
        if context.isPreview && SharedStore.loadCached() == nil {
            completion(placeholder(in: context))
        } else {
            completion(VerdictEntry(date: .now, state: Self.state(at: .now, fetchFailed: false)))
        }
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<VerdictEntry>) -> Void) {
        Task {
            var failed = false
            let config = BoardConfig.load()
            if config.baseURL != nil {
                do {
                    let status = try await BoardClient(config: config).fetchStatus()
                    SharedStore.saveCached(CachedStatus(status: status, fetchedAt: .now))
                } catch {
                    failed = true
                }
            }
            let now = Date.now
            var entries = [VerdictEntry(date: now, state: Self.state(at: now, fetchFailed: failed))]
            // If nothing refreshes it in time, switch to "Can't confirm" rather than keep an old answer.
            if let cached = SharedStore.loadCached() {
                let tooOld = cached.fetchedAt.addingTimeInterval(Theme.complicationMaxAge + 1)
                if tooOld > now {
                    entries.append(VerdictEntry(date: tooOld, state: Self.state(at: tooOld, fetchFailed: failed)))
                }
            }
            completion(Timeline(entries: entries, policy: .after(now.addingTimeInterval(15 * 60))))
        }
    }

    private static func state(at date: Date, fetchFailed: Bool) -> DisplayState {
        DisplayState.make(cached: SharedStore.loadCached(), configured: BoardConfig.load().baseURL != nil,
                          lastFetchFailed: fetchFailed, now: date, maxAge: Theme.complicationMaxAge)
    }
}

struct VerdictWidgetView: View {
    @Environment(\.widgetFamily) private var family
    let entry: VerdictEntry

    private var state: DisplayState { entry.state }
    private var color: Color { Theme.color(for: state.kind) }

    var body: some View {
        switch family {
        case .accessoryCircular: circular
        case .accessoryCorner: corner
        case .accessoryInline: Text(shortText)
        default: rectangular
        }
    }

    /// Also what the Smart Stack shows.
    private var rectangular: some View {
        VStack(alignment: .leading, spacing: 1) {
            Text("WHO'S HERE")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(.secondary)
            Text(state.headline)
                .font(.headline)
                .foregroundStyle(color)
                .lineLimit(1)
                .minimumScaleFactor(0.8)
                .widgetAccentable()
            Text(rectangularDetail)
                .font(.caption)
                .lineLimit(1)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var rectangularDetail: String {
        switch state.kind {
        case .clear, .occupied:
            if let updated = state.updated { return "\(state.detail) · \(Fmt.clock(updated))" }
            return state.detail
        case .cantConfirm: return "Don't arm yet"
        case .notSetUp: return "Open the app to set up"
        case .loading: return ""
        }
    }

    private var circular: some View {
        ZStack {
            AccessoryWidgetBackground()
            Circle()
                .strokeBorder(color, lineWidth: 3)
                .widgetAccentable()
            switch state.kind {
            case .occupied:
                VStack(spacing: 0) {
                    Text("\(state.count)")
                        .font(.system(size: 18, weight: .bold))
                    Image(systemName: "person.fill")
                        .font(.system(size: 10))
                }
            case .clear:
                Image(systemName: "checkmark")
                    .font(.system(size: 20, weight: .bold))
                    .foregroundStyle(color)
                    .widgetAccentable()
            default:
                Image(systemName: state.kind == .notSetUp ? "gearshape" : "questionmark")
                    .font(.system(size: 18, weight: .bold))
                    .foregroundStyle(color)
                    .widgetAccentable()
            }
        }
        .accessibilityLabel(shortText)
    }

    private var corner: some View {
        Image(systemName: Theme.symbol(for: state.kind))
            .font(.title3)
            .foregroundStyle(color)
            .widgetAccentable()
            .widgetLabel {
                Text(shortText)
            }
    }

    private var shortText: String {
        switch state.kind {
        case .clear: return "All clear"
        case .occupied: return "\(state.count) on campus"
        case .cantConfirm: return "Can't confirm"
        case .notSetUp: return "Set up Who's Here"
        case .loading: return "Who's Here"
        }
    }
}
