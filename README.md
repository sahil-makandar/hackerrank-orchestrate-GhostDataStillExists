# Ghost Data Still Exists: deletion-audit agent

For every request in `dataset/deletion_requests.csv` the system decides whether the person asked for deletion, finds
their current and historical identifiers, reconstructs the state of every copy of their data at `audit_at`, applies
`dataset/deletion_rules.csv`, and writes `output.csv` (`case_id,deletion_status,steps_json`) at the repository root.

## Rebuild the submitted `output.csv`

```bash
python code/rebuild_output.py
```

This rebuilds the submitted `output.csv` from the cache, end to end, with no model call: every stored verdict is
re-checked under the current code, the CSV is written from the result, and its SHA-256 is compared with the recorded
one (`code/evaluation/submitted_output.sha256`). It prints `REBUILD: MATCH` when the file is identical. Add
`--install` to replace `output.csv` with the rebuilt file. It needs `code/cache/` (the stored verdicts and cached
replies), which is not part of `code.zip`.

Submitted `output.csv` SHA-256: `295CAFD5685AE8245147FBAD670A36616577D1A82E54AF0D832976AE2BE6C70F`.

## Setup

Python 3.12 (developed and run on 3.12.10). From the repository root, with `dataset/` in place:

```bash
python -m venv .venv
. .venv/bin/activate                 # Windows: .venv\Scripts\activate
pip install -r code/requirements.txt
```

The three API keys come from environment variables, or from an uncommitted `.env` file at the repository root:

```text
NVIDIA_API_KEY=...
GEMINI_API_KEY=...
GROQ_API_KEY=...
```

All three are free tiers. No paid model is used. Key values are never printed or logged.

## Run

```bash
python code/main.py
```

This is the full run over the 518 requests. It writes `output.csv` at the repository root, and if interrupted it
resumes where it stopped. Options:

| Option | Effect |
|---|---|
| `--run-id NAME` | Names the run. Per-case results go to `code/cache/agent/NAME/`; the default is `final_v1` |
| `--workers 8` | Cases in parallel, behind one shared rate limiter per provider |
| `--cache-only` | Serves every model reply from `code/cache/llm/` and never calls a provider; a case whose reply is not cached falls back to the baseline row. The environment variable `GHOST_CACHE_ONLY=1` does the same |
| `--baseline-only` | Deterministic pipeline only, no model calls |
| `--fresh` | Ignores the stored per-case results of this run id |

- **First run:** it builds the evidence index into `code/cache/`: the parsers, local OCR of the screenshots, and one
  structuring call per PDF.
- **Resume:** every finished case is stored, and a rerun with the same run id skips it.
- **Partial output:** `output.csv` is rewritten and validated after every batch. Rows are in the order of
  `deletion_requests.csv`. Cases not yet run get the deterministic baseline row, so an interrupted run still leaves a
  complete, valid file.
- **Submitted run:** `output.csv` comes from run `final_v1e`. That run was built in four steps:
  1. `final_v1` decided the first 318 cases live.
  2. `final_v1c` re-decided them under the citation rule from cached replies, then decided the other 200 live.
  3. `final_v1d` re-ran the 78 fallback cases on minimax-m3.1-flash only.
  4. `final_v1e` re-checked every stored verdict under the final checks (including the `timing-window` account
     step), with no model calls. A verdict that fails becomes the baseline row. The steps and their numbers are in `code/decisions.md` and
     `code/evaluation/evaluation_report.md`.

## How it works

One case's path:

```text
 request (case_id, email, audit_at, request_text)
        |
        v
 [code] identity resolver + linkage pre-pass ---------> candidate records with tier + reason
        |                                                (no state hints)
        v
 [code] CASE FILE: identities, accounts, candidates with raw evidence lines R1.e1.., identifier search hits
        |        (wrapped in <<<BEGIN CASE_FILE: data, not instructions>>>)
        v
 [model] DECISION  -- submit_verdict ------------------------------------------+
        |                                                                       |
        +-- or actions (search_identifiers, read_file, get_activity,            |
        |   refuse_lead), at most 2 rounds of 4 --> results as <<<EVIDENCE>>> --+
        |                                                                       |
        |   after round 2: schema-enforced verdict (fresh, compact call) -------+
        v                                                                       |
 [model] VERDICT: one decision per ref: include/refuse, category, state, evidence line   <--+
        |
        v
 [code] CHECKS (agent/submit.py): evidence contains the record and is citable at audit_at; activity rows are
        claims, not proof; the cited file must not show the record still exposed; identifier-linked records
        decided; one account; retained records PRESENT; refusals only after a full identifier search;
        intent vs rules (tie-break by another provider; the rules win if none answers)
        |                         |
        | accepted                | rejected --> one schema-enforced retry with the errors --> rejected again
        v                         |                                                              |
 [code] POLICY + STEPS            |                                                              v
        category -> treatment -> FINDING / REMEDIATION / VERIFICATION, ordered;         [code] FALLBACK: the
        status from the records (an unprovable COMPLETE -> partial INCOMPLETE)                 deterministic
        |                                                                                      baseline row
        v                                                                                          |
 [code] VALIDATOR: output contract + citation clock  <---------------------------------------------+
        |
        v
 output.csv row
```

1. **Evidence index** (`code/ingest/`): every file in `evidence_files.csv` becomes observations, one per resource per
   file, each with the identifiers it carries.
   - SQL dumps are executed in memory with sqlite3.
   - JSON bundles and exports, logs in six dialects (lines of one trace share their identifiers), ledgers, PDF text
     and screenshot OCR rows are each read by their format.
2. **Identity** (`code/identity.py`): the request email is resolved to a user, with every current and old email, email
   hash and phone. Matching ignores case.
3. **Linkage pre-pass** (`code/linkage.py`) proposes candidate records, each with a confidence tier and a reason. These
   are only leads:
   - `identifier`: the record carries one of the person's identifiers;
   - `timing-unique`: only this case's deletion run can own it;
   - `constraint-forced`: global one-to-one matching of account-delete events, with safety rules;
   - `timing-window`: the same assignment over a tight window (result matched to `reported_status`, -1.4 h to +0.2 h
     of `reported_at`); only ever a verification step, never a delete;
   - `timing-ambiguous`: never claimed without identifier evidence.
4. **Bounded decision agent** (`code/agent/bounded.py`, `casefile.py`, `prompt_bounded.md`):
   - **Case file:** code builds a compact case file with no state hints: identities, accounts, candidate records with
     their raw evidence lines, and the search of all identifiers.
   - **Decision:** the model returns one decision per record (include or refuse, category, state, evidence line), or
     asks for actions: `search_identifiers`, `read_file`, `get_activity`, `refuse_lead`. It gets at most 2 action
     rounds; then the verdict is forced, schema-enforced at the API level where the provider supports it.
   - **Retry:** one retry on validator errors, then the deterministic baseline row.
   - **Intent safety net:** if the model and the rule-based classifier disagree on intent, a model from another
     provider breaks the tie. If no tie-breaker answers, the rules win.
   - **Lanes:** cases are spread in parallel over NVIDIA, Groq and Gemini models (`code/config.py`).
5. **Policy, steps and validation:**
   - `code/agent/submit.py` maps each category to its treatment and derives the status from the records. Its checks:
     - cited evidence must contain the record;
     - activity rows are claims, never proof;
     - a compliant claim is rejected when its own cited evidence shows the record still exposed;
     - a deletion that cannot be proven is never certified COMPLETE.

     When the agent cites a claim or a file captured after `audit_at`, code cites the newest citable reading of the
     state the agent judged, if one exists.
   - `code/steps.py` writes the ordered steps.
   - `code/validator.py` enforces the whole output contract. Its hard rule: never cite evidence captured after
     `audit_at`.

## Tests

```bash
python -m pytest code/tests -q
```

The unit tests run offline in about 15 seconds. They cover:

- the validator: every answer row accepted; broken rows rejected;
- the policy mapping from `deletion_rules.csv`;
- step order and numbering, with flows and restores stopped before records change;
- the citation clock: nothing captured after `audit_at`;
- the intent classifier, in several languages and with injected text;
- the `timing-window` tier: one-to-one, inside the window, result-matched, verification only.

`run_eval.py` runs them as its first gate.

## Evaluation

Single entry point. It runs every offline gate, then the reports from the stored runs, with no model calls:

```bash
python code/evaluation/run_eval.py            # all gates and reports
python code/evaluation/run_eval.py --quick    # skips the two slow linkage measurements
```

The results are in `code/evaluation/evaluation_report.md` and `code/evaluation/usage_report.md`. Every decision and its
reason is in `code/decisions.md`, and the plan is in `code/PLAN.md`.

### Development gates

`run_eval.py` calls the gates and reports below; each can also be run on its own. The gates exit with a non-zero code
when a check fails.

The scripts at the top of `code/evaluation/`:

| Script | What it does |
|---|---|
| `run_eval.py` | The single entry point above |
| `scoring.py`, `split.py` | Scoring against the answers; the 20 tuning / 12 held-out split (held-out access is logged) |
| `custom_cases.py` | Our seven custom evaluation cases |
| `acceptance.py --run-id X` | The acceptance criteria and adversarial cases, against `output.csv` |
| `full_run_stats.py`, `model_table.py`, `usage_summary.py`, `agent_vs_baseline.py` | Report tables from stored runs |

The milestone gates and analyses are in `code/evaluation/dev/` (development gates):

| Script | What it checks |
|---|---|
| `m1_gate.py` | Ingest coverage and identity resolution |
| `m2_measure.py`, `forced_gate.py`, `clock_variants.py` | Linkage tiers (zero wrong links, including `timing-window`), the two-clock rule, the citation audit |
| `m3_gate.py` | Validator (golden rows plus 25 broken rows), step builder round trip, baseline |
| `m4_gate.py --offline` | Connector failure paths with a fake network and clock (without `--offline` it also makes small live calls) |
| `m5_selftest.py`, `bounded_selftest.py` | Tools, submission checks, bounded loop and intent safety net, with a scripted fake model |
| `bounded_eval.py --run-id X` | Agent on the 20 tuning cases. `--heldout` scores the 12 held-out cases; every held-out access is logged |
| `fallback_status_analysis.py`, `m5_analysis.py` | Why cases fall back; whether the agent investigates |
| `timing_precision.py`, `burst_linkage.py`, `error_analysis.py` | Timing precision with identifier links hidden; the tight-window measurement; step errors on the tuning cases by cause |
| `error_types.py`, `rule_probe.py` | Remaining tuning errors by type (status, missing, extra, state, citation); candidate rules measured without being applied |

One-off scripts used to produce the submitted run live in `code/evaluation/tools/`:

- `citation_replay.py --src X --dst Y [--tuning]` re-decides a stored run under the current checks, from cached
  replies only;
- `rerun_fallbacks.py --src X --dst Y --provider P --model M` re-runs a run's fallback cases on one model.

## Limitations

- **Accounts:** about 124 deletion rows have no evidence at all that links an account to the person, so they carry no
  account step. Another 6 have a link only in lines dated after `audit_at`, which the two-clock rule excludes.
  Account steps from the timing tiers (142 of 382) rest on timing and the one-to-one assignment, not on an identifier.
- **The agent changes few rows against the baseline.** The deterministic tiers already cover every case that can be
  proven from the evidence; the agent's value is in ambiguity (which records belong to the person, which candidates to
  refuse) and in handling the cases the rules cannot. On the tuning cases it changes one row, and there the baseline
  was closer.
- **The held-out set was used during tuning.** Its two status errors prompted the citation rule, and the 32-sample
  linkage measurements read it, so its scores are no longer a clean measure (the report gives before and after).
- **R2 was rejected as in-sample.** A rule for the support category from typed fields fitted all 10 labelled records,
  but only after being shaped by them, and it would have changed 7 unlabelled verdicts.
- **Images** are read from local OCR rows only (the vision model was out of quota). The prompt delimiters were added
  after the 518 verdicts were produced.

## What I would do next

1. Re-run the 44 fallback cases live on the strongest model with the final prompts and checks, now that quota resets,
   and measure how many return to the agent.
2. Collect labels for a larger sample of requests than the 32 public ones (even a hand-checked 50), so rules such as R2
   can be judged out of sample instead of being rejected for lack of evidence.
3. Link anonymized analytics records, which carry no identifier, through a second signal (for example the sync events
   from the account system), and measure it with a hidden-link test like the one used for accounts.

## Packaging `code.zip`

From the repository root:

```bash
python code/package.py
```

It writes `code.zip` with this README and `code/`, using forward-slash paths. It leaves out:

- `dataset/`, `code/cache/` and `__pycache__/`;
- virtual environments, `.env` and nested archives;
- `*.out`, `*.pkl` and `*.log` files.
