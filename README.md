# JazzX Automation Scripts

Small command-line utilities for JazzX loan demos, Knowledge Hub cleanup, mock-pipeline data, and PDF validation.

The scripts are instance-neutral: no gateway URL, collection ID, project ID, loan number, or JWT is embedded in this repository. Destructive commands perform a dry run unless their explicit execution flag is supplied.

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

## Original-to-new filename mapping

| Original file | New location | Change |
| --- | --- | --- |
| `delete_collection.py` | `scripts/delete_project_and_collection.py` | Clarifies that it also archives the project; adds preview and UUID/name validation. |
| `delete_collection_entities.py` | `scripts/purge_collection_resources.py` | Fixes filtering, previews, summaries, pagination, and document deletion. |
| `delete_from_collection.py` | Consolidated into `scripts/purge_collection_resources.py` | Removes duplicated curl logic, fixed IDs, and the hard-coded JWT. |
| `ensure_loan_in_broker_portal.py` | `scripts/sync_loan_to_pipeline.py` | Adds dry-run mode, schema checks, name override, and optional initialization. |
| `split.py` | `scripts/split_pdf_by_start_pages.py` | Adds clear semantics, file-based breakpoints, overwrite protection, and output verification. |
| `validate_document_page_counts.py` | `scripts/validate_document_page_counts.py` | Removes embedded credentials, queries both document types, resolves loans safely, and emits clean reports. |

Generated reports, caches, `__pycache__`, and instance-specific JSON fixtures from the old `test/` directory are intentionally excluded.

## Development checks

```bash
python3 -m compileall -q scripts tests
python3 -m unittest discover -s tests -v
```

Every command also supports `--help`.
