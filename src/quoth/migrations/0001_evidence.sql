-- The evidence database: vault notes, their versions and chunks, dense vectors, and the
-- lexical indexes behind `quoth search`. Every row is derived from the vault; a schema
-- change means rebuilding snapshots, never migrating them.

-- One row per vault note (vault-relative POSIX path). `scope` is a metadata label only.
CREATE TABLE sources (
    id     INTEGER PRIMARY KEY,
    path   TEXT NOT NULL UNIQUE,
    scope  TEXT NOT NULL CHECK (length(scope) > 0)
);

-- One row per distinct content of a note; frontmatter is kept as JSON in `metadata`.
-- `content_hash` is the SHA-256 of the file bytes that were indexed; `mtime_ns` and
-- `size_bytes` are the file's stat at its last ingest (kept current while the content is
-- unchanged), so a reader can tell whether the live file changed since indexing.
CREATE TABLE source_versions (
    id            INTEGER PRIMARY KEY,
    source_id     INTEGER NOT NULL REFERENCES sources(id),
    content_hash  TEXT NOT NULL,
    mtime_ns      INTEGER NOT NULL,
    size_bytes    INTEGER NOT NULL CHECK (size_bytes >= 0),
    ingested_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    is_current    INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
    metadata      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX source_versions_source ON source_versions(source_id, is_current);
-- At most one current version per note.
CREATE UNIQUE INDEX source_versions_one_current ON source_versions(source_id) WHERE is_current = 1;

-- Heading-aware passages with 1-based inclusive line locators into the note.
CREATE TABLE chunks (
    id                 INTEGER PRIMARY KEY,
    source_version_id  INTEGER NOT NULL REFERENCES source_versions(id),
    heading_path       TEXT NOT NULL DEFAULT '',
    line_start         INTEGER NOT NULL CHECK (line_start >= 1),
    line_end           INTEGER NOT NULL CHECK (line_end >= line_start),
    text               TEXT NOT NULL
);
CREATE INDEX chunks_version ON chunks(source_version_id);

-- Notes being purged because they became excluded (`.quothignore`, `quoth: exclude`,
-- built-in rules). Ingest fills it, deletes every row of those notes, then empties it.
CREATE TABLE purge_sources (
    source_id  INTEGER PRIMARY KEY
);

-- History is append-only: notes, versions and chunks are never deleted, except the
-- rows of a note being purged (listed in `purge_sources`).
CREATE TRIGGER sources_no_delete BEFORE DELETE ON sources
WHEN OLD.id NOT IN (SELECT source_id FROM purge_sources)
BEGIN SELECT RAISE(ABORT, 'sources are append-only'); END;
CREATE TRIGGER source_versions_no_delete BEFORE DELETE ON source_versions
WHEN OLD.source_id NOT IN (SELECT source_id FROM purge_sources)
BEGIN SELECT RAISE(ABORT, 'source_versions are append-only'); END;
CREATE TRIGGER chunks_no_delete BEFORE DELETE ON chunks
WHEN OLD.source_version_id NOT IN (
    SELECT v.id FROM source_versions v JOIN purge_sources p ON p.source_id = v.source_id)
BEGIN SELECT RAISE(ABORT, 'chunks are append-only'); END;

-- Dense vectors: little-endian float32, L2-normalised. Derived data; vectors of retired
-- versions are pruned before a snapshot is frozen.
CREATE TABLE embeddings (
    chunk_id  INTEGER NOT NULL REFERENCES chunks(id),
    model     TEXT NOT NULL,
    dim       INTEGER NOT NULL CHECK (dim > 0),
    vector    BLOB NOT NULL CHECK (length(vector) = 4 * dim),
    PRIMARY KEY (chunk_id, model)
);

-- Entity tokens per chunk (capitalized words, digit-bearing tokens) for `quoth search`.
CREATE TABLE entities (
    chunk_id  INTEGER NOT NULL REFERENCES chunks(id),
    entity    TEXT NOT NULL,
    cnt       INTEGER NOT NULL,
    PRIMARY KEY (chunk_id, entity)
) WITHOUT ROWID;
CREATE INDEX entities_entity ON entities(entity, cnt DESC);

-- Links from CURRENT chunks to other notes (wikilinks, embeds, relative Markdown links);
-- rows are removed when a version stops being current. `target` is the link target as
-- written (without heading and alias); `heading` the `#heading` part; `line` the 1-based
-- line in the linking note. `status` is 'resolved' (exactly one indexed note: its
-- vault-relative path is `target_path`), 'missing', 'ambiguous' or 'excluded' (the target
-- is a note excluded from the index; its path is never stored). Resolution is redone for
-- every current link on each ingest, so it follows notes being added and removed.
CREATE TABLE links (
    chunk_id     INTEGER NOT NULL REFERENCES chunks(id),
    target       TEXT NOT NULL,
    target_path  TEXT,
    heading      TEXT,
    line         INTEGER NOT NULL CHECK (line >= 1),
    kind         TEXT NOT NULL CHECK (kind IN ('wikilink', 'embed', 'markdown')),
    status       TEXT NOT NULL CHECK (status IN ('resolved', 'missing', 'ambiguous', 'excluded')),
    CHECK ((status = 'resolved') = (target_path IS NOT NULL))
);
CREATE INDEX links_chunk ON links(chunk_id);
CREATE INDEX links_target_path ON links(target_path) WHERE target_path IS NOT NULL;

-- Lexical indexes over CURRENT chunks only (rowid = chunks.id); rows are removed when a
-- version stops being current.
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    title, heading_path, text,
    tokenize = 'unicode61 remove_diacritics 2'
);
CREATE VIRTUAL TABLE chunks_fts_trigram USING fts5(
    title, heading_path, text,
    tokenize = 'trigram'
);
