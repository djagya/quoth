-- Owner/assistant vocabulary for stored values.
-- origin: danil_statement -> owner_statement, danil_vault -> owner_note,
--         sera_proposal -> assistant_proposal, builtin_mirror -> imported.
-- proposer / actor / suggested_by: 'danil' -> 'owner', 'sera' -> 'assistant',
--         'sera:<context>' -> 'assistant:<context>'; every other value is kept.
-- SQLite cannot change a CHECK in place, so `claims` is rebuilt with the same ids, columns,
-- indexes and triggers. The runner applies this with foreign keys off and checks them
-- before commit, so references from ledger_events and claim_suggestions stay valid.
CREATE TABLE claims_v4 (
    id                INTEGER PRIMARY KEY,
    text              TEXT NOT NULL CHECK (length(trim(text)) > 0),
    kind              TEXT NOT NULL CHECK (kind IN ('identity', 'preference', 'consent', 'correction',
                          'environment', 'person', 'taste', 'project_state', 'episode', 'pointer',
                          'unclassified')),
    -- Who the content comes from: the owner's own words or notes, the assistant's
    -- inference, or text imported from the assistant's built-in memory files.
    origin            TEXT NOT NULL CHECK (origin IN ('owner_statement', 'owner_note',
                          'assistant_proposal', 'imported')),
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
INSERT INTO claims_v4(id, text, kind, origin, proposer, subject_key, reason, evidence_quote,
                      evidence_locator, quote_verified, confidence, scope, review_status,
                      auto_accepted, supersedes, valid_from, valid_to, recorded_at, reviewed_at,
                      review_note, superseded_at, superseded_by, forget_reason, last_used_at,
                      use_count)
SELECT id, text, kind,
       CASE origin WHEN 'danil_statement' THEN 'owner_statement'
                   WHEN 'danil_vault' THEN 'owner_note'
                   WHEN 'sera_proposal' THEN 'assistant_proposal'
                   WHEN 'builtin_mirror' THEN 'imported'
                   ELSE origin END,
       CASE WHEN proposer = 'danil' THEN 'owner'
            WHEN proposer = 'sera' THEN 'assistant'
            WHEN substr(proposer, 1, 5) = 'sera:' THEN 'assistant:' || substr(proposer, 6)
            ELSE proposer END,
       subject_key, reason, evidence_quote, evidence_locator, quote_verified, confidence, scope,
       review_status, auto_accepted, supersedes, valid_from, valid_to, recorded_at, reviewed_at,
       review_note, superseded_at, superseded_by, forget_reason, last_used_at, use_count
FROM claims ORDER BY id;
DROP TABLE claims;
ALTER TABLE claims_v4 RENAME TO claims;
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

-- The audit log is append-only; its guard is lifted for this one rewrite and restored.
DROP TRIGGER ledger_events_no_update;
UPDATE ledger_events SET actor = CASE WHEN actor = 'danil' THEN 'owner'
                                      WHEN actor = 'sera' THEN 'assistant'
                                      ELSE 'assistant:' || substr(actor, 6) END
WHERE actor IN ('danil', 'sera') OR substr(actor, 1, 5) = 'sera:';
-- `proposed` and `re_proposed` events record the claim's origin in their JSON detail.
UPDATE ledger_events SET detail = json_set(detail, '$.origin',
           CASE json_extract(detail, '$.origin') WHEN 'danil_statement' THEN 'owner_statement'
                                                 WHEN 'danil_vault' THEN 'owner_note'
                                                 WHEN 'sera_proposal' THEN 'assistant_proposal'
                                                 ELSE 'imported' END)
WHERE json_valid(detail)
  AND json_extract(detail, '$.origin') IN ('danil_statement', 'danil_vault', 'sera_proposal',
                                           'builtin_mirror');
CREATE TRIGGER ledger_events_no_update BEFORE UPDATE ON ledger_events
BEGIN SELECT RAISE(ABORT, 'ledger_events is append-only'); END;

UPDATE claim_suggestions SET suggested_by = CASE WHEN suggested_by = 'danil' THEN 'owner'
                                                 WHEN suggested_by = 'sera' THEN 'assistant'
                                                 ELSE 'assistant:' || substr(suggested_by, 6) END
WHERE suggested_by IN ('danil', 'sera') OR substr(suggested_by, 1, 5) = 'sera:';
