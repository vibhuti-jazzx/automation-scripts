"""Clone all Knowledge Hub entities from one loan collection to another."""

import json
import re
from copy import deepcopy
from uuid import uuid4


READ_ONLY_FIELDS = {"id", "created_at", "updated_at", "created_by", "updated_by"}
LOAN_KEYS = {"loan_number", "loanNumber", "external_loan_number", "externalLoanNumber"}
COLLECTION_KEYS = {"collection_id", "collectionId"}
ONTOLOGY_BY_ENTITY_TYPE = {
    "LoanProject": "loan_project",
    "LoanDetails": "loan_details",
}


def _walk(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)
    else:
        yield value


def _replace(value, ids):
    if isinstance(value, dict):
        return {key: _replace(item, ids) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace(item, ids) for item in value]
    return ids.get(value, value) if isinstance(value, str) else value


def _rewrite_identity(value, loan_number, collection_id):
    if isinstance(value, dict):
        result = {key: _rewrite_identity(item, loan_number, collection_id) for key, item in value.items()}
        for key in LOAN_KEYS.intersection(result):
            result[key] = loan_number
        for key in COLLECTION_KEYS.intersection(result):
            result[key] = collection_id
        return result
    if isinstance(value, list):
        return [_rewrite_identity(item, loan_number, collection_id) for item in value]
    return value


def _entity_id(response):
    if getattr(response, "error", False):
        raise RuntimeError(getattr(response, "text", "entity_create failed"))
    raw = getattr(response, "text", response)
    if isinstance(raw, dict) and raw.get("id"):
        return str(raw["id"])
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("id"):
                return str(data["id"])
        except json.JSONDecodeError:
            pass
        match = re.search(r"(?:^|[\s,(])id=UUID\('([^']+)'\)|\"id\"\s*:\s*\"([0-9a-f-]{36})\"", raw)
        if match:
            return match.group(1) or match.group(2)
    raise RuntimeError(f"entity_create returned no entity ID: {str(raw)[:500]}")


def _ordered(entities):
    by_id = {str(item["id"]): item for item in entities if item.get("id")}
    source_ids = set(by_id)
    dependencies = {
        entity_id: {value for value in _walk(item.get("json_value") or {}) if isinstance(value, str) and value in source_ids and value != entity_id}
        for entity_id, item in by_id.items()
    }
    result = []
    while dependencies:
        ready = [entity_id for entity_id, needs in dependencies.items() if not needs]
        if not ready:
            result.extend(by_id[entity_id] for entity_id in dependencies)
            break
        for entity_id in ready:
            result.append(by_id[entity_id])
            dependencies.pop(entity_id)
        for needs in dependencies.values():
            needs.difference_update(ready)
    return result


async def clone_loan_entities(loan_number: str, source_collection_id: str, target_collection_id: str) -> str:
    """Clone every entity in source_collection_id into target_collection_id."""
    try:
        response = await call_tool(  # noqa: F821
            tool_name="entity_read_json_odata",
            tool_args={"odata_query": f"collection_id eq '{source_collection_id}'", "limit": 1000, "skip": 0},
        )
        if getattr(response, "error", False):
            raise RuntimeError(getattr(response, "text", "Could not read source entities"))
        entities = json.loads(getattr(response, "text", "[]"))
        if isinstance(entities, dict):
            entities = entities.get("items") or entities.get("data") or []
        if not isinstance(entities, list) or not entities:
            raise RuntimeError(f"No entities found in source collection {source_collection_id}")
        invalid = [item for item in entities if not item.get("id") or not item.get("entity_type") or not isinstance(item.get("json_value"), dict)]
        if invalid:
            raise RuntimeError("Source collection contains entities without id, entity_type, or object json_value")

        source_ids = {str(item["id"]) for item in entities}
        placeholders = {source_id: f"pending-reference-{uuid4()}" for source_id in source_ids}
        id_map, original_json, created = {}, {}, []
        for source in _ordered(entities):
            source_id = str(source["id"])
            payload = {key: deepcopy(value) for key, value in source.items() if key not in READ_ONLY_FIELDS}
            # Builder Studio's entity_create does not accept the REST-only
            # ontology_id field returned by entity_read_json_odata.
            payload.pop("ontology_id", None)
            payload["ontology_name"] = (
                ONTOLOGY_BY_ENTITY_TYPE.get(source["entity_type"])
                or source.get("ontology_name")
                or "mortgage_ontology_v2"
            )
            payload["collection_id"] = target_collection_id
            payload["name"] = f"{loan_number}_{source['entity_type']}_{uuid4().hex[:8]}"
            payload["json_value"] = _rewrite_identity(_replace(source["json_value"], {**placeholders, **id_map}), loan_number, target_collection_id)
            create_response = await call_tool(tool_name="entity_create", tool_args=payload)  # noqa: F821
            target_id = _entity_id(create_response)
            id_map[source_id] = target_id
            original_json[source_id] = deepcopy(source["json_value"])
            created.append({"source_id": source_id, "target_id": target_id, "entity_type": source["entity_type"]})

        # A second pass resolves cycles and removes all source entity IDs.
        for source_id, original in original_json.items():
            final_json = _rewrite_identity(_replace(original, id_map), loan_number, target_collection_id)
            patch_response = await call_tool(  # noqa: F821
                tool_name="update_entity_rest_api", tool_args={"entity_id": id_map[source_id], "json_value": final_json}
            )
            if getattr(patch_response, "error", False):
                raise RuntimeError(f"Failed to update clone {id_map[source_id]}: {getattr(patch_response, 'text', '')}")
        return json.dumps({"success": True, "loan_number": loan_number,
                           "source_collection_id": source_collection_id, "target_collection_id": target_collection_id,
                           "created_count": len(created), "created_entities": created, "id_mapping": id_map})
    except Exception as exc:
        return json.dumps({"success": False, "message": str(exc)})


async def duplicate_loan(loan_number: str, source_collection_id: str, target_collection_id: str) -> str:
    """Compatibility entry point for the Builder Studio tool configuration."""
    return await clone_loan_entities(loan_number, source_collection_id, target_collection_id)


async def main(loan_number: str, source_collection_id: str, target_collection_id: str) -> str:
    return await duplicate_loan(loan_number, source_collection_id, target_collection_id)
