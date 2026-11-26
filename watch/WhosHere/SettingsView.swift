import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var store: BoardStore
    @EnvironmentObject private var push: PushRegistrar
    @State private var address = ""
    @State private var token = ""
    @State private var result: String?
    @State private var saving = false

    var body: some View {
        Form {
            Section {
                TextField("192.168.1.20:8085", text: $address)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
            } header: {
                Text("Board address")
            } footer: {
                Text("The address you open the board at in a browser.")
            }

            Section {
                SecureField("Optional", text: $token)
            } header: {
                Text("API token")
            } footer: {
                Text("Only needed if the board has API_TOKEN set.")
            }

            Section {
                Button(saving ? "Checking…" : "Save and test") {
                    Task { await save() }
                }
                .disabled(saving)
                if let result {
                    Text(result).font(.footnote)
                }
            }

            Section("Notifications") {
                Text(push.statusText).font(.footnote)
            }

            Section {
                LabeledContent("Version", value: Self.version)
            }
        }
        .navigationTitle("Settings")
        .onAppear {
            address = store.config.address
            token = store.config.token
        }
    }

    private func save() async {
        let new = BoardConfig(address: address, token: token)
        guard new.baseURL != nil else {
            result = "That address doesn't look right. Try something like 192.168.1.20:8085."
            return
        }
        saving = true
        defer { saving = false }
        if let error = await store.updateConfig(new) {
            result = error
        } else {
            result = "Connected. \(store.state().headline)."
        }
    }

    private static let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "?"
}
