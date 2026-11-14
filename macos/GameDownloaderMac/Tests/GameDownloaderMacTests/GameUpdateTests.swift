import Foundation
import XCTest
@testable import GameDownloaderMac

final class GameUpdateTests: XCTestCase {
    func testUpdateLinksSurviveCatalogRoundTrip() throws {
        let input = #"{"id":"fixture","name":"Fixture","zipUrl":"https://example.com/base","updateZipUrl":"https://example.com/update","updateZipBackupUrl":"https://example.com/backup"}"#.data(using: .utf8)!
        let game = try JSONDecoder().decode(Game.self, from: input)
        let data = try JSONEncoder().encode(game)
        let encoded = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertNotNil(encoded["updateZipUrl"])
        XCTAssertNotNil(encoded["updateZipBackupUrl"])
    }
}
