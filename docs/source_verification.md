# Regulatory source verification

Verified on **2026-10-08** against primary U.S. government sources.

This note records source-backed product logic for Rerouted. It is not legal advice and does not predict how DOT would exercise case-specific enforcement discretion.

## Decision 1: July 1–6, 2026 is an uncovered published-policy interval

Rerouted will treat **2026-07-01 through 2026-07-06** as a real gap between the two published, generally applicable renumbered-flight enforcement-discretion notices.

The first notice, FR Doc. 2025-22140 (90 FR 55999), says the pause began December 5, 2025 and continued **“until June 30, 2026.”** The second notice, FR Doc. 2026-13675 (91 FR 41556), says **“As of July 7, 2026”** DOT was extending the pause. It also describes the new period as one year from publication and says it expires July 7, 2027. The second notice contains no language making the extension retroactive or backdating it to July 1.

The second notice's summary calls the action an extension of the “current” discretion. That general description does not override its specific `DATES` text or its express acknowledgement that the earlier notice ran only through June 30. The product therefore treats July 1–6 as having **no published renumbering-specific enforcement pause**. This label is more precise than claiming DOT was certain to bring an enforcement action.

The carve-out is narrow in both notices. It applies only when a flight is merely renumbered, the passenger is rebooked under the new number, and there is no regulatory “significant change or delay.” Refund obligations remain in the CFR during either pause; the notices change stated enforcement posture, not the underlying entitlement.

### Product intervals and endpoint convention

Configuration uses half-open intervals, `[start, end_exclusive)`, to eliminate boundary ambiguity:

| Product interval | Stored range | Passenger-facing meaning |
|---|---|---|
| Baseline posture | before `2025-12-05` | No generally applicable renumbering carve-out from these notices |
| First limited pause | `[2025-12-05, 2026-07-01)` | Published renumbering carve-out applies through June 30 |
| Published-policy gap | `[2026-07-01, 2026-07-07)` | No published renumbering-specific carve-out |
| Extended limited pause | `[2026-07-07, 2027-07-08)` | Product interprets “expiring on July 7, 2027” as inclusive of July 7 |
| Refresh required | from `2027-07-08` | Do not infer posture without rechecking later DOT action or Refund III |

Recommended boundary fixtures are `2025-12-04`, `2025-12-05`, `2026-06-30`, `2026-07-01`, `2026-07-06`, and `2026-07-07`. The four-date golden narrative should retain `2025-12-04`, `2026-06-30`, `2026-07-03`, and `2026-07-07`; the July 3 case remains an in-gap case.

### Primary sources

- [FR Doc. 2025-22140, published December 5, 2025](https://www.federalregister.gov/documents/2025/12/05/2025-22140/airline-refunds-and-other-consumer-protections)
- [Official GovInfo PDF for FR Doc. 2025-22140](https://www.govinfo.gov/content/pkg/FR-2025-12-05/pdf/2025-22140.pdf)
- [FR Doc. 2026-13675, published July 7, 2026](https://www.federalregister.gov/documents/2026/07/07/2026-13675/airline-refunds-and-other-consumer-protections)
- [Official GovInfo PDF for FR Doc. 2026-13675](https://www.govinfo.gov/content/pkg/FR-2026-07-07/pdf/2026-13675.pdf)

## Decision 2: Refund III has not published

As of this verification date, **Airline Refunds and Other Consumer Protections III, RIN 2105-AF36, has not published as a proposed or final rule**.

The current Unified Agenda record still lists Refund III at the **Proposed Rule Stage**. Its timetable shows an anticipated NPRM date of August 2026 but supplies no Federal Register citation. A Federal Register search by RIN returned only the two enforcement-discretion notices, not a Refund III NPRM or final rule. The July 7 notice likewise says the rulemaking “remains pending.” This was rechecked on October 22, 2026: the eCFR page was current through October 21 and still attributed the operative Part 260 text to the 2024 Refund I and Refund II rulemakings.

Consequences for Rerouted:

- Do not add a fifth posture regime or a Refund III CFR-text version.
- Keep the regulation-text layer and enforcement-posture layer separate.
- Preserve the existing definition of `cancelled flight` in 14 CFR 260.2.
- Recheck the Federal Register, Unified Agenda, and eCFR before changing these conclusions or ingesting data dated after this verification.

### Primary sources

- [Unified Agenda record for RIN 2105-AF36, publication ID 2026](https://www.reginfo.gov/public/do/eAgendaViewRule?RIN=2105-AF36&pubId=202510)
- [Federal Register search for RIN 2105-AF36](https://www.federalregister.gov/documents/search?conditions%5Bterm%5D=2105-AF36)
- [Current 14 CFR Part 260](https://www.ecfr.gov/current/title-14/chapter-II/subchapter-A/part-260)
- [FR Doc. 2026-13675 discussion of pending Refund III](https://www.federalregister.gov/documents/2026/07/07/2026-13675/airline-refunds-and-other-consumer-protections)

## Implementation guardrail

The resolver must always return entitlement and enforcement posture as separate fields. A limited enforcement pause must never be converted into `NOT_ELIGIBLE`: the regulation may still make the refund owed even when DOT has announced that it will not enforce the cited requirements in the notice's narrow circumstances.
