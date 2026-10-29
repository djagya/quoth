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
CREATE TABLE source_versions (
    id            INTEGER PRIMARY KEY,
    source_id     INTEGER NOT NULL REFERENCES sources(id),
    content_hash  TEXT NOT NULL,
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

-- History is append-only: notes, versions and chunks are never deleted.
CREATE TRIGGER sources_no_delete BEFORE DELETE ON sources
BEGIN SELECT RAISE(ABORT, 'sources are append-only'); END;
CREATE TRIGGER source_versions_no_delete BEFORE DELETE ON source_versions
BEGIN SELECT RAISE(ABORT, 'source_versions are append-only'); END;
CREATE TRIGGER chunks_no_delete BEFORE DELETE ON chunks
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
