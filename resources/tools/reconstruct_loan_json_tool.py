"""Knowledge Hub tool: reconstruct a validated loan JSON document from entities.

The standalone ``reconstruct_loan_json.py`` script is the reference
implementation.  This tool exposes the same operation to an Assistant flow,
using the runtime's ``entity_read_json_odata`` and ``DocumentService``.  The
destination dashboard project is created through the Assistant project API.
"""

import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from app.collection.models import DocumentType
from app.documents.document_service import DocumentService


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tool_root() -> Path:
    # resources/tools/reconstruct_loan_json_tool.py -> loan-clone-automation
    return Path(__file__).resolve().parents[2]


def _response_json(response):
    if getattr(response, "error", False):
        raise RuntimeError(getattr(response, "text", "Knowledge Hub query failed"))
    raw = getattr(response, "text", response)
    result = json.loads(raw) if isinstance(raw, str) else raw
    if isinstance(result, dict):
        return result.get("items") or result.get("data") or []
    return result if isinstance(result, list) else []


def _runtime_gateway_url() -> str:
    """Resolve the target gateway from the Builder request context.

    Kernel exposes ``get_request_headers`` to custom tools.  The forwarded Host
    identifies the active instance; standard JazzX public hosts map from
    ``<instance>.jazzx.co`` to ``<instance>-gw.jazzx.co``.  Custom deployments
    can configure ``JAZZX_GATEWAY_URL`` in the Builder runtime instead.
    """
    configured = os.getenv("JAZZX_GATEWAY_URL")
    if configured:
        return configured.rstrip("/")

    get_headers = globals().get("get_request_headers")
    headers = get_headers() if callable(get_headers) else {}
    headers = headers if isinstance(headers, dict) else {}
    raw_host = next(
        (headers.get(key) for key in ("x-forwarded-host", "host", "origin", "referer") if headers.get(key)),
        "",
    )
    raw_host = str(raw_host).split(",", 1)[0].strip()
    if not raw_host:
        raise RuntimeError(
            "Could not determine the JazzX instance URL from the request context. "
            "Configure JAZZX_GATEWAY_URL in the Builder runtime."
        )

    parsed = urlparse(raw_host if "://" in raw_host else f"https://{raw_host}")
    host = parsed.netloc or parsed.path
    host = host.split("@")[-1].split(":", 1)[0]
    if host.endswith(".jazzx.co") and not host.split(".", 1)[0].endswith("-gw"):
        instance, suffix = host.split(".", 1)
        host = f"{instance}-gw.{suffix}"
    if not host:
        raise RuntimeError("Could not determine the JazzX instance gateway URL")
    return f"https://{host}"


def _create_dashboard_project(gateway_url: str, access_token: str, name: str, description: str) -> dict:
    """Create a visible Assistant project using the instance's Assistant API."""
    payload = {
        "name": name,
        "description": description,
        "image_url": "/images/project/folderImages/teal-folder.svg",
        "display_color": "teal",
    }
    request = Request(
        f"{gateway_url.rstrip('/')}/assistant/api/v1/project/resources",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "JazzX-BuilderStudio-Reconstructor/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8")
            project = json.loads(raw) if raw else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"Project creation failed ({exc.code}): {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Project creation failed: {exc.reason}") from exc
    if not isinstance(project, dict) or not project.get("id") or not project.get("collection_id"):
        raise RuntimeError(f"Project creation response is missing id or collection_id: {project}")
    return project


async def reconstruct_loan_json_tool(
    loan_number: str,
    target_loan_number: str,
    access_token: str,
) -> str:
    """Rebuild, validate, and store a loan JSON document in Knowledge Hub.

    Args:
        loan_number: Existing source loan number to find.
        target_loan_number: Loan number written to the generated document.
        access_token: Valid target-instance JWT, without the ``Bearer `` prefix.

    Returns:
        JSON with success, document_id, filename, and source/target collection IDs.
    """
    if not loan_number or not target_loan_number or not access_token:
        return json.dumps({"success": False, "message": "loan_number, target_loan_number, and access_token are required"})

    try:
        # Locate the collection from its LoanCore entity, so callers do not
        # need to know where the source loan currently lives.
        source_collection_id = None
        skip = 0
        while source_collection_id is None:
            response = await call_tool(  # noqa: F821 - injected by Assistant runtime
                tool_name="entity_read_json_odata",
                tool_args={"odata_query": "entity_type eq 'LoanCore'", "limit": 1000, "skip": skip},
            )
            cores = _response_json(response)
            for core in cores:
                value = core.get("json_value") or {}
                if isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except json.JSONDecodeError:
                        value = {}
                numbers = {str(value.get(key, "")) for key in ("loan_number", "external_loan_number", "loanNumber", "externalLoanNumber")}
                if loan_number in numbers:
                    source_collection_id = core.get("collection_id")
                    break
            if len(cores) < 1000:
                break
            skip += 1000
        if not source_collection_id:
            return json.dumps({"success": False, "message": f"No LoanCore found for loan number {loan_number}"})

        response = await call_tool(  # noqa: F821
            tool_name="entity_read_json_odata",
            tool_args={"odata_query": f"collection_id eq '{source_collection_id}'", "limit": 1000, "skip": 0},
        )
        entities = _response_json(response)
        if not entities:
            return json.dumps({"success": False, "message": "No entities found in the source collection"})

        root = _tool_root()
        reconstructor = _load_module("loan_reconstructor", root / "reconstruct_loan_json.py")
        validator = _load_module("loan_input_validator", root / "validate_input.py")
        document = reconstructor.reconstruct(target_loan_number, entities)
        document["project_name"] = target_loan_number
        document["loan"]["loanNumber"] = target_loan_number
        validation = validator.validate_input(document)
        if validation.has_errors:
            return json.dumps({
                "success": False,
                "message": "Reconstructed JSON failed validation; no document was created",
                "validation_errors": [error.__dict__ for error in validation.errors],
            }, default=str)

        project = _create_dashboard_project(
            _runtime_gateway_url(),
            access_token,
            target_loan_number,
            f"Reconstructed loan {target_loan_number}",
        )
        target_collection_id = project.get("collection_id")
        if not target_collection_id:
            raise RuntimeError(f"Project creation returned no collection_id: {project}")

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"{target_loan_number}_{timestamp}_{uuid4().hex[:8]}.txt"
        content = (json.dumps(document, indent=2) + "\n").encode("utf-8")
        created = await DocumentService.create_document(
            collection_id=UUID(target_collection_id),
            name=filename,
            type=DocumentType.text,
            doc_obj=content,
            meta_data={
                "source": "knowledge-hub-reconstruction",
                "content_type": "application/json",
                "source_collection_id": source_collection_id,
                "source_loan_number": loan_number,
                "target_loan_number": target_loan_number,
            },
            storage_only=True,
            size=len(content),
        )
        return json.dumps({
            "success": True,
            "source_collection_id": source_collection_id,
            "target_collection_id": target_collection_id,
            "project_id": project.get("id"),
            "project_name": target_loan_number,
            "source_loan_number": loan_number,
            "target_loan_number": target_loan_number,
            "entity_count": len(entities),
            "document_id": str(created.id),
            "filename": filename,
            "validation_errors": 0,
            "validation_warnings": validation.warning_count,
        })
    except Exception as exc:
        return json.dumps({"success": False, "message": str(exc)})


async def reconstruct_loan_json(loan_number: str, target_loan_number: str, access_token: str) -> str:
    return await reconstruct_loan_json_tool(loan_number, target_loan_number, access_token)


async def main(loan_number: str, target_loan_number: str, access_token: str) -> str:
    return await reconstruct_loan_json(loan_number, target_loan_number, access_token)
