"""Claims gold labelling: machine proposals the owner confirms (never confirmed by itself).

`quoth eval label-claims SET --ledger DB --out NEWSET` scores the ledger's accepted current
claims against each answerable row's question (dense with a local embedding model when one
loads, else word overlap) and writes NEWSET: a copy of SET where every row that got
candidates is `provenance.review: pending` (not scored) and carries them in
`meta.claims_label`. Nothing leaves the machine: the ledger is opened read-only, NEWSET is
written `private` (mode 0600) and the command prints counts only.

`quoth eval review-claims NEWSET [--ledger DB]` walks the pending rows in a terminal (plain
`input()`, like `eval review`): the owner picks the right claims by number, says none is
right, dismisses the row unjudged, skips or quits. A decision restores the row's own
provenance, adds the picked claims as anchors (`{"claim": {"text", "id", "subject_key"?}}`)
with `expect.claims_judged: true`, and rewrites NEWSET at once.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3

from .claims_gold import Resolver
from .format import EvalSetError, load_set, review, row_errors, scored, write_set

DEFAULT_TOP = 5
DEFAULT_MODEL = "bge-m3"
AUTHOR = "script:label-claims"
ACTIONS = "claims: numbers, comma-separated | n none is right | d dismiss (not judged) | s skip | q quit"


class LabelError(ValueError):
    pass


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def accepted_claims(ledger) -> list[dict]:
    """Accepted current claims of a ledger file: what prefetch may inject (read-only)."""
    resolver = Resolver.open(ledger)
    try:
        return [{"id": r["id"], "text": r["text"], "subject_key": r["subject_key"], "kind": r["kind"]}
                for r in resolver.conn.execute(
                    "SELECT id, text, subject_key, kind FROM claims WHERE review_status = 'accepted'"
                    " AND valid_to IS NULL AND superseded_by IS NULL ORDER BY id")]
    finally:
        resolver.close()


class Scorer:
    """Claim-to-question scores: cosine with an embedder, else the gate's word coverage."""

    def __init__(self, claims: list[dict], model: str | None = DEFAULT_MODEL, embedder=None):
        from .. import relevance
        self.claims = claims
        self.relevance = relevance
        self.embedder = embedder
        self.why_lexical = None
        if self.embedder is None and model:
            from ..embed.backends import EmbedderUnavailable, get_embedder
            try:
                self.embedder = get_embedder(model)
            except (EmbedderUnavailable, ValueError) as exc:
                self.why_lexical = type(exc).__name__
        self.name = f"dense:{self.embedder.spec.name}" if self.embedder is not None else "lexical"
        self.matrix = None
        if self.embedder is not None and claims:
            self.matrix = self.embedder.embed_passages([c["text"] for c in claims])

    def top(self, question: str, k: int, min_score: float | None = None) -> list[dict]:
        terms = self.relevance.terms(question)
        lexical = [self.relevance.coverage(terms, c["text"]) or 0.0 for c in self.claims]
        if self.matrix is not None:
            import numpy as np
            scores = [float(x) for x in self.matrix @ np.asarray(self.embedder.embed_query(question))]
        else:
            scores = lexical
        order = sorted(range(len(self.claims)), key=lambda i: (-scores[i], -self.claims[i]["id"]))
        out = []
        for i in order[:k]:
            # No floor by default with a model (the owner judges); words need one shared word.
            if (min_score is not None and scores[i] < min_score) or (self.matrix is None and scores[i] <= 0):
                continue
            c = self.claims[i]
            item = {"id": c["id"], "text": c["text"], "score": round(scores[i], 4), "lexical": round(lexical[i], 4)}
            if c.get("subject_key"):
                item["subject_key"] = c["subject_key"]
            out.append(item)
        return out


def _has_claims_gold(row: dict) -> bool:
    expect = row["expect"]
    return bool(expect.get("claims_judged")) or any(s["store"] == "claims" for s in expect["sources"])


def propose(set_path, ledger, out, *, model: str | None = DEFAULT_MODEL, top: int = DEFAULT_TOP,
            min_score: float | None = None, privacy: str = "private", embedder=None) -> dict:
    """Write NEWSET with claims candidates for every answerable, scored row that has no claims
    gold yet. Returns counts (no text)."""
    set_path, out = Path(set_path), Path(out)
    if out.resolve() == set_path.resolve():
        raise LabelError("write the proposals to a new set (--out), not over the input")
    if top < 1:
        raise LabelError("--top must be at least 1")
    evalset = load_set(set_path)
    if privacy == "public" and evalset.manifest.get("privacy") != "public":
        raise LabelError("a private set's proposals stay private")
    claims = accepted_claims(ledger)
    scorer = Scorer(claims, model=model, embedder=embedder)
    now = _now()
    rows, summary = [], {"rows": len(evalset.rows), "claims": len(claims), "scorer": scorer.name,
                         "pending": 0, "no_candidates": 0, "already_labelled": 0, "abstain": 0,
                         "not_scored": 0, "grouped": 0}
    if scorer.why_lexical:
        summary["dense_unavailable"] = scorer.why_lexical
    for row in evalset.rows:
        if not scored(row):
            summary["not_scored"] += 1
        elif row["expect"]["abstain"]:
            summary["abstain"] += 1
        elif _has_claims_gold(row):
            summary["already_labelled"] += 1
        elif row["expect"].get("groups"):
            summary["grouped"] += 1  # all-of rows: a claim is not one of their groups
        else:
            candidates = scorer.top(row["question"], top, min_score) if claims else []
            if not candidates:
                summary["no_candidates"] += 1
            else:
                prior = row.get("provenance")
                meta = dict(row.get("meta") or {})
                meta["claims_label"] = {"state": "pending", "scorer": scorer.name, "proposed_at": now,
                                        "prior_provenance": prior, "candidates": candidates}
                row = {**row, "provenance": {**(prior or {"author": AUTHOR}), "review": "pending"}, "meta": meta}
                summary["pending"] += 1
        rows.append(row)
    write_set(rows, out, privacy=privacy, fixture=evalset.manifest["fixture"])
    return summary


# ---------------------------------------------------------------- review

def pending_rows(rows: list[dict]) -> list[int]:
    return [i for i, r in enumerate(rows)
            if review(r) == "pending" and ((r.get("meta") or {}).get("claims_label") or {}).get("state") == "pending"]


def anchor(candidate: dict) -> dict:
    a = {"text": candidate["text"], "id": candidate["id"]}
    if candidate.get("subject_key"):
        a["subject_key"] = candidate["subject_key"]
    return a


def decide(row: dict, picked: list[dict] | None, *, by: str = "owner", at: str | None = None) -> dict:
    """The row after the owner's decision: `picked` claims (possibly none: no claim is right),
    or None to dismiss it unjudged. Its own provenance comes back either way."""
    label = row["meta"]["claims_label"]
    out = {k: v for k, v in row.items() if k != "provenance"}
    if label.get("prior_provenance"):
        out["provenance"] = label["prior_provenance"]
    meta = {k: v for k, v in row["meta"].items() if k != "claims_label"}
    done = {"state": "dismissed" if picked is None else "confirmed", "scorer": label.get("scorer"),
            "proposed_at": label.get("proposed_at"), "proposed": len(label.get("candidates") or []),
            "by": by, "at": at or _now()}
    if picked is not None:
        expect = dict(row["expect"])
        known = {s["match"]["claim"].get("id") for s in expect["sources"]
                 if s["store"] == "claims" and "claim" in s["match"]}
        expect["sources"] = expect["sources"] + [{"store": "claims", "match": {"claim": anchor(c)}}
                                                 for c in picked if c["id"] not in known]
        expect["claims_judged"] = True
        out["expect"] = expect
        done["chosen"] = len(picked)
    meta["claims_label"] = done
    out["meta"] = meta
    return out


def _show(row: dict, n: int, total: int, say) -> None:
    label = row["meta"]["claims_label"]
    say("")
    say(f"--- row {n}/{total}  {row['id']}  [{row['kind']}]  scorer {label.get('scorer')}")
    say(f"question: {row['question']}")
    vault = [s["match"].get("path") for s in row["expect"]["sources"] if s["store"] == "vault"]
    if vault:
        say("vault gold: " + ", ".join(p for p in vault if p))
    for i, c in enumerate(label["candidates"], 1):
        key = f"  [{c['subject_key']}]" if c.get("subject_key") else ""
        say(f"  {i}. #{c['id']} ({c['score']:.3f}) {c['text']}{key}")


def _parse(answer: str, n: int) -> tuple[list[int] | None, str | None]:
    tokens = [t for t in re.split(r"[,\s]+", answer) if t]
    if not tokens or not all(t.isdigit() for t in tokens):
        return None, ACTIONS
    picks = []
    for t in tokens:
        i = int(t)
        if not 1 <= i <= n:
            return None, f"no claim numbered {i}"
        if i not in picks:
            picks.append(i)
    return picks, None


def review_claims(set_path, *, ledger=None, by: str = "owner", ask=input, say=print) -> dict:
    """The review loop over NEWSET's pending rows; rewrites the set after each decision.
    With `ledger`, a picked claim must still be accepted and current there."""
    set_path = Path(set_path)
    evalset = load_set(set_path)
    rows = list(evalset.rows)
    privacy, fixture = evalset.manifest["privacy"], evalset.manifest["fixture"]
    resolver = Resolver.open(ledger) if ledger else None
    todo = pending_rows(rows)
    summary = {"pending": len(todo), "confirmed": 0, "none_right": 0, "dismissed": 0, "skipped": 0}
    if not todo:
        say("no rows to review")
    say(ACTIONS)
    try:
        for n, idx in enumerate(todo, 1):
            row = rows[idx]
            candidates = row["meta"]["claims_label"]["candidates"]
            _show(row, n, len(todo), say)
            while True:
                try:
                    answer = ask("> ").strip().lower()
                except EOFError:
                    answer = "q"
                if answer in ("q", "s"):
                    break
                if answer == "n":
                    picked = []
                elif answer == "d":
                    picked = None
                else:
                    nums, why = _parse(answer, len(candidates))
                    if nums is None:
                        say(why)
                        continue
                    picked = [candidates[i - 1] for i in nums]
                    if resolver is not None:
                        gone = [c["id"] for c in picked if not resolver.resolve(anchor(c))["ids"]]
                        if gone:
                            say("no longer accepted and current in the ledger: " + ", ".join(f"#{i}" for i in gone))
                            continue
                new = decide(row, picked, by=by)
                errors = row_errors(new)
                if errors:
                    say("row not valid, kept pending: " + "; ".join(errors))
                    answer = "s"
                    break
                rows[idx] = new
                try:
                    write_set(rows, set_path, privacy=privacy, fixture=fixture)
                except (EvalSetError, OSError) as exc:
                    rows[idx] = row
                    say(f"could not write the set, kept pending: {exc}")
                    answer = "s"
                    break
                key = "dismissed" if picked is None else ("confirmed" if picked else "none_right")
                summary[key] += 1
                say({"dismissed": "dismissed: not judged", "none_right": "judged: no claim is right",
                     "confirmed": f"judged: {len(picked or [])} claim(s) right"}[key])
                break
            if answer == "q":
                break
            if answer == "s":
                summary["skipped"] += 1
    finally:
        if resolver is not None:
            resolver.close()
    summary["remaining"] = len(pending_rows(rows))
    return summary


def label_errors(ledger) -> None:
    """Fail early with a plain reason when the ledger cannot be read."""
    try:
        Resolver.open(ledger).close()
    except (OSError, sqlite3.Error) as exc:
        raise LabelError(f"cannot read the ledger: {type(exc).__name__}") from exc
