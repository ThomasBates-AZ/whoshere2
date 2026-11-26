import UserNotifications
import WatchKit
import WidgetKit

final class AppDelegate: NSObject, WKApplicationDelegate, UNUserNotificationCenterDelegate {
    func applicationDidFinishLaunching() {
        UNUserNotificationCenter.current().delegate = self
        Task { @MainActor in
            await PushRegistrar.shared.start()
        }
    }

    func didRegisterForRemoteNotifications(withDeviceToken deviceToken: Data) {
        let hex = deviceToken.map { String(format: "%02x", $0) }.joined()
        Task { @MainActor in
            await PushRegistrar.shared.didRegister(token: hex)
        }
    }

    func didFailToRegisterForRemoteNotificationsWithError(_ error: Error) {
        Task { @MainActor in
            PushRegistrar.shared.didFail(error)
        }
    }

    /// The board's notifications are marked content-available, so watchOS wakes the app briefly:
    /// fetch the new answer so the complications change along with the notification.
    func didReceiveRemoteNotification(_ userInfo: [AnyHashable: Any],
                                      fetchCompletionHandler completionHandler: @escaping (WKBackgroundFetchResult) -> Void) {
        Task { @MainActor in
            await BoardStore.shared.refresh()
            WidgetCenter.shared.reloadAllTimelines()
            completionHandler(.newData)
        }
    }

    /// A notification while the app is open: still show it, and refresh the screen.
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        Task { @MainActor in
            await BoardStore.shared.refresh()
        }
        completionHandler([.banner, .list, .sound])
    }
}
