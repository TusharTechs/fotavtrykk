# Refresh evidence

Two genuine live runs of the same 150 companies, hours apart — not a synthetic
fixture.

| file | what it is |
|---|---|
| `previous.jsonl` | the earlier run's terminal envelopes |
| `current.jsonl` | the later run's terminal envelopes |
| `changes.jsonl` | typed change events between them |
| `refresh-report.json` | the replay report, including the idempotency check |

Reproduce with no network access and no cost:

```bash
uv run fotavtrykk refresh \
  --previous artifact/refresh/previous.jsonl \
  --current  artifact/refresh/current.jsonl \
  --output   artifact/refresh/changes.jsonl \
  --report   artifact/refresh/refresh-report.json
```

The report records `profiles_fetched_this_run: 0`, `false_changes: 0`,
`idempotent_rerun: true` and `evidence_complete: true`. Idempotency is checked by
diffing the current snapshot against itself and requiring silence.

Every change carries evidence for **both** sides — source URL, retrieval time
and content hash before and after — so a reviewer can re-check either end.

A failed or blocked source never erases a value: it preserves the last supported
one, marks it stale and reports `source_unavailable`. Only a source that was read
successfully and no longer reports a value yields `became_unavailable`.
