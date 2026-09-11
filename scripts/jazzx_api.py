#!/usr/bin/env python3
"""Shared HTTP and validation helpers for the JazzX automation scripts."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from typing import Any, Iterator

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


DEFAULT_TIMEOUT = 30.0


class ApiError(RuntimeError):
    """An API request failed or returned an unexpected response."""


def require_value(value: str | None, *, name: str, env_name: str | None = None) -> str:
    resolved = value or (os.getenv(env_name) if env_name else None)
    if not resolved:
        suffix = f" or set {env_name}" if env_name else ""
        raise ValueError(f"{name} is required{suffix}")
    return resolved


def normalize_gateway_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value.startswith(("http://", "https://")):
        raise ValueError("gateway URL must start with http:// or https://")
    return value


def normalize_token(value: str) -> str:
    value = value.strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    if not value:
        raise ValueError("token cannot be empty")
    return value


def validate_uuid(value: str, *, name: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{name} must be a UUID, got {value!r}") from exc


def extract_items(payload: Any, *, keys: tuple[str, ...] = ("items", "data", "results", "entities")) -> list[dict[str, Any]]:
    """Return a list from common JazzX response envelopes."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        raise ApiError(f"expected a JSON object or list, got {type(payload).__name__}")
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            for nested_key in keys:
                nested = value.get(nested_key)
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, dict)]
    return []


def parse_json_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


@dataclass
class JazzXClient:
    gateway_url: str
    token: str
    timeout: float = DEFAULT_TIMEOUT

    def __post_init__(self) -> None:
        self.gateway_url = normalize_gateway_url(self.gateway_url)
        self.token = normalize_token(self.token)
        if self.timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
            respect_retry_after_header=True,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        expected: tuple[int, ...] = (200,),
    ) -> requests.Response:
        url = f"{self.gateway_url}/{path.lstrip('/')}"
        try:
            response = self.session.request(
                method,
                url,
                params=params,
                json=json_body,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ApiError(f"{method.upper()} {url} failed: {exc}") from exc
        if response.status_code not in expected:
            body = response.text.strip().replace("\n", " ")[:1000]
            raise ApiError(
                f"{method.upper()} {url} returned HTTP {response.status_code}: {body or '<empty body>'}"
            )
        return response

    def request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.request(method, path, **kwargs)
        if not response.content:
            return {}
        try:
            return response.json()
        except requests.JSONDecodeError as exc:
            raise ApiError(f"{method.upper()} {response.url} did not return valid JSON") from exc

    def iter_offset_pages(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        page_size: int = 100,
        offset_param: str = "offset",
        limit_param: str = "limit",
        item_keys: tuple[str, ...] = ("items", "data", "results", "entities"),
        max_pages: int = 10_000,
    ) -> Iterator[dict[str, Any]]:
        if page_size < 1 or page_size > 1000:
            raise ValueError("page_size must be between 1 and 1000")
        base_params = dict(params or {})
        seen_signatures: set[tuple[str, ...]] = set()
        offset = 0
        for _ in range(max_pages):
            page_params = {**base_params, limit_param: page_size, offset_param: offset}
            payload = self.request_json("GET", path, params=page_params)
            items = extract_items(payload, keys=item_keys)
            if not items:
                return
            signature = tuple(str(item.get("id", "")) for item in items)
            if signature in seen_signatures:
                raise ApiError("pagination returned a repeated page; stopping to avoid an infinite loop")
            seen_signatures.add(signature)
            yield from items
            if len(items) < page_size:
                return
            offset += len(items)
        raise ApiError(f"pagination exceeded {max_pages} pages")
