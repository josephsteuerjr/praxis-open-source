# Praxis memory runtime

This directory is mounted as private runtime state. Public source contains only
this contract and synthetic templates; real people, rooms, runs, journals,
computer evidence and access grants are never published.

Canonical memory is readable Markdown plus append-only JSONL with provenance.
`INDEX.md` and `maps/` are bounded, grep-friendly navigation views. SQLite under
`.state/` and optional vectors under `.vectors/` are disposable query indexes:
they may be rebuilt from canon and never outrank it.

Raw journal entries are episodic logs, not identity, policy or verified facts.
Normative self text is loaded only from a provenance-valid
`soul/self/CURRENT.md`; quarantined legacy self files are explicit archive
evidence and are not automatic prompt input.
