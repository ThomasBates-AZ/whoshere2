import SwiftUI

@main
struct WhosHereApp: App {
    @WKApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var store = BoardStore.shared
    @StateObject private var push = PushRegistrar.shared
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            NavigationStack {
                HomeView()
            }
            .environmentObject(store)
            .environmentObject(push)
            .tint(Theme.tint)
        }
        .onChange(of: scenePhase) { _, phase in
            guard phase == .active else { return }
            Task {
                await store.refresh()
                await push.resend()
            }
        }
    }
}

enum Route: Hashable {
    case onCampus
    case person(OnSitePerson)
    case settings
}
