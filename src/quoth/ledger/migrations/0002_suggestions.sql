-- Non-binding review suggestions (presort): what the assistant or an operator thinks the
-- owner should do with a pending claim. They never change a claim; the owner's approval
-- applies them.
CREATE TABLE claim_suggestions (
    id              INTEGER PRIMARY KEY,
    claim_id        INTEGER NOT NULL REFERENCES claims(id),
    action          TEXT NOT NULL CHECK (action IN ('accept', 'reject', 'duplicate', 'stale', 'keep')),
    suggested_kind  TEXT,
    rewrite         TEXT,
    duplicate_of    INTEGER REFERENCES claims(id),
    note            TEXT,
    suggested_by    TEXT NOT NULL,
    suggested_at    TEXT NOT NULL,
    replaced_by     INTEGER REFERENCES claim_suggestions(id)
);
CREATE INDEX claim_suggestions_current ON claim_suggestions(claim_id) WHERE replaced_by IS NULL;
