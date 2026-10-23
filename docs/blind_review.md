# Scenario-only blind review packet

`data/eval/blind_review_packet.jsonl` contains 20 scenarios sampled from the golden
candidates: three from each of the first four alphabetically ordered families and two
from each remaining family. The packet keeps only a fresh packet ID, scenario facts,
and `as_of_date`. It omits original case IDs,
family names, expected entitlements, governing clauses, refusal labels, and source
notes so reviewers do not see the answer key.

On 2026-10-23, the synthetic golden cases `gs-031`–`gs-040`, `gs-042`, and `gs-049`
were completed with explicit no-disability and no disability-related accessibility-change
facts. These are stipulated fixture values, not observations or evidence about real
passengers. The packet was regenerated to keep its sampled `gs-032` scenario in sync;
the existing `blind-010` review predates this completion and did not independently
review those added facts.

**Human adjudication is pending.** This packet has not been independently reviewed,
scored, or changed based on an adjudicator. Store completed decisions separately and
keep this source packet unchanged for reproducibility.

Regenerate with:

```bash
.venv/bin/python scripts/build_blind_review_packet.py
```
