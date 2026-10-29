-- Guided review: suggestions gain a short topic label, the `covered` action (already in
-- the owner's notes, with the vault locator that covers it) and that locator. SQLite cannot
-- widen a CHECK in place, so the table is rebuilt; every row and replaced_by link is kept.
CREATE TABLE claim_suggestions_v3 (
    id                INTEGER PRIMARY KEY,
    claim_id          INTEGER NOT NULL REFERENCES claims(id),
    action            TEXT NOT NULL CHECK (action IN ('accept', 'reject', 'duplicate', 'stale', 'keep',
                          'covered')),
    suggested_kind    TEXT,
    rewrite           TEXT,
    duplicate_of      INTEGER REFERENCES claims(id),
    note              TEXT,
    suggested_by      TEXT NOT NULL,
    suggested_at      TEXT NOT NULL,
    replaced_by       INTEGER REFERENCES claim_suggestions_v3(id),
    topic             TEXT CHECK (topic IS NULL OR length(topic) BETWEEN 1 AND 40),
    evidence_locator  TEXT CHECK (action != 'covered' OR coalesce(length(evidence_locator), 0) > 0)
);
INSERT INTO claim_suggestions_v3(id, claim_id, action, suggested_kind, rewrite, duplicate_of, note,
                                 suggested_by, suggested_at, replaced_by)
SELECT id, claim_id, action, suggested_kind, rewrite, duplicate_of, note, suggested_by, suggested_at,
       replaced_by FROM claim_suggestions;
DROP TABLE claim_suggestions;
ALTER TABLE claim_suggestions_v3 RENAME TO claim_suggestions;
CREATE INDEX claim_suggestions_current ON claim_suggestions(claim_id) WHERE replaced_by IS NULL;
-- Review decisions carry their batch (the approving message's locator) in ledger_events.detail;
-- undo finds a batch's decisions there.
CREATE INDEX ledger_events_action ON ledger_events(action);
