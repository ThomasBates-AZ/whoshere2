# Who's Here for Apple Watch

The in/out board on your wrist. It's a watch-only app (no iPhone app needed) that reads Who's Here 2.0's `/api/status`.

- **Home:** the answer in big type: *All clear to arm*, *Not clear · 3 on campus*, or *Can't confirm*.
- **On campus:** who's here, where (each hotspot keeps its own color), and since when. People in the 10-minute departure grace period are marked *Leaving?*.
- **Person:** location, arrival time, last seen. The app never shows Wi-Fi network names.
- **Complications** (rectangular, circular, corner, inline; the rectangular one also works in the Smart Stack).
- **Notifications:** a tap on the wrist when it becomes all clear to arm, sent by the server through Apple's push service.

It's built to be safe to trust at the end of the night. Like the board, it never says "all clear" from an answer it can't vouch for: if the board's answer is more than a few minutes old (30 minutes for complications), it shows *Can't confirm* instead.

## What you need

- A Mac with **Xcode 16** or newer, and [Homebrew](https://brew.sh)
- A paid **Apple Developer Program** membership. Free accounts can't use push notifications or App Groups.
- An Apple Watch on **watchOS 10** or newer, with **Developer Mode** turned on (Settings → Privacy & Security → Developer Mode on the watch; it appears after the watch has been connected to Xcode once)
- Who's Here **3.7.0** or newer on the server

## Build it

1. Install XcodeGen: `brew install xcodegen`
2. Edit `project.yml` and set the three **CHANGE** settings:
   - `DEVELOPMENT_TEAM`: your Team ID
   - `WH_BUNDLE_ID`: a bundle ID you own, for example `org.ironwood.whoshere.watchkitapp`. Write it down: the server needs the same value.
   - `WH_BOARD_URL` (optional but recommended): the board's address, for example `http://192.168.1.20:8085`. Otherwise you type it into the app's Settings on the watch.
3. In this folder, run `xcodegen`. It creates `WhosHere.xcodeproj`; open it.
4. Pick the **WhosHere** scheme and your watch as the run destination, then **Run** (⌘R). The first install takes a while. Xcode's automatic signing registers the bundle IDs, the App Group (`group.<your bundle ID>`) and push notifications for you.
5. On the watch, allow notifications when asked.

To change the app later, edit the Swift files, bump `MARKETING_VERSION` in `project.yml` (minor for most changes, major for big ones), run `xcodegen` again, and add a line under **Revision history** below.

## Turn on notifications on the server

Follow **Apple Watch** in the server's [README](../whoshere2/README.md#apple-watch): make an APNs key, set `APNS_KEY_ID`, `APNS_TEAM_ID`, `APNS_BUNDLE_ID` (the `WH_BUNDLE_ID` above) and `APNS_SANDBOX=true`, upload the `.p8` key under **Admin → Apple Watch**, and send a test notification.

`APNS_SANDBOX=true` is right while the app is installed from Xcode. If you later distribute it with TestFlight, switch to `false`.

## Things to know

- **The watch reads the board directly**, so it has to be able to reach the server: on the campus network (the watch uses your iPhone's connection when they're together, or Wi-Fi networks your iPhone knows), or through a VPN such as UniFi Teleport. Off the network the home screen says *Can't confirm* and shows when it last heard from the board. Notifications still arrive anywhere.
- **Plain `http://` works with an IP address or a `.local` name.** Apple blocks unencrypted connections to other names (like `whoshere.example.org`). Use the IP address, or put the board behind HTTPS.
- **Complications** refresh about every 15 minutes, which is as often as watchOS allows, and right away when a notification arrives.
- **API token:** if the server has `API_TOKEN` set, enter it in the app's Settings.

## Troubleshooting

| What you see | Try |
|---|---|
| *Can't reach the board: … App Transport Security …* | The address is an `http://` hostname. Use the IP address instead. |
| *Can't reach the board: Could not connect to the server* | The watch isn't on the campus network, or the address or port is wrong. Open the same address in a browser on your iPhone. |
| *The board wants an API token* | Enter the server's `API_TOKEN` in Settings. |
| Settings says notifications aren't set up on the board | Finish the server steps above (Admin → Apple Watch). |
| Admin → Apple Watch shows `BadDeviceToken` | `APNS_SANDBOX` doesn't match how the app was installed (Xcode = `true`). |
| No watch listed on Admin → Apple Watch | Open the app once on the watch and allow notifications. If you said no, turn notifications on in the Watch app on your iPhone (Notifications → Who's Here). |

## Revision history

- **1.0.0** (2026-11-26): First version. Home screen with all-clear answer, on-campus list, person detail, settings, complications, and all-clear notifications.
