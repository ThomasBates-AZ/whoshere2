import UserNotifications
import WatchKit

/// Asks for notification permission, gets this watch's push token from Apple, and gives it to the board.
@MainActor
final class PushRegistrar: ObservableObject {
    static let shared = PushRegistrar()

    @Published private(set) var statusText = "Checking…"

    private var token: String? {
        get { SharedStore.defaults.string(forKey: "pushToken") }
        set { SharedStore.defaults.set(newValue, forKey: "pushToken") }
    }

    func start() async {
        do {
            let granted = try await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound])
            if granted {
                WKApplication.shared().registerForRemoteNotifications()
            } else {
                statusText = "Off. Turn on notifications for Who's Here in the Watch app on your iPhone."
            }
        } catch {
            statusText = "Can't ask for notifications: \(error.localizedDescription)"
        }
    }

    func didRegister(token: String) async {
        self.token = token
        await resend()
    }

    func didFail(_ error: Error) {
        statusText = "Apple didn't give this watch a push token: \(error.localizedDescription)"
    }

    /// Sends the token again; also how the board learns the watch is still in use.
    func resend() async {
        guard let token else { return }
        let config = BoardConfig.load()
        guard config.baseURL != nil else {
            statusText = "Add the board's address to get notifications."
            return
        }
        do {
            let enabled = try await BoardClient(config: config)
                .register(pushToken: token, name: WKInterfaceDevice.current().name)
            statusText = enabled
                ? "On. You'll get a tap when it's all clear to arm."
                : "This watch is registered, but notifications aren't set up on the board yet (Admin → Apple Watch)."
        } catch {
            statusText = "Couldn't tell the board about this watch. \(error.localizedDescription)"
        }
    }
}
