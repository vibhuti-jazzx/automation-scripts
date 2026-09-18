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
  --project-name 1441010_1
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

## Development checks

```bash
python3 -m compileall -q scripts tests
python3 -m unittest discover -s tests -v
```

Every command also supports `--help`.
