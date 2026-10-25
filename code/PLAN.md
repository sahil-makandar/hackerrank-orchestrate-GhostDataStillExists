# Ghost Data Still Exists: final design

This file describes the system as built. Every decision and its reason, including the path that led here, is in
`decisions.md`. The numbers are in `evaluation/evaluation_report.md`.

## Task

For each of the 518 requests in `dataset/deletion_requests.csv`, write `case_id,deletion_status,steps_json` to the
root `output.csv`:

1. Does the text authorise deletion? If not, the row is NOT_APPLICABLE with `[]`.
2. Who is the person? Find every current and historical email, email hash and phone.
3. Find every copy of their data, using only information available at `audit_at`.
4. Apply `deletion_rules.csv` to each record.
5. COMPLETE needs cited verification steps. Otherwise the row is INCOMPLETE: findings (cited), then remediation, then
   verification (no evidence).

## Rules the system keeps (user decisions)

- **Judgement belongs to the agent.** It decides intent, identity, each record's state and category, and how to settle
  conflicting evidence. Code builds the case file, applies the policy, orders the steps and validates them.
- **No shortcuts.** No case_id ranges or case-family shortcuts. NOT_APPLICABLE comes only from the request text.
- **Two clocks.** An observation's own time decides what is known at `audit_at`; the file's capture time decides what
  may be cited. Nothing captured after `audit_at` is ever cited.
- **Timing links.** Timing-ambiguous links are never claimed without identifier evidence. Timing-unique,
  constraint-forced and timing-window links stay, because each passed a zero-wrong gate. A timing-window account is
  only ever a verification step, never a delete.
- **Never certify what can't be proven.** An unprovable deletion becomes a partial INCOMPLETE row, `[]` only if
  nothing can be stated.
- **Intent.** If the model and the rules disagree, a model from another provider breaks the tie; if none answers, the
  rules win.
- **Fallback.** If the agent fails a case, that case gets the deterministic baseline row. A COMPLETE fallback row must
  have only cited verification steps and pass the validator.
- **Held-out.** The 32 samples split into 20 tuning and 12 held-out cases. The held-out cases are no longer clean (see
  the evaluation report).
- **Cost.** Free tiers only.

## Architecture (`code/`)

| Part | File(s) | Role |
|---|---|---|
| Evidence index | `ingest/` | SQL (sqlite3), JSON, six log dialects with trace joining, ledgers, PDFs (text, then one structuring call), screenshots (local OCR) become observations per resource and file |
| Identity | `identity.py` | email → user, with every historical identifier, matched without regard to case |
| Linkage pre-pass | `linkage.py` | candidates with a tier and a reason: identifier, timing-unique, constraint-forced, timing-window, timing-ambiguous |
| State reader | `state.py` | readings per copy; replicas are separate copies; only readings after the request count |
| Policy and steps | `policy.py`, `steps.py` | category → treatment → finding, remediation and verification operations, in the required order |
| Validator | `validator.py` | the whole output contract, plus the citation clock |
| Baseline | `baseline.py` | deterministic pipeline: the per-case fallback |
| Agent | `agent/bounded.py`, `casefile.py`, `tools.py`, `prompt_bounded.md` | bounded decision agent (below) |
| Submission checks | `agent/submit.py` | what the agent claims is checked against the evidence; status derived by code |
| Connector | `llm/client.py` | lanes, fallbacks, rate limits, quota parking, disk cache, usage log, cache-only mode |
| Config | `config.py` | lanes, enforced-verdict order, token caps, wait limits |

**Bounded decision agent.**

- **Case file:** code builds it without state hints: identities, accounts, candidate records with their raw evidence
  lines, and the search of all identifiers.
- **Verdict:** the model makes one decision per record reference, or asks for actions (`search_identifiers`,
  `read_file`, `get_activity`, `refuse_lead`).
- **Limits:** at most 2 action rounds, then a schema-enforced verdict; one retry, at most 5 calls; then the baseline.
- **Lanes:** cases are spread in parallel over NVIDIA (minimax-m3.1-flash, nemotron-3-ultra), Gemini and Groq.

**Submission checks.**

- Evidence must contain the record, and must be citable at `audit_at`.
- Activity rows are claims, never proof.
- A compliant claim is rejected when its own cited evidence shows the record still exposed.
- Retained records can't be ABSENT.
- Every ambiguous candidate must be decided, and a refusal needs a search of every identifier.
- When the agent cites a claim or a later file, code cites the newest citable reading of the state the agent judged.

## Gates (all offline; `python code/evaluation/run_eval.py`)

| Gate | Pass condition |
|---|---|
| M1 `m1_gate` | every manifest file parsed; every resource id in the evidence indexed; all 550 request emails resolved |
| M2 `m2_measure`, `forced_gate`, `clock_variants` | 0 wrong timing-unique, constraint-forced and timing-window links; citation audit clean |
| M3 `m3_gate` | validator accepts the 32 golden rows and rejects 25 broken rows; the step builder rebuilds the golden rows |
| M4 `m4_gate --offline` | connector failure paths, cache and usage log |
| M5 `m5_selftest`, `bounded_selftest` | tools, submission checks, bounded loop, retry, budget, intent safety net |
| M7 `custom_cases`, `acceptance`, `rebuild_output.py` | 7 custom cases; the acceptance criteria and adversarial cases on `output.csv` |

## Final run

`output.csv` comes from run `final_v1e`. That run was built in four steps:

1. `final_v1`: the first 318 cases, live.
2. `final_v1c`: the citation rule applied by cached replay, then the other 200 cases live.
3. `final_v1d`: the 78 fallback cases re-run on minimax-m3.1-flash only.
4. `final_v1e`: every stored verdict re-checked under the final checks, with no model calls; one that fails becomes the
   baseline row.

Result: 518 rows, 474 from the agent and 44 baseline fallbacks (8.5%). `python code/rebuild_output.py` rebuilds the file
from the cache and checks its SHA-256.
