-- Mutable core-memory ledger (separate file from the immutable evidence snapshot).
-- Typed bitemporal claims: forgetting/superseding sets valid_to/superseded_by, never DELETE.
CREATE TABLE claims (
    id                INTEGER PRIMARY KEY,
    text              TEXT NOT NULL CHECK (length(trim(text)) > 0),
    kind              TEXT NOT NULL CHECK (kind IN ('identity', 'preference', 'consent', 'correction',
                          'environment', 'person', 'taste', 'project_state', 'episode', 'pointer',
                          'unclassified')),
    -- Who the content comes from (values renamed in 0004).
    origin            TEXT NOT NULL CHECK (origin IN ('danil_statement', 'danil_vault',
                          'sera_proposal', 'builtin_mirror')),
    proposer          TEXT NOT NULL CHECK (length(proposer) > 0),
    subject_key       TEXT,
    reason            TEXT,
    evidence_quote    TEXT,
    evidence_locator  TEXT,
    quote_verified    INTEGER NOT NULL DEFAULT 0 CHECK (quote_verified IN (0, 1)),
    confidence        REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    scope             TEXT NOT NULL DEFAULT 'general' CHECK (length(scope) > 0),
    review_status     TEXT NOT NULL DEFAULT 'pending'
                          CHECK (review_status IN ('pending', 'accepted', 'rejected', 'expired')),
    auto_accepted     INTEGER NOT NULL DEFAULT 0 CHECK (auto_accepted IN (0, 1)),
    supersedes        INTEGER REFERENCES claims(id),
    valid_from        TEXT,
    valid_to          TEXT,
    recorded_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    reviewed_at       TEXT,
    review_note       TEXT,
    superseded_at     TEXT,
    superseded_by     INTEGER REFERENCES claims(id),
    forget_reason     TEXT,
    last_used_at      TEXT,
    use_count         INTEGER NOT NULL DEFAULT 0 CHECK (use_count >= 0)
);
CREATE INDEX claims_status ON claims(review_status);
CREATE INDEX claims_kind ON claims(kind);
CREATE INDEX claims_subject ON claims(subject_key);
CREATE TRIGGER claims_no_delete BEFORE DELETE ON claims
BEGIN SELECT RAISE(ABORT, 'claims are never deleted; set valid_to/superseded_by'); END;
-- What was claimed, and from where, is immutable: an edit is a new superseding claim.
CREATE TRIGGER claims_content_immutable
BEFORE UPDATE OF text, kind, origin, proposer, evidence_quote, evidence_locator, quote_verified,
                 recorded_at ON claims
BEGIN SELECT RAISE(ABORT, 'claim content is immutable; record a superseding claim'); END;

-- Append-only audit of every ledger decision.
CREATE TABLE ledger_events (
    id        INTEGER PRIMARY KEY,
    claim_id  INTEGER REFERENCES claims(id),
    action    TEXT NOT NULL,
    actor     TEXT NOT NULL,
    at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    detail    TEXT
);
CREATE INDEX ledger_events_claim ON ledger_events(claim_id);
CREATE INDEX ledger_events_at ON ledger_events(at);
CREATE TRIGGER ledger_events_no_update BEFORE UPDATE ON ledger_events
BEGIN SELECT RAISE(ABORT, 'ledger_events is append-only'); END;
CREATE TRIGGER ledger_events_no_delete BEFORE DELETE ON ledger_events
BEGIN SELECT RAISE(ABORT, 'ledger_events is append-only'); END;

-- Compiled core history (each published core text, append-only).
CREATE TABLE core_versions (
    id          INTEGER PRIMARY KEY,
    sha256      TEXT NOT NULL UNIQUE,
    content     TEXT NOT NULL,
    claim_ids   TEXT NOT NULL,
    budget      INTEGER NOT NULL,
    compiled_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TRIGGER core_versions_no_update BEFORE UPDATE ON core_versions
BEGIN SELECT RAISE(ABORT, 'core_versions is append-only'); END;
CREATE TRIGGER core_versions_no_delete BEFORE DELETE ON core_versions
BEGIN SELECT RAISE(ABORT, 'core_versions is append-only'); END;
