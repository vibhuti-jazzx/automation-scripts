# JazzX Automation Scripts

Small command-line utilities for JazzX loan demos, Knowledge Hub cleanup, mock-pipeline data, and PDF validation.

The scripts are instance-neutral: no gateway URL, collection ID, project ID, loan number, or JWT is embedded in this repository. Destructive commands perform a dry run unless their explicit execution flag is supplied.

Python 3.10 or newer is required.

## Setup

```bash
git clone https://github.com/vibhutimishra09/automation-scripts.git
cd automation-scripts
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt

export JAZZX_GATEWAY_URL="https://poc-gw.jazzx.co"
export JAZZX_TOKEN="<current JWT>"
```

Tokens expire and must never be committed. You may pass `--gateway-url` and `--token` instead of using environment variables, but the token can then be retained in shell history.

## Script reference

### `scripts/xml_to_loan_json.py`

Converts a MISMO 3.4 XML export into the deterministic `project_details`,
`loan_core`, `borrowers`, `subject_property`, and optional `loan_details`
format used by the loan automation. It uses only Python's standard library;
there is no LLM call, network dependency, or external prompt. Values absent
from the XML are omitted rather than guessed.

```bash
python3 scripts/xml_to_loan_json.py \
  "path/to/Loan 3.4.xml" \
  --output "path/to/loan.json"
```

Use `--omit-loan-details` when only the core entity payload is needed. The
summary is printed to stderr, while JSON is written to stdout if `--output` is
omitted.

### `resources/tools/`

This directory contains the Builder Studio tool bundles migrated from the
loan-clone automation project. Each tool includes its Python implementation,
JSON parameter definition, and YAML manifest:

- `clone_loan_entities_tool`
- `create_loan_from_document_tool`
- `reconstruct_loan_json_tool`

### `scripts/delete_project_and_collection.py`

Deletes a Knowledge Hub collection and archives the related Assistant project. Project names are resolved across all API pages; ambiguous names are rejected. A direct `--project-id` must be a dashboard UUID, not a loan number or project name.

Preview:

```bash
python3 scripts/delete_project_and_collection.py \
  --collection-id d7c0888d-b311-48ac-87da-672c761b99d3 \
  --project-name 1025017098_01
```

Execute after checking the preview:

```bash
python3 scripts/delete_project_and_collection.py \
  --collection-id d7c0888d-b311-48ac-87da-672c761b99d3 \
  --project-id f91531ff-893b-4929-8d6b-920030f14bff \
  --execute
```

Use `--yes` only for an already-reviewed non-interactive run. A missing collection is treated as already deleted. If one of the two operations fails, the script still attempts the other and reports a non-zero exit code.

### `scripts/purge_collection_resources.py`

Lists or deletes selected Reasoner entities and optionally uploaded documents without deleting the collection itself. It replaces the two overlapping legacy scripts `delete_collection_entities.py` and `delete_from_collection.py`.

Preview all supported entity types and documents:

```bash
python3 scripts/purge_collection_resources.py \
  --collection-id 039b3dfb-486c-446d-b2e7-0936c0c3afd1 \
  --all-entity-types \
  --include-documents
```

Delete only findings and conditions:

```bash
python3 scripts/purge_collection_resources.py \
  --collection-id 039b3dfb-486c-446d-b2e7-0936c0c3afd1 \
  --entity-types Condition Finding FindingSet \
  --execute
```

Delete only documents:

```bash
python3 scripts/purge_collection_resources.py \
  --collection-id 039b3dfb-486c-446d-b2e7-0936c0c3afd1 \
  --documents-only \
  --execute
```

The command deduplicates unstable paginated responses, treats an already-absent resource as successful, bounds concurrency, and refuses an empty resource selection.

### `scripts/sync_loan_to_pipeline.py`

Adds an existing Assistant project to the mortgage-app pipeline mock. It does not create the project or Knowledge Hub entities. Existing entries are detected by project UUID or displayed loan number.

Preview:

```bash
python3 scripts/sync_loan_to_pipeline.py \
  --input stockton-loans/Loan12/5310308674_loan.json \
  --project-id f3045c31-3b0f-450a-aa8c-57b6f932b78b
```

Apply the change, creating a minimal pipeline response only if `GET /mock-server/pipeline` returns 404:

```bash
python3 scripts/sync_loan_to_pipeline.py \
  --input stockton-loans/Loan12/5310308674_loan.json \
  --project-id f3045c31-3b0f-450a-aa8c-57b6f932b78b \
  --pipeline-loan-number 5310308674_1 \
  --initialize-if-missing \
  --apply
```

`--pipeline-loan-number` controls the name/number shown on the loan-pipeline page. The script accepts both raw LOS-style loan JSON and the mapped `loan_core`/`loan_details` format.

### `scripts/split_pdf_by_start_pages.py`

Splits a PDF using one-based pages on which each document begins. Page 1 is added automatically if omitted. Outputs are written atomically and re-opened to verify their page counts; existing outputs are protected unless `--overwrite` is supplied.

```bash
python3 scripts/split_pdf_by_start_pages.py \
  "Council credit docs.pdf" \
  1 12 13 14 19 21 31 42 \
  --output-dir "Council credit docs_split"
```

For a long list, put page numbers in a text/CSV file separated by whitespace, commas, or semicolons:

```bash
python3 scripts/split_pdf_by_start_pages.py \
  "Council credit docs.pdf" \
  --start-pages-file council_breakpoints.txt
```

Start pages must be unique, ascending, and within the PDF. A breakpoint is the first page of a new output document, so 68 start pages produce 68 PDFs.

### `scripts/validate_document_page_counts.py`

Fetches `SourceDocument` and `ClassifiedDocument` entities, calls the kernel `count_pdf_page` tool for each unique internal file, and compares every source page count with the sum of its classified children. It can also compare the sum of source documents with a local pre-split PDF.

By collection:

```bash
python3 scripts/validate_document_page_counts.py \
  --collection-id 039b3dfb-486c-446d-b2e7-0936c0c3afd1 \
  --original-pdf "Council credit docs.pdf"
```

By loan number (resolved live through `LoanProject` entities):

```bash
python3 scripts/validate_document_page_counts.py --loan-number 5310308674
```

JSON and text reports are saved under `document-page-count-reports/`. The command returns exit code `1` for a mismatch/incomplete comparison and `2` for an operational error. Missing internal file IDs and unmatched classified documents are reported explicitly.

### `scripts/jazzx_api.py`

Shared library used by the other API scripts. It centralizes URL/token normalization, UUID checks, bounded GET retries, common response-envelope parsing, and repeated-page protection. It is not normally run directly.

## Loan creation and cloning

The following commands use the payload builders in `entity_creators/`. Set the target instance and a current token first:

```bash
export JAZZX_GATEWAY_URL="https://poc-gw.jazzx.co"
export JAZZX_TOKEN="<current JWT>"
```

### `loan-clone-automation/add_loan_mock_data.py`

Creates or reuses an Assistant project, upserts its mortgage entities, and adds or repairs its row in the mock-server loan pipeline. Ontology UUIDs are resolved by stable ontology name in the selected instance, so environment-specific IDs are not hard-coded.

Validate the input locally without API calls:

```bash
python3 loan-clone-automation/add_loan_mock_data.py \
  --input "stockton-loans/Loan12/5310308674_loan.json" \
  --validate-only
```

Create/update the project, entities, and pipeline row:

```bash
python3 loan-clone-automation/add_loan_mock_data.py \
  --input "stockton-loans/Loan12/5310308674_loan.json" \
  --project-name 5310308674_1 \
  --initialize-pipeline-mock
```

If an existing project response does not contain its Knowledge Hub collection, pass the known collection explicitly:

```bash
python3 loan-clone-automation/add_loan_mock_data.py \
  --input loan.json \
  --project-name 5310308674_1 \
  --collection-id 28e49cd4-6c1d-4b9d-a1d8-9bc8ae7e8565
```

Useful modes:

- `--pipeline-only` leaves Knowledge Hub entities unchanged and only adds/repairs the pipeline row.
- `--skip-pipeline-update` creates/updates project entities without touching the pipeline mock.
- `--loan-id <project UUID> --collection-id <collection UUID>` targets an existing project directly.
- `--project-name` is also the loan number displayed in the pipeline.

The script validates input before making network calls, rejects invalid UUIDs and ambiguous duplicate projects/entities, retrieves every API page, and stops on an entity failure instead of presenting a partial loan as successful. Entity IDs and names are deterministic for repeatable runs; legacy randomized entities are matched using stable business fields when the match is unambiguous. `Conv` is supported for Freddie Mac loans, and `mortgageType` is read from `loan.loanProduct`.

### `loan-clone-automation/loan_clone.py`

Clones every Knowledge Hub entity from a reference loan into a newly created project. It rewrites loan/project/collection identifiers and entity relationships, including circular references, then verifies the resulting entities.

Use `--remove-macer` to exclude only the MACER workflow entities (`MacerInvocationEvent`, `Finding`, `FindingSet`, `LoanFindingsSummary`, `Condition`, and `MergedConditionSet`). Document-related entities such as `SourceDocument`, `ClassifiedDocument`, `PageCollection`, and `LoanDocumentScope` are retained. The result includes the number and types of omitted entities.

Use `--copy-documents` to copy the source collection's document files—including source and split documents—into the target collection. The upload is started but asynchronous document processing is not awaited, so queued processing does not block the entity clone. Knowledge Hub exposes no per-document download URL, so files are fetched through the collection download endpoint, which returns a zip archive even for a single document.

`--workers` (default 4) sets how many documents are copied in parallel. `--document-timeout` (default 600s) is a wall-clock deadline for one document's download or upload; a document that exceeds it is reported as failed and the run continues, with the full list in the `--output` file. On a resumed run, documents already present in the target are matched by `meta_data.source_document_id`, falling back to filename and size. If the target holds documents but none can be matched, the run stops rather than duplicate the collection; `--allow-duplicate-documents` overrides that.

If the requested project name already exists as one active project, the script resumes it instead of failing. Entities created by this script have stable names, so a rerun reuses and updates existing entities and creates only missing ones. If the project API omits the existing collection ID, pass it with `--target-collection-id` (this is different from `--source-collection-id`). The request `--timeout` is applied to each API call; it does not limit the complete clone duration.

Preview from an exported entity file without API writes:

```bash
python3 loan-clone-automation/loan_clone.py \
  --source-file source_entities.json \
  --target-loan-number 1441010_1 \
  --dry-run \
  --output clone_preview.json
```

Clone a live loan when its collection is known:

```bash
python3 loan-clone-automation/loan_clone.py \
  --reference-loan-number 1441010 \
  --target-loan-number 1441010_1 \
  --source-collection-id <source collection UUID> \
  --project-name 1441010_1 \
  --remove-macer
```

Alternatively, pass `--dashboard-collection-id` to locate the source collection through its `LoanProject`. `--validate-only` performs source checks without creating a project. Semantic warnings block creation unless explicitly reviewed with `--allow-validation-warnings`. Active duplicate project names, duplicate entity IDs, invalid UUIDs, malformed pagination, and a target equal to the reference loan are rejected.

### `loan-clone-automation/reconstruct_loan_json.py`

Reads an existing loan’s Knowledge Hub entities and reconstructs the raw loan JSON format accepted by the entity creators. It validates the result and can upload it to an existing collection or create a new Assistant project first.

Inspect source entities using GET requests only:

```bash
python3 loan-clone-automation/reconstruct_loan_json.py \
  --loan-number 1441010 \
  --source-collection-id <source collection UUID> \
  --inspect
```

Reconstruct and validate without any remote write:

```bash
python3 loan-clone-automation/reconstruct_loan_json.py \
  --loan-number 1441010 \
  --source-collection-id <source collection UUID> \
  --dry-run \
  --output reconstructed_1441010.json
```

Create a project and upload the validated document:

```bash
python3 loan-clone-automation/reconstruct_loan_json.py \
  --loan-number 1441010 \
  --source-collection-id <source collection UUID> \
  --create-project \
  --project-name 1441010_reconstructed \
  --output reconstructed_1441010.json
```

The remote document is stored as a uniquely named `.txt` because affected Knowledge Hub environments reject `.json` uploads; its content remains formatted JSON. The command validates UUIDs, safe filename characters, duplicate projects, response envelopes, repeated pages, timeouts, and local output directories.

### `loan-clone-automation/validate_input.py`

Validates a raw loan JSON file against the fields, types, ranges, and enums required by the entity payload builders. This command is local and makes no API calls.

```bash
python3 loan-clone-automation/validate_input.py \
  --input loan.json \
  --verbose \
  --output validation_report.json
```

Use `--json-output` to print machine-readable results. Exit code `0` means no validation errors; exit code `1` means invalid input. Boolean and non-finite values are rejected for numeric fields, and `Conv` is accepted as the Freddie Mac loan-type key.

### `entity_creators/`

Internal Pydantic payload builders used by `add_loan_mock_data.py`. They build and validate `LoanProject`, `LoanCore`, borrower, asset, income, liability, subject property, employment, credit report, loan application, and loan-details entities. This directory is a library and is not invoked directly.

### `scripts/deployment_validation.py`

A read-only checklist you run **before** a deployment and again **after**. The second
run diffs itself against the first and reports what the deployment broke.

That before/after shape is the point. A JazzX instance is never completely clean, so a
flat list of failures is unreadable — what matters is which checks *changed*. A check
that was already failing is reported as pre-existing and kept out of the headline, and
in after mode only a genuine regression sets a non-zero exit code.

Six groups, each emitting the same `CheckResult` record:

| group | what it proves |
| --- | --- |
| `platform` | the things that explain most other failures: the token has not expired and who it belongs to, the local clock agrees with the server, and each service the gateway fronts answers. Runs first for that reason. |
| `frontend` | the federated micro-frontends: `/env.js` is served, and every `VITE_MICROFRONTEND_URL_*` it declares returns a real JavaScript module. A missing remote breaks whole sections of the UI while every backend check stays green. |
| `deployment` | what is running at this URL: `api-health`, deployment history, and the Azure container app or function app behind the hostname. In after mode an **unchanged image digest fails** — the deploy did not land. |
| `process` | every tool, sub-process and agent a process references actually resolves on this instance; every agent has a `kernel_agent_id` and can reach the tools it lists. |
| `collections` | every UUID in the global config resolves, ontologies are loaded, no entity points at a missing ontology. |
| `traces` | MLflow is reachable, the experiment exists, traces are both present *and* complete. |
| `pipeline` | the loan pipeline answers and every row resolves to a live project. |
| `assistant` | the JazzX Assistant and the **Luna agent**: an agent named `luna_agent` exists and is bound to the kernel, its tools and its model are deployed, assistant-to-agent bindings resolve, MCP is healthy, and message traces show Luna actually answering. |
| `loan` | loan creation would succeed — proven without creating a loan. |

Nothing here writes. Every call is a `GET`, or a `POST` to a search-only MLflow endpoint
(`/experiments/search`, `/runs/search` — MLflow has no GET form for those). Azure is read
through `az containerapp|functionapp list|show` only. The guarantee is enforced rather
than asserted: a non-`GET` raises `AssertionError` before the request is built, and the
run header plus every report carry the call counts:

```
token:  valid 2.5h, until 2026-10-01T10:54:13+00:00
calls:  9 read, 0 write  (this tool never writes)
```

The token line is printed on every run, not behind `--verbose` — an expired token makes
every other line meaningless. A token that has already expired stops the run with exit
`3` before a single check executes, rather than producing a report of false failures.

Pointing it at the **web host** instead of the gateway also stops the run with exit `3`.
A static web host answers every path — so a run against it invents successes rather than
failing — and it is detected by the shape of the response to a nonsense path: HTML or an
Azure Storage XML error rather than the gateway's JSON. The error names the `-gw` host to
use instead.

Before the deployment:

Point `--gateway-url` at the **gateway** host, not the UI. The UI is a single-page app
that answers every path with `index.html` and HTTP 200, so a run against it reports
nonsense rather than failing. The gateway is the host that returns
`{"status": false, "message": "Endpoint not found."}` on an unknown path — for the POC
instance the UI is `poc.jazzx.co` and the gateway is `poc-gw.jazzx.co`.

Builder Studio is reached through the gateway at `/builder_studio/api/v1`, which is the
default. Its own `openapi.json` says `/api/v1`, because that is its internal mount; using
that value 404s every Builder Studio call. Override with `--bss-prefix` if a gateway
fronts it elsewhere.

```bash
export JAZZX_TOKEN="<current JWT>"       # jazz login <ui-url> writes one to .jwt_token.json

python3 scripts/deployment_validation.py \
  --gateway-url https://poc-gw.jazzx.co \
  --label poc \
  --collection-id 039b3dfb-486c-446d-b2e7-0936c0c3afd1 \
  --output-dir sanity/
```

Each run writes one Markdown report, `<label>-<timestamp>.md`: the summary, every failure and
undecided check with what to do, a table of every APP_CONFIG id (key, id, what it resolves
to, valid or not), the config drift table when `--compare-gateway-url` is given, and every
check. No JSON is written. `--baseline` still accepts a JSON report written by an older run.

Exit codes: `0` clean — in after mode, no regressions; `1` a check failed, or in after
mode regressed; `2` degraded, checks could not run; `3` the script could not start.

`unknown` is deliberately not `fail`. MLflow being unreachable is not the same finding as
MLflow having no traces, and collapsing them makes the report cry wolf. Only a check that
ran and found a real problem fails.

**The Luna checks are the reason the `assistant` group exists separately.** Luna is an
ordinary kernel agent by name — `assistant/app/conversation/api.py` pins
`LUNA_AGENT_NAME = "luna_agent"` and the fulfilment UI pins the same string — but it
takes a different code path from every other agent: `create_new_japes_conversation`,
which answers over the JAPES queue instead of inline. So Luna can have a valid record,
a live model and every tool deployed, and still never reply to anybody, because the
JAPES response-queue consumer is not running. Nothing about the agent shows that. What
shows it is a message trace left in `queued` — `MessageTraceStatus` has only `queued`
and `completed`, so anything still queued after `--stuck-trace-minutes` (default 15) is
hung. That check is the group's real payload.

The same group also catches message traces with no `trace_id` or `span_id`: those
conversations ran but were never recorded in MLflow, which is the "the traces are
missing" report that the `traces` group alone cannot explain — MLflow is reachable and
the experiment is fine, the conversations just never reached it.

`--user-id` is optional. When it is not given, the user id is read from the token's
`sub` claim, since that is where it already is.

**Why `frontend` is a separate group.** The shell app loads each micro-frontend with a
dynamic `import()` of a `remoteEntry.js` named in `/env.js`. When a remote has not been
uploaded, every service is healthy, every API answers, and the page still dies — the
import rejects with `Failed to fetch dynamically imported module` and the user gets a
generic error card. No backend check sees it. The group catches two cases, and the second
is the quieter one:

- **404** — the remote was never uploaded.
- **200 with `text/html`** — a path fallback served `index.html` in place of the module.
  The browser refuses to execute it, but the status code says healthy, so anything that
  checks only the code calls this fine.

The UI is a different host from the gateway, related by a `-gw` suffix on the first label
(`staging-gw.plazahomemortgage.jazzx.co` serves `staging.plazahomemortgage.jazzx.co`).
That is derived automatically; pass `--ui-url` when a deployment does not follow it.
Requests to the UI host deliberately carry no `Authorization` header — it is not behind
the gateway's auth and has no business seeing the token.

### Tool presence report

`--tool-report` adds a deduplicated table of every tool any process or agent reaches for,
ticked when it is deployed on the instance:

```
TOOL PRESENCE  —  every tool referenced by a process or agent on this instance

    tool                     used by                           status
  ✗ ghost_tool_a             2 processes                       MISSING
       referenced by: createLoan, syncBorrowers
  ✗ ghost_tool_b             1 process, 1 agent                MISSING
       referenced by: syncBorrowers, income_agent
  ✓ entity_create            2 processes, 1 agent              deployed

  3/5 referenced tools deployed, 2 missing.
  1 deployed tool(s) nothing references: never_used_tool
```

It deduplicates deliberately: one missing tool referenced by nine processes is one thing
to fix, not nine findings to read. A tool that resolves for one caller and not another
counts as missing — the optimistic reading would hide a real break. `--tool-report-all`
lists the deployed tools too; by default only the missing ones are shown, with the last
line giving the totals. The same structure is written to the JSON report as `tool_report`.

Useful flags: `--checks` selects groups (`process,pipeline`), `--skip-azure` avoids
shelling out to the `az` CLI, `--max-processes` bounds a slow run, `--trace-window` sets
how many hours of history count as recent (default 24), `--ui-url` names the web host when it cannot be derived, `--luna-agent-name` overrides the
agent name the Assistant expects, `--stuck-trace-minutes` sets how long a queued message
trace may sit before it counts as hung, and `--verbose` lists the passing checks as well
as the problems.

For a fast pre-deploy smoke test, `--checks platform,assistant` covers the token, every
service behind the gateway, and whether Luna is answering, in a handful of calls.

Group `collections` resolves every UUID in APP_CONFIG against the instance (ontology,
collection, entity, or nothing); this replaced `audit_app_config_ids.py`.
Group `process` parses each BPMN definition for the shapes the real processes use —
`setVariable("toolName", "...")`, `callActivity/@calledElement`, and
`flowable:delegateExpression` — so a tool added to a process is picked up without editing
this script. A `calledElement` resolved from a process variable is reported as dynamic and
not judged, since its target cannot be known statically.

#### Comparing APP_CONFIG with another instance (`config_drift`)

With `--compare-gateway-url`, APP_CONFIG is also compared with a reference instance key by
key. Ids are expected to differ and are ignored; a key that resolves to a different kind,
name or detail, or exists on one side only, fails. The report gets a `config_drift` section.

```bash
export JAZZX_TOKEN_B="<token for the reference instance>"
python3 scripts/deployment_validation.py \
  --gateway-url https://<host>-gw.<domain> --label staging --output-dir sanity/ \
  --compare-gateway-url https://<reference>-gw.<domain> --compare-label prod
```

While walking processes it prints `process 37/177 KEY`; `--no-progress` turns that off.

### `scripts/customer_access_report.py`

Lists every login by a customer user on an instance: who, when, for how long, and from
which IP. Every non-JazzX user counts as a customer user; JazzX domains are excluded
unless `--include-internal` is given.

```bash
export JAZZX_TOKEN="<your token from the instance UI>"
export KEYCLOAK_ADMIN_PASSWORD="<keycloak admin password>"
python3 scripts/customer_access_report.py --base-url https://poc.jazzx.co --admin-user admin
```

Login history comes from the platform's own records (`GET /assistant/api/v1/user_sessions`,
read with `JAZZX_TOKEN`). The UI writes one row each time it gets a token, so a user's
rows less than `--gap-minutes` (default 30) apart are merged into one visit. Keycloak
supplies the email for each user id. Without `JAZZX_TOKEN` the script falls back to
Keycloak login events, which exist only when "Save events" is on for the realm. A UI
URL is mapped to its `-gw` gateway host.

The report is the login history as one CSV in `customer-access-reports/` (gitignored,
because it contains customer emails and IPs), one row per visit, newest first. By default it
covers every recorded login from the beginning through the end of today (in `--timezone`) and
is named for that range, e.g. `poc-login-history-20261008-to-20261022.csv`. Use `--since` /
`--until` (YYYY-MM-DD) to narrow the range, and `--markdown` to also write the Markdown summary.

## Development checks

```bash
python3 -m compileall -q scripts tests
python3 -m unittest discover -s tests -v
```

Every command also supports `--help`.
