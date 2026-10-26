# Build prompt — `scripts/deployment_sanity.py`

## Goal

Build a **read-only, checklist-driven deployment sanity checker** for a JazzX instance.
It takes one environment URL, works out what is deployed there, runs a fixed checklist of
verifications, and reports which checklist items are **clean, dirty, or unknown**.

It is run **twice** — once before a deployment, once after — and the second run diffs
itself against the first. A check that passed before and fails after is the headline
output; a check that was already failing before is noise and must be labelled as
pre-existing, not as a regression.

## Non-negotiable constraints

1. **Read-only.** Every call is a `GET`, or a `POST` to an endpoint whose documented
   contract is validate/dry-run/compare only (e.g. `/bss_process/runtime/processes/validate`,
   `/bss_deployment/deployments/compare`). Never create, update, delete, publish or deploy.
2. **No loan is ever created.** Loan-creation sanity means proving the *preconditions* hold,
   not exercising the write path. See check group F.
3. **No secrets in output.** Tokens, connection strings and subscription IDs are redacted in
   every report and log line. Never write a token to a file.
4. **A missing capability is `unknown`, not `fail`.** If MLflow is unreachable or `az` is not
   logged in, that item is `unknown` with a stated reason. Only a check that ran and found a
   real problem is `fail`. This distinction is the whole point of the tool — do not collapse it.
5. **Never blocks.** One failing group must not stop the others. Run everything, report everything.

## Reuse what exists — do not reinvent

- `scripts/jazzx_api.py` — `JazzXClient` (bearer auth, retry/backoff, `iter_offset_pages`),
  `extract_items`, `parse_json_value`, `ApiError`, `require_value`, `normalize_gateway_url`,
  `validate_uuid`. Use it for every HTTP call.
- `scripts/audit_app_config_ids.py` — **this is the model.** Same docstring style, same
  read-only posture, same `--output-dir` JSON report, same two-instance comparison structure.
  The new script is its sibling. Match its conventions rather than introducing new ones.
- `requirements.txt` — `requests`, `pydantic`, `pypdf`. Add a dependency only if unavoidable.

Python 3.10+, stdlib + the above. No new framework.

## Inputs

```bash
python3 scripts/deployment_sanity.py \
    --gateway-url https://poc-gw.jazzx.co \
    --label poc \
    --output-dir sanity/ \
    [--baseline sanity/poc-before.json] \
    [--checks process,collections,traces,pipeline,loan] \
    [--collection-id <uuid>] \
    [--skip-azure]
```

- Token from `JAZZX_TOKEN` (or `--token`, documented as leaking into shell history).
- `--baseline` switches the run into **after mode**: same checklist, plus a diff section.
- `--checks` selects groups; default is all.
- Exit codes: `0` all clean · `1` at least one `fail` · `2` only `unknown`/degraded ·
  `3` the script itself could not run (bad URL, bad token, no reachable gateway).

## The checklist model

Everything the script does is one `CheckResult`. Define it once and make every group emit it:

```python
@dataclass
class CheckResult:
    group: str          # "deployment" | "process" | "collections" | "traces" | "pipeline" | "loan"
    id: str             # stable slug, e.g. "process.tool_bound.extract_income"
    title: str          # human sentence
    status: str         # "pass" | "fail" | "unknown" | "skipped"
    detail: str         # why — names the actual id/name/count that decided it
    evidence: dict      # the raw values the verdict rests on (redacted)
    remediation: str    # the one concrete next step when not pass
```

`id` must be **stable across runs** — the before/after diff joins on it. An id that
encodes a timestamp or a list index breaks the diff; encode the entity's name or UUID.

## Check groups

### A. Deployment identity (Azure)

Answer: *what is actually running at this URL, and did it change?*

1. Resolve the URL to its Azure resource. Try, in order, and record which one worked:
   - the instance's own build/version endpoint if one exists (probe `/health`, `/version`,
     `/api/v1/bss_process/api-health` — the last one is confirmed to exist);
   - `az containerapp show` / `az functionapp show` via the `az` CLI, matched by hostname;
   - a `--resource-group` / `--app-name` override the user supplies.
   If none resolve, the whole group is `unknown` with the reason — not `fail`.
2. Record image tag / digest, revision name, revision `provisioningState` and `runningState`,
   replica count, and last-deployed timestamp.
3. `fail` if: no revision is active, active replicas are 0, provisioning failed, or traffic is
   split across revisions when the baseline had a single revision.
4. In after mode, `fail` if the image digest is **unchanged** from the baseline — a deployment
   that did not change the running image is the failure mode this tool exists to catch.

Note: the kernel deploys as an Azure Function (`kernel/.github/workflows/az-function.yml`)
and Builder Studio as a container image (`builder-studio-service/Dockerfile`). Handle both
shapes; do not assume one.

### B. Process → tool → agent closure

This is the primary group. The question is not "does the process exist" but
**"is every tool and agent that this process depends on actually deployed and usable here."**

1. List processes: `GET /api/v1/bss_process/processes` (and `/processes/categories`).
2. For each process, get its deployed definition and version:
   `GET /api/v1/bss_process/runtime/processes/{key}`,
   `GET /api/v1/bss_process/runtime/processes/{key}/versions`.
   Record the **published/active version** and whether a newer draft version exists unpublished.
3. Extract the process's dependency set — every tool and agent it references. The reference
   may appear in the BPMN/definition body, in `resourcedata`
   (`GET /api/v1/bss_process/resourcedata/{process_definition_id}`), or in service-task
   properties. Walk the structure generically the way `find_uuids()` in
   `audit_app_config_ids.py` walks config, so a new reference shape is caught without a
   code change. Do not hardcode a list of tool names.
4. Resolve each dependency against the instance:
   - tools: `GET /api/v1/bss_tool/` , `GET /api/v1/bss_tool/{tool_id}`,
     and `GET /api/v1/bss_tool/native` + `/native/{tool_name}` for native tools;
   - agents: `GET /api/v1/bss_agent/`, `GET /api/v1/bss_agent/{agent_id}`.
5. Emit one `CheckResult` per (process, dependency) pair. `fail` when a referenced tool or
   agent does not resolve, resolves to a different kind than expected, is present but
   unpublished/inactive, or its version differs from the version the process was published
   against. This dangling-reference case — process deployed, tool it calls not deployed —
   is the exact bug the group is for.
6. Add a whole-instance check: tools and agents that exist but **nothing references**. Report
   as informational, never `fail`.
7. `POST /api/v1/bss_process/runtime/processes/validate` where the endpoint accepts an
   existing definition without mutating it. Confirm from the OpenAPI spec
   (`builder-studio-service/openapi/openapi.json`) that it is non-mutating before calling it;
   if that is not certain, skip it and say so.

### C. Collections and ontologies

1. `GET /knowledge_hub/api/v1/global-config/` — every UUID in it must resolve.
   `audit_app_config_ids.py` already does this; **call into it rather than duplicating it**,
   and fold its findings in as `CheckResult`s.
2. `GET /knowledge_hub/api/v1/reasoning/ontologies` — the ontologies named in global config
   (findings ontology, docs ontology, and any others present) must exist, be non-empty, and
   carry the entity types the processes and agents reference.
3. `GET /knowledge_hub/api/v1/collections` — the configured collections resolve; the target
   collection (from `--collection-id` or config) exists and is readable.
4. For the target collection: `GET /knowledge_hub/api/v1/reasoning/entities` filtered to it.
   Check that the entity types the loan flow needs are present and that their counts are
   non-zero where a non-zero count is expected. `fail` on an entity whose `ontology_id`
   points at an ontology that does not exist — an orphaned entity is a silent breakage.
5. Cross-instance shape: same config key must resolve to the same *kind* and *name* on both
   sides. UUIDs are expected to differ per instance and must not be compared.

### D. ML traces (MLflow)

Tracing is MLflow-based — see `kernel/app/agent/mlflow_service.py` (autolog for OpenAI and
Anthropic, experiment/run context).

1. Is the tracking URI configured and reachable? Unreachable ⇒ `unknown`, not `fail`.
2. Does the expected experiment exist?
3. Are there **recent** traces — runs within a configurable `--trace-window` (default 24h)?
   Zero recent runs on an instance that is serving traffic is the "traces are not present"
   symptom; report it as `fail` with the last-seen timestamp in `detail`.
4. Spot-check trace completeness on the most recent N runs: a run with a start but no end,
   no spans, or zero token/latency metrics is a broken trace, not a missing one. Distinguish
   the two in `detail` — they have different causes.
5. In after mode, compare trace volume against the baseline window. A collapse to zero after
   deployment is the regression to shout about.

### E. Loan pipeline

1. `GET /mock-server/pipeline` — reachable, returns a well-formed payload, non-empty.
   A 404 means the pipeline was never initialised; that is a distinct status from an empty
   pipeline, and the `detail` must say which. (`scripts/sync_loan_to_pipeline.py` already
   handles this 404 case — match its interpretation.)
2. `GET /mock-server/api/mock-data` — reachable and consistent with the pipeline.
3. Every pipeline entry's `project_id` resolves via `GET /assistant/api/v1/project/{project_id}`.
   A pipeline row pointing at a deleted project is a broken pipeline.
4. Each referenced project's collection resolves, and its documents endpoint answers
   (`GET /knowledge_hub/api/v1/collections/{collection_id}/documents`).
5. Report counts: pipeline rows, resolvable rows, orphaned rows. `fail` on any orphan.

### F. Loan creation sanity — verification only, no loan created

Prove that *if* a loan were created now it would succeed, without creating one.

1. Enumerate the API calls the loan-creation path makes. Derive them from the existing
   automation rather than guessing: `loan-clone-automation/loan_clone.py`,
   `entity_creators/`, and `resources/tools/create_loan_from_document_tool`.
   Produce this list as part of the work and put it in the report, so the checklist is
   auditable against the real flow.
2. For each call in that list, verify **without invoking it**:
   - the endpoint exists and is not 404/405 — use `OPTIONS`, or a `GET` on the collection
     form of the resource, never the `POST`;
   - the caller is authorised — a `401`/`403` here is a `fail`, and it is the single most
     common cause of a loan-creation break after a deployment;
   - every id the payload would carry (ontology ids, collection id, entity-type ids,
     process key, tool ids) resolves — reuse group B and C results rather than re-fetching;
   - the request schema in `openapi/openapi.json` still matches the payload the automation
     builds. A field the automation sends that the deployed schema no longer accepts, or a
     newly-required field the automation does not send, is a `fail` — this catches the
     contract drift that a live smoke test would only find by creating a bad loan.
3. Validate a **representative payload offline** against the deployed schema. Build it from a
   fixture (an existing `clone-*.json` reduced to a template, with a synthetic loan number).
   Never send it.
4. Explicitly assert in the report that no loan was created, with the count of write calls
   attempted: `0`.

## Before / after comparison

- Every run writes `<output-dir>/<label>-<timestamp>.json` **and** updates
  `<output-dir>/<label>-latest.json`.
- With `--baseline`, add a `comparison` section joining on `CheckResult.id`:
  - `regressed` — was `pass`, now `fail`. This is the headline; print it first.
  - `fixed` — was `fail`, now `pass`.
  - `still_failing` — `fail` in both. Labelled pre-existing, excluded from the headline.
  - `appeared` / `disappeared` — ids present in only one run. A disappeared check often
    means an entity was deleted by the deployment, so treat it as noteworthy, not as noise.
  - `unchanged` — collapsed to a count.
- Exit code in after mode is driven by `regressed`, not by total failures. A deployment that
  changed nothing for the worse should exit `0` even on a dirty instance.

## Output

Two renderings of the same data:

1. **Terminal** — one line per check, grouped, `PASS` / `FAIL` / `UNKN` / `SKIP`, then a
   summary block: counts per group, then the regression list. Plain ASCII status words,
   colour only when stdout is a TTY. Readable when piped to a file.
2. **JSON** — full `CheckResult` list plus a top-level `summary`, `environment`
   (URL, resolved Azure resource, image digest, run timestamp) and, in after mode,
   `comparison`. This file is the baseline for the next run, so its schema must be stable
   and versioned with a `schema_version` field.

The checklist is "clean" when no check is `fail` and no check is `unknown`. Say exactly that
in one final line, and when it is not clean, name the first blocking item.

## Deliverables

1. `scripts/deployment_sanity.py`
2. A `## scripts/deployment_sanity.py` section appended to `README.md`, matching the style of
   the existing entries — what it does, the two-run workflow, worked before/after example,
   exit codes.
3. `tests/` coverage for the pure logic: `CheckResult` diffing, dependency extraction from a
   fixture process definition, status roll-up, exit-code selection. Use recorded fixtures;
   no live calls in tests.

## Confirm before coding

State your assumption and proceed if there is no answer — do not block on these, but call
them out in the final summary:

1. **Azure resolution** — is the URL mapped to an Azure Container App, an Azure Function, or
   an App Gateway / Front Door in front of several? The mapping method changes group A most.
2. **`az` auth** — is the runner expected to be `az login`-ed, or should group A be
   `--skip-azure` by default and opt-in?
3. **MLflow** — where is the tracking server, and is it reachable from where this script runs?
4. **Multi-tenant** — does one gateway URL host several collections/tenants, so the checklist
   must be run per collection rather than per URL?
