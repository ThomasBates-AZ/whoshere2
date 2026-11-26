import SwiftUI

/// The first screen: the answer in big type, then a way into the list.
struct HomeView: View {
    @EnvironmentObject private var store: BoardStore

    var body: some View {
        TimelineView(.periodic(from: .now, by: 10)) { context in
            let state = store.state(at: context.date)
            ScrollView {
                VStack(spacing: 8) {
                    VerdictCard(state: state)

                    if state.kind == .notSetUp {
                        NavigationLink(value: Route.settings) {
                            Label("Settings", systemImage: "gearshape")
                        }
                    } else if !store.people.isEmpty && state.kind != .clear {
                        NavigationLink(value: Route.onCampus) {
                            HStack {
                                Text(state.kind == .occupied ? "See who's here" : "Last known list")
                                Spacer()
                                Image(systemName: "chevron.right").font(.footnote.weight(.semibold))
                            }
                        }
                    }

                    if let updated = state.updated {
                        Text("Updated \(Fmt.ago(updated, now: context.date))")
                            .font(.footnote)
                            .foregroundStyle(Theme.secondary)
                    }
                    if let error = store.lastError, state.kind != .notSetUp {
                        Text(error)
                            .font(.footnote)
                            .foregroundStyle(Theme.secondary)
                            .multilineTextAlignment(.center)
                    }
                }
            }
        }
        .navigationTitle("Who's Here")
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                NavigationLink(value: Route.settings) {
                    Image(systemName: "gearshape")
                        .accessibilityLabel("Settings")
                }
            }
        }
        .navigationDestination(for: Route.self) { route in
            switch route {
            case .onCampus: OnCampusView()
            case .person(let person): PersonView(person: person)
            case .settings: SettingsView()
            }
        }
        .task {
            // Refresh every 30 seconds while the app is on screen.
            while !Task.isCancelled {
                await store.refresh()
                try? await Task.sleep(for: .seconds(30))
            }
        }
    }
}

struct VerdictCard: View {
    let state: DisplayState

    var body: some View {
        content
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 18).fill(Theme.background(for: state.kind)))
            .accessibilityElement(children: .combine)
    }

    @ViewBuilder private var content: some View {
        switch state.kind {
        case .clear:
            VStack(spacing: 6) {
                Image(systemName: "checkmark")
                    .font(.system(size: 26, weight: .bold))
                    .foregroundStyle(.black)
                    .frame(width: 52, height: 52)
                    .background(Circle().fill(Theme.green))
                Text(state.headline)
                    .font(.system(size: 20, weight: .bold))
                    .foregroundStyle(Theme.green)
                    .multilineTextAlignment(.center)
                Text(state.detail)
                    .font(.footnote)
            }
            .frame(maxWidth: .infinity)
        case .loading:
            VStack(spacing: 6) {
                ProgressView()
                Text(state.headline).font(.footnote)
            }
            .frame(maxWidth: .infinity)
        default:
            VStack(alignment: .leading, spacing: 4) {
                Image(systemName: Theme.symbol(for: state.kind))
                    .font(.title3)
                    .foregroundStyle(Theme.color(for: state.kind))
                Text(state.headline)
                    .font(.system(size: 22, weight: .bold))
                    .foregroundStyle(Theme.color(for: state.kind))
                Text(state.detail)
                    .font(.system(size: 15, weight: .medium))
                if state.kind == .cantConfirm {
                    Text("Don't arm yet. Check the building.")
                        .font(.footnote)
                        .foregroundStyle(Theme.secondary)
                }
            }
        }
    }
}
