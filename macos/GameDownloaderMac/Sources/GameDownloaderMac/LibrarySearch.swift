import Foundation

enum LibrarySearch {
    static func matches(query: String, name: String, description: String?) -> Bool {
        let terms = query.split(whereSeparator: \.isWhitespace)
        guard !terms.isEmpty else { return true }
        let searchableText = [name, description ?? ""].joined(separator: " ")
        return terms.allSatisfy { term in
            searchableText.range(of: String(term), options: [.caseInsensitive, .diacriticInsensitive],
                                 locale: .current) != nil
        }
    }
}
