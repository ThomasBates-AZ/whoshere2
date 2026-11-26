import SwiftUI

/// Everyone the board counts as on campus, in the board's order.
struct OnCampusView: View {
    @EnvironmentObject private var store: BoardStore

    var body: some View {
        List {
            if store.people.isEmpty {
                Text("Nobody's on campus.")
                    .foregroundStyle(Theme.secondary)
            }
            ForEach(store.people) { person in
                NavigationLink(value: Route.person(person)) {
                    PersonRow(person: person)
                }
            }
        }
        .navigationTitle("On campus · \(store.people.count)")
    }
}

struct PersonRow: View {
    let person: OnSitePerson

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 4) {
                Text(person.name)
                    .font(.headline)
                    .lineLimit(1)
                Spacer(minLength: 2)
                if person.isLeaving {
                    Pill(text: "Leaving?", color: Theme.amber)
                }
            }
            LocationLabel(location: person.location)
            Text(person.subtitle())
                .font(.footnote)
                .foregroundStyle(Theme.secondary)
        }
        .padding(.vertical, 2)
    }
}

/// One person: where they are, since when, last seen. No Wi-Fi network.
struct PersonView: View {
    @EnvironmentObject private var store: BoardStore
    let person: OnSitePerson

    /// The latest copy from the board, if they're still on it.
    private var current: OnSitePerson? {
        store.people.first { $0.name == person.name }
    }

    var body: some View {
        let shown = current ?? person
        ScrollView {
            VStack(alignment: .leading, spacing: 8) {
                Text(shown.name)
                    .font(.system(size: 22, weight: .bold))
                HStack(spacing: 6) {
                    if current == nil {
                        Pill(text: "Left", color: Theme.secondary)
                    } else if shown.isLeaving {
                        Pill(text: "Leaving?", color: Theme.amber)
                    } else {
                        Pill(text: "Here", color: Theme.green)
                    }
                    if let since = shown.sinceDate {
                        Text("since \(Fmt.clock(since))")
                            .font(.footnote)
                            .foregroundStyle(Theme.secondary)
                    }
                }
                VStack(alignment: .leading, spacing: 8) {
                    Field(label: "Location") {
                        LocationLabel(location: shown.location, font: .system(size: 15, weight: .semibold))
                    }
                    Field(label: "Last seen") {
                        Text(shown.lastSeenDate.map { capitalized(Fmt.ago($0)) } ?? "—")
                            .font(.system(size: 15, weight: .medium))
                    }
                }
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 16).fill(Theme.card))
            }
        }
        .navigationTitle("On campus")
    }

    private func capitalized(_ text: String) -> String {
        text.prefix(1).uppercased() + text.dropFirst()
    }
}

struct Field<Content: View>: View {
    let label: String
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(label)
                .font(.caption2)
                .foregroundStyle(Theme.secondary)
            content
        }
    }
}

struct Pill: View {
    let text: String
    let color: Color

    var body: some View {
        Text(text)
            .font(.system(size: 12, weight: .bold))
            .foregroundStyle(.black)
            .padding(.horizontal, 7)
            .padding(.vertical, 2)
            .background(Capsule().fill(color))
    }
}

struct LocationLabel: View {
    let location: String?
    var font: Font = .footnote

    var body: some View {
        let color = Theme.locationColor(location)
        HStack(spacing: 5) {
            Circle()
                .fill(color)
                .frame(width: 7, height: 7)
                .accessibilityHidden(true)
            Text(location ?? "Location unknown")
                .font(font)
                .foregroundStyle(color)
                .lineLimit(1)
        }
    }
}
