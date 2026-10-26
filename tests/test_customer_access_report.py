from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from customer_access_report import (
    build_sessions, keycloak_root, merge_visits, report_window, sessions_from_app, to_user, write_reports,
)

T0 = 1_760_000_000_000  # ms
MIN = 60_000


def ev(type_, t, sid="s1", uid="u1"):
    return {"type": type_, "time": T0 + t, "sessionId": sid, "userId": uid,
            "clientId": "jazzx-ui", "ipAddress": "10.0.0.1"}


class CustomerAccessReportTests(unittest.TestCase):
    def test_session_end_preference(self) -> None:
        events = [
            ev("LOGIN", 0, "a"), ev("REFRESH_TOKEN", 5 * MIN, "a"), ev("LOGOUT", 30 * MIN, "a"),
            ev("LOGIN", 0, "b"), ev("REFRESH_TOKEN", 10 * MIN, "b"), ev("REFRESH_TOKEN", 25 * MIN, "b"),
            ev("LOGIN", 0, "c"),
            ev("LOGIN", 0, "d"),
            ev("REFRESH_TOKEN", 0, "orphan"),
            {"type": "LOGIN_ERROR", "time": T0, "userId": "u1"},
        ]
        live = {"d": {"id": "d", "lastAccess": T0 + 45 * MIN}}
        sessions, failed = build_sessions(events, live)
        by_id = {s.session_id: s for s in sessions}
        self.assertEqual(set(by_id), {"a", "b", "c", "d"})
        self.assertEqual((by_id["a"].end_basis, by_id["a"].duration.seconds), ("logout", 1800))
        self.assertEqual((by_id["b"].end_basis, by_id["b"].duration.seconds), ("last_activity", 1500))
        self.assertEqual((by_id["c"].end_basis, by_id["c"].duration), ("login_only", None))
        self.assertEqual((by_id["d"].end_basis, by_id["d"].duration.seconds), ("active", 2700))
        self.assertEqual(failed, {"u1": 1})

    def test_keycloak_root_uses_gateway_host(self) -> None:
        self.assertEqual(keycloak_root("https://poc.jazzx.co/"), "https://poc-gw.jazzx.co/keycloak")
        self.assertEqual(keycloak_root("https://poc-gw.jazzx.co"), "https://poc-gw.jazzx.co/keycloak")

    def test_app_rows_merge_into_visits(self) -> None:
        from datetime import timedelta
        rows = [
            {"id": "1", "user_id": "u1", "logon_at": "2026-10-08T10:00:00Z", "updated_at": "2026-10-08T10:20:00Z"},
            {"id": "2", "user_id": "u1", "logon_at": "2026-10-08T10:30:00Z", "logoff_at": "2026-10-08T11:00:00Z"},
            {"id": "3", "user_id": "u1", "logon_at": "2026-10-09T09:00:00Z"},
            {"id": "4", "user_id": "u2", "logon_at": "2026-10-08T10:05:00", "updated_at": "2026-10-08T10:05:00"},
        ]
        visits = merge_visits(sessions_from_app(rows), timedelta(minutes=30))
        u1 = [v for v in visits if v.user_id == "u1"]
        self.assertEqual(len(u1), 2)
        self.assertEqual((u1[0].duration.seconds, u1[0].end_basis, u1[0].parts), (3600, "logout", 2))
        self.assertEqual((u1[1].duration, u1[1].end_basis), (None, "login_only"))
        u2 = [v for v in visits if v.user_id == "u2"][0]
        self.assertEqual(u2.end_basis, "login_only")

    def test_valid_ip_rejects_urls(self) -> None:
        from customer_access_report import valid_ip
        self.assertTrue(valid_ip("10.0.0.1"))
        self.assertFalse(valid_ip("https://poc-gw.jazzx.co/assistant"))

    def test_reports_exclude_internal_users(self) -> None:
        users = {u.id: u for u in map(to_user, [
            {"id": "u1", "username": "a@stockton.com", "email": "a@stockton.com", "firstName": "Ann"},
            {"id": "u2", "username": "dev@jazzx.ai", "email": "dev@jazzx.ai"},
        ])}
        sessions, _ = build_sessions([ev("LOGIN", 0, "a", "u1"), ev("LOGOUT", 20 * MIN, "a", "u1"),
                                           ev("LOGIN", 0, "b", "u2")])
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_reports(
                out_dir=Path(tmp), label="poc", tz=ZoneInfo("UTC"), users=users, sessions=sessions,
                internal=("jazzx.ai",), include_internal=False, markdown=True,
                meta={"base_url": "https://x", "realm": "r",
                      "source": "test", "earliest_event": None},
            )
            csv_text, md = paths[0].read_text(), paths[1].read_text()
            self.assertIn("a@stockton.com", md)
            self.assertIn("20m", md)
            self.assertIn("10.0.0.1", md)
            self.assertNotIn("jazzx.ai", md)
            self.assertIn("a@stockton.com", csv_text)
            self.assertNotIn("jazzx.ai", csv_text)

    def test_default_is_login_history_csv_only_from_the_beginning_to_today(self) -> None:
        from datetime import datetime, timezone
        users = {"u1": to_user({"id": "u1", "username": "a@stockton.com", "email": "a@stockton.com"})}
        rows = [{"id": str(i), "user_id": "u1", "logon_at": day} for i, day in enumerate(
            ["2026-09-01T10:00:00Z", "2026-10-08T10:00:00Z", "2026-10-21T10:00:00Z"])]
        sessions = merge_visits(sessions_from_app(rows), __import__("datetime").timedelta(minutes=30))
        tz = ZoneInfo("UTC")
        since, until = report_window(None, "2026-10-22", tz)
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_reports(out_dir=Path(tmp), label="poc", tz=tz, users=users, sessions=sessions,
                                  internal=("jazzx.ai",), include_internal=False, since=since, until=until,
                                  meta={"base_url": "https://x", "realm": "r", "source": "t"})
            self.assertEqual([p.suffix for p in paths], [".csv"])
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["poc-login-history-20260901-to-20261022.csv"])
            lines = paths[0].read_text().splitlines()
            self.assertEqual(len(lines), 4)                      # header + every login since the beginning
            self.assertTrue(lines[1].startswith("a@stockton.com,,2026-10-21"))
        self.assertIsNone(since)
        self.assertEqual(until, datetime(2026, 10, 22, 23, 59, 59, 999999, tzinfo=tz))

    def test_since_until_narrow_the_range(self) -> None:
        users = {"u1": to_user({"id": "u1", "username": "a@stockton.com", "email": "a@stockton.com"})}
        rows = [{"id": str(i), "user_id": "u1", "logon_at": day} for i, day in enumerate(
            ["2026-10-07T23:00:00Z", "2026-10-08T10:00:00Z", "2026-10-15T23:30:00Z", "2026-10-16T00:30:00Z"])]
        sessions = merge_visits(sessions_from_app(rows), __import__("datetime").timedelta(minutes=30))
        tz = ZoneInfo("UTC")
        since, until = report_window("2026-10-08", "2026-10-15", tz)
        with tempfile.TemporaryDirectory() as tmp:
            (path,) = write_reports(out_dir=Path(tmp), label="poc", tz=tz, users=users, sessions=sessions,
                                    internal=(), include_internal=False, since=since, until=until,
                                    meta={"base_url": "https://x", "realm": "r", "source": "t"})
            self.assertEqual(path.name, "poc-login-history-20261008-to-20261015.csv")
            self.assertEqual(len(path.read_text().splitlines()), 3)   # 10-08 and 10-15 only

    def test_bad_dates_are_refused(self) -> None:
        tz = ZoneInfo("UTC")
        with self.assertRaises(ValueError):
            report_window("08-10-2026", None, tz)
        with self.assertRaises(ValueError):
            report_window("2026-10-20", "2026-10-10", tz)


    def test_all_login_records_are_read_even_when_the_server_caps_page_size(self) -> None:
        from customer_access_report import AppSessions
        records = [{"id": str(i), "user_id": "u1", "logon_at": "2026-10-08T10:00:00Z"} for i in range(130)]

        class CappedApi:                     # server returns at most 50 per page, whatever limit says
            def request_json(self, method, path, params=None):
                return records[params["offset"]:params["offset"] + min(params["limit"], 50)]

        class IgnoresOffset:                 # server ignores offset entirely
            def request_json(self, method, path, params=None):
                return records[:50]

        app = AppSessions.__new__(AppSessions)
        app.client = CappedApi()
        self.assertEqual(len(app.all()), 130)
        app.client = IgnoresOffset()
        self.assertEqual(len(app.all()), 50)      # stops instead of looping forever


if __name__ == "__main__":
    unittest.main()
