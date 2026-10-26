"""Tests for the pure logic in scripts/deployment_validation.py.

Nothing here touches the network. The parts worth testing are the ones a live
run cannot check: that the BPMN walker finds the references that are really
there and ignores the ones that only look like references, that the before/after
diff keeps a pre-existing failure out of the regression list, and that the exit
code follows from that distinction rather than from a raw failure count.
"""

import contextlib
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from deployment_validation import (  # noqa: E402
    GROUPS,
    LUNA_AGENT_NAME,
    FAIL,
    INFO,
    PASS,
    SKIPPED,
    UNKNOWN,
    CheckResult,
    compare,
    check_digest_changed,
    exit_code,
    decode_jwt_payload,
    extract_dependencies,
    redact,
    summarise,
    _find_agent,
    _index_by_name,
    _is_inactive,
    set_bss_prefix,
    service_probes,
    derive_ui_url,
    _is_javascript,
    MFE_URL_RE,
    FEDERATE_BASE_RE,
    INSTANCE_URL_RE,
    token_expiry,
    looks_like_static_site,
    build_tool_report,
    _entity_type_count,
    _extract_xml,
)
import deployment_validation as ds  # noqa: E402


def _bpmn(body: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL"
             xmlns:flowable="http://flowable.org/bpmn">
  <process id="p1" name="P1" isExecutable="true">
{body}
  </process>
</definitions>"""


class TestDependencyExtraction(unittest.TestCase):
    def deps(self, body):
        found, error = extract_dependencies(_bpmn(body))
        self.assertIsNone(error)
        return {(d.kind, d.ref) for d in found}

    def test_tool_name_literal_in_script(self):
        body = """    <scriptTask id="prep" scriptFormat="groovy">
      <script>execution.setVariable("toolName", "los_fetch_loan_snapshot")</script>
    </scriptTask>"""
        self.assertIn(("tool", "los_fetch_loan_snapshot"), self.deps(body))

    def test_snake_case_tool_name_variant(self):
        body = """    <scriptTask id="prep" scriptFormat="groovy">
      <script>execution.setVariable("tool_name", "truv_exchange_token_tool")</script>
    </scriptTask>"""
        self.assertIn(("tool", "truv_exchange_token_tool"), self.deps(body))

    def test_single_quoted_literals(self):
        body = """    <scriptTask id="prep"><script>
      execution.setVariable('toolName', 'entity_create')
    </script></scriptTask>"""
        self.assertIn(("tool", "entity_create"), self.deps(body))

    def test_domain_entity_is_not_mistaken_for_an_agent(self):
        """`real_estate_agent_name` is a mortgage field, not an agent reference.

        The leading-quote anchor in the pattern is what keeps it out; without it
        every loan process reports a missing agent.
        """
        body = """    <scriptTask id="prep"><script>
      execution.setVariable('real_estate_agent_name', 'Jane Doe')
      def x = payload.real_estate_agent_id
    </script></scriptTask>"""
        self.assertEqual({d for d in self.deps(body) if d[0] == "agent"}, set())

    def test_call_activity_is_a_subprocess_dependency(self):
        body = '    <callActivity id="c1" calledElement="DocumentProcessingV2" />'
        self.assertIn(("subprocess", "DocumentProcessingV2"), self.deps(body))

    def test_quoted_expression_call_activity_is_resolved_to_its_literal(self):
        body = """    <callActivity id="c1" calledElement="${'macerTriggerSender'}" />"""
        self.assertIn(("subprocess", "macerTriggerSender"), self.deps(body))

    def test_variable_call_activity_is_marked_dynamic(self):
        body = '    <callActivity id="c1" calledElement="${targetProcess}" />'
        found, _ = extract_dependencies(_bpmn(body))
        dynamic = [d for d in found if d.kind == "subprocess"]
        self.assertEqual(len(dynamic), 1)
        self.assertTrue(dynamic[0].dynamic)

    def test_delegate_expression_bean_is_captured_without_the_wrapper(self):
        body = ('    <serviceTask id="s1" '
                'flowable:delegateExpression="${kernelToolInvocationSyncDelegate}" />')
        self.assertIn(("delegate", "kernelToolInvocationSyncDelegate"), self.deps(body))

    def test_duplicate_references_collapse(self):
        body = """    <scriptTask id="a"><script>
      execution.setVariable("toolName", "entity_create")
    </script></scriptTask>
    <scriptTask id="b"><script>
      execution.setVariable("toolName", "entity_create")
    </script></scriptTask>"""
        found, _ = extract_dependencies(_bpmn(body))
        self.assertEqual(sum(1 for d in found if d.ref == "entity_create"), 1)

    def test_element_id_is_recorded_for_the_reference(self):
        body = """    <scriptTask id="prepareArgs"><script>
      execution.setVariable("toolName", "los_cancel_lock")
    </script></scriptTask>"""
        found, _ = extract_dependencies(_bpmn(body))
        tool = next(d for d in found if d.kind == "tool")
        self.assertEqual(tool.where, "prepareArgs")

    def test_unparseable_xml_reports_an_error_and_no_dependencies(self):
        found, error = extract_dependencies("<definitions><process>")
        self.assertEqual(found, [])
        self.assertIsNotNone(error)
        self.assertIn("did not parse", error)

    def test_a_definition_with_no_references_yields_nothing(self):
        self.assertEqual(self.deps('    <startEvent id="s" />'), set())


class TestCheckResult(unittest.TestCase):
    def test_rejects_an_unknown_group(self):
        with self.assertRaises(ValueError):
            CheckResult("nonsense", "x", "t", PASS)

    def test_rejects_an_unknown_status(self):
        with self.assertRaises(ValueError):
            CheckResult("process", "x", "t", "probably-fine")


class TestSummarise(unittest.TestCase):
    def test_clean_requires_no_failures_and_no_unknowns(self):
        rows = [CheckResult("process", "a", "A", PASS),
                CheckResult("process", "b", "B", INFO),
                CheckResult("loan", "c", "C", SKIPPED)]
        self.assertTrue(summarise(rows)["clean"])

    def test_an_unknown_alone_makes_it_unclean(self):
        rows = [CheckResult("process", "a", "A", PASS),
                CheckResult("traces", "b", "B", UNKNOWN)]
        summary = summarise(rows)
        self.assertFalse(summary["clean"])
        self.assertIsNone(summary["first_blocking"])

    def test_first_blocking_is_the_first_failure_in_order(self):
        rows = [CheckResult("process", "a", "A", PASS),
                CheckResult("process", "b", "B", FAIL),
                CheckResult("loan", "c", "C", FAIL)]
        self.assertEqual(summarise(rows)["first_blocking"], "b")


def _report(checks, digest=None):
    return {
        "schema_version": 1,
        "environment": {"run_at": "2026-09-30T00:00:00+00:00", "image_digest": digest},
        "checks": [{"group": "process", "id": cid, "title": cid, "status": st, "detail": ""}
                   for cid, st in checks],
    }


class TestComparison(unittest.TestCase):
    def test_pass_to_fail_is_a_regression(self):
        result = compare(_report([("a", PASS)]), _report([("a", FAIL)]))
        self.assertEqual([r["id"] for r in result["regressed"]], ["a"])
        self.assertEqual(result["still_failing"], [])

    def test_fail_to_fail_is_pre_existing_not_a_regression(self):
        result = compare(_report([("a", FAIL)]), _report([("a", FAIL)]))
        self.assertEqual(result["regressed"], [])
        self.assertEqual([r["id"] for r in result["still_failing"]], ["a"])

    def test_fail_to_pass_is_fixed(self):
        result = compare(_report([("a", FAIL)]), _report([("a", PASS)]))
        self.assertEqual([r["id"] for r in result["fixed"]], ["a"])

    def test_unknown_to_fail_is_a_regression(self):
        result = compare(_report([("a", UNKNOWN)]), _report([("a", FAIL)]))
        self.assertEqual([r["id"] for r in result["regressed"]], ["a"])

    def test_a_new_check_appears_rather_than_regressing(self):
        result = compare(_report([]), _report([("a", FAIL)]))
        self.assertEqual([r["id"] for r in result["appeared"]], ["a"])
        self.assertEqual(result["regressed"], [])

    def test_a_vanished_check_is_reported_separately(self):
        result = compare(_report([("a", PASS)]), _report([]))
        self.assertEqual([r["id"] for r in result["disappeared"]], ["a"])

    def test_unchanged_passes_are_only_counted(self):
        result = compare(_report([("a", PASS), ("b", PASS)]),
                         _report([("a", PASS), ("b", PASS)]))
        self.assertEqual(result["unchanged_count"], 2)
        self.assertEqual(result["regressed"], [])


class TestImageDigest(unittest.TestCase):
    def test_an_unchanged_digest_fails_because_the_deploy_did_not_land(self):
        result = check_digest_changed(_report([], "img@sha256:aaa"), _report([], "img@sha256:aaa"))
        self.assertEqual(result.status, FAIL)
        self.assertIn("did not take", result.detail)

    def test_a_changed_digest_passes(self):
        result = check_digest_changed(_report([], "img@sha256:aaa"), _report([], "img@sha256:bbb"))
        self.assertEqual(result.status, PASS)

    def test_a_missing_digest_is_unknown_not_a_failure(self):
        self.assertEqual(check_digest_changed(_report([]), _report([], "x")).status, UNKNOWN)


class TestExitCode(unittest.TestCase):
    def test_before_mode_fails_on_any_failure(self):
        self.assertEqual(exit_code(summarise([CheckResult("process", "a", "A", FAIL)]), None), 1)

    def test_before_mode_returns_two_when_only_degraded(self):
        self.assertEqual(exit_code(summarise([CheckResult("traces", "a", "A", UNKNOWN)]), None), 2)

    def test_before_mode_returns_zero_when_clean(self):
        self.assertEqual(exit_code(summarise([CheckResult("process", "a", "A", PASS)]), None), 0)

    def test_after_mode_ignores_pre_existing_failures(self):
        """A dirty instance that got no worse is a successful deployment."""
        summary = summarise([CheckResult("process", "a", "A", FAIL)])
        comparison = compare(_report([("a", FAIL)]), _report([("a", FAIL)]))
        self.assertEqual(exit_code(summary, comparison), 0)

    def test_after_mode_fails_on_a_regression(self):
        summary = summarise([CheckResult("process", "a", "A", FAIL)])
        comparison = compare(_report([("a", PASS)]), _report([("a", FAIL)]))
        self.assertEqual(exit_code(summary, comparison), 1)


class TestRedaction(unittest.TestCase):
    def test_secret_shaped_keys_are_replaced(self):
        out = redact({"token": "abc", "connection_string": "x", "name": "keep"})
        self.assertEqual(out, {"token": "<redacted>", "connection_string": "<redacted>",
                               "name": "keep"})

    def test_a_jwt_in_a_free_text_value_is_replaced(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcDEF123"
        self.assertEqual(redact({"detail": f"failed with {jwt} sent"}),
                         {"detail": "failed with <redacted> sent"})

    def test_nested_structures_are_walked(self):
        out = redact({"a": [{"api_key": "k", "ok": 1}]})
        self.assertEqual(out, {"a": [{"api_key": "<redacted>", "ok": 1}]})

    def test_key_matching_ignores_case(self):
        self.assertEqual(redact({"Authorization": "Bearer x"}), {"Authorization": "<redacted>"})


class TestJwtDecoding(unittest.TestCase):
    """The token check must work without a network call and without verifying."""

    @staticmethod
    def _token(claims):
        import base64, json

        def seg(obj):
            raw = json.dumps(obj).encode()
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")

        return f"{seg({'alg': 'RS256'})}.{seg(claims)}.signature"

    def test_claims_are_read_without_verifying_the_signature(self):
        claims = decode_jwt_payload(self._token({"exp": 123, "sub": "u1"}))
        self.assertEqual(claims["exp"], 123)
        self.assertEqual(claims["sub"], "u1")

    def test_padding_is_restored_for_segments_that_need_it(self):
        # base64 segments are stripped of '=' in a JWT; a decoder that forgets to
        # re-pad fails on roughly three quarters of real tokens.
        for size in range(1, 12):
            token = self._token({"sub": "x" * size})
            self.assertIsNotNone(decode_jwt_payload(token), f"failed at size {size}")

    def test_a_non_jwt_returns_none_rather_than_raising(self):
        self.assertIsNone(decode_jwt_payload("not-a-jwt"))
        self.assertIsNone(decode_jwt_payload("a.b"))

    def test_undecodable_payload_returns_none(self):
        self.assertIsNone(decode_jwt_payload("header.!!!not-base64!!!.sig"))


class TestAgentLookup(unittest.TestCase):
    def test_luna_is_found_case_insensitively(self):
        agents = [{"name": "Other"}, {"name": "Luna_Agent", "id": "a1"}]
        self.assertEqual(_find_agent(agents, LUNA_AGENT_NAME)["id"], "a1")

    def test_a_missing_agent_returns_none(self):
        self.assertIsNone(_find_agent([{"name": "other"}], LUNA_AGENT_NAME))

    def test_luna_agent_name_matches_what_the_assistant_pins(self):
        """assistant/app/conversation/api.py and the fulfilment UI both hardcode
        this exact string; a drift here makes the whole group check nothing."""
        self.assertEqual(LUNA_AGENT_NAME, "luna_agent")

    def test_name_index_is_lowercased_and_covers_alternate_keys(self):
        index = _index_by_name([{"name": "Alpha"}, {"tool_name": "Beta"}, {"key": "Gamma"}])
        self.assertEqual(set(index), {"alpha", "beta", "gamma"})

    def test_first_entry_wins_on_a_duplicate_name(self):
        index = _index_by_name([{"name": "dup", "id": 1}, {"name": "dup", "id": 2}])
        self.assertEqual(index["dup"]["id"], 1)


class TestInactivity(unittest.TestCase):
    def test_blank_and_healthy_statuses_count_as_usable(self):
        for status in ("", "active", "published", "enabled", "deployed", "ACTIVE"):
            self.assertEqual(_is_inactive({"status": status}), "", status)

    def test_anything_else_gives_a_reason(self):
        self.assertIn("draft", _is_inactive({"status": "draft"}))


class TestBssPrefix(unittest.TestCase):
    """The gateway fronts Builder Studio under /builder_studio; its own openapi.json
    says /api/v1. Taking the spec's value 404s every BSS call, so the prefix is a
    setting and every derived path must follow it."""

    def setUp(self):
        self.addCleanup(set_bss_prefix, ds.BSS_PREFIX_DEFAULT)

    def test_the_default_is_the_gateway_mount_not_the_internal_one(self):
        set_bss_prefix(ds.BSS_PREFIX_DEFAULT)
        self.assertEqual(ds.PROCESS_HEALTH, "/builder_studio/api/v1/bss_process/api-health")

    def test_every_derived_path_follows_the_prefix(self):
        set_bss_prefix("/custom/v2")
        for path in (ds.PROCESS_HEALTH, ds.PROCESS_LIST, ds.TOOL_LIST,
                     ds.NATIVE_TOOL_LIST, ds.AGENT_LIST, ds.DEPLOY_HISTORY,
                     ds.PROCESS_XML, ds.PROCESS_VERSIONS, ds.PROCESS_BY_KEY):
            self.assertTrue(path.startswith("/custom/v2/"), path)

    def test_slashes_are_normalised(self):
        for given in ("builder_studio/api/v1", "/builder_studio/api/v1/"):
            set_bss_prefix(given)
            self.assertEqual(ds.BSS, "/builder_studio/api/v1")

    def test_service_probes_track_the_current_prefix(self):
        set_bss_prefix("/custom/v2")
        probes = dict(service_probes())
        self.assertTrue(probes["builder_studio"].startswith("/custom/v2/"))


ENV_JS = """
window.env = {
  API_URL: 'https://staging-gw.plazahomemortgage.jazzx.co/auth',
  VITE_INSTANCE_URL: 'https://staging-gw.plazahomemortgage.jazzx.co',
  VITE_FEDERATE_WEB_URL: 'https://staging.plazahomemortgage.jazzx.co',
  VITE_MICROFRONTEND_URL_1: '/assistantuiweb/assets/remoteEntry.js',
  VITE_MICROFRONTEND_URL_10: '/tenth-ui/assets/remoteEntry.js',
  VITE_MICROFRONTEND_URL_2: '/knowledge-ui/assets/remoteEntry.js',
};
"""


class TestUiUrlDerivation(unittest.TestCase):
    """The UI and the gateway are different hosts, related by a -gw suffix on the
    first label. Checking the UI's assets against the gateway host finds nothing."""

    def test_the_gw_suffix_is_dropped_from_the_first_label(self):
        self.assertEqual(
            derive_ui_url("https://staging-gw.plazahomemortgage.jazzx.co"),
            "https://staging.plazahomemortgage.jazzx.co")

    def test_a_short_host_works_too(self):
        self.assertEqual(derive_ui_url("https://poc-gw.jazzx.co"), "https://poc.jazzx.co")

    def test_a_trailing_path_is_discarded(self):
        self.assertEqual(derive_ui_url("https://poc-gw.jazzx.co/api/v1"),
                         "https://poc.jazzx.co")

    def test_a_port_is_preserved(self):
        self.assertEqual(derive_ui_url("http://local-gw.test:8080"), "http://local.test:8080")

    def test_a_host_without_the_convention_returns_none_rather_than_guessing(self):
        self.assertIsNone(derive_ui_url("https://api.example.com"))
        self.assertIsNone(derive_ui_url("https://poc.jazzx.co"))

    def test_a_label_that_is_only_the_suffix_returns_none(self):
        self.assertIsNone(derive_ui_url("https://-gw.jazzx.co"))


class TestEnvJsParsing(unittest.TestCase):
    def test_every_remote_is_found(self):
        found = dict((i, u) for i, u in MFE_URL_RE.findall(ENV_JS))
        self.assertEqual(len(found), 3)
        self.assertEqual(found["2"], "/knowledge-ui/assets/remoteEntry.js")

    def test_remotes_sort_numerically_not_lexically(self):
        """URL_10 must come after URL_2, which string ordering gets wrong."""
        order = [i for i, _ in sorted(MFE_URL_RE.findall(ENV_JS), key=lambda p: int(p[0]))]
        self.assertEqual(order, ["1", "2", "10"])

    def test_the_federate_base_is_the_ui_host_not_the_gateway(self):
        self.assertEqual(FEDERATE_BASE_RE.search(ENV_JS).group(1),
                         "https://staging.plazahomemortgage.jazzx.co")

    def test_the_declared_instance_url_is_the_gateway(self):
        self.assertEqual(INSTANCE_URL_RE.search(ENV_JS).group(1),
                         "https://staging-gw.plazahomemortgage.jazzx.co")

    def test_api_url_is_not_mistaken_for_the_instance_url(self):
        self.assertNotIn("/auth", INSTANCE_URL_RE.search(ENV_JS).group(1))

    def test_double_quoted_values_parse_too(self):
        self.assertEqual(
            MFE_URL_RE.findall('VITE_MICROFRONTEND_URL_3: "/x/assets/remoteEntry.js"'),
            [("3", "/x/assets/remoteEntry.js")])

    def test_config_with_no_remotes_yields_nothing(self):
        self.assertEqual(MFE_URL_RE.findall("window.env = { API_URL: 'x' };"), [])


class TestJavascriptContentType(unittest.TestCase):
    def test_javascript_types_are_accepted(self):
        for ct in ("application/javascript", "text/javascript",
                   "application/x-javascript", "application/ecmascript"):
            self.assertTrue(_is_javascript(ct), ct)

    def test_an_html_fallback_is_rejected(self):
        """A 200 that serves index.html in place of the module is the quiet failure:
        the status code says healthy and the browser still refuses to run it."""
        self.assertFalse(_is_javascript("text/html"))

    def test_a_blob_error_document_is_rejected(self):
        self.assertFalse(_is_javascript("application/xml"))

    def test_a_missing_content_type_is_rejected(self):
        self.assertFalse(_is_javascript(""))


class TestTokenExpiry(unittest.TestCase):
    """Printed in the run header on every run, not behind --verbose: an expired
    token makes every other line of the report meaningless."""

    @staticmethod
    def _tok(exp):
        import base64, json

        def seg(o):
            return base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")

        return f"{seg({'alg': 'RS256'})}.{seg({'exp': exp})}.sig"

    def test_an_expired_token_is_called_out_in_capitals(self):
        import time

        summary, iso = token_expiry(self._tok(int(time.time()) - 7200))
        self.assertTrue(summary.startswith("EXPIRED"), summary)
        self.assertIn("2.0h ago", summary)
        self.assertTrue(iso)

    def test_a_token_expiring_within_the_hour_reports_minutes(self):
        import time

        summary, _ = token_expiry(self._tok(int(time.time()) + 600))
        self.assertIn("min", summary)
        self.assertFalse(summary.startswith("EXPIRED"))

    def test_a_healthy_token_reports_hours_and_an_absolute_time(self):
        import time

        summary, iso = token_expiry(self._tok(int(time.time()) + 9000))
        self.assertIn("valid 2.5h", summary)
        self.assertIn("until", summary)
        self.assertTrue(iso.endswith("+00:00"))

    def test_a_token_with_no_exp_claim_says_so(self):
        import base64, json

        def seg(o):
            return base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")

        summary, iso = token_expiry(f"{seg({'alg': 'x'})}.{seg({'sub': 'u'})}.sig")
        self.assertEqual(summary, "no exp claim")
        self.assertEqual(iso, "")

    def test_an_unreadable_token_does_not_raise(self):
        self.assertEqual(token_expiry("garbage")[0], "not a readable JWT")


class TestStaticSiteDetection(unittest.TestCase):
    """Pointing the tool at the web host instead of the gateway must fail fast.
    The UI answers every path, so a run against it invents successes."""

    def test_an_spa_index_page_is_detected(self):
        self.assertTrue(looks_like_static_site("<!doctype html><html lang=\"en\">"))

    def test_detection_is_case_insensitive(self):
        self.assertTrue(looks_like_static_site("<!DOCTYPE HTML><html>"))

    def test_an_azure_blob_error_is_detected(self):
        """Blob storage rejects the Authorization header with 403 XML, so the
        host never gets as far as serving HTML -- the earlier version of this
        check missed exactly that case."""
        self.assertTrue(looks_like_static_site(
            '<?xml version="1.0"?><Error><Code>AuthenticationFailed</Code>'))
        self.assertTrue(looks_like_static_site(
            '<?xml version="1.0"?><Error><Code>BlobNotFound</Code>'))

    def test_a_real_gateway_404_is_not_flagged(self):
        self.assertFalse(looks_like_static_site(
            '{"status": false, "message": "Endpoint not found."}'))

    def test_a_parsed_json_body_is_not_flagged(self):
        self.assertFalse(looks_like_static_site({"status": False}))
        self.assertFalse(looks_like_static_site(None))

    def test_a_marker_far_down_a_long_body_is_ignored(self):
        self.assertFalse(looks_like_static_site("x" * 500 + "<!doctype html"))


def _tool_check(tool, status, *, process=None, agent=None):
    ev = {"tool": tool, "agent": agent} if agent else {"kind": "tool", "ref": tool,
                                                       "process": process}
    return CheckResult("process", f"process.x.tool.{tool}", f"tool {tool}", status,
                       "", ev)


class TestToolReport(unittest.TestCase):
    """One missing tool referenced by nine processes is one problem to fix, not
    nine, so the report deduplicates by tool and counts the users."""

    def test_present_and_missing_are_both_counted(self):
        r = build_tool_report([_tool_check("a", PASS, process="p1"),
                               _tool_check("b", FAIL, process="p1")])
        self.assertEqual((r["referenced"], r["present"], r["missing"]), (2, 1, 1))

    def test_one_tool_referenced_by_several_processes_is_one_row(self):
        r = build_tool_report([_tool_check("a", FAIL, process="p1"),
                               _tool_check("a", FAIL, process="p2"),
                               _tool_check("a", FAIL, process="p3")])
        self.assertEqual(r["referenced"], 1)
        self.assertEqual(r["tools"][0]["processes"], ["p1", "p2", "p3"])

    def test_process_and_agent_users_are_tracked_separately(self):
        r = build_tool_report([_tool_check("a", PASS, process="p1"),
                               _tool_check("a", PASS, agent="ag1")])
        row = r["tools"][0]
        self.assertEqual(row["processes"], ["p1"])
        self.assertEqual(row["agents"], ["ag1"])

    def test_missing_anywhere_means_missing(self):
        """A tool that resolves for one caller and not another is still a problem;
        the optimistic reading would hide it."""
        r = build_tool_report([_tool_check("a", PASS, process="p1"),
                               _tool_check("a", FAIL, process="p2")])
        self.assertFalse(r["tools"][0]["present"])
        self.assertEqual(r["missing"], 1)

    def test_missing_tools_sort_first(self):
        r = build_tool_report([_tool_check("aaa", PASS, process="p"),
                               _tool_check("zzz", FAIL, process="p")])
        self.assertEqual([t["tool"] for t in r["tools"]], ["zzz", "aaa"])

    def test_non_tool_checks_are_ignored(self):
        r = build_tool_report([CheckResult("process", "x", "t", FAIL, "",
                                           {"kind": "subprocess", "ref": "subFlow"})])
        self.assertEqual(r["referenced"], 0)

    def test_orphans_are_carried_through(self):
        rows = [_tool_check("a", PASS, process="p"),
                CheckResult("process", "process.orphan_tools", "orphans", INFO, "",
                            {"orphan_tools": ["unused_one"]})]
        self.assertEqual(build_tool_report(rows)["unreferenced_deployed"], ["unused_one"])


class TestOntologyTypeCount(unittest.TestCase):
    """None and 0 must stay distinct: one means the response cannot answer, the
    other means the ontology is genuinely empty. Merging them either invents a
    failure or hides one."""

    def test_a_list_of_entity_types_is_counted(self):
        self.assertEqual(_entity_type_count({"entity_types": ["A", "B"]}), 2)

    def test_alternate_key_spellings_are_read(self):
        for key in ("entityTypes", "types", "classes", "entities"):
            self.assertEqual(_entity_type_count({key: ["A"]}), 1, key)

    def test_a_json_string_schema_is_parsed(self):
        self.assertEqual(_entity_type_count({"schema": '{"types": {"A": 1, "B": 2}}'}), 2)

    def test_an_empty_list_is_zero_not_unknown(self):
        self.assertEqual(_entity_type_count({"entity_types": []}), 0)

    def test_a_payload_that_cannot_answer_is_none_not_zero(self):
        self.assertIsNone(_entity_type_count({"id": "x", "name": "y"}))


class TestXmlExtraction(unittest.TestCase):
    def test_raw_xml_is_returned(self):
        self.assertIsNotNone(_extract_xml("<?xml version='1.0'?><definitions/>"))

    def test_json_with_an_xml_field_is_unwrapped(self):
        self.assertEqual(_extract_xml({"xml": "<definitions/>"}), "<definitions/>")

    def test_a_data_envelope_is_unwrapped(self):
        self.assertEqual(_extract_xml({"data": {"xml": "<d/>"}}), "<d/>")

    def test_an_empty_xml_field_is_not_accepted(self):
        self.assertIsNone(_extract_xml({"xml": "   "}))

    def test_plain_text_is_rejected(self):
        self.assertIsNone(_extract_xml("Endpoint not found"))


class TestGroupRegistration(unittest.TestCase):
    def test_the_new_groups_are_registered(self):
        for group in ("assistant", "platform", "frontend"):
            self.assertIn(group, GROUPS)

    def test_platform_runs_first_so_its_findings_explain_the_rest(self):
        self.assertEqual(GROUPS[0], "platform")

    def test_every_group_can_own_a_check_result(self):
        for group in GROUPS:
            CheckResult(group, "x", "t", PASS)


# ---------------------------------------------------------------------------
# split & classify, and MACER readiness
# ---------------------------------------------------------------------------


class FakeProbe:
    """Serves a canned instance through the Probe interface. Records every path
    so a test can prove nothing but reads happened."""

    def __init__(self, *, processes=(), tools=(), native=(), agents=(), ontologies=(),
                 config=None, entities=None, gets=None, list_code=200):
        self.lists = {
            ds.PROCESS_LIST: [{"key": k, "status": "active", "flowable_deployment_id": "d"}
                              if isinstance(k, str) else k for k in processes],
            ds.TOOL_LIST: [{"name": t, "status": "active"} for t in tools],
            ds.NATIVE_TOOL_LIST: [{"name": t} for t in native],
            ds.AGENT_LIST: [{"name": a, "kernel_agent_id": "k", "status": "active"}
                            if isinstance(a, str) else a for a in agents],
            ds.ONTOLOGIES: list(ontologies),
        }
        self.config = config
        self.entities = entities or {}
        self.gets = gets or {}
        self.list_code = list_code
        self.list_status = {}
        self.calls = []
        self.writes_attempted = 0

    def items(self, path, **params):
        self.calls.append(("items", path, params))
        if path == ds.ENTITIES:
            q = params.get("odata_query", "")
            etype = q.split("entity_type eq '")[1].rstrip("'")
            return list(self.entities.get(etype, []))
        self.list_status[path] = self.list_code
        return list(self.lists.get(path, [])) if self.list_code == 200 else []

    def get(self, path, *, params=None, expected=(200,)):
        self.calls.append(("get", path, params))
        if path == ds.GLOBAL_CONFIG:
            if self.config is None:
                return 200, {"items": []}
            return 200, {"items": [{"name": "APP_CONFIG", "value": self.config}]}
        return self.gets.get(path, (404, None))


def _all_v1_ready(**extra):
    chain = ds.CLASSIFY_CHAINS["v1"]
    return dict(
        processes=[chain["entry"], *chain["processes"]],
        tools=chain["tools"],
        agents=list(ds.CLASSIFY_AGENTS),
        ontologies=[{"id": n, "name": n} for n in ds.CLASSIFY_ONTOLOGIES],
        config={"dmn_mapping": {"external_to_jazz_mapping": {"dmn_name": "ext"},
                                "jazz_to_external_mapping": {"dmn_name": "int"}}},
        **extra,
    )


def _by_status(results):
    out = {}
    for r in results:
        out.setdefault(r.status, []).append(r.id)
    return out


class TestSplitClassifyReadiness(unittest.TestCase):
    def test_a_fully_deployed_v1_chain_has_no_failures(self):
        results = ds.check_split_classify(
            FakeProbe(**_all_v1_ready()), ds.load_inventory(FakeProbe(**_all_v1_ready())),
            config_name="APP_CONFIG", collection_id=None)
        self.assertNotIn(FAIL, _by_status(results))
        self.assertIn("classify.v2.entry", [r.id for r in results])  # absent v2 is info

    def test_a_missing_tool_the_process_walker_cannot_see_fails(self):
        ready = _all_v1_ready()
        ready["tools"] = [t for t in ready["tools"] if t != "splitting_orchestration_tool"]
        probe = FakeProbe(**ready)
        results = ds.check_split_classify(probe, ds.load_inventory(probe),
                                          config_name="APP_CONFIG", collection_id=None)
        self.assertIn("classify.v1.tool.splitting_orchestration_tool", _by_status(results)[FAIL])

    def test_native_tools_count_as_registered(self):
        ready = _all_v1_ready()
        ready["tools"] = [t for t in ready["tools"] if t != "get_document_metadata"]
        ready["native"] = ["get_document_metadata"]
        probe = FakeProbe(**ready)
        results = ds.check_split_classify(probe, ds.load_inventory(probe),
                                          config_name="APP_CONFIG", collection_id=None)
        self.assertNotIn(FAIL, _by_status(results))

    def test_an_agent_never_pushed_to_the_kernel_fails(self):
        ready = _all_v1_ready()
        ready["agents"] = [a for a in ready["agents"] if a != "classification_agent"]
        ready["agents"].append({"name": "classification_agent", "kernel_agent_id": None})
        probe = FakeProbe(**ready)
        results = ds.check_split_classify(probe, ds.load_inventory(probe),
                                          config_name="APP_CONFIG", collection_id=None)
        self.assertIn("classify.agent.classification_agent", _by_status(results)[FAIL])

    def test_a_template_placeholder_in_the_dmn_config_fails(self):
        ready = _all_v1_ready()
        ready["config"]["dmn_mapping"]["external_to_jazz_mapping"]["dmn_name"] = "{{X}}"
        probe = FakeProbe(**ready)
        results = ds.check_split_classify(probe, ds.load_inventory(probe),
                                          config_name="APP_CONFIG", collection_id=None)
        self.assertIn("classify.config.dmn_mapping_external_to_jazz_mapping_dmn_name",
                      _by_status(results)[FAIL])

    def test_a_refused_process_list_is_unknown_not_a_wall_of_failures(self):
        probe = FakeProbe(list_code=403)
        results = ds.check_split_classify(probe, ds.load_inventory(probe),
                                          config_name="APP_CONFIG", collection_id=None)
        self.assertEqual([(r.id, r.status) for r in results], [("classify.inventory", UNKNOWN)])

    def test_failed_and_unclassified_documents_in_the_target_loan_fail(self):
        def ent(state):
            return {"json_value": {"state": state}}
        ready = _all_v1_ready(entities={
            "SourceDocument": [ent("PARSING_FAILED"), ent("DONE")],
            "ClassifiedDocument": [ent("CLASSIFICATION_ERROR")],
        })
        probe = FakeProbe(**ready)
        results = ds.check_split_classify(probe, ds.load_inventory(probe),
                                          config_name="APP_CONFIG", collection_id="c1")
        failed = _by_status(results)[FAIL]
        self.assertIn("classify.collection.source_failed", failed)
        self.assertIn("classify.collection.classify_failed", failed)
        query = next(c for c in probe.calls if c[1] == ds.ENTITIES)[2]["odata_query"]
        self.assertIn("collection_id eq 'c1'", query)

    def test_documents_never_classified_fail_but_in_flight_ones_do_not(self):
        idle = _all_v1_ready(entities={"SourceDocument": [{"json_value": {"state": "DONE"}}]})
        probe = FakeProbe(**idle)
        results = ds.check_split_classify(probe, ds.load_inventory(probe),
                                          config_name="APP_CONFIG", collection_id="c1")
        self.assertIn("classify.collection.classified", _by_status(results)[FAIL])

        busy = _all_v1_ready(entities={"SourceDocument": [{"json_value": {"state": "PARSING"}}]})
        probe = FakeProbe(**busy)
        results = ds.check_split_classify(probe, ds.load_inventory(probe),
                                          config_name="APP_CONFIG", collection_id="c1")
        self.assertNotIn(FAIL, _by_status(results))

    def test_state_summary_reads_json_value_strings_too(self):
        s = ds.summarise_document_states(
            [{"json_value": '{"state": "split_failed"}'}, {"json_value": {"state": "DONE"}}], [])
        self.assertEqual(s["source_failed"], 1)
        self.assertEqual(s["source_states"], {"SPLIT_FAILED": 1, "DONE": 1})


def _macer_block(**over):
    block = {"invoke_macer": True, "jtbd_entity_id": "jtbd-1", "docs_ontology_id": "o-docs",
             "findings_ontology_id": "o-find", "mortgage_ontology_id": "o-mort",
             "guidelines_collection_id": "g-1", "timeout_duration": "PT2H",
             "model_name": "gpt-5.2"}
    block.update(over)
    return block


def _macer_ready(**extra):
    onto_ids = ["o-docs", "o-find", "o-mort", ds.MACER_INVOCATION_ONTOLOGY_ID]
    return dict(
        processes=list(ds.MACER_PROCESSES),
        tools=list(ds.MACER_TOOLS),
        ontologies=[{"id": i, "name": i} for i in onto_ids],
        config={"macer_config": {"default": _macer_block(),
                                 "by_loan_status": {"PROSPECT": _macer_block(invoke_macer=False)}}},
        gets={f"{ds.COLLECTIONS}/g-1": (200, {}), f"{ds.ENTITIES}/jtbd-1": (200, {})},
        **extra,
    )


class TestMacerReadiness(unittest.TestCase):
    def run_checks(self, ready, collection_id=None):
        probe = FakeProbe(**ready)
        return probe, ds.check_macer(probe, ds.load_inventory(probe),
                                     config_name="APP_CONFIG", collection_id=collection_id)

    def test_a_ready_instance_has_no_failures_and_never_claims_the_worker(self):
        probe, results = self.run_checks(_macer_ready())
        self.assertNotIn(FAIL, _by_status(results))
        worker = next(r for r in results if r.id == "macer.worker")
        self.assertEqual(worker.status, INFO)

    def test_checking_macer_only_ever_reads(self):
        probe, _ = self.run_checks(_macer_ready(), collection_id="c1")
        self.assertTrue(all(kind in ("get", "items") for kind, _, _ in probe.calls))
        self.assertFalse(any("invoke" in path or "invocation" in path
                             for _, path, _ in probe.calls))

    def test_missing_scheduler_process_and_gate_tool_fail(self):
        ready = _macer_ready()
        ready["processes"].remove("macerInvocationScheduler")
        ready["tools"].remove("check_macer_cooldown")
        _, results = self.run_checks(ready)
        failed = _by_status(results)[FAIL]
        self.assertIn("macer.process.macerinvocationscheduler", failed)
        self.assertIn("macer.tool.check_macer_cooldown", failed)

    def test_the_hard_coded_invocation_ontology_must_exist(self):
        ready = _macer_ready()
        ready["ontologies"] = [o for o in ready["ontologies"]
                               if o["id"] != ds.MACER_INVOCATION_ONTOLOGY_ID]
        _, results = self.run_checks(ready)
        self.assertTrue(any(r.id.startswith("macer.ontology.") and r.status == FAIL
                            for r in results))

    def test_config_ids_that_resolve_nowhere_fail(self):
        ready = _macer_ready()
        ready["ontologies"] = [o for o in ready["ontologies"] if o["id"] != "o-find"]
        ready["gets"] = {}  # guidelines collection and JTBD entity both 404
        _, results = self.run_checks(ready)
        failed = _by_status(results)[FAIL]
        self.assertIn("macer.config.findings_ontology_id.ontology.o_find", failed)
        self.assertIn("macer.config.guidelines_collection", failed)
        self.assertIn("macer.config.jtbd_entity", failed)

    def test_config_problems_name_every_missing_or_placeholder_key(self):
        problems = ds.macer_config_problems({
            "default": _macer_block(jtbd_entity_id="{{MACER_JTBD_ENTITY_ID}}"),
            "by_loan_status": {"PROCESSING": {"invoke_macer": True}},
        })
        self.assertIn("default.jtbd_entity_id = '{{MACER_JTBD_ENTITY_ID}}'", problems)
        self.assertIn("by_loan_status.PROCESSING.model_name = None", problems)
        self.assertEqual(ds.macer_config_problems(None), ["macer_config is missing"])

    def test_disabled_by_default_is_reported_not_failed(self):
        ready = _macer_ready()
        ready["config"]["macer_config"]["default"]["invoke_macer"] = False
        _, results = self.run_checks(ready)
        enabled = next(r for r in results if r.id == "macer.config.enabled")
        self.assertEqual(enabled.status, INFO)

    def test_past_invocations_on_the_target_loan_are_read_as_evidence(self):
        done = _macer_ready(entities={"MacerInvocationEvent": [
            {"json_value": {"macer_status": "DONE"}}]})
        _, results = self.run_checks(done, collection_id="c1")
        self.assertEqual(next(r for r in results if r.id == "macer.collection.events").status,
                         PASS)
        errored = _macer_ready(entities={"MacerInvocationEvent": [
            {"json_value": {"macer_status": "ERROR"}}]})
        _, results = self.run_checks(errored, collection_id="c1")
        self.assertEqual(next(r for r in results if r.id == "macer.collection.events").status,
                         FAIL)

    def test_new_groups_are_registered(self):
        self.assertIn("classify", GROUPS)
        self.assertIn("macer", GROUPS)


class TestClassifyMacerOptIn(unittest.TestCase):
    BASE = ["--gateway-url", "https://example-gw.jazzx.co", "--token", "t"]

    def test_off_by_default(self):
        args = ds.parse_args(self.BASE)
        self.assertNotIn("classify", args.groups)
        self.assertNotIn("macer", args.groups)
        self.assertIn("loan", args.groups)

    def test_the_flag_adds_both_to_every_default_group(self):
        args = ds.parse_args(self.BASE + ["--classify-macer"])
        self.assertEqual(args.groups, list(ds.DEFAULT_GROUPS) + ["classify", "macer"])

    def test_the_flag_with_a_narrowed_checks_list_runs_just_those(self):
        args = ds.parse_args(self.BASE + ["--checks", "loan", "--classify-macer"])
        self.assertEqual(args.groups, ["loan", "classify", "macer"])

    def test_naming_them_in_checks_without_the_flag_is_refused(self):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                ds.parse_args(self.BASE + ["--checks", "macer"])

    def test_one_of_them_with_the_flag_runs_both(self):
        args = ds.parse_args(self.BASE + ["--checks", "classify", "--classify-macer"])
        self.assertEqual(args.groups, ["classify", "macer"])


# --- argument parsing, progress, and report rendering ---

class Args(unittest.TestCase):
    def test_basic_options_parse(self):
        args = ds.parse_args([
            "--gateway-url", "https://x-gw.example", "--token", "t",
            "--checks", "platform,loan", "--max-processes", "5"])
        self.assertEqual(args.groups, ["platform", "loan"])
        self.assertEqual(args.max_processes, 5)

    def test_process_checks_are_switched_off(self):
        self.assertNotIn("process", ds.DEFAULT_GROUPS)
        with self.assertRaises(SystemExit):
            ds.parse_args(["--checks", "process"])

class Progress(unittest.TestCase):
    def test_non_tty_prints_first_every_tenth_and_last(self):
        out = io.StringIO()
        report = ds.process_progress(out)
        for i in range(1, 24):
            report(i, 23, f"P{i}")
        lines = out.getvalue().splitlines()
        self.assertEqual([ln.split()[1] for ln in lines], ["1/23", "10/23", "20/23", "23/23"])

    def test_process_walk_calls_progress(self):
        class FakeProbe:
            list_status = {}
            def items(self, path, params=None):
                return [{"key": "B"}, {"key": "A"}]
            def get(self, path, params=None):
                return 404, None
        seen = []
        ds.check_process_closure(FakeProbe(), max_processes=None,
                                 progress=lambda d, t, k: seen.append((d, t, k)))
        self.assertEqual([s[:2] for s in seen], [(1, 2), (2, 2)])


class MarkdownReport(unittest.TestCase):
    def test_report_lists_failures_without_configuration_audit(self):
        checks = [ds._bad("platform", "platform.x", "Service x answers", "HTTP 404", "Deploy x"),
                  ds._ok("loan", "loan.y", "Loan y | ok")]
        report = {
            "environment": {"label": "staging", "gateway_url": "https://a", "run_at": "now",
                            "groups_run": ["platform", "loan"], "token_expiry": "1h",
                            "image_digest": None, "read_calls": 3, "write_calls_attempted": 0,
                            "transport_errors": []},
            "checks": [ds.asdict(c) for c in checks],
            "summary": ds.summarise(checks),
        }
        md = ds.render_markdown(report)
        self.assertIn("## Failures (1)", md)
        self.assertIn("| platform | Service x answers | HTTP 404 | Deploy x |", md)
        self.assertIn("Loan y \\| ok", md)
        self.assertNotIn("APP_CONFIG", md)
        self.assertNotIn("## APP_CONFIG compared", md)

    def test_header_shows_token_lifetime_and_no_image_digest(self):
        report = {"environment": {"label": "s", "gateway_url": "https://a", "run_at": "now",
                                  "groups_run": [], "token_lifetime": "1h", "token_expiry": "x",
                                  "image_digest": "sha256:abc", "read_calls": 0,
                                  "write_calls_attempted": 0, "transport_errors": []},
                  "checks": [], "summary": ds.summarise([])}
        md = ds.render_markdown(report)
        self.assertIn("| Token lifetime | 1h |", md)
        self.assertNotIn("Image digest", md)

    def test_report_hides_internal_macer_default_config_path(self):
        check = ds._bad("macer", "macer.config.complete", "macer_config is complete",
                        "macer_config.default.jtbd_entity_id is missing", "Update the config")
        report = {
            "environment": {"label": "s", "gateway_url": "https://a", "run_at": "now",
                             "groups_run": ["macer"], "token_expiry": "1h",
                             "image_digest": None, "read_calls": 0, "write_calls_attempted": 0,
                             "transport_errors": []},
            "checks": [ds.asdict(check)],
            "summary": ds.summarise([check]),
        }
        md = ds.render_markdown(report)
        self.assertNotIn("macer_config.default", md)
        self.assertIn("MACER default setting jtbd_entity_id", md)

    def test_token_lifetime(self):
        import base64, json as _json
        body = base64.urlsafe_b64encode(_json.dumps({"iat": 1000, "exp": 4600}).encode()).decode().rstrip("=")
        self.assertEqual(ds.token_lifetime(f"h.{body}.s"), "1h")
        self.assertEqual(ds.token_lifetime("garbage"), "unknown")


if __name__ == "__main__":
    unittest.main()
