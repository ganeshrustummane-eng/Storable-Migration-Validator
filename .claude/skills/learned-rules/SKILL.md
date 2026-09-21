---
name: learned-rules
description: "Use when working on the learned-rules feedback loop: saving/activating/deactivating a learned rule (src/rule_book.py), recording a human correction to an AI/fuzzy column-mapping decision (src/learning/feedback.py), or anything touching rule_book_learned.json. Distinct from base-rules-datatypes (the static, immutable rules_catalog.json base rule set). Files: src/rule_book.py, src/learning/feedback.py, src/learning/retrieval.py, rule_book_learned.json."
---

# Learned rules -- the mutable, human-gated feedback loop

## Scope boundary vs. base-rules-datatypes

`src/rule_book.py`'s `RuleBook` class serves BOTH the static base-rule registry
(covered by the **base-rules-datatypes** skill) and this learned-rules mechanism.
This skill covers only the learned/mutable half: everything that reads or writes
`rule_book_learned.json`. Base rules (from `src/rules/rules_catalog.json` +
`src/rules/base_rules.py`) are immutable and out of scope here -- never edit them
as part of a "learned rule" change.

## What actually gets learned, and how it's gated

There are two independent kinds of "learning," both persisted into the same file
(`rule_book_learned.json`) under different top-level keys, with explicit
read-merge-write logic in each writer so neither clobbers the other's key
(`RuleBook._flush_learned()`):

1. **Gap-filler rules** (`rule_book.py`, key `learned_rules`) -- a new
   source-type/target-type pair that no base rule covers specifically. Lifecycle:
   - `save_learned_rule()` persists a new `RuleEntry` with `status="draft"` by
     default. A draft rule is advisory only -- it is never consulted during real
     SQL generation.
   - `_validate_sql_template()` runs on every saved learned rule's
     `pg_sql_template`/`sf_sql_template` and rejects `;`, comment markers
     (`--`, `/*`, `*/`), and DDL/DML keywords (`DROP, DELETE, ALTER, TRUNCATE,
     INSERT, UPDATE, GRANT, REVOKE, EXEC, EXECUTE, CREATE, MERGE, CALL`). Base
     rules get no equivalent check -- they're trusted, pre-tested code; a learned
     rule is untrusted input by construction.
   - `activate_learned_rule()` is the only way a learned rule can ever affect real
     SQL generation, and it requires `reuses_rule` to already point at a real,
     existing base-rule id. Once active, `get_rule_for_type()`'s gap-filler lookup
     (`_find_active_gap_filler()`) returns the **base rule's own template** that
     `reuses_rule` points to -- never the learned rule's own SQL text. This is the
     "anti-hallucination guard": a learned rule can only ever replay an
     already-tested base rule for a previously-uncovered type pair, never inject
     novel custom SQL into generation.
   - `deactivate_learned_rule()` flips it back to inactive without deleting it.
   - Base rules always win: if a base rule specifically covers a type pair, the
     gap-filler lookup is never consulted for that pair, active or not.

2. **Column-mapping corrections** (`src/learning/feedback.py`, key
   `learned_corrections`) -- a human correcting an AI/fuzzy schema-mapping decision
   ("wrong rule" or "wrong target column" for a given source column). Recorded via
   `FeedbackRecorder.record()` -> `MismatchFeedback` -> `_persist()`. There's also an
   interactive CLI prompt, `prompt_for_feedback()`, for capturing this at the
   command line. `src/learning/retrieval.py`'s `LearnedRuleRetriever`/
   `LearnedExample` read these back at matching time to give a correctly-mapped
   pair a confidence boost in future schema-mapping runs (consumed by
   `rule_book.py`'s matching path, not by the gap-filler SQL-generation path above).

## The shared-file coupling is real and fragile -- be careful

Both writers touch the same physical file (`rule_book_learned.json`) but own
different top-level keys. If you add a new field or a new writer to either
`rule_book.py` or `feedback.py`, make sure it still round-trips through a
read-existing -> merge -> write-whole-file cycle rather than overwriting the file
wholesale, or you will silently delete the other mechanism's data. Check
`_flush_learned()`'s merge behavior before changing either writer.

## Known documentation drift -- do not trust blindly

`docs/rules/rule-book.md`'s "Learned Rules" section describes a lifecycle with
`approved`/`rejected` states, a `RULE_ADMIN` role, and a `VersionStore` concept.
**None of that exists in `rule_book.py`.** The real lifecycle is `draft` -> `active`
only, gated by `activate_learned_rule()`/`deactivate_learned_rule()`, with no role
concept found anywhere in the code. Treat that doc section as aspirational/stale
until someone either updates the doc or builds what it describes -- don't assume
the richer workflow exists just because it's documented.

## Verification checklist

- [ ] Never let a learned rule's own SQL template execute directly in generation --
  only `reuses_rule` replay of an already-active base rule is allowed.
- [ ] Never activate a learned rule whose `reuses_rule` doesn't resolve to a real
  base-rule id -- `save_learned_rule()` should already block this; don't bypass it.
- [ ] Confirm a base rule doesn't already specifically cover the type pair before
  treating something as a "gap" needing a learned rule.
- [ ] After any change to `rule_book.py` or `feedback.py`'s write path, verify
  `rule_book_learned.json`'s OTHER top-level key survives a round trip untouched.
- [ ] Don't rewrite `docs/rules/rule-book.md`'s learned-rule lifecycle claims into
  code without the user asking for that -- that's a feature-build, not a doc fix.
- [ ] `py_compile` any touched `.py` file before calling the change done.
