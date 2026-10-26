#!/usr/bin/env python3
"""Read-only deployment validation for one JazzX instance.

Run it before a deployment, run it again after, and the second run tells you what
the deployment broke. That is the whole design: a JazzX instance is never 100%
clean, so a raw list of failures is unreadable. What matters is the delta.

Check groups, every one emitting the same CheckResult shape:

  deployment   what is actually running at this URL -- image digest, revision,
               replica state, deployment history. In after mode, an UNCHANGED
               image digest is a failure: the deploy did not take.
  process      the group this tool exists for. Every process is walked for the
               tools, sub-processes and agents it references, and each reference
               is resolved against this instance. A process deployed without the
               tool it calls is the silent break that shows up hours later as a
               stuck instance.
  collections  ontologies exist and are populated, collections are readable and
               no entity points at a missing ontology. Run config_audit.py for
               the separate global-config UUID audit.
  traces       MLflow is reachable, the experiment exists, and traces are both
               PRESENT and COMPLETE. Those are different faults.
  pipeline     the loan pipeline answers and every row resolves to a live
               project and collection.
  loan         loan creation would succeed -- proven WITHOUT creating a loan.
  classify     [only with --classify-macer] document split & classify would
               run: both upload chains' processes, the tools and agents they
               call (which the process walker cannot see), their ontologies and
               DMN config. With --collection-id, that loan's documents are not
               failed, stuck, or never classified.
  macer        [only with --classify-macer] MACER would run -- proven WITHOUT
               invoking it: trigger, scheduler and runner processes, the gate
               tools, the invocation ontology, and a complete macer_config whose
               ids resolve. The worker itself has no HTTP surface and is
               reported as such, not passed.

Nothing here writes. Every call is a GET, or a POST to an endpoint whose
contract is search/compare only. The report states the write-call count, and it
is always zero.

    export JAZZX_TOKEN="<bearer token>"
    export JAZZX_USER_ID="<user uuid>"        # x-user-id, required by Builder Studio

    # before the deployment
    python3 scripts/deployment_validation.py --gateway-url https://<host> \
        --label poc --output-dir sanity/

    # add split & classify and MACER readiness (off by default)
    python3 scripts/deployment_validation.py --gateway-url https://<host> \
        --label poc --output-dir sanity/ --classify-macer

    # also compare APP_CONFIG with a reference instance (e.g. prod)
    export JAZZX_TOKEN_B="<bearer token for the reference instance>"
    python3 scripts/deployment_validation.py --gateway-url https://<host> \
        --label poc --output-dir sanity/ \
        --compare-gateway-url https://<other-host> --compare-label prod

Writes one Markdown report, <output-dir>/<label>-<time>.md: summary, failures,
undecided checks, and all deployment checks. Use config_audit.py for a dedicated
APP_CONFIG UUID report.

Exit codes: 0 clean, 1 a check failed (after mode: a check REGRESSED),
2 degraded -- checks could not run, 3 the script could not start.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jazzx_api import (  # noqa: E402
    ApiError,
    JazzXClient,
    extract_items,
    normalize_gateway_url,
    parse_json_value,
    require_value,
)

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Endpoints. Verified against builder-studio-service/openapi/openapi.json and
# the existing automation; see README for provenance.
# ---------------------------------------------------------------------------
# Builder Studio mounts its routes at /api/v1 internally, which is what its
# openapi.json shows, but the gateway fronts it under /builder_studio. Taking the
# prefix from the spec gives a 404 on every BSS call, so it is a setting rather
# than a constant -- other gateways front it differently.
BSS_PREFIX_DEFAULT = "/builder_studio/api/v1"
BSS = BSS_PREFIX_DEFAULT
PROCESS_HEALTH = f"{BSS}/bss_process/api-health"
PROCESS_LIST = f"{BSS}/bss_process/runtime/processes"
PROCESS_BY_KEY = f"{BSS}/bss_process/runtime/processes/{{key}}"
PROCESS_VERSIONS = f"{BSS}/bss_process/runtime/processes/{{key}}/versions"
PROCESS_XML = f"{BSS}/bss_process/resourcedata/{{definition_id}}"
TOOL_LIST = f"{BSS}/bss_tool/"
NATIVE_TOOL_LIST = f"{BSS}/bss_tool/native"
AGENT_LIST = f"{BSS}/bss_agent/"
DEPLOY_HISTORY = f"{BSS}/bss_deployment/deployments/history"



def set_bss_prefix(prefix: str) -> None:
    """Repoint every Builder Studio path at a different gateway mount."""
    global BSS, PROCESS_HEALTH, PROCESS_LIST, PROCESS_BY_KEY, PROCESS_VERSIONS
    global PROCESS_XML, TOOL_LIST, NATIVE_TOOL_LIST, AGENT_LIST, DEPLOY_HISTORY
    BSS = "/" + prefix.strip("/")
    PROCESS_HEALTH = f"{BSS}/bss_process/api-health"
    PROCESS_LIST = f"{BSS}/bss_process/runtime/processes"
    PROCESS_BY_KEY = f"{BSS}/bss_process/runtime/processes/{{key}}"
    PROCESS_VERSIONS = f"{BSS}/bss_process/runtime/processes/{{key}}/versions"
    PROCESS_XML = f"{BSS}/bss_process/resourcedata/{{definition_id}}"
    TOOL_LIST = f"{BSS}/bss_tool/"
    NATIVE_TOOL_LIST = f"{BSS}/bss_tool/native"
    AGENT_LIST = f"{BSS}/bss_agent/"
    DEPLOY_HISTORY = f"{BSS}/bss_deployment/deployments/history"


KH = "/knowledge_hub/api/v1"
GLOBAL_CONFIG = f"{KH}/global-config/"
ONTOLOGIES = f"{KH}/reasoning/ontologies"
ENTITIES = f"{KH}/reasoning/entities"
COLLECTIONS = f"{KH}/collections"
COLLECTION_DOCS = f"{KH}/collections/{{collection_id}}/documents"

ASSISTANT_PROJECTS = "/assistant/api/v1/projects"
ASSISTANT_PROJECT = "/assistant/api/v1/project/{project_id}"
ASSISTANT_RESOURCES = "/assistant/api/v1/project/resources"

PIPELINE = "/mock-server/pipeline"
MOCK_DATA = "/mock-server/api/mock-data"

# The Assistant is the service Luna runs in front of. Luna is an ordinary kernel
# agent by name -- assistant/app/conversation/api.py pins LUNA_AGENT_NAME =
# "luna_agent", and the fulfilment UI pins the same string -- but it takes a
# different code path from every other agent: `create_new_japes_conversation`,
# which answers over the JAPES queue rather than inline. So Luna can be perfectly
# configured and still never reply, and no check on the agent record alone would
# show it. The message-trace checks below are what catch that.
LUNA_AGENT_NAME = "luna_agent"
ASSISTANT = "/assistant/api/v1"
ASSISTANT_AGENTS = f"{ASSISTANT}/assistant_agents"
ASSISTANT_MCP_HEALTH = f"{ASSISTANT}/mcp/health"
ASSISTANT_MESSAGE_TRACES = f"{ASSISTANT}/message_traces"
ASSISTANT_CONVERSATIONS = f"{ASSISTANT}/conversations"

# The UI is a separate host from the gateway and serves its runtime config from
# /env.js, which is where the federated micro-frontend URLs live. A remote listed
# there but not uploaded gives the browser a 404 for a module it is mid-import of,
# which surfaces to the user as a blank error page and to the operator as nothing
# at all -- no service is down, no API call failed.
FRONTEND_ENV_PATH = "/env.js"
MFE_URL_RE = re.compile(r"""VITE_MICROFRONTEND_URL_(\d+)\s*:\s*['"]([^'"]+)['"]""")
FEDERATE_BASE_RE = re.compile(r"""VITE_FEDERATE_WEB_URL\s*:\s*['"]([^'"]+)['"]""")
INSTANCE_URL_RE = re.compile(r"""VITE_INSTANCE_URL\s*:\s*['"]([^'"]+)['"]""")

KERNEL = "/kernel/api/v1"
KERNEL_AGENTS = f"{KERNEL}/agents"
KERNEL_AGENT = f"{KERNEL}/agents/{{agent_id}}"
KERNEL_TOOLS = f"{KERNEL}/agents/tools"
KERNEL_LLM = f"{KERNEL}/llm"

# Every service the gateway fronts. One being down explains failures that would
# otherwise be read as a dozen unrelated breakages.
def service_probes() -> tuple[tuple[str, str], ...]:
    return (
        ("builder_studio", PROCESS_HEALTH),
        ("assistant", ASSISTANT_PROJECTS),
        ("knowledge_hub", COLLECTIONS),
        ("kernel", KERNEL_TOOLS),
        ("mock_server", PIPELINE),
    )

# 100, not 500: bss_deployment/deployments/history rejects anything larger with a
# 422, and a rejected page is indistinguishable from an empty one to the caller --
# the check then reports "no rows" for what was really a bad request.
PAGE_LIMIT = 100

# ---------------------------------------------------------------------------
# Status vocabulary
# ---------------------------------------------------------------------------
PASS, FAIL, UNKNOWN, SKIPPED, INFO = "pass", "fail", "unknown", "skipped", "info"

GROUPS = ("platform", "deployment", "frontend", "process", "collections", "traces",
          "pipeline", "assistant", "loan", "classify", "macer")
#: Run only when asked for with --classify-macer, never by default.
OPT_IN_GROUPS = ("classify", "macer")
#: Commented out in run_checks for now: the process / tool / agent validation.
DISABLED_GROUPS = ("process",)
DEFAULT_GROUPS = tuple(g for g in GROUPS if g not in OPT_IN_GROUPS + DISABLED_GROUPS)


@dataclass
class CheckResult:
    """One checklist line. `id` must be stable across runs -- the before/after
    diff joins on it, so it encodes the entity's key or name, never an index."""

    group: str
    id: str
    title: str
    status: str
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    remediation: str = ""

    def __post_init__(self) -> None:
        if self.group not in GROUPS:
            raise ValueError(f"unknown group {self.group!r}")
        if self.status not in (PASS, FAIL, UNKNOWN, SKIPPED, INFO):
            raise ValueError(f"unknown status {self.status!r}")


def _ok(group: str, cid: str, title: str, detail: str = "", **ev: Any) -> CheckResult:
    return CheckResult(group, cid, title, PASS, detail, ev)


def _bad(group: str, cid: str, title: str, detail: str, fix: str, **ev: Any) -> CheckResult:
    return CheckResult(group, cid, title, FAIL, detail, ev, fix)


def _unk(group: str, cid: str, title: str, why: str, fix: str = "", **ev: Any) -> CheckResult:
    return CheckResult(group, cid, title, UNKNOWN, why, ev, fix)


def _info(group: str, cid: str, title: str, detail: str, **ev: Any) -> CheckResult:
    return CheckResult(group, cid, title, INFO, detail, ev)


# ---------------------------------------------------------------------------
# Redaction. Nothing secret reaches a report file or the terminal.
# ---------------------------------------------------------------------------
SECRET_KEY_RE = re.compile(
    r"token|secret|password|passwd|apikey|api_key|authorization|connection_?string"
    r"|subscription|client_secret|sas|credential",
    re.I,
)
BEARER_RE = re.compile(r"\b(?:ey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)")


def redact(value: Any) -> Any:
    """Strip anything secret-shaped, by key name and by JWT shape."""
    if isinstance(value, dict):
        return {
            k: ("<redacted>" if SECRET_KEY_RE.search(str(k)) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return BEARER_RE.sub("<redacted>", value)
    return value


# ---------------------------------------------------------------------------
# HTTP helpers layered over JazzXClient
# ---------------------------------------------------------------------------
class Probe:
    """Counts every call and refuses to make a write one.

    The guarantee this tool sells is that it never mutates the instance. A
    guarantee asserted in a docstring is worth nothing, so it is enforced here
    and the resulting count is published in the report.
    """

    READ_ONLY_POSTS = (
        "/api/2.0/mlflow/",  # MLflow search endpoints are POST-only by design
    )

    def __init__(self, client: JazzXClient) -> None:
        self.client = client
        self.reads = 0
        self.writes_attempted = 0
        self.errors: list[str] = []
        # Status of the first page of each list call, so a caller can tell an
        # empty collection from a request the server refused.
        self.list_status: dict[str, int] = {}

    def _guard(self, method: str, path: str) -> None:
        if method.upper() in ("GET", "HEAD", "OPTIONS"):
            return
        if method.upper() == "POST" and any(p in path for p in self.READ_ONLY_POSTS):
            return
        self.writes_attempted += 1
        raise AssertionError(f"refusing a write call: {method} {path}")

    def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> tuple[int, Any]:
        """Returns (status_code, parsed_body_or_None). Never raises on HTTP status."""
        self._guard("GET", path)
        self.reads += 1
        widened = tuple(sorted(set(expected) | {400, 401, 403, 404, 405, 500, 502, 503}))
        try:
            response = self.client.request("GET", path, params=params, expected=widened)
        except ApiError as exc:
            self.errors.append(f"GET {path}: {exc}")
            return 0, None
        if not response.content:
            return response.status_code, None
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, response.text

    def items(self, path: str, **params: Any) -> list[dict[str, Any]]:
        """Page a list endpoint. Stops on a short page or a non-200."""
        out: list[dict[str, Any]] = []
        skip = 0
        while True:
            code, body = self.get(path, params={"skip": skip, "limit": PAGE_LIMIT, **params})
            if skip == 0:
                self.list_status[path] = code
            if code != 200 or body is None:
                return out
            page = extract_items(
                body, keys=("items", "data", "results", "entities", "processes", "tools", "agents",
                            "deployments")
            )
            if skip and page and out[-len(page):] == page:
                # The endpoint ignores skip and repeats its first page; without this
                # the loop would make up to 1000 requests before giving up.
                self.errors.append(f"GET {path}: pagination repeated a page; stopped at {len(out)}")
                return out
            out.extend(page)
            if len(page) < PAGE_LIMIT:
                return out
            skip += PAGE_LIMIT
            if skip > 100_000:
                return out


# ---------------------------------------------------------------------------
# Group A -- deployment identity
# ---------------------------------------------------------------------------
def _az(*args: str, timeout: float = 60.0) -> tuple[bool, Any]:
    """Run an `az` command, returning (ok, parsed_json_or_message)."""
    if shutil.which("az") is None:
        return False, "the az CLI is not installed"
    try:
        proc = subprocess.run(
            ["az", *args, "--output", "json"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"az {' '.join(args[:2])} timed out after {timeout:.0f}s"
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout).strip().splitlines()
        hint = message[0] if message else f"az exited {proc.returncode}"
        if "az login" in (proc.stderr or ""):
            hint = "not logged in to Azure (run `az login`)"
        return False, hint
    try:
        return True, json.loads(proc.stdout or "null")
    except json.JSONDecodeError:
        return False, "az returned output that is not JSON"


def _azure_lookup(hostname: str) -> tuple[str, dict[str, Any] | None, str]:
    """Find the Azure resource serving `hostname`.

    Returns (kind, resource, reason). A hostname behind Front Door or an
    application gateway will not match either list, and that is reported as
    unknown rather than guessed at.
    """
    ok, apps = _az("containerapp", "list", "--query",
                   "[].{name:name,rg:resourceGroup,fqdn:properties.configuration.ingress.fqdn}")
    if ok and isinstance(apps, list):
        for app in apps:
            if str(app.get("fqdn") or "").lower() == hostname.lower():
                got, detail = _az("containerapp", "show", "--name", str(app["name"]),
                                  "--resource-group", str(app["rg"]))
                if got:
                    return "containerapp", detail, ""
    elif not ok:
        return "", None, str(apps)

    ok, funcs = _az("functionapp", "list", "--query",
                    "[].{name:name,rg:resourceGroup,host:defaultHostName}")
    if ok and isinstance(funcs, list):
        for fn in funcs:
            if str(fn.get("host") or "").lower() == hostname.lower():
                got, detail = _az("functionapp", "show", "--name", str(fn["name"]),
                                  "--resource-group", str(fn["rg"]))
                if got:
                    return "functionapp", detail, ""
    elif not ok:
        return "", None, str(funcs)

    return "", None, (
        f"no container app or function app has {hostname} as its hostname; it is "
        "probably behind Front Door or an application gateway"
    )


def _container_identity(resource: dict[str, Any]) -> dict[str, Any]:
    """Pull image, revision and replica state out of an `az containerapp show`."""
    props = resource.get("properties") or {}
    template = props.get("template") or {}
    containers = template.get("containers") or []
    images = [str(c.get("image") or "") for c in containers if c.get("image")]
    ingress = props.get("configuration", {}).get("ingress") or {}
    traffic = ingress.get("traffic") or []
    return {
        "name": resource.get("name"),
        "images": images,
        "active_revision_name": template.get("revisionSuffix") or props.get("latestRevisionName"),
        "latest_ready_revision": props.get("latestReadyRevisionName"),
        "provisioning_state": props.get("provisioningState"),
        "running_status": props.get("runningStatus"),
        "traffic_weights": [
            {"revision": t.get("revisionName") or "latest", "weight": t.get("weight")}
            for t in traffic
        ],
        "min_replicas": (template.get("scale") or {}).get("minReplicas"),
        "max_replicas": (template.get("scale") or {}).get("maxReplicas"),
    }


def check_deployment(probe: Probe, gateway_url: str, *, skip_azure: bool) -> list[CheckResult]:
    g = "deployment"
    out: list[CheckResult] = []

    code, body = probe.get(PROCESS_HEALTH)
    if code == 200:
        out.append(_ok(g, "deployment.api_health", "Builder Studio answers its health check",
                       f"HTTP 200 from {PROCESS_HEALTH}", response=redact(body)))
    elif code == 0:
        out.append(_bad(g, "deployment.api_health", "Builder Studio answers its health check",
                        f"{PROCESS_HEALTH} is unreachable",
                        "Confirm the gateway URL and that the service is running.",
                        path=PROCESS_HEALTH))
    else:
        out.append(_bad(g, "deployment.api_health", "Builder Studio answers its health check",
                        f"HTTP {code} from {PROCESS_HEALTH}",
                        "The service is up but unhealthy; check its logs.",
                        status_code=code))

    history = probe.items(DEPLOY_HISTORY, include_current="true")
    if history:
        newest = max(history, key=lambda d: str(d.get("created_at") or ""))
        out.append(_info(g, "deployment.history", "Most recent recorded deployment",
                         f"{newest.get('git_tag') or '?'} on {newest.get('host_branch') or '?'} "
                         f"at {newest.get('created_at') or '?'}",
                         git_tag=newest.get("git_tag"),
                         host_branch=newest.get("host_branch"),
                         version_num=newest.get("version_num"),
                         created_at=newest.get("created_at"),
                         deployment_count=len(history)))
    else:
        code = probe.list_status.get(DEPLOY_HISTORY, 0)
        why = (f"{DEPLOY_HISTORY} returned no rows" if code == 200
               else f"{DEPLOY_HISTORY} returned HTTP {code or 'a transport error'}")
        out.append(_unk(g, "deployment.history", "Most recent recorded deployment", why,
                        "Only affects provenance reporting; no action if git-backed "
                        "deployment is not in use here.", status_code=code))

    if skip_azure:
        out.append(CheckResult(g, "deployment.azure", "Azure resource identity", SKIPPED,
                               "--skip-azure was given"))
        return out

    hostname = urlparse(gateway_url).hostname or ""
    kind, resource, reason = _azure_lookup(hostname)
    if resource is None:
        out.append(_unk(g, "deployment.azure", "Azure resource identity", reason,
                        "Pass --skip-azure to silence this, or run from a host with "
                        "`az login` and visibility of the subscription.",
                        hostname=hostname))
        return out

    if kind == "containerapp":
        ident = _container_identity(resource)
        out.append(_info(g, "deployment.azure", "Azure resource identity",
                         f"container app {ident['name']} serving {hostname}",
                         **redact(ident)))

        state = str(ident.get("provisioning_state") or "").lower()
        running = str(ident.get("running_status") or "").lower()
        if state in ("succeeded", "") and running in ("running", ""):
            out.append(_ok(g, "deployment.azure.revision_healthy", "Active revision is healthy",
                           f"provisioningState={ident['provisioning_state']}, "
                           f"runningStatus={ident['running_status']}"))
        else:
            out.append(_bad(g, "deployment.azure.revision_healthy", "Active revision is healthy",
                            f"provisioningState={ident['provisioning_state']}, "
                            f"runningStatus={ident['running_status']}",
                            "Inspect the revision in the portal; the new image probably "
                            "failed to start."))

        weights = [w for w in ident["traffic_weights"] if (w.get("weight") or 0) > 0]
        if len(weights) > 1:
            out.append(_info(g, "deployment.azure.traffic_split", "Traffic is on one revision",
                             f"{len(weights)} revisions are taking traffic: "
                             + ", ".join(f"{w['revision']}={w['weight']}%" for w in weights),
                             traffic=weights))
        else:
            out.append(_ok(g, "deployment.azure.traffic_split", "Traffic is on one revision",
                           weights[0]["revision"] if weights else "single revision"))

        if ident.get("min_replicas") == 0:
            out.append(_info(g, "deployment.azure.replicas", "Replicas are provisioned",
                             "minReplicas is 0; the app scales to zero and the first "
                             "request after idle will be slow"))
        else:
            out.append(_ok(g, "deployment.azure.replicas", "Replicas are provisioned",
                           f"minReplicas={ident.get('min_replicas')}"))
    else:
        props = resource.get("properties") or {}
        ident = {
            "name": resource.get("name"),
            "state": props.get("state"),
            "enabled": props.get("enabled"),
            "host_names": props.get("hostNames"),
            "last_modified": props.get("lastModifiedTimeUtc"),
        }
        out.append(_info(g, "deployment.azure", "Azure resource identity",
                         f"function app {ident['name']} serving {hostname}", **redact(ident)))
        if str(ident.get("state") or "").lower() == "running" and ident.get("enabled") is not False:
            out.append(_ok(g, "deployment.azure.revision_healthy", "Function app is running",
                           f"state={ident['state']}"))
        else:
            out.append(_bad(g, "deployment.azure.revision_healthy", "Function app is running",
                            f"state={ident.get('state')}, enabled={ident.get('enabled')}",
                            "Start the function app, or check its deployment slot."))
    return out


# ---------------------------------------------------------------------------
# Group B -- process / tool / agent closure
#
# Reference shapes, read off the real BPMN in mortgage-app rather than assumed:
#
#   tool           execution.setVariable("toolName", "los_fetch_loan_snapshot")
#                  execution.setVariable("tool_name", "truv_exchange_token_tool")
#   sub-process    <callActivity calledElement="DocumentProcessingV2" />
#                  <callActivity calledElement="${'macerTriggerSender'}" />
#   java delegate  flowable:delegateExpression="${kernelToolInvocationSyncDelegate}"
#
# The leading quote in the tool pattern matters: without it `real_estate_agent_name`
# matches the agent pattern and the report fills with mortgage domain entities that
# have nothing to do with agents.
# ---------------------------------------------------------------------------
TOOL_REF_RE = re.compile(r"""["'](?:tool_?[Nn]ame|tool_?[Kk]ey)["']\s*,\s*["']([A-Za-z0-9_.\-]+)["']""")
AGENT_REF_RE = re.compile(
    r"""["'](?:agent_?[Nn]ame|agent_?[Kk]ey)["']\s*,\s*["']([A-Za-z0-9_.\-]+)["']"""
)
EXPR_LITERAL_RE = re.compile(r"^\$\{\s*['\"]([A-Za-z0-9_.\-]+)['\"]\s*\}$")
DELEGATE_RE = re.compile(r"^\$\{\s*([A-Za-z0-9_.]+)\s*\}$")
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attr(elem: ET.Element, name: str) -> str | None:
    """Attribute lookup that ignores the namespace prefix."""
    for key, value in elem.attrib.items():
        if _localname(key) == name:
            return value
    return None


def _extract_xml(body: Any) -> str | None:
    """Pull a BPMN document out of whatever shape the endpoint returned.

    resourcedata answers with raw application/xml; the by-key endpoint answers
    with JSON carrying an `xml` field, sometimes inside a `data` envelope.
    """
    if isinstance(body, str):
        return body if "<" in body[:200] else None
    if not isinstance(body, dict):
        return None
    for candidate in (body, body.get("data")):
        if isinstance(candidate, dict):
            value = candidate.get("xml")
            if isinstance(value, str) and value.strip():
                return value
    return None


@dataclass
class Dependency:
    kind: str  # "tool" | "subprocess" | "agent" | "delegate"
    ref: str
    where: str  # the BPMN element id it was found on
    dynamic: bool = False


def extract_dependencies(xml_text: str) -> tuple[list[Dependency], str | None]:
    """Every tool, sub-process, agent and delegate a process definition references.

    Returns (dependencies, parse_error). A definition that will not parse yields
    an empty list and the error, which the caller turns into `unknown` -- an
    unparseable definition is not evidence that a tool is missing.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        return [], f"BPMN XML did not parse: {exc}"

    deps: list[Dependency] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, ref: str, where: str, dynamic: bool = False) -> None:
        ref = ref.strip()
        if not ref or (kind, ref) in seen:
            return
        seen.add((kind, ref))
        deps.append(Dependency(kind, ref, where, dynamic))

    def walk(elem: ET.Element, owner: str) -> None:
        """Descend carrying the nearest ancestor that has an id.

        Tool names live in the <script> child of a <scriptTask>, and a <script>
        element has no id of its own. Reporting `script` as the location would
        leave the reader hunting through the BPMN, so the enclosing task's id is
        carried down instead.
        """
        tag = _localname(elem.tag)
        where = _attr(elem, "id") or owner or tag

        if tag == "callActivity":
            called = _attr(elem, "calledElement")
            if called:
                literal = EXPR_LITERAL_RE.match(called)
                if literal:
                    add("subprocess", literal.group(1), where)
                elif called.startswith("${"):
                    # Resolved from a process variable at run time; which process it
                    # reaches cannot be known statically, so it is recorded and not judged.
                    add("subprocess", called, where, dynamic=True)
                else:
                    add("subprocess", called, where)

        delegate = _attr(elem, "delegateExpression")
        if delegate:
            match = DELEGATE_RE.match(delegate)
            add("delegate", match.group(1) if match else delegate, where)

        for text in (elem.text or "", elem.tail or ""):
            if not text or len(text) < 8:
                continue
            for name in TOOL_REF_RE.findall(text):
                add("tool", name, where)
            for name in AGENT_REF_RE.findall(text):
                add("agent", name, where)

        for child in elem:
            walk(child, where)

    walk(root, "")
    return deps, None


def _index_by_name(items: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for item in items:
        for key in ("name", "tool_name", "key", "display_name"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                index.setdefault(value.strip().lower(), item)
    return index


def _is_inactive(item: dict[str, Any]) -> str:
    """Return a reason when an entity exists but is not usable, else ''."""
    status = str(item.get("status") or "").strip().lower()
    if status in ("", "active", "published", "enabled", "deployed"):
        return ""
    return f"status is {status!r}"


def process_progress(stream: Any = None) -> Any:
    """progress(done, total, key) for check_process_closure: one line, rewritten in place
    on a terminal, otherwise a line every 10 processes so logs stay short."""
    stream = stream or sys.stderr
    tty = stream.isatty()

    def report(done: int, total: int, key: str) -> None:
        line = f"  process {done}/{total}  {key}"
        if tty:
            stream.write("\r" + line[:100].ljust(100))
            if done == total:
                stream.write("\n")
        elif done == 1 or done == total or done % 10 == 0:
            stream.write(line + "\n")
        stream.flush()
    return report


def check_process_closure(probe: Probe, *, max_processes: int | None,
                          progress: Any = None) -> list[CheckResult]:
    """`progress`, if given, is called as progress(done, total, key) for each process."""
    g = "process"
    out: list[CheckResult] = []

    def listed(what: str, path: str) -> list[dict[str, Any]]:
        print(f"  listing {what} ...", end="", file=sys.stderr, flush=True)
        started = time.monotonic()
        rows = probe.items(path)
        code = probe.list_status.get(path, 0)
        status = "" if code == 200 else f", HTTP {code or 'no response'}"
        print(f" {len(rows)} ({time.monotonic() - started:.1f}s{status})",
              file=sys.stderr, flush=True)
        return rows

    custom_tools = listed("custom tools", TOOL_LIST)
    native_tools = listed("native tools", NATIVE_TOOL_LIST)
    agents = listed("agents", AGENT_LIST)
    processes = listed("processes", PROCESS_LIST)

    if not processes:
        out.append(_unk(g, "process.inventory", "Processes can be listed",
                        f"{PROCESS_LIST} returned nothing -- wrong x-user-id header, "
                        "insufficient permission, or genuinely no processes",
                        "Set JAZZX_USER_ID to a user with Builder Studio access."))
        return out

    tool_index = _index_by_name(custom_tools)
    native_index = _index_by_name(native_tools)
    agent_index = _index_by_name(agents)
    agent_by_id = {str(a.get("id")): a for a in agents if a.get("id")}
    process_keys = {str(p.get("key")).lower() for p in processes if p.get("key")}

    out.append(_info(g, "process.inventory", "Builder Studio inventory",
                     f"{len(processes)} processes, {len(custom_tools)} custom tools, "
                     f"{len(native_tools)} native tools, {len(agents)} agents",
                     processes=len(processes), custom_tools=len(custom_tools),
                     native_tools=len(native_tools), agents=len(agents)))

    # ---- agents: the "is this tool agent actually deployed" question ----
    for agent in sorted(agents, key=lambda a: str(a.get("name") or "")):
        name = str(agent.get("name") or agent.get("id") or "?")
        slug = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_") or "unnamed"
        atype = str(agent.get("type") or "?")

        # An agent lives in Builder Studio but RUNS in the kernel. A null
        # kernel_agent_id means it was authored and never pushed -- the process
        # that calls it will fail at run time with the agent looking fine in the UI.
        if "kernel_agent_id" in agent:
            if agent.get("kernel_agent_id"):
                out.append(_ok(g, f"process.agent.{slug}.deployed",
                               f"Agent {name!r} is deployed to the kernel",
                               f"kernel_agent_id={agent['kernel_agent_id']}", type=atype))
            else:
                out.append(_bad(g, f"process.agent.{slug}.deployed",
                                f"Agent {name!r} is deployed to the kernel",
                                "kernel_agent_id is null: the agent exists in Builder Studio "
                                "but was never pushed to the kernel",
                                f"Re-sync or re-publish agent {name!r}.",
                                type=atype, agent_id=agent.get("id")))

        reason = _is_inactive(agent)
        if reason:
            out.append(_bad(g, f"process.agent.{slug}.active", f"Agent {name!r} is active",
                            reason, f"Publish or re-enable agent {name!r}.",
                            type=atype, status=agent.get("status")))

        for tool_name in agent.get("tools") or []:
            key = str(tool_name).strip().lower()
            tslug = re.sub(r"[^a-z0-9_]+", "_", key).strip("_") or "unnamed"
            cid = f"process.agent.{slug}.tool.{tslug}"
            title = f"Agent {name!r} can reach tool {tool_name!r}"
            found = tool_index.get(key) or native_index.get(key)
            if found is None:
                out.append(_bad(g, cid, title,
                                f"tool {tool_name!r} is not deployed on this instance "
                                "(neither custom nor native)",
                                f"Deploy tool {tool_name!r}, or remove it from agent {name!r}.",
                                agent=name, tool=tool_name))
            elif _is_inactive(found):
                out.append(_bad(g, cid, title,
                                f"tool {tool_name!r} exists but {_is_inactive(found)}",
                                f"Publish tool {tool_name!r}.",
                                agent=name, tool=tool_name, status=found.get("status")))
            else:
                out.append(_ok(g, cid, title, f"resolved to {found.get('id') or tool_name}",
                               agent=name, tool=tool_name))

        for callable_id in agent.get("callable_agent_ids") or []:
            cslug = re.sub(r"[^a-z0-9]+", "", str(callable_id).lower())[:12]
            cid = f"process.agent.{slug}.calls.{cslug}"
            title = f"Agent {name!r} can reach callable agent {callable_id}"
            target = agent_by_id.get(str(callable_id))
            if target is None:
                out.append(_bad(g, cid, title,
                                f"callable_agent_ids points at {callable_id}, which is not "
                                "an agent on this instance",
                                f"Deploy the missing agent, or drop the reference from {name!r}.",
                                agent=name, callable_agent_id=callable_id))
            else:
                out.append(_ok(g, cid, title, f"resolved to {target.get('name')}"))

    # ---- processes ----
    ordered = sorted(processes, key=lambda p: str(p.get("key") or ""))
    if max_processes is not None:
        ordered = ordered[:max_processes]

    for position, proc in enumerate(ordered, start=1):
        key = str(proc.get("key") or "").strip()
        if progress is not None:
            progress(position, len(ordered), key)
        if not key:
            continue
        slug = re.sub(r"[^a-z0-9_]+", "_", key.lower()).strip("_")

        # Deployed to Flowable? A process row with no flowable_deployment_id is a
        # Builder Studio record of something the engine has never seen.
        dep_id = proc.get("flowable_deployment_id")
        dep_err = proc.get("flowable_deployment_error")
        if dep_err:
            out.append(_bad(g, f"process.{slug}.engine", f"Process {key!r} reached the engine",
                            f"flowable_deployment_error: {str(dep_err)[:200]}",
                            f"Re-publish {key!r} and read the engine's rejection.",
                            key=key))
        elif dep_id:
            out.append(_ok(g, f"process.{slug}.engine", f"Process {key!r} reached the engine",
                           f"flowable_deployment_id={dep_id}", key=key))
        else:
            out.append(_bad(g, f"process.{slug}.engine", f"Process {key!r} reached the engine",
                            "no flowable_deployment_id: Builder Studio has the definition but "
                            "the engine never received it",
                            f"Publish {key!r}.", key=key, status=proc.get("status")))

        proc_type = str(proc.get("type") or "bpmn")
        definition_id: str | None = None
        code, versions = probe.get(PROCESS_VERSIONS.format(key=key),
                                   params={"process_type": proc_type})
        if code == 200 and isinstance(versions, dict):
            if versions.get("sync_warning"):
                out.append(_bad(g, f"process.{slug}.sync",
                                f"Process {key!r} is in sync with the engine",
                                f"sync_warning: {str(versions['sync_warning'])[:200]}",
                                "Builder Studio and Flowable disagree about the published "
                                f"version of {key!r}; re-publish to reconcile.", key=key))
            else:
                out.append(_ok(g, f"process.{slug}.sync",
                               f"Process {key!r} is in sync with the engine",
                               f"{versions.get('total_published', 0)} published version(s)"))

            published = versions.get("published_versions") or []
            latest = next((v for v in published if v.get("is_latest")), None)
            # ProcessVersionItem.id IS the process definition id -- it carries
            # flowable_deployment_id as a separate field. The resourcedata endpoint
            # wants the definition id; handing it the deployment id 404s on every
            # process, which is silent because the caller falls through to another
            # lookup and only reports "could not be fetched".
            chosen = latest or (published[0] if published else None)
            if chosen and chosen.get("id"):
                definition_id = str(chosen["id"])
            if latest and latest.get("suspended"):
                out.append(_bad(g, f"process.{slug}.suspended",
                                f"Process {key!r} is not suspended",
                                f"version {latest.get('version')} is suspended in the engine: "
                                "it is deployed but will not start",
                                f"Activate the process definition for {key!r}.", key=key))
            draft = versions.get("draft_version")
            if draft:
                out.append(_info(g, f"process.{slug}.draft",
                                 f"Process {key!r} has unpublished work",
                                 f"draft version {draft} exists above the published version",
                                 key=key, draft_version=draft))

        # Dependency closure.
        xml_text = proc.get("xml")
        attempts: list[str] = []
        for path, params in (
            (PROCESS_XML.format(definition_id=definition_id) if definition_id else None,
             {"process_type": proc_type}),
            (PROCESS_BY_KEY.format(key=key), {"process_type": proc_type}),
            (PROCESS_BY_KEY.format(key=key), None),
        ):
            if xml_text or path is None:
                continue
            code, body = probe.get(path, params=params)
            attempts.append(f"{path} -> HTTP {code or 'error'}")
            if code != 200:
                continue
            xml_text = _extract_xml(body)

        if not xml_text:
            out.append(_unk(g, f"process.{slug}.dependencies",
                            f"Dependencies of {key!r} resolve",
                            "the definition XML could not be fetched: "
                            + "; ".join(attempts),
                            "Without the XML the tool/sub-process references in this "
                            "process cannot be checked.",
                            key=key, attempts=attempts,
                            definition_id=definition_id, process_type=proc_type))
            continue

        deps, parse_error = extract_dependencies(str(xml_text))
        if parse_error:
            out.append(_unk(g, f"process.{slug}.dependencies",
                            f"Dependencies of {key!r} resolve", parse_error,
                            "The stored definition is not valid BPMN XML.", key=key))
            continue

        if not deps:
            out.append(_info(g, f"process.{slug}.dependencies",
                             f"Dependencies of {key!r} resolve",
                             "the definition references no tools, sub-processes or agents",
                             key=key))
            continue

        for dep in deps:
            dslug = re.sub(r"[^a-z0-9_]+", "_", dep.ref.lower()).strip("_")[:60] or "ref"
            cid = f"process.{slug}.{dep.kind}.{dslug}"
            title = f"Process {key!r} can reach {dep.kind} {dep.ref!r}"
            ev = {"process": key, "kind": dep.kind, "ref": dep.ref, "element": dep.where}

            if dep.dynamic:
                out.append(_info(g, cid, title,
                                 f"{dep.ref} is resolved from a variable at run time and "
                                 "cannot be checked statically", **ev))
                continue

            if dep.kind == "tool":
                found = tool_index.get(dep.ref.lower()) or native_index.get(dep.ref.lower())
                if found is None:
                    out.append(_bad(g, cid, title,
                                    f"tool {dep.ref!r} is referenced by element "
                                    f"{dep.where!r} but is not deployed here",
                                    f"Deploy tool {dep.ref!r} before running {key!r}.", **ev))
                elif _is_inactive(found):
                    out.append(_bad(g, cid, title,
                                    f"tool {dep.ref!r} exists but {_is_inactive(found)}",
                                    f"Publish tool {dep.ref!r}.", **ev))
                else:
                    out.append(_ok(g, cid, title, "deployed", **ev))

            elif dep.kind == "subprocess":
                if dep.ref.lower() in process_keys:
                    out.append(_ok(g, cid, title, "deployed"))
                else:
                    out.append(_bad(g, cid, title,
                                    f"callActivity {dep.where!r} calls process {dep.ref!r}, "
                                    "which is not deployed here",
                                    f"Deploy process {dep.ref!r}.", **ev))

            elif dep.kind == "agent":
                found = agent_index.get(dep.ref.lower())
                if found is None:
                    out.append(_bad(g, cid, title,
                                    f"agent {dep.ref!r} is not deployed here",
                                    f"Deploy agent {dep.ref!r}.", **ev))
                else:
                    out.append(_ok(g, cid, title, f"resolved to {found.get('id')}"))

            else:  # delegate -- a Spring bean inside the process engine
                out.append(_info(g, cid, title,
                                 f"Java delegate {dep.ref!r}; its presence is a property of "
                                 "the engine image and is not checkable over the API", **ev))

    # ---- orphans: informational, never a failure ----
    referenced: set[str] = set()
    for res in out:
        if res.evidence.get("kind") == "tool" and res.evidence.get("ref"):
            referenced.add(str(res.evidence["ref"]).lower())
        if res.evidence.get("tool"):
            referenced.add(str(res.evidence["tool"]).lower())
    orphans = sorted(
        str(t.get("name")) for t in custom_tools
        if t.get("name") and str(t["name"]).lower() not in referenced
    )
    if orphans:
        out.append(_info(g, "process.orphan_tools", "Tools nothing references",
                         f"{len(orphans)} custom tool(s) are deployed but referenced by no "
                         f"process or agent: {', '.join(orphans[:12])}"
                         + (" ..." if len(orphans) > 12 else ""),
                         orphan_tools=orphans))
    return out


# ---------------------------------------------------------------------------
# Group C -- collections and ontologies
# ---------------------------------------------------------------------------
def _entity_type_count(ontology: dict[str, Any]) -> int | None:
    """How many entity types an ontology defines, or None when the payload does
    not say. None and 0 mean very different things and must not be merged: one is
    "this response cannot answer", the other is "the ontology is empty"."""
    for key in ("entity_types", "entityTypes", "types", "classes", "entities"):
        value = ontology.get(key)
        if isinstance(value, (list, dict)):
            return len(value)
    schema = ontology.get("schema")
    if isinstance(schema, str):
        schema = parse_json_value(schema)
    if isinstance(schema, dict):
        for key in ("entity_types", "types", "definitions", "properties"):
            value = schema.get(key)
            if isinstance(value, (list, dict)):
                return len(value)
    return None

def check_collections(
    probe: Probe, client: JazzXClient, *, collection_id: str | None
) -> list[CheckResult]:
    g = "collections"
    out: list[CheckResult] = []

    # Ontologies exist and carry entity types.
    ontologies = probe.items(ONTOLOGIES)
    code = probe.list_status.get(ONTOLOGIES, 0)
    if not ontologies and code != 200:
        # A refused or failed call says nothing about whether ontologies exist --
        # an expired token here used to read as "no ontologies loaded".
        fix = ("The token was rejected; refresh it and run again." if code in (401, 403)
               else "The ontology list could not be fetched; check knowledge_hub.")
        out.append(_unk(g, "collections.ontologies.present", "Ontologies are loaded",
                        f"{ONTOLOGIES} returned HTTP {code or 'a transport error'}",
                        fix, status_code=code))
    elif not ontologies:
        out.append(_bad(g, "collections.ontologies.present", "Ontologies are loaded",
                        f"{ONTOLOGIES} returned none",
                        "Nothing that reads the ontology can work; load the ontologies.",
                        status_code=code))
    else:
        out.append(_ok(g, "collections.ontologies.present", "Ontologies are loaded",
                       f"{len(ontologies)} ontology/ontologies", count=len(ontologies)))
        for onto in ontologies:
            name = str(onto.get("name") or onto.get("id") or "?")
            slug = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_") or "ontology"
            count = _entity_type_count(onto)
            detail_code = None
            # The list response omits entity types, so an empty count there means
            # nothing. Fetch the ontology itself before concluding anything.
            if count is None and onto.get("id"):
                detail_code, detail = probe.get(f"{ONTOLOGIES}/{onto['id']}")
                if detail_code == 200 and isinstance(detail, dict):
                    count = _entity_type_count(detail)
            cid = f"collections.ontology.{slug}.populated"
            title = f"Ontology {name!r} defines entity types"
            if count is None:
                out.append(_unk(g, cid, title,
                                "neither the list row nor the per-ontology fetch "
                                f"(HTTP {detail_code or 'not attempted'}) exposes an "
                                "entity-type field",
                                "The response shape changed; this check needs updating.",
                                ontology_id=onto.get("id"), status_code=detail_code))
            elif count == 0:
                out.append(_bad(g, cid, title,
                                "the ontology exists but defines no entity types",
                                "Anything reasoning over it has nothing to match; "
                                "re-upload the ontology.", ontology_id=onto.get("id")))
            else:
                out.append(_ok(g, cid, title, f"{count} type(s)",
                               ontology_id=onto.get("id"), type_count=count))

    ontology_ids = {str(o.get("id")) for o in ontologies if o.get("id")}

    # The target collection.
    collections = probe.items(COLLECTIONS)
    out.append(_info(g, "collections.inventory", "Collections are listable",
                     f"{len(collections)} collection(s)", count=len(collections)))

    target = collection_id
    if target is None:
        out.append(CheckResult(g, "collections.target", "Target collection is readable", SKIPPED,
                               "no --collection-id was given"))
        return out

    code, body = probe.get(f"{COLLECTIONS}/{target}")
    if code != 200:
        out.append(_bad(g, "collections.target", "Target collection is readable",
                        f"HTTP {code} for collection {target}",
                        "The collection id is wrong or the collection was deleted.",
                        collection_id=target))
        return out
    out.append(_ok(g, "collections.target", "Target collection is readable",
                   str((body or {}).get("name") or target), collection_id=target))

    entities = probe.items(ENTITIES, collection_id=target)
    by_type: dict[str, int] = {}
    orphaned: list[str] = []
    for ent in entities:
        by_type[str(ent.get("entity_type") or "?")] = (
            by_type.get(str(ent.get("entity_type") or "?"), 0) + 1
        )
        oid = str(ent.get("ontology_id") or "")
        if oid and ontology_ids and oid not in ontology_ids:
            orphaned.append(f"{ent.get('id')} -> {oid}")

    if entities:
        out.append(_ok(g, "collections.target.entities", "Target collection has entities",
                       f"{len(entities)} entities across {len(by_type)} type(s)",
                       counts_by_type=by_type))
    else:
        out.append(_bad(g, "collections.target.entities", "Target collection has entities",
                        "the collection resolves but contains no reasoning entities",
                        "Anything reading this collection returns empty rather than "
                        "erroring; confirm ingestion ran.", collection_id=target))

    if orphaned:
        out.append(_bad(g, "collections.target.orphan_entities",
                        "No entity points at a missing ontology",
                        f"{len(orphaned)} entity/entities carry an ontology_id that does not "
                        f"exist: {', '.join(orphaned[:5])}"
                        + (" ..." if len(orphaned) > 5 else ""),
                        "These entities are unreadable by anything ontology-aware; "
                        "re-ingest them or restore the ontology.",
                        orphan_count=len(orphaned), sample=orphaned[:20]))
    elif entities:
        out.append(_ok(g, "collections.target.orphan_entities",
                       "No entity points at a missing ontology",
                       f"all {len(entities)} entities reference a live ontology"))
    return out


# ---------------------------------------------------------------------------
# Group D -- MLflow traces
#
# Tracing is MLflow-based (kernel/app/agent/mlflow_service.py: autolog for OpenAI
# and Anthropic under an experiment/run context). Two faults hide behind one
# symptom and must not be merged: traces MISSING (none arrived) and traces
# INCOMPLETE (they arrived truncated). The causes are unrelated.
# ---------------------------------------------------------------------------
def _mlflow_post(url: str, path: str, body: dict[str, Any], timeout: float) -> tuple[int, Any]:
    import requests  # noqa: PLC0415

    try:
        response = requests.post(
            f"{url.rstrip('/')}{path}", json=body, timeout=timeout,
            headers={"Content-Type": "application/json"},
        )
    except requests.RequestException as exc:
        return 0, str(exc)
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, response.text


def check_traces(
    *, tracking_uri: str | None, experiment: str | None, window_hours: float, timeout: float
) -> list[CheckResult]:
    g = "traces"
    if not tracking_uri:
        return [_unk(g, "traces.reachable", "MLflow tracking server is reachable",
                     "no tracking URI: pass --mlflow-uri or set MLFLOW_TRACKING_URI",
                     "Without it, a silent loss of tracing cannot be detected.")]

    out: list[CheckResult] = []
    code, body = _mlflow_post(
        tracking_uri, "/api/2.0/mlflow/experiments/search", {"max_results": 1000}, timeout
    )
    if code != 200:
        return [_unk(g, "traces.reachable", "MLflow tracking server is reachable",
                     f"experiments/search returned {code or 'a connection error'}: "
                     f"{str(body)[:200]}",
                     "Check MLFLOW_TRACKING_URI and network access from this host.",
                     tracking_uri=tracking_uri)]

    experiments = (body or {}).get("experiments") or []
    out.append(_ok(g, "traces.reachable", "MLflow tracking server is reachable",
                   f"{len(experiments)} experiment(s)", tracking_uri=tracking_uri))

    chosen = None
    if experiment:
        chosen = next((e for e in experiments if str(e.get("name")) == experiment), None)
        if chosen is None:
            names = ", ".join(sorted(str(e.get("name")) for e in experiments)[:10])
            out.append(_bad(g, "traces.experiment", "The expected experiment exists",
                            f"no experiment named {experiment!r}; found: {names or '<none>'}",
                            "Traces are being written somewhere else, or not at all.",
                            expected=experiment))
            return out
        out.append(_ok(g, "traces.experiment", "The expected experiment exists",
                       f"{experiment} (id {chosen.get('experiment_id')})"))
    else:
        if not experiments:
            out.append(_bad(g, "traces.experiment", "At least one experiment exists",
                            "the tracking server has no experiments at all",
                            "Nothing has ever logged a trace here."))
            return out
        chosen = max(experiments, key=lambda e: str(e.get("last_update_time") or ""))
        out.append(_info(g, "traces.experiment", "Experiment selected for the trace check",
                         f"no --mlflow-experiment given; using the most recently updated: "
                         f"{chosen.get('name')}"))

    exp_id = str(chosen.get("experiment_id"))
    cutoff_ms = int((time.time() - window_hours * 3600) * 1000)
    code, body = _mlflow_post(
        tracking_uri, "/api/2.0/mlflow/runs/search",
        {"experiment_ids": [exp_id], "max_results": 200,
         "order_by": ["attributes.start_time DESC"]},
        timeout,
    )
    if code != 200:
        out.append(_unk(g, "traces.recent", "Traces arrived recently",
                        f"runs/search returned {code}: {str(body)[:200]}", "",
                        experiment_id=exp_id))
        return out

    runs = (body or {}).get("runs") or []
    recent = [r for r in runs if int((r.get("info") or {}).get("start_time") or 0) >= cutoff_ms]
    last_ms = max((int((r.get("info") or {}).get("start_time") or 0) for r in runs), default=0)
    last_seen = (
        datetime.fromtimestamp(last_ms / 1000, tz=timezone.utc).isoformat() if last_ms else "never"
    )

    if recent:
        out.append(_ok(g, "traces.recent", "Traces arrived recently",
                       f"{len(recent)} run(s) in the last {window_hours:g}h; "
                       f"most recent {last_seen}",
                       recent_count=len(recent), window_hours=window_hours, last_seen=last_seen))
    else:
        out.append(_bad(g, "traces.recent", "Traces arrived recently",
                        f"no runs in the last {window_hours:g}h; last trace seen {last_seen}",
                        "Traces are not being written. Check the kernel's "
                        "mlflow_tracking_uri and that autolog is enabled.",
                        recent_count=0, window_hours=window_hours, last_seen=last_seen))

    # Completeness, on whatever recent runs exist. A run that started and never
    # ended, or carries no metrics, is a BROKEN trace, not a missing one.
    sample = recent[:25]
    if not sample:
        out.append(CheckResult(g, "traces.complete", "Recent traces are complete", SKIPPED,
                               "no recent runs to inspect"))
        return out

    broken: list[str] = []
    for run in sample:
        info = run.get("info") or {}
        data = run.get("data") or {}
        run_id = str(info.get("run_id") or info.get("run_uuid") or "?")
        if str(info.get("status") or "").upper() == "RUNNING" and not info.get("end_time"):
            age_h = (time.time() * 1000 - int(info.get("start_time") or 0)) / 3_600_000
            if age_h > 1:
                broken.append(f"{run_id}: started {age_h:.1f}h ago, never ended")
        elif not info.get("end_time"):
            broken.append(f"{run_id}: no end_time")
        elif not (data.get("metrics") or data.get("tags")):
            broken.append(f"{run_id}: no metrics or tags logged")

    if broken:
        out.append(_bad(g, "traces.complete", "Recent traces are complete",
                        f"{len(broken)} of {len(sample)} sampled runs are truncated: "
                        f"{'; '.join(broken[:4])}" + (" ..." if len(broken) > 4 else ""),
                        "Traces are being started and not finished -- usually the process "
                        "is crashing mid-run, not a tracing outage.",
                        broken_count=len(broken), sampled=len(sample), sample=broken[:20]))
    else:
        out.append(_ok(g, "traces.complete", "Recent traces are complete",
                       f"all {len(sample)} sampled runs ended with data"))
    return out


# ---------------------------------------------------------------------------
# Group E -- loan pipeline
# ---------------------------------------------------------------------------
def check_pipeline(probe: Probe, *, max_rows: int) -> list[CheckResult]:
    g = "pipeline"
    out: list[CheckResult] = []

    code, body = probe.get(PIPELINE)
    if code == 404:
        # sync_loan_to_pipeline.py treats 404 as "never initialised", which is a
        # different thing from an empty pipeline and has a different fix.
        out.append(_bad(g, "pipeline.reachable", "The loan pipeline responds",
                        f"HTTP 404 from {PIPELINE}: the pipeline mock was never initialised",
                        "Seed it with scripts/sync_loan_to_pipeline.py "
                        "--initialize-if-missing.", status_code=404))
        return out
    if code != 200:
        out.append(_bad(g, "pipeline.reachable", "The loan pipeline responds",
                        f"HTTP {code or 'connection error'} from {PIPELINE}",
                        "The loan pipeline page is broken for every user.",
                        status_code=code))
        return out

    rows = extract_items(body, keys=("items", "data", "results", "loans", "pipeline"))
    if not rows and isinstance(body, dict):
        for value in body.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                rows = value
                break

    out.append(_ok(g, "pipeline.reachable", "The loan pipeline responds",
                   f"HTTP 200, {len(rows)} row(s)", row_count=len(rows)))

    if not rows:
        out.append(_bad(g, "pipeline.populated", "The loan pipeline has rows",
                        "the pipeline responds but is empty",
                        "The pipeline page renders blank; re-sync loans into it.",
                        row_count=0))
        return out
    out.append(_ok(g, "pipeline.populated", "The loan pipeline has rows", f"{len(rows)} row(s)"))

    code, _ = probe.get(MOCK_DATA)
    if code == 200:
        out.append(_ok(g, "pipeline.mock_data", "Pipeline mock data is reachable", "HTTP 200"))
    else:
        out.append(_bad(g, "pipeline.mock_data", "Pipeline mock data is reachable",
                        f"HTTP {code or 'connection error'} from {MOCK_DATA}",
                        "The pipeline lists loans whose detail pages will fail.",
                        status_code=code))

    orphans: list[str] = []
    checked = 0
    for row in rows[:max_rows]:
        pid = None
        for key in ("project_id", "projectId", "id", "project"):
            value = row.get(key)
            if isinstance(value, str) and UUID_RE.match(value):
                pid = value
                break
        if not pid:
            continue
        checked += 1
        code, _ = probe.get(ASSISTANT_PROJECT.format(project_id=pid))
        if code != 200:
            label = row.get("loan_number") or row.get("loanNumber") or row.get("name") or pid
            orphans.append(f"{label} -> project {pid} (HTTP {code})")

    if not checked:
        out.append(_unk(g, "pipeline.rows_resolve", "Every pipeline row resolves to a project",
                        "no row carries a recognisable project UUID",
                        "The pipeline payload shape changed; this check needs updating.",
                        row_count=len(rows)))
    elif orphans:
        out.append(_bad(g, "pipeline.rows_resolve", "Every pipeline row resolves to a project",
                        f"{len(orphans)} of {checked} checked rows point at a project that is "
                        f"gone: {'; '.join(orphans[:4])}" + (" ..." if len(orphans) > 4 else ""),
                        "These rows render but 404 when opened; remove them or restore "
                        "the projects.",
                        orphan_count=len(orphans), checked=checked, sample=orphans[:20]))
    else:
        out.append(_ok(g, "pipeline.rows_resolve", "Every pipeline row resolves to a project",
                       f"all {checked} checked row(s) resolve", checked=checked))
    return out


# ---------------------------------------------------------------------------
# Group F -- loan creation sanity, WITHOUT creating a loan
#
# The call list is the one loan-clone-automation/loan_clone.py and entity_creators/
# actually use. Each entry names the write call being vouched for and the
# read-only probe that stands in for it.
# ---------------------------------------------------------------------------
LOAN_CREATION_CALLS = [
    {
        "step": "create the Assistant project",
        "write_call": f"POST {ASSISTANT_PROJECTS}",
        "probe": ASSISTANT_PROJECTS,
    },
    {
        # POST /project/resources has no GET form: a GET is routed to
        # /project/{project_id} and the server tries to parse "resources" as a
        # UUID, giving a 422 that says nothing about whether the step would work.
        # The project list is the honest stand-in -- same service, same auth.
        "step": "attach project resources",
        "write_call": f"POST {ASSISTANT_RESOURCES}",
        "probe": ASSISTANT_PROJECTS,
    },
    {
        "step": "create reasoning entities (LoanCore, Borrower, ...)",
        "write_call": f"POST {ENTITIES}",
        "probe": ENTITIES,
    },
    {
        "step": "patch an entity during reconciliation",
        "write_call": f"PATCH {ENTITIES}/{{entity_id}}",
        "probe": ENTITIES,
    },
    {
        "step": "upload the loan document",
        "write_call": f"POST {KH}/collections/{{collection_id}}/newdocuments",
        "probe": COLLECTIONS,
    },
]

# entity_creators/base.py reads these; a stale value here is the most common
# reason loan creation starts failing after an environment is rebuilt.
ONTOLOGY_ENV_VARS = (
    "JAZZX_MORTGAGE_ONTOLOGY_ID",
    "JAZZX_LOAN_PROJECT_ONTOLOGY_ID",
    "JAZZX_LOAN_DETAILS_ONTOLOGY_ID",
)


def check_loan_creation(probe: Probe, *, collection_id: str | None) -> list[CheckResult]:
    g = "loan"
    out: list[CheckResult] = []

    for call in LOAN_CREATION_CALLS:
        slug = re.sub(r"[^a-z0-9]+", "_", call["step"].lower()).strip("_")[:40]
        cid = f"loan.endpoint.{slug}"
        title = f"Loan creation step would reach: {call['step']}"
        code, _ = probe.get(call["probe"], params={"limit": 1})
        ev = {"write_call": call["write_call"], "probed": call["probe"], "status_code": code}

        if code in (401, 403):
            out.append(_bad(g, cid, title,
                            f"HTTP {code} on {call['probe']} -- this token is not authorised",
                            "Loan creation will fail at this step. Re-issue the token with "
                            "the right scope.", **ev))
        elif code in (404, 405):
            out.append(_bad(g, cid, title,
                            f"HTTP {code} on {call['probe']} -- the endpoint is gone or moved",
                            f"The automation still calls {call['write_call']}; the deployed "
                            "API no longer serves it.", **ev))
        elif code == 0:
            out.append(_unk(g, cid, title, f"{call['probe']} is unreachable",
                            "Cannot vouch for this step.", **ev))
        elif code >= 500:
            out.append(_bad(g, cid, title, f"HTTP {code} on {call['probe']} -- server error",
                            "The service behind this step is unhealthy.", **ev))
        else:
            out.append(_ok(g, cid, title, f"HTTP {code}, authorised", **ev))

    # The ontology ids the payload builders will stamp onto every entity.
    ontologies = probe.items(ONTOLOGIES)
    known = {str(o.get("id")) for o in ontologies if o.get("id")}
    for var in ONTOLOGY_ENV_VARS:
        cid = f"loan.ontology_env.{var.lower()}"
        title = f"{var} points at a live ontology"
        value = os.getenv(var, "").strip()
        if not value:
            out.append(CheckResult(g, cid, title, SKIPPED, f"{var} is not set in this shell",
                                   {"env_var": var},
                                   "Set it if loans are created from this machine."))
        elif not known:
            out.append(_unk(g, cid, title, "the ontology list could not be fetched", "",
                            env_var=var))
        elif value in known:
            out.append(_ok(g, cid, title, f"{value} resolves", env_var=var, ontology_id=value))
        else:
            out.append(_bad(g, cid, title,
                            f"{var}={value} matches no ontology on this instance",
                            "Every entity the creators build would carry a dead ontology_id. "
                            "Refresh the variable from this instance.",
                            env_var=var, ontology_id=value))

    if collection_id:
        code, _ = probe.get(COLLECTION_DOCS.format(collection_id=collection_id), params={"limit": 1})
        if code == 200:
            out.append(_ok(g, "loan.target_collection_writable",
                           "The target collection accepts document reads",
                           "HTTP 200 on its documents endpoint", collection_id=collection_id))
        else:
            out.append(_bad(g, "loan.target_collection_writable",
                            "The target collection accepts document reads",
                            f"HTTP {code} on {COLLECTION_DOCS.format(collection_id=collection_id)}",
                            "The document upload step of loan creation would fail.",
                            collection_id=collection_id, status_code=code))
    else:
        out.append(CheckResult(g, "loan.target_collection_writable",
                               "The target collection accepts document reads", SKIPPED,
                               "no --collection-id was given"))

    out.append(_ok(g, "loan.no_writes", "No loan was created",
                   f"{probe.writes_attempted} write call(s) attempted",
                   write_calls_attempted=probe.writes_attempted,
                   read_calls=probe.reads))
    return out


# ---------------------------------------------------------------------------
# Group -- frontend. The federated micro-frontends.
#
# The shell app loads each remote with a dynamic import() of a remoteEntry.js
# named in /env.js. When a remote is missing, every service is healthy, every API
# answers, and the page still dies: the import rejects with "Failed to fetch
# dynamically imported module" and the shell shows its generic error card. No
# backend check of any kind sees it, which is why this group exists.
#
# Two distinct failures, and the second is the nastier one:
#   404            the remote was never uploaded
#   200 text/html  a path fallback served index.html in place of the module, so
#                  the browser refuses to execute it -- the host looks fine to
#                  curl and to any check that only reads the status code
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Groups -- split & classify readiness, and MACER readiness
#
# Both answer "would this run?" without running it. Neither starts a process,
# uploads a document or enqueues a MACER invocation: every call below is a GET
# through the Probe, so the write guard covers them like everything else.
#
# The process group's BPMN walker cannot be relied on here. Split and classify
# pass tool names as `<flowable:in sourceExpression="name" target="tool_name"/>`
# and from inside other tools' Python, neither of which it reads -- so the chain
# is listed explicitly, from config-poc's process definitions.
# ---------------------------------------------------------------------------


@dataclass
class Inventory:
    """Builder Studio and Knowledge Hub inventory, fetched once per run."""

    processes: dict[str, dict[str, Any]]
    tools: dict[str, dict[str, Any]]
    native_tools: dict[str, dict[str, Any]]
    agents: dict[str, dict[str, Any]]
    ontologies_by_name: dict[str, dict[str, Any]]
    ontologies_by_id: dict[str, dict[str, Any]]
    process_list_status: int
    ontology_list_status: int


def load_inventory(probe: Probe) -> Inventory:
    processes = probe.items(PROCESS_LIST)
    ontologies = probe.items(ONTOLOGIES)
    return Inventory(
        processes={str(p.get("key")).strip().lower(): p for p in processes if p.get("key")},
        tools=_index_by_name(probe.items(TOOL_LIST)),
        native_tools=_index_by_name(probe.items(NATIVE_TOOL_LIST)),
        agents=_index_by_name(probe.items(AGENT_LIST)),
        ontologies_by_name=_index_by_name(ontologies),
        ontologies_by_id={str(o.get("id")): o for o in ontologies if o.get("id")},
        process_list_status=probe.list_status.get(PROCESS_LIST, 0),
        ontology_list_status=probe.list_status.get(ONTOLOGIES, 0),
    )


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_") or "x"


def _process_ready(g: str, prefix: str, inv: Inventory, key: str, why: str) -> CheckResult:
    """One process: present in Builder Studio, reached the engine, not inactive."""
    cid, title = f"{prefix}.process.{_slug(key)}", f"Process {key!r} is deployed"
    proc = inv.processes.get(key.lower())
    if proc is None:
        return _bad(g, cid, title, f"not on this instance ({why})",
                    f"Deploy process {key!r} from config.", key=key)
    if proc.get("flowable_deployment_error"):
        return _bad(g, cid, title,
                    f"rejected by the engine: {str(proc['flowable_deployment_error'])[:200]}",
                    f"Re-publish {key!r} and read the engine's rejection.", key=key)
    if "flowable_deployment_id" in proc and not proc.get("flowable_deployment_id"):
        return _bad(g, cid, title,
                    "Builder Studio has it but the engine never received it",
                    f"Publish {key!r}.", key=key)
    reason = _is_inactive(proc)
    if reason:
        return _bad(g, cid, title, reason, f"Publish or re-enable {key!r}.", key=key)
    return _ok(g, cid, title, why, key=key)


def _tool_ready(g: str, prefix: str, inv: Inventory, name: str, why: str) -> CheckResult:
    cid, title = f"{prefix}.tool.{_slug(name)}", f"Tool {name!r} is registered"
    found = inv.tools.get(name.lower()) or inv.native_tools.get(name.lower())
    if found is None:
        return _bad(g, cid, title, f"neither a custom nor a native tool ({why})",
                    f"Deploy tool {name!r}.", tool=name)
    reason = _is_inactive(found)
    if reason:
        return _bad(g, cid, title, reason, f"Publish tool {name!r}.", tool=name)
    return _ok(g, cid, title, why, tool=name)


def _agent_ready(g: str, prefix: str, inv: Inventory, name: str, why: str) -> CheckResult:
    cid, title = f"{prefix}.agent.{_slug(name)}", f"Agent {name!r} is deployed"
    found = inv.agents.get(name.lower())
    if found is None:
        return _bad(g, cid, title, f"not on this instance ({why})",
                    f"Deploy agent {name!r}.", agent=name)
    if "kernel_agent_id" in found and not found.get("kernel_agent_id"):
        return _bad(g, cid, title,
                    "exists in Builder Studio but was never pushed to the kernel",
                    f"Re-sync or re-publish agent {name!r}.", agent=name)
    reason = _is_inactive(found)
    if reason:
        return _bad(g, cid, title, reason, f"Publish agent {name!r}.", agent=name)
    return _ok(g, cid, title, why, agent=name)


def _ontology_ready(
    g: str, prefix: str, inv: Inventory, *, name: str | None = None,
    ontology_id: str | None = None, why: str,
) -> CheckResult:
    label = name or ontology_id or "?"
    cid, title = f"{prefix}.ontology.{_slug(label)}", f"Ontology {label!r} is loaded"
    found = (inv.ontologies_by_name.get(name.lower()) if name
             else inv.ontologies_by_id.get(str(ontology_id)))
    if found is None:
        if inv.ontology_list_status != 200:
            return _unk(g, cid, title,
                        f"the ontology list returned HTTP {inv.ontology_list_status or 'nothing'}",
                        "Refresh the token and run again.")
        return _bad(g, cid, title, f"no such ontology ({why})",
                    f"Load ontology {label!r}; entities of its type cannot be created.",
                    name=name, ontology_id=ontology_id)
    return _ok(g, cid, title, why, ontology_id=found.get("id"), name=found.get("name"))


def _inventory_blocked(g: str, prefix: str, inv: Inventory) -> CheckResult | None:
    """A refused process list says nothing about what is deployed; say that
    once instead of reporting every process as missing."""
    if inv.processes:
        return None
    return _unk(g, f"{prefix}.inventory", "Processes can be listed",
                f"{PROCESS_LIST} returned HTTP {inv.process_list_status or 'nothing'}",
                "Set JAZZX_USER_ID to a user with Builder Studio access and refresh the token.")


def _dig(value: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def fetch_global_config(probe: Probe, name: str) -> tuple[dict[str, Any] | None, str]:
    """The parsed value of one global config, read-only. Returns (value, why-not)."""
    code, body = probe.get(GLOBAL_CONFIG, params={"name": name})
    if code != 200:
        return None, f"{GLOBAL_CONFIG} returned HTTP {code or 'a transport error'}"
    rows = extract_items(body, keys=("items", "data", "results", "configs"))
    if isinstance(body, dict) and not rows and (body.get("name") or body.get("value")):
        rows = [body]
    match = next((r for r in rows if str(r.get("name") or r.get("key") or "") == name), None)
    if match is None:
        return None, f"no global config named {name!r}"
    raw = match.get("value")
    if not raw and match.get("id"):
        dcode, detail = probe.get(f"{GLOBAL_CONFIG.rstrip('/')}/{match['id']}")
        if dcode == 200 and isinstance(detail, dict):
            raw = detail.get("value", detail.get("config"))
    value = parse_json_value(raw) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        return None, f"global config {name!r} has no readable value"
    return value, ""


def _is_placeholder(value: Any) -> bool:
    """`{{NAME}}` is config-poc's template marker: a value nobody filled in."""
    return isinstance(value, str) and value.strip().startswith("{{")


# ---- split & classify -------------------------------------------------------

#: The two upload chains in config-poc. The loan page's upload starts
#: UPLOAD_DOCUMENTS_FROM_DASHBOARD, i.e. the v1 chain; v2 is checked when it is
#: deployed, and its absence is not a failure.
CLASSIFY_CHAINS: dict[str, dict[str, Any]] = {
    "v1": {
        "entry": "uploadDocumentsFromDashboard",
        "required": True,
        "processes": [
            "DocumentProcessing", "parseDocument", "checkDuplicateDocumentV1_5",
            "splitDocument", "classificationSubprocessGeneric", "pdfToMdConversion",
            "getDocumentName", "externalToJazzClassificationMapping",
            "jazzToExternalClassificationMapping", "invokeKernelToolCallFromProcessV0",
            "invokeKernelToolCallFromProcessWithRetry", "sendDocumentProcessingNotification",
        ],
        "tools": [
            "get_document_metadata", "save_azure_json_kh", "azure_pdf_to_json",
            "los_pdf_to_md", "azure_pdf_to_md_hack", "splitting_orchestration_tool",
            "document_boundary_master_tool", "remake_doc_list_helper_tool_process",
            "classification_orchestration_tool", "fetch_azure_page_json_entity_kh",
        ],
    },
    "v2": {
        "entry": "uploadDocumentsFromDashboardV2",
        "required": False,
        "processes": [
            "DocumentProcessingV2", "splitDocumentV2", "classificationSubprocessGeneric",
            "externalToJazzClassificationMapping", "jazzToExternalClassificationMapping",
            "invokeKernelToolCallFromProcessV0", "sendDocumentProcessingNotification",
        ],
        "tools": [
            "parse_and_create_entity_kh", "get_document_metadata",
            "splitting_orchestration_tool", "document_boundary_master_tool",
            "remake_doc_list_helper_tool", "classification_orchestration_tool",
            "fetch_azure_page_json_entity_kh",
        ],
    },
}
#: Agents the split and classify tools call from their own Python.
CLASSIFY_AGENTS = (
    "classification_agent", "check_splitting", "document_chunk_agent",
    "chunk_merge_agent", "boundary_consolidation_agent", "final_boundary_check_agent",
)
#: Looked up by name at run time; a missing one fails every document.
CLASSIFY_ONTOLOGIES = ("document_source", "document_classified", "azure_json_docintel_ontology")
CLASSIFY_CONFIG_KEYS = (
    "dmn_mapping.external_to_jazz_mapping.dmn_name",
    "dmn_mapping.jazz_to_external_mapping.dmn_name",
)
SOURCE_FAILED_STATES = {"DOWNLOAD_FAILED", "PARSING_FAILED", "SPLIT_FAILED"}
SOURCE_IN_FLIGHT_STATES = {"RECEIVED", "DOWNLOADED", "PARSING", "PARSED", "READY",
                           "DUPLICATE_CHECK", "IN_PROGRESS", "CLASSIFYING"}
CLASSIFIED_FAILED_STATES = {"CLASSIFICATION_ERROR"}


def _state(entity: dict[str, Any]) -> str:
    value = entity.get("json_value")
    value = parse_json_value(value) if isinstance(value, str) else value
    return str((value or {}).get("state") or "").upper() if isinstance(value, dict) else ""


def _entities_of(probe: Probe, collection_id: str, entity_type: str) -> list[dict[str, Any]]:
    # odata_query, not collection_id/entity_type params: the entities endpoint
    # ignores those and returns every collection's rows.
    return probe.items(
        ENTITIES,
        odata_query=f"collection_id eq '{collection_id}' and entity_type eq '{entity_type}'",
    )


def summarise_document_states(
    sources: list[dict[str, Any]], classified: list[dict[str, Any]]
) -> dict[str, Any]:
    """Counts and problem lists for one collection's documents. Pure, so the
    rules are testable without an instance."""
    source_states: dict[str, int] = {}
    for s in sources:
        st = _state(s) or "UNKNOWN"
        source_states[st] = source_states.get(st, 0) + 1
    classified_states: dict[str, int] = {}
    for c in classified:
        st = _state(c) or "UNKNOWN"
        classified_states[st] = classified_states.get(st, 0) + 1
    return {
        "sources": len(sources),
        "classified": len(classified),
        "source_states": source_states,
        "classified_states": classified_states,
        "source_failed": sum(v for k, v in source_states.items() if k in SOURCE_FAILED_STATES),
        "source_in_flight": sum(v for k, v in source_states.items()
                                if k in SOURCE_IN_FLIGHT_STATES),
        "classified_failed": sum(v for k, v in classified_states.items()
                                 if k in CLASSIFIED_FAILED_STATES),
    }


def check_split_classify(
    probe: Probe, inv: Inventory, *, config_name: str, collection_id: str | None
) -> list[CheckResult]:
    g, p = "classify", "classify"
    out: list[CheckResult] = []
    blocked = _inventory_blocked(g, p, inv)
    if blocked:
        return [blocked]

    for chain, spec in CLASSIFY_CHAINS.items():
        entry = spec["entry"]
        if entry.lower() not in inv.processes and not spec["required"]:
            out.append(_info(g, f"{p}.{chain}.entry", f"Upload chain {chain} ({entry})",
                             "not deployed on this instance; its dependencies are not checked"))
            continue
        why = f"split & classify, {chain} chain"
        out.append(_process_ready(g, f"{p}.{chain}", inv, entry, f"entry point of {why}"))
        out.extend(_process_ready(g, f"{p}.{chain}", inv, k, why) for k in spec["processes"])
        out.extend(_tool_ready(g, f"{p}.{chain}", inv, t, why) for t in spec["tools"])

    out.extend(_agent_ready(g, p, inv, a, "called by the split/classify tools")
               for a in CLASSIFY_AGENTS)
    out.extend(_ontology_ready(g, p, inv, name=n, why="looked up by name during processing")
               for n in CLASSIFY_ONTOLOGIES)

    config, why_not = fetch_global_config(probe, config_name)
    if config is None:
        out.append(_unk(g, f"{p}.config", f"{config_name} is readable", why_not,
                        f"Confirm a global config named {config_name!r} exists."))
    else:
        for key in CLASSIFY_CONFIG_KEYS:
            value = _dig(config, key)
            cid, title = f"{p}.config.{_slug(key)}", f"{config_name}.{key} is set"
            if value in (None, "") or _is_placeholder(value):
                out.append(_bad(g, cid, title,
                                f"value is {value!r}: classification mapping has no DMN to run",
                                f"Set {key} in {config_name} to the deployed DMN's name.",
                                key=key))
            else:
                out.append(_ok(g, cid, title, str(value), key=key))

    if not collection_id:
        out.append(CheckResult(g, f"{p}.collection", "Documents in a loan classified",
                               SKIPPED, "no --collection-id was given"))
        return out

    sources = _entities_of(probe, collection_id, "SourceDocument")
    classified = _entities_of(probe, collection_id, "ClassifiedDocument")
    s = summarise_document_states(sources, classified)
    ev = {k: s[k] for k in ("source_states", "classified_states")}
    if not sources:
        out.append(_info(g, f"{p}.collection.sources", "Loan has source documents",
                         "no SourceDocument in this collection: nothing has been uploaded "
                         "for classification yet", collection_id=collection_id))
        return out
    out.append(_info(g, f"{p}.collection.states", "Document states in the target loan",
                     f"{s['sources']} source(s), {s['classified']} classified", **ev))
    if s["source_failed"]:
        out.append(_bad(g, f"{p}.collection.source_failed", "No source document failed",
                        f"{s['source_failed']} source document(s) in a failed state "
                        f"({', '.join(k for k in s['source_states'] if k in SOURCE_FAILED_STATES)})",
                        "PARSING_FAILED usually means Azure Document Intelligence is "
                        "unreachable or unconfigured on the kernel "
                        "(AZURE__DOCUMENT_INTELLIGENCE_URL); SPLIT_FAILED points at the "
                        "split tools or their LLMs.", **ev))
    else:
        out.append(_ok(g, f"{p}.collection.source_failed", "No source document failed",
                       f"none of {s['sources']} failed", **ev))
    if s["classified_failed"]:
        out.append(_bad(g, f"{p}.collection.classify_failed", "No classification errored",
                        f"{s['classified_failed']} classified document(s) in CLASSIFICATION_ERROR",
                        "Check classification_agent and its LLM.", **ev))
    if s["source_in_flight"]:
        out.append(_info(g, f"{p}.collection.in_flight", "Documents still processing",
                         f"{s['source_in_flight']} source document(s) not finished yet; "
                         "re-run later -- a count that does not move is a stuck pipeline",
                         **ev))
    if s["classified"] == 0 and not s["source_in_flight"]:
        out.append(_bad(g, f"{p}.collection.classified", "Documents were classified",
                        f"{s['sources']} source document(s), none in flight, and no "
                        "ClassifiedDocument at all",
                        "Classification never ran; check classificationSubprocessGeneric.",
                        **ev))
    elif s["classified"]:
        out.append(_ok(g, f"{p}.collection.classified", "Documents were classified",
                       f"{s['classified']} ClassifiedDocument(s)", **ev))
    return out


# ---- MACER --------------------------------------------------------------------

#: The trigger -> scheduler -> runner chain, plus the dashboard's manual path.
MACER_PROCESSES = (
    "macerTriggerSender", "invokeMacerScheduler", "macerInvocationScheduler",
    "macerRunner", "invokeMacerFromDashboard", "findingsToCondition",
    "terminateMacerScheduler",
)
#: The scheduler's gates and the runner's config lookup, then what they call.
MACER_TOOLS = (
    "macer_gating", "check_all_docs_processed", "check_macer_cooldown",
    "check_macer_inprogress", "check_pending_macer_events", "get_macer_config_by_loan_status",
    "read_kh_config", "entity_read_json_odata", "filter_entities_rest_api",
    "get_loan_external_ids_by_collection", "los_check_loan_lock_tool",
    "resolve_loan_program_from_product_name",
)
#: macerTriggerSender writes every MacerInvocationEvent against this id,
#: hard-coded in the BPMN -- the one id here that config cannot repoint.
MACER_INVOCATION_ONTOLOGY_ID = "2f89f191-34a2-464b-9238-c9893c78b9f0"
MACER_REQUIRED_KEYS = (
    "invoke_macer", "jtbd_entity_id", "docs_ontology_id", "findings_ontology_id",
    "mortgage_ontology_id", "guidelines_collection_id", "timeout_duration", "model_name",
)
MACER_ONTOLOGY_KEYS = ("docs_ontology_id", "findings_ontology_id", "mortgage_ontology_id")
MACER_STUCK_STATES = {"PENDING", "QUEUED", "INPROGRESS"}


def macer_config_problems(macer_config: Any) -> list[str]:
    """Missing or placeholder keys in macer_config. Pure, for testing."""
    if not isinstance(macer_config, dict):
        return ["macer_config is missing"]
    problems: list[str] = []
    blocks = {"default": macer_config.get("default")}
    for status, block in (macer_config.get("by_loan_status") or {}).items():
        blocks[f"by_loan_status.{status}"] = block
    if not isinstance(blocks["default"], dict):
        problems.append("macer_config.default is missing")
    for where, block in blocks.items():
        if not isinstance(block, dict):
            continue
        for key in MACER_REQUIRED_KEYS:
            value = block.get(key)
            if value is None or value == "" or _is_placeholder(value):
                problems.append(f"{where}.{key} = {value!r}")
    return problems


def check_macer(
    probe: Probe, inv: Inventory, *, config_name: str, collection_id: str | None
) -> list[CheckResult]:
    g, p = "macer", "macer"
    out: list[CheckResult] = []
    blocked = _inventory_blocked(g, p, inv)
    if blocked:
        return [blocked]

    out.extend(_process_ready(g, p, inv, k, "MACER trigger/scheduler/runner chain")
               for k in MACER_PROCESSES)
    out.extend(_tool_ready(g, p, inv, t, "MACER scheduler gate or config lookup")
               for t in MACER_TOOLS)
    out.append(_ontology_ready(g, p, inv, ontology_id=MACER_INVOCATION_ONTOLOGY_ID,
                               why="macerTriggerSender writes MacerInvocationEvent against it"))

    config, why_not = fetch_global_config(probe, config_name)
    if config is None:
        out.append(_unk(g, f"{p}.config", f"{config_name}.macer_config is readable", why_not,
                        f"Confirm a global config named {config_name!r} exists."))
    else:
        macer_config = config.get("macer_config")
        problems = macer_config_problems(macer_config)
        if problems:
            out.append(_bad(g, f"{p}.config.complete", "macer_config is complete",
                            f"{len(problems)} problem(s): {'; '.join(problems[:6])}"
                            + (" ..." if len(problems) > 6 else ""),
                            f"Fill in macer_config in {config_name}; macerRunner sends these "
                            "to the worker and it rejects a payload without them.",
                            problems=problems[:30]))
        else:
            out.append(_ok(g, f"{p}.config.complete", "macer_config is complete",
                           "default and every loan status carry the required keys"))

        default = (macer_config or {}).get("default") if isinstance(macer_config, dict) else None
        if isinstance(default, dict):
            enabled = default.get("invoke_macer")
            if enabled is True:
                out.append(_ok(g, f"{p}.config.enabled", "MACER is enabled by default",
                               "macer_config.default.invoke_macer = true"))
            else:
                out.append(_info(g, f"{p}.config.enabled", "MACER is enabled by default",
                                 f"macer_config.default.invoke_macer = {enabled!r}: MACER will "
                                 "not run unless a loan status enables it"))
            for key in MACER_ONTOLOGY_KEYS:
                value = default.get(key)
                if not value or _is_placeholder(value):
                    continue  # already reported as incomplete
                out.append(_ontology_ready(g, f"{p}.config.{key}", inv, ontology_id=str(value),
                                           why=f"macer_config.default.{key}"))
            gid = default.get("guidelines_collection_id")
            if gid and not _is_placeholder(gid):
                code, _ = probe.get(f"{COLLECTIONS}/{gid}")
                cid, title = f"{p}.config.guidelines_collection", "Guidelines collection exists"
                if code == 200:
                    out.append(_ok(g, cid, title, str(gid), collection_id=gid))
                elif code in (401, 403, 0):
                    out.append(_unk(g, cid, title, f"HTTP {code or 'transport error'}",
                                    "Refresh the token and run again."))
                else:
                    out.append(_bad(g, cid, title, f"HTTP {code} for collection {gid}",
                                    "Repoint guidelines_collection_id at a live collection.",
                                    collection_id=gid))
            jid = default.get("jtbd_entity_id")
            if jid and not _is_placeholder(jid):
                code, _ = probe.get(f"{ENTITIES}/{jid}")
                cid, title = f"{p}.config.jtbd_entity", "JTBD entity exists"
                if code == 200:
                    out.append(_ok(g, cid, title, str(jid), entity_id=jid))
                elif code in (401, 403, 0):
                    out.append(_unk(g, cid, title, f"HTTP {code or 'transport error'}",
                                    "Refresh the token and run again."))
                else:
                    out.append(_bad(g, cid, title, f"HTTP {code} for entity {jid}",
                                    "Repoint jtbd_entity_id at a live JTBD entity.",
                                    entity_id=jid))

    # The worker itself is a queue consumer with no HTTP surface, so nothing
    # through the gateway can prove it is up. Say so rather than imply a pass.
    out.append(_info(g, f"{p}.worker", "MACER worker is consuming its queue",
                     "not checkable through the gateway: the worker has no HTTP endpoint. "
                     "Confirm the container is running and japes-macer-invocation is "
                     "draining in Azure."))

    if collection_id:
        events = _entities_of(probe, collection_id, "MacerInvocationEvent")
        states: dict[str, int] = {}
        for e in events:
            value = e.get("json_value")
            value = parse_json_value(value) if isinstance(value, str) else value
            st = str((value or {}).get("macer_status") or "UNKNOWN").upper() \
                if isinstance(value, dict) else "UNKNOWN"
            states[st] = states.get(st, 0) + 1
        cid = f"{p}.collection.events"
        if not events:
            out.append(_info(g, cid, "MACER has run on the target loan",
                             "no MacerInvocationEvent yet for this collection"))
        elif states.get("DONE"):
            out.append(_ok(g, cid, "MACER has run on the target loan",
                           f"{states['DONE']} invocation(s) DONE: the worker has consumed "
                           "and answered for this loan", states=states))
        elif states.get("ERROR"):
            out.append(_bad(g, cid, "MACER has run on the target loan",
                            f"{states['ERROR']} invocation(s) ERROR and none DONE",
                            "Read the MacerInvocationEvent error and the worker log.",
                            states=states))
        else:
            out.append(_info(g, cid, "MACER has run on the target loan",
                             f"invocations exist but none finished: {states}", states=states))
    return out


def derive_ui_url(gateway_url: str) -> str | None:
    """Guess the UI host from the gateway host.

    The convention is a `-gw` suffix on the first label: the UI at
    staging.plazahomemortgage.jazzx.co is served by the gateway at
    staging-gw.plazahomemortgage.jazzx.co. When the name does not follow it,
    this returns None rather than inventing a host, and the caller asks for
    --ui-url instead of reporting a failure against something imaginary.
    """
    parsed = urlparse(gateway_url)
    host = parsed.hostname or ""
    labels = host.split(".")
    if not labels or not labels[0].endswith("-gw"):
        return None
    labels[0] = labels[0][: -len("-gw")]
    if not labels[0]:
        return None
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme or 'https'}://{'.'.join(labels)}{port}"


def fetch_unauthenticated(url: str, timeout: float, *, max_body: int = 300) -> tuple[int, str, str]:
    """GET a static asset with no Authorization header.

    The UI host is not behind the gateway's auth and has no business seeing the
    bearer token, so this deliberately does not reuse the authenticated session.
    Returns (status, content_type, body_prefix).
    """
    import requests  # noqa: PLC0415

    try:
        response = requests.get(url, timeout=timeout, headers={"Accept": "*/*"})
    except requests.RequestException as exc:
        return 0, "", str(exc)[:200]
    return (
        response.status_code,
        response.headers.get("Content-Type", "").split(";")[0].strip().lower(),
        response.text[:max_body],
    )


def _is_javascript(content_type: str) -> bool:
    return "javascript" in content_type or "ecmascript" in content_type


def check_frontend(
    probe: Probe, *, ui_url: str | None, gateway_url: str, timeout: float
) -> list[CheckResult]:
    g = "frontend"
    out: list[CheckResult] = []

    if ui_url is None:
        ui_url = derive_ui_url(gateway_url)
    if ui_url is None:
        return [_unk(g, "frontend.ui_url", "The UI host is known",
                     f"{gateway_url} does not follow the <name>-gw convention, so the UI "
                     "host cannot be derived",
                     "Pass --ui-url with the host that serves the web app.")]
    ui_url = ui_url.rstrip("/")

    env_url = f"{ui_url}{FRONTEND_ENV_PATH}"
    # env.js carries every remote URL, so it is read whole rather than sampled.
    code, content_type, env_js = fetch_unauthenticated(env_url, timeout, max_body=200_000)
    probe.reads += 1

    if code != 200:
        return [_bad(g, "frontend.env_js", "The UI serves its runtime config",
                     f"HTTP {code or 'connection error'} for {env_url}",
                     "Without env.js the shell has no API URLs and no remotes; the whole "
                     "web app is dead. Confirm --ui-url names the web host.",
                     url=env_url, status_code=code)]
    if not _is_javascript(content_type):
        return [_bad(g, "frontend.env_js", "The UI serves its runtime config",
                     f"{env_url} returned {content_type or 'no content type'} rather than "
                     "JavaScript -- a path fallback is serving something else in its place",
                     "The shell cannot read its configuration.",
                     url=env_url, content_type=content_type)]
    out.append(_ok(g, "frontend.env_js", "The UI serves its runtime config",
                   f"{env_url} returned JavaScript", url=env_url))

    # Which gateway does the UI actually talk to? A UI pointing somewhere else is
    # why a perfectly healthy gateway can still have a broken-looking app.
    instance = INSTANCE_URL_RE.search(env_js)
    if instance:
        declared = instance.group(1).rstrip("/")
        if declared.rstrip("/") == gateway_url.rstrip("/"):
            out.append(_ok(g, "frontend.instance_url", "The UI points at this gateway",
                           declared, declared=declared))
        else:
            out.append(_info(g, "frontend.instance_url", "The UI points at this gateway",
                             f"the UI is configured for {declared}, but this run checked "
                             f"{gateway_url}",
                             declared=declared, checked=gateway_url))

    remotes = sorted(MFE_URL_RE.findall(env_js), key=lambda pair: int(pair[0]))
    base = FEDERATE_BASE_RE.search(env_js)
    remote_base = (base.group(1).rstrip("/") if base else ui_url)

    if not remotes:
        out.append(_unk(g, "frontend.remotes", "Micro-frontend remotes are declared",
                        "env.js declares no VITE_MICROFRONTEND_URL_* entries",
                        "Either this build has no federated remotes, or the naming "
                        "convention changed and this check needs updating."))
        return out

    out.append(_info(g, "frontend.remotes", "Micro-frontend remotes are declared",
                     f"{len(remotes)} remote(s) configured, loaded from {remote_base}",
                     count=len(remotes), base=remote_base))

    missing: list[str] = []
    for index, path in remotes:
        # /<name>/assets/remoteEntry.js -- the first segment names the remote.
        segments = [seg for seg in path.split("/") if seg]
        name = segments[0] if segments else f"remote_{index}"
        slug = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_") or f"remote_{index}"
        cid = f"frontend.remote.{slug}"
        title = f"Micro-frontend {name!r} is deployed"
        url = path if path.startswith("http") else f"{remote_base}/{path.lstrip('/')}"

        code, content_type, body = fetch_unauthenticated(url, timeout)
        probe.reads += 1
        ev = {"remote": name, "url": url, "status_code": code, "content_type": content_type}

        if code == 0:
            out.append(_unk(g, cid, title, f"{url} is unreachable", "", **ev))
        elif code == 404:
            missing.append(name)
            blob = "BlobNotFound" in body
            out.append(_bad(g, cid, title,
                            f"HTTP 404 for {url}"
                            + (" (Azure Blob Storage: BlobNotFound)" if blob else ""),
                            f"The {name!r} build output was never uploaded. Every route "
                            "served by it fails with 'Failed to fetch dynamically imported "
                            "module'. Re-run the deployment step that publishes it.", **ev))
        elif code != 200:
            missing.append(name)
            out.append(_bad(g, cid, title, f"HTTP {code} for {url}",
                            f"The {name!r} remote cannot be loaded.", **ev))
        elif not _is_javascript(content_type):
            missing.append(name)
            out.append(_bad(g, cid, title,
                            f"HTTP 200 but {content_type or 'no content type'} for {url} -- "
                            "a path fallback is serving a page in place of the module",
                            "The browser refuses to execute this as a module, so the import "
                            "fails even though the URL returns 200. Any check reading only "
                            "the status code would call this healthy.", **ev))
        else:
            out.append(_ok(g, cid, title, f"HTTP 200, {content_type}", **ev))

    if missing:
        out.append(_info(g, "frontend.deploy_completeness", "Remote deployment completeness",
                         f"{len(missing)} of {len(remotes)} remotes are missing: "
                         + ", ".join(missing)
                         + (". Several missing at once points at a partial or failed "
                            "multi-remote deploy rather than one bad build."
                            if len(missing) > 1 else ""),
                         missing=missing, total=len(remotes)))
    return out


# ---------------------------------------------------------------------------
# Group -- platform. The things that are true of any instance, and that explain
# most of what else fails. An expired token reads as "every endpoint is broken";
# clock skew reads as "auth is broken"; one dead service reads as a dozen
# unrelated failures. Checking them first makes the rest of the report legible.
# ---------------------------------------------------------------------------
def decode_jwt_payload(token: str) -> dict[str, Any] | None:
    """Read a JWT's claims WITHOUT verifying it.

    The signature is the server's business. All this needs is `exp` and `sub`,
    and reading them locally costs no network call and catches the single most
    common reason a sanity run is meaningless: the token died an hour ago.
    """
    import base64  # noqa: PLC0415

    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError):
        return None


# An API gateway answers an unknown path with JSON ({"status": false, ...}).
# A static web host answers with its index.html, or -- when an Authorization
# header confuses the storage backend -- with an Azure Storage XML error. Either
# shape means the URL is the web app, not the API.
STATIC_SITE_MARKERS = (
    "<!doctype html",
    "blobnotfound",
    "authenticationfailed",
    "resourcenotfound",
    "invalidqueryparametervalue",
)


def looks_like_static_site(body: Any) -> bool:
    if not isinstance(body, str):
        return False
    head = body[:400].lower()
    return any(marker in head for marker in STATIC_SITE_MARKERS)


def token_expiry(token: str) -> tuple[str, str]:
    """(human summary, ISO expiry) for the run header.

    Printed on every run regardless of verbosity: an expired or nearly-expired
    token makes the entire report meaningless, so it must not be something the
    reader has to opt in to see.
    """
    claims = decode_jwt_payload(token)
    if claims is None:
        return "not a readable JWT", ""
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        return "no exp claim", ""
    when = datetime.fromtimestamp(exp, tz=timezone.utc)
    left = exp - time.time()
    if left <= 0:
        return f"EXPIRED {abs(left)/3600:.1f}h ago at {when.isoformat()}", when.isoformat()
    # The run header needs to answer the operational question immediately:
    # how long can this token still be used?  ``iat`` is optional in JWTs, so
    # report the remaining validity from ``exp`` regardless of whether the
    # issuer included an issued-at timestamp.
    if left >= 3600:
        remaining = f"{left / 3600:.1f}h"
    elif left >= 60:
        remaining = f"{max(1, round(left / 60))} min"
    else:
        remaining = f"{max(1, round(left))} sec"
    return f"valid {remaining}, until {when.isoformat()}", when.isoformat()


def token_lifetime(token: str) -> str:
    """Total lifetime of the token (exp - iat), e.g. "1h"; "unknown" without both claims."""
    claims = decode_jwt_payload(token) or {}
    exp, iat = claims.get("exp"), claims.get("iat")
    if isinstance(exp, (int, float)) and isinstance(iat, (int, float)) and exp > iat:
        return _duration(exp - iat)
    return "unknown"


def _duration(seconds: float) -> str:
    """1h 30m / 45m / 2d 3h -- token lifetimes span minutes to days."""
    minutes = int(round(seconds / 60))
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    parts = [f"{v}{u}" for v, u in ((days, "d"), (hours, "h"), (minutes, "m")) if v]
    return " ".join(parts) or "0m"


def check_platform(probe: Probe, token: str) -> list[CheckResult]:
    g = "platform"
    out: list[CheckResult] = []

    claims = decode_jwt_payload(token)
    if claims is None:
        out.append(_unk(g, "platform.token.valid", "The token is a readable JWT",
                        "the token is not a three-part JWT, so its expiry cannot be read",
                        "If it is an opaque token this check does not apply."))
    else:
        exp = claims.get("exp")
        now = time.time()
        if not isinstance(exp, (int, float)):
            out.append(_unk(g, "platform.token.valid", "The token has not expired",
                            "the JWT carries no exp claim", ""))
        elif exp <= now:
            gone = (now - exp) / 3600
            out.append(_bad(g, "platform.token.valid", "The token has not expired",
                            f"the token expired {gone:.1f}h ago "
                            f"({datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()})",
                            "Every other check in this run is meaningless. Get a fresh "
                            "token and run again.",
                            expires_at=datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()))
        else:
            left = (exp - now) / 60
            title = "The token has not expired"
            when = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()
            if left < 5:
                out.append(_bad(g, "platform.token.valid", title,
                                f"the token expires in {left:.1f} minutes",
                                "It will die mid-run. Refresh it first.", expires_at=when))
            else:
                iat = claims.get("iat")
                lifetime = (f"total lifetime {_duration(exp - iat)}"
                            if isinstance(iat, (int, float)) and exp > iat
                            else "total lifetime unknown (no iat claim)")
                out.append(_ok(g, "platform.token.valid", title,
                               f"{lifetime} (until {when})",
                               expires_at=when))

        who = claims.get("preferred_username") or claims.get("email") or claims.get("sub")
        roles = ((claims.get("realm_access") or {}).get("roles")) or []
        out.append(_info(g, "platform.token.identity", "Token identity",
                         f"{who} with roles: {', '.join(sorted(map(str, roles))) or '<none>'}",
                         subject=claims.get("sub"), username=who, roles=sorted(map(str, roles)),
                         issuer=claims.get("iss")))

    # Clock skew. A host whose clock has drifted rejects or accepts tokens wrongly,
    # and the resulting 401s look like a permissions problem rather than a time one.
    before = time.time()
    try:
        response = probe.client.request("GET", PROCESS_HEALTH, expected=tuple(range(200, 600)))
        served = response.headers.get("Date")
    except ApiError:
        served = None
    probe.reads += 1
    if served:
        try:
            from email.utils import parsedate_to_datetime  # noqa: PLC0415

            server_time = parsedate_to_datetime(served).timestamp()
            skew = server_time - (before + time.time()) / 2
            if abs(skew) > 120:
                out.append(_bad(g, "platform.clock_skew", "Local and server clocks agree",
                                f"this host is {abs(skew):.0f}s "
                                f"{'behind' if skew > 0 else 'ahead of'} the server",
                                "Token validation and every timestamp comparison in this "
                                "report are unreliable. Fix NTP on this host.",
                                skew_seconds=round(skew)))
            else:
                out.append(_ok(g, "platform.clock_skew", "Local and server clocks agree",
                               f"{skew:+.0f}s", skew_seconds=round(skew)))
        except (TypeError, ValueError):
            out.append(_unk(g, "platform.clock_skew", "Local and server clocks agree",
                            f"the server Date header did not parse: {served!r}", ""))
    else:
        out.append(_unk(g, "platform.clock_skew", "Local and server clocks agree",
                        "the server sent no Date header", ""))

    # Which services this gateway actually fronts.
    unauthorised: list[str] = []
    down: list[str] = []
    probes = service_probes()
    for name, path in probes:
        code, _ = probe.get(path, params={"limit": 1})
        cid = f"platform.service.{name}"
        title = f"Service {name} answers through the gateway"
        if code in (200, 201, 204):
            out.append(_ok(g, cid, title, f"HTTP {code}", path=path))
        elif code in (401, 403):
            unauthorised.append(name)
            out.append(_bad(g, cid, title, f"HTTP {code} -- this token cannot reach {name}",
                            f"Either the token lacks a scope for {name} or the gateway "
                            "route is misconfigured.", path=path, status_code=code))
        elif code == 0:
            down.append(name)
            out.append(_bad(g, cid, title, f"{path} is unreachable",
                            f"{name} is down or not routed on this gateway.", path=path))
        else:
            out.append(_bad(g, cid, title, f"HTTP {code}",
                            f"{name} is reachable but unhealthy.", path=path, status_code=code))

    if unauthorised and len(unauthorised) < len(probes):
        out.append(_info(g, "platform.auth_consistency", "Auth reaches every service",
                         f"the token works for some services but not {', '.join(unauthorised)}; "
                         "that is a gateway or scope problem, not a dead service",
                         unauthorised=unauthorised))
    return out


# ---------------------------------------------------------------------------
# Group -- JazzX Assistant and the Luna agent.
#
# Luna is the agent the fulfilment UI talks to. Three things can be wrong and only
# the third is visible from the agent record:
#
#   1. the agent is missing or unbound          -- caught by the agent checks
#   2. its tools or model are not deployed      -- caught by the dependency checks
#   3. it answers over the JAPES queue and the  -- caught ONLY by message traces
#      consumer is not running, so every        left stuck in `queued`
#      conversation hangs forever
#
# The third is the one that looks healthy in every dashboard and is dead to users.
# ---------------------------------------------------------------------------
def _find_agent(agents: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for agent in agents:
        if str(agent.get("name") or "").strip().lower() == name.lower():
            return agent
    return None


def check_assistant(
    probe: Probe, *, luna_name: str, stuck_minutes: float, trace_window_hours: float
) -> list[CheckResult]:
    g = "assistant"
    out: list[CheckResult] = []

    code, _ = probe.get(ASSISTANT_PROJECTS, params={"limit": 1})
    if code != 200:
        out.append(_bad(g, "assistant.reachable", "The Assistant service answers",
                        f"HTTP {code or 'connection error'} from {ASSISTANT_PROJECTS}",
                        "Luna is unusable; nothing else in this group can be judged.",
                        status_code=code))
        return out
    out.append(_ok(g, "assistant.reachable", "The Assistant service answers", "HTTP 200"))

    code, body = probe.get(ASSISTANT_MCP_HEALTH)
    if code == 200:
        out.append(_ok(g, "assistant.mcp_health", "The Assistant MCP client is healthy",
                       "HTTP 200", response=redact(body)))
    elif code == 404:
        out.append(_unk(g, "assistant.mcp_health", "The Assistant MCP client is healthy",
                        f"{ASSISTANT_MCP_HEALTH} is not served on this build",
                        "Only applies to builds with the MCP client mounted."))
    else:
        out.append(_bad(g, "assistant.mcp_health", "The Assistant MCP client is healthy",
                        f"HTTP {code or 'connection error'} from {ASSISTANT_MCP_HEALTH}",
                        "Any Luna capability served over MCP will fail.", status_code=code))

    code, _ = probe.get(ASSISTANT_CONVERSATIONS, params={"limit": 1})
    if code == 200:
        out.append(_ok(g, "assistant.conversations", "Conversations can be listed", "HTTP 200"))
    else:
        out.append(_bad(g, "assistant.conversations", "Conversations can be listed",
                        f"HTTP {code or 'connection error'} from {ASSISTANT_CONVERSATIONS}",
                        "The Assistant UI cannot load its conversation list.",
                        status_code=code))

    # ---- the Luna agent itself ----
    agents = probe.items(KERNEL_AGENTS)
    source = "kernel"
    if not agents:
        agents = probe.items(AGENT_LIST)
        source = "builder studio"

    luna = _find_agent(agents, luna_name)
    if luna is None:
        names = sorted(str(a.get("name")) for a in agents if a.get("name"))
        out.append(_bad(g, "assistant.luna.present", f"The {luna_name} agent exists",
                        f"no agent named {luna_name!r} among {len(agents)} agent(s) "
                        f"known to {source}: {', '.join(names[:10]) or '<none>'}"
                        + (" ..." if len(names) > 10 else ""),
                        f"The Assistant pins the name {luna_name!r}; without an agent by "
                        "exactly that name the UI has nothing to talk to.",
                        expected_name=luna_name, source=source, agent_count=len(agents)))
        return out

    out.append(_ok(g, "assistant.luna.present", f"The {luna_name} agent exists",
                   f"id {luna.get('id')} (via {source})",
                   agent_id=luna.get("id"), type=luna.get("type"), source=source))

    reason = _is_inactive(luna)
    if reason:
        out.append(_bad(g, "assistant.luna.active", f"The {luna_name} agent is active", reason,
                        f"Publish or enable {luna_name}.", status=luna.get("status")))
    else:
        out.append(_ok(g, "assistant.luna.active", f"The {luna_name} agent is active",
                       str(luna.get("status") or "active")))

    if "kernel_agent_id" in luna and not luna.get("kernel_agent_id"):
        out.append(_bad(g, "assistant.luna.kernel_bound",
                        f"The {luna_name} agent is bound to the kernel",
                        "kernel_agent_id is null: Luna exists in Builder Studio but was "
                        "never pushed to the kernel that runs it",
                        f"Re-sync {luna_name} to the kernel.", agent_id=luna.get("id")))
    elif luna.get("kernel_agent_id"):
        out.append(_ok(g, "assistant.luna.kernel_bound",
                       f"The {luna_name} agent is bound to the kernel",
                       f"kernel_agent_id={luna['kernel_agent_id']}"))

    # Luna's tools.
    tool_index = _index_by_name(probe.items(TOOL_LIST))
    tool_index.update(_index_by_name(probe.items(NATIVE_TOOL_LIST)))
    kernel_tools = _index_by_name(probe.items(KERNEL_TOOLS))
    luna_tools = luna.get("tools") or []
    if not luna_tools:
        out.append(_info(g, "assistant.luna.tools", f"The {luna_name} agent has tools",
                         "the agent record lists no tools; either it needs none or the "
                         "listing does not expand them"))
    for tool_name in luna_tools:
        key = str(tool_name).strip().lower()
        slug = re.sub(r"[^a-z0-9_]+", "_", key).strip("_") or "tool"
        cid = f"assistant.luna.tool.{slug}"
        title = f"{luna_name} can reach tool {tool_name!r}"
        found = tool_index.get(key) or kernel_tools.get(key)
        if found is None:
            out.append(_bad(g, cid, title,
                            f"tool {tool_name!r} is not deployed on this instance",
                            f"Deploy {tool_name!r}; Luna will fail whenever it reaches "
                            "for this tool.", tool=tool_name))
        elif _is_inactive(found):
            out.append(_bad(g, cid, title, f"tool {tool_name!r} {_is_inactive(found)}",
                            f"Publish tool {tool_name!r}.", tool=tool_name))
        else:
            out.append(_ok(g, cid, title, "deployed", tool=tool_name))

    # Luna's model. A model that is missing or flagged deprecated is a slow-burn
    # failure: it works until the provider withdraws it.
    model = None
    for key in ("llm", "llm_name", "model", "model_name", "llm_config"):
        value = luna.get(key)
        if isinstance(value, str) and value.strip():
            model = value.strip()
            break
        if isinstance(value, dict) and value.get("name"):
            model = str(value["name"])
            break
    llms = probe.items(KERNEL_LLM)
    if model is None:
        out.append(_info(g, "assistant.luna.model", f"The {luna_name} model is available",
                         "the agent record does not name its model in the list response"))
    elif not llms:
        out.append(_unk(g, "assistant.luna.model", f"The {luna_name} model is available",
                        f"{KERNEL_LLM} returned no models, so {model!r} cannot be checked",
                        "", model=model))
    else:
        match = _index_by_name(llms).get(model.lower())
        if match is None:
            out.append(_bad(g, "assistant.luna.model", f"The {luna_name} model is available",
                            f"Luna is bound to model {model!r}, which is not configured here",
                            f"Configure {model!r} on this instance or repoint Luna.",
                            model=model))
        elif match.get("deprecated"):
            out.append(_bad(g, "assistant.luna.model", f"The {luna_name} model is available",
                            f"model {model!r} is marked deprecated",
                            "It will stop working when the provider withdraws it; "
                            "move Luna to a current model.", model=model))
        else:
            out.append(_ok(g, "assistant.luna.model", f"The {luna_name} model is available",
                           f"{model} is configured and not deprecated", model=model))

    # ---- assistant -> agent bindings ----
    bindings = probe.items(ASSISTANT_AGENTS)
    known_ids = {str(a.get("id")) for a in agents if a.get("id")}
    dangling = [b for b in bindings
                if b.get("agent_id") and str(b["agent_id"]) not in known_ids]
    if not bindings:
        out.append(_info(g, "assistant.bindings", "Assistant-to-agent bindings resolve",
                         f"{ASSISTANT_AGENTS} lists no bindings"))
    elif dangling:
        out.append(_bad(g, "assistant.bindings", "Assistant-to-agent bindings resolve",
                        f"{len(dangling)} of {len(bindings)} binding(s) point at an agent "
                        "that does not exist: "
                        + ", ".join(str(b.get("agent_id")) for b in dangling[:4]),
                        "The assistant those bindings belong to cannot start a "
                        "conversation.", dangling=len(dangling), total=len(bindings)))
    else:
        out.append(_ok(g, "assistant.bindings", "Assistant-to-agent bindings resolve",
                       f"all {len(bindings)} binding(s) resolve"))

    # ---- message traces: the Luna liveness signal ----
    traces = probe.items(ASSISTANT_MESSAGE_TRACES)
    if not traces:
        code = probe.list_status.get(ASSISTANT_MESSAGE_TRACES, 0)
        why = (f"{ASSISTANT_MESSAGE_TRACES} returned no rows" if code == 200
               else f"{ASSISTANT_MESSAGE_TRACES} returned HTTP {code or 'a transport error'}")
        out.append(_unk(g, "assistant.traces.recent", "Luna conversations are being traced",
                        why, "Either nobody has used Luna, or message tracing is off.",
                        window_hours=trace_window_hours, status_code=code))
        return out

    cutoff = datetime.now(timezone.utc) - timedelta(hours=trace_window_hours)
    stuck_cutoff = datetime.now(timezone.utc) - timedelta(minutes=stuck_minutes)

    def created(trace: dict[str, Any]) -> datetime | None:
        raw = str(trace.get("created_at") or "")
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    recent = [t for t in traces if (created(t) or datetime.min.replace(
        tzinfo=timezone.utc)) >= cutoff]
    if recent:
        out.append(_ok(g, "assistant.traces.recent", "Luna conversations are being traced",
                       f"{len(recent)} message trace(s) in the last {trace_window_hours:g}h",
                       recent=len(recent), total=len(traces)))
    else:
        newest = max((created(t) for t in traces if created(t)), default=None)
        out.append(_bad(g, "assistant.traces.recent", "Luna conversations are being traced",
                        f"no message traces in the last {trace_window_hours:g}h; newest is "
                        f"{newest.isoformat() if newest else 'undated'}",
                        "Either nobody is using Luna, or conversations are failing before "
                        "a trace is written.",
                        recent=0, total=len(traces)))

    # A trace stuck in `queued` is the JAPES signature: the invocation was handed
    # to the queue and nothing ever consumed it. MessageTraceStatus has only
    # `queued` and `completed`, so anything old and still queued is hung.
    stuck = [t for t in traces
             if str(t.get("status") or "").lower() == "queued"
             and (created(t) or datetime.now(timezone.utc)) < stuck_cutoff]
    if stuck:
        out.append(_bad(g, "assistant.traces.stuck", "No Luna invocation is hung",
                        f"{len(stuck)} message trace(s) still 'queued' after "
                        f"{stuck_minutes:g} minutes: "
                        + ", ".join(str(t.get("id")) for t in stuck[:4])
                        + (" ..." if len(stuck) > 4 else ""),
                        "Luna accepted these and never answered. The JAPES response queue "
                        "consumer is almost certainly not running "
                        "(enable_japes_response_queue_consumer).",
                        stuck_count=len(stuck), stuck_minutes=stuck_minutes,
                        sample=[str(t.get("id")) for t in stuck[:20]]))
    else:
        out.append(_ok(g, "assistant.traces.stuck", "No Luna invocation is hung",
                       f"no trace has been queued longer than {stuck_minutes:g} minutes"))

    # The link from an Assistant message trace to the MLflow span. When this is
    # null the conversation worked but left no observable trail, which is the
    # "traces are missing" report that the MLflow group alone cannot explain.
    sample = recent or traces[:50]
    unlinked = [t for t in sample if not t.get("trace_id") and not t.get("span_id")]
    if unlinked:
        out.append(_bad(g, "assistant.traces.linked", "Message traces carry an MLflow id",
                        f"{len(unlinked)} of {len(sample)} recent trace(s) have neither "
                        "trace_id nor span_id",
                        "These conversations ran without being recorded in MLflow. Nothing "
                        "about them can be debugged after the fact.",
                        unlinked=len(unlinked), sampled=len(sample)))
    else:
        out.append(_ok(g, "assistant.traces.linked", "Message traces carry an MLflow id",
                       f"all {len(sample)} sampled trace(s) are linked"))

    undelivered = [t for t in sample if t.get("webhook_url") and not t.get("webhook_delivered_at")
                   and str(t.get("status") or "").lower() == "completed"]
    if undelivered:
        out.append(_bad(g, "assistant.traces.webhooks", "Completed traces delivered their webhook",
                        f"{len(undelivered)} completed trace(s) never delivered their webhook",
                        "Whatever was waiting on the callback never heard back.",
                        undelivered=len(undelivered)))
    else:
        out.append(_ok(g, "assistant.traces.webhooks",
                       "Completed traces delivered their webhook", "none outstanding"))
    return out


# ---------------------------------------------------------------------------
# Report assembly and before/after diff
# ---------------------------------------------------------------------------
def summarise(results: list[CheckResult]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    by_group: dict[str, dict[str, int]] = {}
    for res in results:
        by_status[res.status] = by_status.get(res.status, 0) + 1
        by_group.setdefault(res.group, {})
        by_group[res.group][res.status] = by_group[res.group].get(res.status, 0) + 1
    failures = [r for r in results if r.status == FAIL]
    return {
        "total": len(results),
        "by_status": by_status,
        "by_group": by_group,
        "clean": not by_status.get(FAIL) and not by_status.get(UNKNOWN),
        "first_blocking": failures[0].id if failures else None,
    }


def compare(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Join two runs on CheckResult.id.

    A check that failed before and fails now is pre-existing, not a regression.
    Conflating the two is what makes a sanity report unreadable, so they are
    separate buckets and only `regressed` drives the exit code.
    """
    old = {c["id"]: c for c in baseline.get("checks", [])}
    new = {c["id"]: c for c in current.get("checks", [])}

    def row(cid: str) -> dict[str, Any]:
        a, b = old.get(cid), new.get(cid)
        return {
            "id": cid,
            "title": (b or a or {}).get("title", ""),
            "group": (b or a or {}).get("group", ""),
            "before": (a or {}).get("status"),
            "after": (b or {}).get("status"),
            "detail": (b or {}).get("detail") or (a or {}).get("detail", ""),
        }

    regressed, fixed, still_failing, appeared, disappeared = [], [], [], [], []
    unchanged = 0
    for cid in sorted(set(old) | set(new)):
        before = (old.get(cid) or {}).get("status")
        after = (new.get(cid) or {}).get("status")
        if before is None:
            appeared.append(row(cid))
        elif after is None:
            # Usually an entity the deployment deleted, so it is worth surfacing
            # rather than folding into the unchanged count.
            disappeared.append(row(cid))
        elif before == after == FAIL:
            still_failing.append(row(cid))
        elif before == after:
            unchanged += 1
        elif after == FAIL:
            regressed.append(row(cid))
        elif before == FAIL:
            fixed.append(row(cid))
        else:
            unchanged += 1

    return {
        "baseline_run": baseline.get("environment", {}).get("run_at"),
        "baseline_digest": baseline.get("environment", {}).get("image_digest"),
        "regressed": regressed,
        "fixed": fixed,
        "still_failing": still_failing,
        "appeared": appeared,
        "disappeared": disappeared,
        "unchanged_count": unchanged,
    }


def check_digest_changed(baseline: dict[str, Any], current: dict[str, Any]) -> CheckResult | None:
    """In after mode, an unchanged image digest means the deploy did not land.

    This is the failure the whole before/after workflow exists to catch: every
    other check passes, because nothing was deployed.
    """
    before = baseline.get("environment", {}).get("image_digest")
    after = current.get("environment", {}).get("image_digest")
    cid = "deployment.image_changed"
    title = "The deployment changed the running image"
    if not before or not after:
        return _unk("deployment", cid, title,
                    "the image digest is unknown on one or both runs",
                    "Needs Azure lookup on both runs to be meaningful.",
                    before=before, after=after)
    if before == after:
        return _bad("deployment", cid, title,
                    f"the running image is unchanged ({after}); the deployment did not take",
                    "Nothing was deployed. Check the pipeline actually pushed and the "
                    "revision rolled over.", before=before, after=after)
    return _ok("deployment", cid, title, f"{before} -> {after}", before=before, after=after)


# ---------------------------------------------------------------------------
# Tool presence report
#
# "Is every tool this instance's processes and agents reach for actually here?"
# The per-check lines answer it one reference at a time; this rolls them into one
# deduplicated list, because the same missing tool referenced by nine processes is
# one problem to fix, not nine.
# ---------------------------------------------------------------------------
def build_tool_report(results: list[CheckResult]) -> dict[str, Any]:
    tools: dict[str, dict[str, Any]] = {}

    def row(name: str) -> dict[str, Any]:
        return tools.setdefault(name, {"tool": name, "present": None,
                                       "processes": [], "agents": []})

    for res in results:
        ev = res.evidence
        name = None
        user, bucket = None, None
        if ev.get("kind") == "tool" and ev.get("ref"):
            name, user, bucket = str(ev["ref"]), ev.get("process"), "processes"
        elif ev.get("tool") and ev.get("agent"):
            name, user, bucket = str(ev["tool"]), ev.get("agent"), "agents"
        elif ev.get("tool") and res.id.startswith("assistant.luna.tool."):
            name, user, bucket = str(ev["tool"]), "luna_agent", "agents"
        if not name:
            continue
        entry = row(name)
        if res.status == PASS:
            entry["present"] = True if entry["present"] is not False else False
        elif res.status == FAIL:
            entry["present"] = False
        if user and user not in entry[bucket]:
            entry[bucket].append(str(user))

    rows = sorted(tools.values(), key=lambda r: (r["present"] is not False, r["tool"]))
    missing = [r for r in rows if r["present"] is False]
    orphans: list[str] = []
    for res in results:
        if res.id == "process.orphan_tools":
            orphans = list(res.evidence.get("orphan_tools") or [])
    return {
        "referenced": len(rows),
        "present": sum(1 for r in rows if r["present"]),
        "missing": len(missing),
        "unreferenced_deployed": orphans,
        "tools": rows,
    }


def render_tool_report(report: dict[str, Any], *, show_all: bool) -> None:
    tty = sys.stdout.isatty()
    tick = "\u2713" if sys.stdout.encoding and "utf" in sys.stdout.encoding.lower() else "y"
    cross = "\u2717" if sys.stdout.encoding and "utf" in sys.stdout.encoding.lower() else "N"

    def mark(present: bool | None) -> str:
        glyph, color = (tick, "\033[32m") if present else (cross, "\033[31m")
        return f"{color}{glyph}{RESET}" if tty else glyph

    rows = report["tools"]
    if not rows:
        return
    print("\n" + "=" * 96)
    print("TOOL PRESENCE  —  every tool referenced by a process or agent on this instance")
    print("=" * 96)
    width = min(max((len(r["tool"]) for r in rows), default=20) + 2, 52)
    shown = rows if show_all else [r for r in rows if not r["present"]]
    if shown:
        print(f"\n  {'':2}{'tool':<{width}}{'used by':<34}status")
        print("  " + "-" * (width + 46))
    for r in shown:
        users = []
        if r["processes"]:
            users.append(f"{len(r['processes'])} process"
                         + ("es" if len(r["processes"]) != 1 else ""))
        if r["agents"]:
            users.append(f"{len(r['agents'])} agent"
                         + ("s" if len(r["agents"]) != 1 else ""))
        status = "deployed" if r["present"] else "MISSING"
        print(f"  {mark(r['present'])} {r['tool'][:width - 2]:<{width}}"
              f"{', '.join(users):<34}{status}")
        if not r["present"]:
            first = (r["processes"] + r["agents"])[:4]
            if first:
                print(f"       referenced by: {', '.join(first)}"
                      + (" ..." if len(r["processes"]) + len(r["agents"]) > 4 else ""))
    if not show_all and not shown:
        print(f"\n  All {report['referenced']} referenced tool(s) are deployed.")
    print(f"\n  {report['present']}/{report['referenced']} referenced tools deployed, "
          f"{report['missing']} missing."
          + ("" if show_all else "   (--tool-report-all lists the deployed ones too)"))
    if report["unreferenced_deployed"]:
        extra = report["unreferenced_deployed"]
        print(f"  {len(extra)} deployed tool(s) nothing references: "
              + ", ".join(extra[:8]) + (" ..." if len(extra) > 8 else ""))
    print("=" * 96)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
MARK = {PASS: "PASS", FAIL: "FAIL", UNKNOWN: "UNKN", SKIPPED: "SKIP", INFO: "INFO"}
COLOR = {PASS: "\033[32m", FAIL: "\033[31m", UNKNOWN: "\033[33m",
         SKIPPED: "\033[90m", INFO: "\033[36m"}
RESET = "\033[0m"


def render(results: list[CheckResult], comparison: dict[str, Any] | None,
           env: dict[str, Any], *, verbose: bool) -> None:
    tty = sys.stdout.isatty()

    def paint(status: str) -> str:
        mark = MARK[status]
        return f"{COLOR[status]}{mark}{RESET}" if tty else mark

    print()
    print("=" * 96)
    print(f"DEPLOYMENT VALIDATION  —  {env['label']}  @  {env['gateway_url']}")
    print(f"run at {env['run_at']}   checks: {', '.join(env['groups_run'])}")
    print(f"token:  {env.get('token_expiry', 'unknown')}")
    print(f"calls:  {env.get('read_calls', 0)} read, "
          f"{env.get('write_calls_attempted', 0)} write  (this tool never writes)")
    print("=" * 96)

    for group in GROUPS:
        rows = [r for r in results if r.group == group]
        if not rows:
            continue
        counts = summarise(rows)["by_status"]
        tally = "  ".join(f"{MARK[s]} {counts[s]}" for s in (FAIL, UNKNOWN, PASS, INFO, SKIPPED)
                          if counts.get(s))
        print(f"\n  {group.upper()}   {tally}")
        print("  " + "-" * 92)
        for res in rows:
            if not verbose and res.status in (PASS, INFO, SKIPPED):
                continue
            print(f"  {paint(res.status)}  {res.title}")
            if res.detail:
                print(f"        {res.detail}")
            if res.remediation and res.status == FAIL:
                print(f"        -> {res.remediation}")
        if not verbose and not any(r.status in (FAIL, UNKNOWN) for r in rows):
            print(f"        all {len(rows)} check(s) clean  (--verbose to list them)")

    summary = summarise(results)
    print("\n" + "=" * 96)
    print("  " + "   ".join(f"{MARK[s]}: {n}" for s, n in sorted(summary["by_status"].items())))

    if comparison is not None:
        print()
        if comparison["regressed"]:
            print(f"  {len(comparison['regressed'])} REGRESSION(S) — passed before, "
                  "failing now:")
            for row in comparison["regressed"]:
                print(f"    {paint(FAIL)}  [{row['group']}] {row['title']}")
                if row["detail"]:
                    print(f"          {row['detail']}")
        else:
            print("  No regressions: nothing that passed before is failing now.")
        for bucket, label in (("fixed", "fixed by this deployment"),
                              ("disappeared", "checks that vanished (entity deleted?)"),
                              ("appeared", "new checks")):
            rows = comparison[bucket]
            if rows:
                print(f"  {len(rows)} {label}: "
                      + ", ".join(r["id"] for r in rows[:6])
                      + (" ..." if len(rows) > 6 else ""))
        if comparison["still_failing"]:
            print(f"  {len(comparison['still_failing'])} pre-existing failure(s), "
                  "unchanged by this deployment.")

    print()
    if summary["clean"]:
        print("  CHECKLIST CLEAN — no failures and nothing left unknown.")
    else:
        bits = []
        if summary["by_status"].get(FAIL):
            bits.append(f"{summary['by_status'][FAIL]} failure(s)")
        if summary["by_status"].get(UNKNOWN):
            bits.append(f"{summary['by_status'][UNKNOWN]} unknown")
        print(f"  CHECKLIST NOT CLEAN — {', '.join(bits)}.")
        if summary["first_blocking"]:
            blocking = next(r for r in results if r.id == summary["first_blocking"])
            print(f"  First blocking item: {blocking.title} — {blocking.detail}")
    print("=" * 96 + "\n")


def exit_code(summary: dict[str, Any], comparison: dict[str, Any] | None) -> int:
    if comparison is not None:
        # After mode judges the DEPLOYMENT, not the instance. An instance that was
        # already dirty and got no worse is a successful deployment.
        return 1 if comparison["regressed"] else 0
    if summary["by_status"].get(FAIL):
        return 1
    if summary["by_status"].get(UNKNOWN):
        return 2
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def build_client(args: argparse.Namespace) -> JazzXClient:
    client = JazzXClient(
        gateway_url=normalize_gateway_url(require_value(
            args.gateway_url, name="--gateway-url", env_name="JAZZX_GATEWAY_URL")),
        token=require_value(args.token, name="--token", env_name="JAZZX_TOKEN"),
        timeout=args.timeout,
    )
    # Builder Studio requires x-user-id on its list endpoints and 422s without it.
    # The token already carries the user id in `sub`, so derive it rather than
    # making the caller look it up and paste it in.
    headers: dict[str, str] = {}
    user_id = args.user_id
    if not user_id:
        claims = decode_jwt_payload(client.token) or {}
        if isinstance(claims.get("sub"), str):
            user_id = claims["sub"]
            print(f"x-user-id not given; using the token subject {user_id}", file=sys.stderr)
    if user_id:
        headers["x-user-id"] = user_id
    if args.workspace_id:
        headers["x-workspace-id"] = args.workspace_id
    if headers:
        client.session.headers.update(headers)
    return client


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--gateway-url", default=os.getenv("JAZZX_GATEWAY_URL", ""))
    parser.add_argument("--token", default=os.getenv("JAZZX_TOKEN"))
    parser.add_argument("--user-id", default=os.getenv("JAZZX_USER_ID"),
                        help="x-user-id header; Builder Studio list endpoints require it")
    parser.add_argument("--workspace-id", default=os.getenv("JAZZX_WORKSPACE_ID"),
                        help="x-workspace-id header")
    parser.add_argument("--label", default="instance", help="Names this instance in reports")
    parser.add_argument("--checks", default=",".join(DEFAULT_GROUPS),
                        help=f"Comma-separated subset of: "
                             f"{', '.join(GROUPS)} "
                             f"({', '.join(OPT_IN_GROUPS)} also need --classify-macer)")
    parser.add_argument("--classify-macer", action="store_true",
                        help="Also check split & classify and MACER readiness (still "
                             "read-only: neither is run). Off unless given.")
    parser.add_argument("--collection-id", help="Target collection for the deeper checks")
    parser.add_argument("--config-name", default="APP_CONFIG")
    parser.add_argument("--baseline", type=Path,
                        help="A previous JSON report (older runs wrote them); switches "
                             "this run into after mode")
    parser.add_argument("--output-dir", type=Path, help="Where to write the Markdown report (<label>-<time>.md)")
    parser.add_argument("--skip-azure", action="store_true",
                        help="Do not shell out to the az CLI")
    parser.add_argument("--mlflow-uri", default=os.getenv("MLFLOW_TRACKING_URI"))
    parser.add_argument("--mlflow-experiment", default=os.getenv("MLFLOW_EXPERIMENT_NAME"))
    parser.add_argument("--trace-window", type=float, default=24.0,
                        help="Hours of MLflow history that count as recent (default 24)")
    parser.add_argument("--ui-url", default=os.getenv("JAZZX_UI_URL"),
                        help="Host serving the web app. Derived from --gateway-url by "
                             "dropping the -gw suffix when not given")
    parser.add_argument("--bss-prefix", default=os.getenv("JAZZX_BSS_PREFIX", BSS_PREFIX_DEFAULT),
                        help="Gateway mount for Builder Studio "
                             f"(default {BSS_PREFIX_DEFAULT})")
    parser.add_argument("--luna-agent-name", default=LUNA_AGENT_NAME,
                        help=f"Name the Assistant expects its agent to have "
                             f"(default {LUNA_AGENT_NAME})")
    parser.add_argument("--stuck-trace-minutes", type=float, default=15.0,
                        help="A message trace still 'queued' after this long is hung "
                             "(default 15)")
    parser.add_argument("--max-processes", type=int,
                        help="Check only the first N processes (by key) -- for a fast run")
    parser.add_argument("--max-pipeline-rows", type=int, default=50)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--tool-report", action="store_true",
                        help="Print the tool presence table: every tool referenced by a "
                             "process or agent, ticked when deployed here")
    parser.add_argument("--tool-report-all", action="store_true",
                        help="With --tool-report, list the deployed tools as well as the "
                             "missing ones")
    parser.add_argument("--verbose", action="store_true", help="List passing checks too")
    parser.add_argument("--no-progress", action="store_true",
                        help="Do not print per-process progress")
    args = parser.parse_args(argv)

    set_bss_prefix(args.bss_prefix)
    args.groups = [g.strip() for g in args.checks.split(",") if g.strip()]
    disabled = [g for g in args.groups if g in DISABLED_GROUPS]
    if disabled:
        parser.error(f"check group(s) {', '.join(disabled)} are switched off in this script")
    unknown = [g for g in args.groups if g not in GROUPS]
    if unknown:
        parser.error(f"unknown check group(s): {', '.join(unknown)}; pick from {', '.join(GROUPS)}")
    opt_in = [g for g in args.groups if g in OPT_IN_GROUPS]
    if opt_in and not args.classify_macer:
        parser.error(f"{', '.join(opt_in)} run only with --classify-macer")
    if args.classify_macer:
        args.groups += [g for g in OPT_IN_GROUPS if g not in args.groups]
    if args.collection_id:
        try:
            import uuid  # noqa: PLC0415
            uuid.UUID(args.collection_id)
        except ValueError:
            parser.error("--collection-id must be a UUID")
    if args.trace_window <= 0:
        parser.error("--trace-window must be greater than zero")
    return args


def preflight(args: argparse.Namespace, client: JazzXClient, probe: Probe
              ) -> tuple[int | None, dict[str, Any] | None, str, str]:
    """Baseline, token and gateway checks. Returns (exit code to stop with, or None;
    baseline report; token summary; token expiry)."""
    baseline: dict[str, Any] | None = None
    if args.baseline:
        if not args.baseline.is_file():
            print(f"ERROR: baseline {args.baseline} does not exist", file=sys.stderr)
            return 3, None, "", ""
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        if baseline.get("schema_version") != SCHEMA_VERSION:
            print(f"ERROR: baseline schema_version {baseline.get('schema_version')} != "
                  f"{SCHEMA_VERSION}; re-record the before run", file=sys.stderr)
            return 3, None, "", ""

    token_summary, token_exp_iso = token_expiry(client.token)
    print(f"token: {token_summary}", file=sys.stderr)
    if token_summary.startswith("EXPIRED"):
        print("ERROR: the token has expired; every check would be meaningless. "
              "Get a fresh token and run again.", file=sys.stderr)
        return 3, None, token_summary, token_exp_iso
    if token_summary == "not a readable JWT":
        print("ERROR: JAZZX_TOKEN / --token is not a valid token (placeholder text, or cut "
              "off when pasted); every call would be refused. Paste the full token and run "
              "again.", file=sys.stderr)
        return 3, None, token_summary, token_exp_iso

    # A UI host answers 200 with index.html for every path, including nonsense
    # ones, so a run against it reports invented successes rather than failing.
    # Catching it here costs one request and saves a whole misleading report.
    nonsense = "/__deployment_validation_probe__"
    code, body = probe.get(nonsense)
    if looks_like_static_site(body):
        suggestion = ""
        host = urlparse(client.gateway_url).hostname or ""
        labels = host.split(".")
        if labels and not labels[0].endswith("-gw"):
            labels[0] += "-gw"
            suggestion = (f"  Try the gateway host instead: "
                          f"https://{'.'.join(labels)}")
        print(f"ERROR: {client.gateway_url} is a static web host, not the API gateway "
              f"-- {nonsense} returned a page or a storage error rather than JSON."
              f"{suggestion}", file=sys.stderr)
        return 3, None, token_summary, token_exp_iso
    return None, baseline, token_summary, token_exp_iso


def run_checks(args: argparse.Namespace, client: JazzXClient, probe: Probe, *,
               progress: Any = None) -> tuple[list[CheckResult], str | None]:
    """Run the selected check groups. Returns (results, image digest if seen)."""
    results: list[CheckResult] = []
    image_digest: str | None = None

    if "platform" in args.groups:
        print("checking platform basics ...", file=sys.stderr)
        results.extend(check_platform(probe, client.token))

    if "deployment" in args.groups:
        print("checking deployment identity ...", file=sys.stderr)
        rows = check_deployment(probe, client.gateway_url, skip_azure=args.skip_azure)
        results.extend(rows)
        for row in rows:
            images = row.evidence.get("images") or []
            if images:
                image_digest = str(images[0])
                break

    if "frontend" in args.groups:
        print("checking micro-frontend remotes ...", file=sys.stderr)
        results.extend(check_frontend(
            probe, ui_url=args.ui_url, gateway_url=client.gateway_url, timeout=args.timeout))

    # Process / tool / agent validation is switched off for now (too slow on large
    # instances). Uncomment this block and remove "process" from DISABLED_GROUPS to restore it.
    # if "process" in args.groups:
    #     print("checking process / tool / agent closure ...", file=sys.stderr)
    #     results.extend(check_process_closure(probe, max_processes=args.max_processes,
    #                                          progress=progress))

    if "collections" in args.groups:
        print("checking collections and ontologies ...", file=sys.stderr)
        results.extend(check_collections(
            probe, client, collection_id=args.collection_id))

    if "traces" in args.groups:
        print("checking ML traces ...", file=sys.stderr)
        results.extend(check_traces(
            tracking_uri=args.mlflow_uri, experiment=args.mlflow_experiment,
            window_hours=args.trace_window, timeout=args.timeout))

    if "pipeline" in args.groups:
        print("checking the loan pipeline ...", file=sys.stderr)
        results.extend(check_pipeline(probe, max_rows=args.max_pipeline_rows))

    if "assistant" in args.groups:
        print("checking the Assistant and the Luna agent ...", file=sys.stderr)
        results.extend(check_assistant(
            probe, luna_name=args.luna_agent_name,
            stuck_minutes=args.stuck_trace_minutes,
            trace_window_hours=args.trace_window))

    if "loan" in args.groups:
        print("checking loan-creation preconditions ...", file=sys.stderr)
        results.extend(check_loan_creation(probe, collection_id=args.collection_id))

    inventory: Inventory | None = None
    if "classify" in args.groups or "macer" in args.groups:
        inventory = load_inventory(probe)

    if "classify" in args.groups:
        print("checking split & classify readiness ...", file=sys.stderr)
        results.extend(check_split_classify(
            probe, inventory, config_name=args.config_name, collection_id=args.collection_id))

    if "macer" in args.groups:
        print("checking MACER readiness (without invoking it) ...", file=sys.stderr)
        results.extend(check_macer(
            probe, inventory, config_name=args.config_name, collection_id=args.collection_id))
    return results, image_digest


def _cell(value: Any) -> str:
    """A value made safe for one Markdown table cell."""
    text = "-" if value in (None, "") else str(value)
    # Keep internal config paths out of the human-facing report while retaining
    # the underlying validation details in the check data.
    text = text.replace("macer_config.default.", "MACER default setting ")
    text = text.replace("macer_config.default", "MACER default configuration")
    return text.replace("|", "\\|").replace("\n", " ").strip()


def render_markdown(report: dict[str, Any]) -> str:
    """The whole run as one Markdown document: summary, what needs attention,
    every APP_CONFIG id, config drift, before/after changes, and every check."""
    env, summary = report["environment"], report["summary"]
    checks = report["checks"]
    lines: list[str] = []
    add = lines.append

    add(f"# Deployment validation — {env['label']}")
    add("")
    add("| | |")
    add("|---|---|")
    for name, value in (("Gateway", env["gateway_url"]), ("Run at (UTC)", env["run_at"]),
                        ("Checks", ", ".join(env["groups_run"])),
                        ("Token lifetime", env.get("token_lifetime") or env.get("token_expiry")),
                        ("Calls", f"{env['read_calls']} read, {env['write_calls_attempted']} write")):
        add(f"| {name} | {_cell(value)} |")
    add("")

    add("## Summary")
    add("")
    statuses = (FAIL, UNKNOWN, PASS, INFO, SKIPPED)
    add("| Group | " + " | ".join(s.capitalize() for s in statuses) + " |")
    add("|---|" + "---:|" * len(statuses))
    for group, counts in summary["by_group"].items():
        add(f"| {group} | " + " | ".join(str(counts.get(s, 0) or "") for s in statuses) + " |")
    add("| **Total** | " + " | ".join(f"**{summary['by_status'].get(s, 0)}**" for s in statuses) + " |")
    add("")

    for status, heading in ((FAIL, "Failures"), (UNKNOWN, "Could not be decided")):
        rows = [c for c in checks if c["status"] == status]
        if not rows:
            continue
        add(f"## {heading} ({len(rows)})")
        add("")
        add("| Group | Check | Detail | What to do |")
        add("|---|---|---|---|")
        for c in rows:
            add(f"| {c['group']} | {_cell(c['title'])} | {_cell(c['detail'])} | {_cell(c.get('remediation'))} |")
        add("")

    comparison = report.get("comparison")
    if comparison:
        add(f"## Changes since the baseline ({_cell(comparison.get('baseline_run'))})")
        add("")
        for key, heading in (("regressed", "Regressed"), ("fixed", "Fixed"),
                             ("still_failing", "Still failing"), ("appeared", "New checks"),
                             ("disappeared", "Gone")):
            rows = comparison.get(key) or []
            if rows:
                add(f"**{heading} ({len(rows)})**")
                add("")
                for r in rows:
                    add(f"- {_cell(r['title'])} ({r['before'] or '-'} → {r['after'] or '-'})")
                add("")
        add(f"{comparison['unchanged_count']} unchanged.")
        add("")

    if env.get("transport_errors"):
        add("## Transport errors")
        add("")
        for e in env["transport_errors"]:
            add(f"- {_cell(e)}")
        add("")

    add(f"## All checks ({len(checks)})")
    add("")
    add("| Group | Status | Check | Detail |")
    add("|---|---|---|---|")
    for c in checks:
        add(f"| {c['group']} | {c['status']} | {_cell(c['title'])} | {_cell(c['detail'])} |")
    add("")
    return "\n".join(lines)


def finish(args: argparse.Namespace, client: JazzXClient, probe: Probe,
           results: list[CheckResult], image_digest: str | None,
           baseline: dict[str, Any] | None, token_summary: str, token_exp_iso: str, *,
           groups_run: list[str] | None = None) -> int:
    """Summarise, compare with the baseline, print, write the report, return the exit code."""
    env = {
        "label": args.label,
        "gateway_url": client.gateway_url,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "groups_run": groups_run or args.groups,
        "image_digest": image_digest,
        "token_expiry": token_summary,
        "token_expires_at": token_exp_iso,
        "token_lifetime": token_lifetime(client.token),
        "read_calls": probe.reads,
        "write_calls_attempted": probe.writes_attempted,
        "transport_errors": probe.errors[:20],
    }
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "environment": env,
        "checks": [redact(asdict(r)) for r in results],
    }
    comparison = None
    if baseline is not None:
        digest_check = check_digest_changed(baseline, report)
        if digest_check is not None:
            results.append(digest_check)
            report["checks"].append(redact(asdict(digest_check)))
        comparison = compare(baseline, report)
        report["comparison"] = comparison

    report["summary"] = summarise(results)
    tool_report = build_tool_report(results)
    if tool_report["referenced"]:
        report["tool_report"] = tool_report
    render(results, comparison, env, verbose=args.verbose)
    if (args.tool_report or args.tool_report_all) and tool_report["referenced"]:
        render_tool_report(tool_report, show_all=args.tool_report_all)

    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = args.output_dir / f"{args.label}-{stamp}.md"
        path.write_text(render_markdown(report), encoding="utf-8")
        print(f"Report written to {path}", file=sys.stderr)

    return exit_code(report["summary"], comparison)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    client = build_client(args)
    probe = Probe(client)
    stop, baseline, token_summary, token_exp_iso = preflight(args, client, probe)
    if stop is not None:
        return stop
    progress = None if args.no_progress else process_progress()
    results, image_digest = run_checks(args, client, probe, progress=progress)

    return finish(args, client, probe, results, image_digest, baseline,
                  token_summary, token_exp_iso)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ApiError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(3)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        raise SystemExit(3)
