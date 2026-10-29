import CryptoKit
import Foundation
import Security

struct PairLink: Codable, Equatable {
    let url: URL
    let host: String

    var fingerprint: String?

    init?(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: trimmed), url.scheme?.lowercased() == "https",
              let host = url.host, PairLink.isPrivateIPv4(host) else { return nil }
        let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        let fp = items.first(where: { $0.name == "fp" })?.value?.lowercased()
        self.url = url
        self.host = host
        self.fingerprint = (fp?.count == 64 && fp!.allSatisfy(\.isHexDigit)) ? fp : nil
    }

    static func isPrivateIPv4(_ host: String) -> Bool {
        let parts = host.split(separator: ".").compactMap { Int($0) }
        guard parts.count == 4, parts.allSatisfy({ (0...255).contains($0) }) else { return false }
        switch (parts[0], parts[1]) {
        case (10, _), (192, 168): return true
        case (172, 16...31): return true
        case (169, 254): return true
        default: return false
        }
    }

    mutating func accepts(_ trust: SecTrust) -> Bool {
        guard let leaf = PairLink.leafCertificate(trust) else { return false }
        let der = SecCertificateCopyData(leaf) as Data
        let hex = SHA256.hash(data: der).map { String(format: "%02x", $0) }.joined()
        if let expected = fingerprint { return expected == hex }
        fingerprint = hex
        return true
    }

    private static func leafCertificate(_ trust: SecTrust) -> SecCertificate? {
        (SecTrustCopyCertificateChain(trust) as? [SecCertificate])?.first
    }

    private static let key = "pairLink.v1"
    static func load() -> PairLink? {
        guard let data = UserDefaults.standard.data(forKey: key) else { return nil }
        return try? JSONDecoder().decode(PairLink.self, from: data)
    }
    func save() {
        if let data = try? JSONEncoder().encode(self) { UserDefaults.standard.set(data, forKey: PairLink.key) }
    }
    static func forget() { UserDefaults.standard.removeObject(forKey: key) }
}
