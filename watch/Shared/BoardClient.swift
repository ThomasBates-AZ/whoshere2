import Foundation

/// Where the board is, shared between the app and its complications through the App Group.
struct BoardConfig: Equatable {
    var address: String
    var token: String

    static func load() -> BoardConfig {
        let defaults = SharedStore.defaults
        let builtIn = (Bundle.main.object(forInfoDictionaryKey: "WHDefaultBoardURL") as? String) ?? ""
        return BoardConfig(address: defaults.string(forKey: "boardAddress") ?? builtIn,
                           token: defaults.string(forKey: "apiToken") ?? "")
    }

    func save() {
        SharedStore.defaults.set(address.trimmingCharacters(in: .whitespacesAndNewlines), forKey: "boardAddress")
        SharedStore.defaults.set(token.trimmingCharacters(in: .whitespacesAndNewlines), forKey: "apiToken")
    }

    /// "192.168.1.20:8085" or "http://whoshere.local:8085/" → http://192.168.1.20:8085
    var baseURL: URL? {
        var text = address.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        if !text.contains("://") { text = "http://" + text }
        while text.hasSuffix("/") { text.removeLast() }
        guard let url = URL(string: text), url.host != nil,
              ["http", "https"].contains(url.scheme?.lowercased() ?? "") else { return nil }
        return url
    }
}

/// App Group storage: settings plus the last answer, so complications can show it without the app.
enum SharedStore {
    static let defaults: UserDefaults = {
        if let group = Bundle.main.object(forInfoDictionaryKey: "WHAppGroup") as? String, !group.isEmpty,
           let shared = UserDefaults(suiteName: group) {
            return shared
        }
        return .standard
    }()

    private static let cacheKey = "cachedStatus"

    static func loadCached() -> CachedStatus? {
        guard let data = defaults.data(forKey: cacheKey) else { return nil }
        return try? JSONDecoder().decode(CachedStatus.self, from: data)
    }

    static func saveCached(_ cached: CachedStatus) {
        if let data = try? JSONEncoder().encode(cached) {
            defaults.set(data, forKey: cacheKey)
        }
    }

    static func clearCache() {
        defaults.removeObject(forKey: cacheKey)
    }
}

enum BoardError: LocalizedError {
    case notConfigured
    case needsToken
    case http(Int)
    case unreachable(String)
    case badResponse

    var errorDescription: String? {
        switch self {
        case .notConfigured: return "Add the board's address in Settings."
        case .needsToken: return "The board wants an API token. Add it in Settings."
        case .http(let code): return "The board answered with error \(code)."
        case .unreachable(let why): return "Can't reach the board: \(why)"
        case .badResponse: return "That address answered, but not the way Who's Here does."
        }
    }
}

/// Talks to the Who's Here server.
struct BoardClient {
    var config: BoardConfig = .load()

    private static let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }()

    func fetchStatus() async throws -> BoardStatus {
        let data = try await send(try request("api/status"))
        do {
            return try Self.decoder.decode(BoardStatus.self, from: data)
        } catch {
            throw BoardError.badResponse
        }
    }

    /// Tells the board this watch's push token. Returns whether the board has notifications set up.
    func register(pushToken: String, name: String) async throws -> Bool {
        let body = try JSONSerialization.data(withJSONObject: ["token": pushToken, "name": name])
        let data = try await send(try request("api/push/register", method: "POST", body: body))
        struct Reply: Decodable { var pushEnabled: Bool? }
        return (try? Self.decoder.decode(Reply.self, from: data).pushEnabled) ?? false
    }

    private func request(_ path: String, method: String = "GET", body: Data? = nil) throws -> URLRequest {
        guard let base = config.baseURL else { throw BoardError.notConfigured }
        var request = URLRequest(url: base.appendingPathComponent(path), cachePolicy: .reloadIgnoringLocalCacheData,
                                 timeoutInterval: 10)
        request.httpMethod = method
        if !config.token.isEmpty {
            request.setValue("Bearer \(config.token)", forHTTPHeaderField: "Authorization")
        }
        if let body {
            request.httpBody = body
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        return request
    }

    private func send(_ request: URLRequest) async throws -> Data {
        let result: (Data, URLResponse)
        do {
            result = try await URLSession.shared.data(for: request)
        } catch let error as URLError {
            throw BoardError.unreachable(error.localizedDescription)
        }
        let (data, response) = result
        guard let http = response as? HTTPURLResponse else { throw BoardError.badResponse }
        if http.statusCode == 401 { throw BoardError.needsToken }
        guard (200..<300).contains(http.statusCode) else { throw BoardError.http(http.statusCode) }
        return data
    }
}
