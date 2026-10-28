import Foundation
import GRDB

// DatabaseQueue serializes every operation; codecs are local to each call.
// This cache may safely be used from background tasks without UI-thread I/O.
public final class FeedCache: @unchecked Sendable {
    private let database: DatabaseQueue

    public init(path: String) throws {
        database = try DatabaseQueue(path: path)
        var migrator = DatabaseMigrator()
        migrator.registerMigration("initialArchive") { db in
            try db.create(table: "articles") { table in
                table.column("id", .text).primaryKey()
                table.column("payload", .blob).notNull()
                table.column("isRead", .boolean).notNull().defaults(to: false)
                table.column("isSaved", .boolean).notNull().defaults(to: false)
            }
            try db.create(table: "snapshots") { table in
                table.column("name", .text).primaryKey()
                table.column("payload", .blob).notNull()
            }
        }
        try migrator.migrate(database)
    }

    public func merge(_ articles: [Article]) throws {
        try database.write { db in
            for article in articles {
                try db.execute(sql: """
                    INSERT INTO articles (id, payload) VALUES (?, ?)
                    ON CONFLICT(id) DO UPDATE SET payload = excluded.payload
                    WHERE articles.payload != excluded.payload
                    """, arguments: [article.id, try encodeSnapshot(article)])
            }
        }
    }

    public func articles() throws -> [Article] {
        try database.read { db in
            let decoder = makeDecoder()
            return try Row.fetchAll(db, sql: "SELECT * FROM articles").map { row in
                var article = try decoder.decode(Article.self, from: row["payload"] as Data)
                article.isRead = row["isRead"]
                article.isSaved = row["isSaved"]
                return (article, article.feedDate)
            }.sorted { $0.1 > $1.1 }.map(\.0)
        }
    }

    public func mark(_ id: String, read: Bool? = nil, saved: Bool? = nil) throws {
        try database.write { db in
            if let read {
                try db.execute(sql: "UPDATE articles SET isRead = ? WHERE id = ?", arguments: [read, id])
            }
            if let saved {
                try db.execute(sql: "UPDATE articles SET isSaved = ? WHERE id = ?", arguments: [saved, id])
            }
        }
    }

    public func store<T: Encodable>(_ value: T, named name: String) throws {
        try database.write { db in
            try db.execute(sql: """
                INSERT INTO snapshots (name, payload) VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET payload = excluded.payload
                WHERE snapshots.payload != excluded.payload
                """, arguments: [name, try encodeSnapshot(value)])
        }
    }

    public func load<T: Decodable>(_ type: T.Type, named name: String) throws -> T? {
        try database.read { db in
            guard let data = try Data.fetchOne(db, sql: "SELECT payload FROM snapshots WHERE name = ?",
                                              arguments: [name]) else { return nil }
            return try makeDecoder().decode(type, from: data)
        }
    }

    private func encodeSnapshot<T: Encodable>(_ value: T) throws -> Data {
        let encoder = makeEncoder()
        // Stable ordering makes equivalent snapshots byte-identical, including
        // nested objects, so background polls do not rewrite the archive.
        encoder.outputFormatting = .sortedKeys
        return try encoder.encode(value)
    }
}
