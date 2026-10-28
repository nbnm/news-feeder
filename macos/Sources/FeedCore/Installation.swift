import Foundation

public func startupBackendURL(environmentURL: String?, savedURL: String?, savedDemoMode: Bool,
                              hasBundledBackend: Bool) -> String {
    if let environmentURL { return environmentURL }
    let selected = savedURL ?? "http://127.0.0.1:8000"
    // Early development launches forced the live URL through the environment,
    // leaving the original demo address in defaults. Retire only that known demo;
    // custom and remote backend choices remain intact.
    if hasBundledBackend, savedDemoMode, let url = URL(string: selected),
       url.scheme == "http", ["127.0.0.1", "localhost"].contains(url.host ?? ""), url.port == 8765 {
        return "http://127.0.0.1:8000"
    }
    return selected
}
