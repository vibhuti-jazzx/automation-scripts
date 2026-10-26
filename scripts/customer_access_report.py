#!/usr/bin/env python3
"""Report who logged in to a JazzX instance, when, and for how long.

Primary source -- the platform's own login records (Assistant service):

  * GET {gateway}/assistant/api/v1/user_sessions   one row per UI login
    (logon_at, logoff_at, ip_address, user_agent, user_id)

The UI writes a row each time it gets a token, so one visit can leave several
rows. Rows for the same user less than --gap-minutes apart are merged into one
visit; the visit runs from the first logon to the last logoff/update.

Secondary source and user lookup -- the Keycloak Admin API of the realm:

  * login events   GET /admin/realms/{realm}/events   (LOGIN, LOGOUT, LOGIN_ERROR,
                   CODE_TO_TOKEN, REFRESH_TOKEN)
  * users          GET /admin/realms/{realm}/users     (email / name lookup)
  * live sessions  GET /admin/realms/{realm}/users/{id}/sessions

Events are grouped into sessions by Keycloak session id. A session starts at its
LOGIN event and ends at, in order of preference:

  logout        an explicit LOGOUT event
  active        the session is still open; end = its last access time
  last_activity the last token refresh / code exchange seen for that session
  login_only    nothing after the login was recorded; duration is unknown

Token refreshes happen while the UI is open, so `last_activity` is a close lower
bound on time spent, not an exact figure. Keycloak only keeps events if "Save
events" is enabled for the realm and only for its configured expiration; the
report prints both so a short history is not mistaken for low usage.

Every non-JazzX user is treated as a customer user; internal domains (jazzx.*)
are left out unless --include-internal is given.

Output: the login history as one CSV, covering every recorded login from the
beginning through the end of today (in --timezone). Narrow it with --since /
--until (YYYY-MM-DD). --markdown also writes the Markdown summary.

Example:

    export JAZZX_TOKEN="<your token from the instance UI>"      # reads user_sessions
    export KEYCLOAK_ADMIN_PASSWORD="<keycloak admin password>"  # maps user ids to emails
    python3 scripts/customer_access_report.py \\
        --base-url https://poc.jazzx.co --admin-user admin
"""

from __future__ import annotations

import argparse
import base64
import csv
import ipaddress
import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jazzx_api import ApiError, JazzXClient, require_value  # noqa: E402


EVENT_TYPES = ("LOGIN", "LOGOUT", "LOGIN_ERROR", "CODE_TO_TOKEN", "REFRESH_TOKEN")
ACTIVITY_TYPES = {"CODE_TO_TOKEN", "REFRESH_TOKEN"}
DEFAULT_INTERNAL_DOMAINS = ("jazzx.co", "jazzx.ai", "jazzx.dev", "jazzx.com")
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1] / "customer-access-reports"
PAGE_SIZE = 500


# --------------------------------------------------------------------------- data


@dataclass
class User:
    id: str
    username: str
    email: str
    name: str
    created: datetime | None
    enabled: bool

    @property
    def domain(self) -> str:
        addr = self.email or self.username
        return addr.rsplit("@", 1)[1].lower() if "@" in addr else ""


@dataclass
class Session:
    session_id: str
    user_id: str
    client: str
    ip: str
    start: datetime
    end: datetime | None = None
    end_basis: str = "login_only"
    activity: list[datetime] = field(default_factory=list)
    parts: int = 1

    @property
    def duration(self) -> timedelta | None:
        if self.end is None or self.end < self.start:
            return None
        return self.end - self.start


# ----------------------------------------------------------------------- helpers


def decode_jwt_claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return {}


def ms_to_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def iso_to_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address((value or "").split(",")[0].strip())
    except ValueError:
        return False
    return True


def fmt_duration(value: timedelta | None) -> str:
    if value is None:
        return "unknown"
    minutes = int(value.total_seconds() // 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m" if minutes else "<1m"


def fmt_dt(value: datetime | None, tz: ZoneInfo) -> str:
    return value.astimezone(tz).strftime("%Y-%m-%d %H:%M") if value else ""


def report_window(since: str | None, until: str | None, tz: ZoneInfo) -> tuple[datetime | None, datetime]:
    """--since/--until days (YYYY-MM-DD, in `tz`) -> [start of since, end of until].
    No --since means from the first recorded login; no --until means through today."""
    def day(value: str, flag: str) -> datetime:
        try:
            return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=tz)
        except ValueError:
            raise ValueError(f"{flag} must be YYYY-MM-DD, got {value!r}") from None
    start = day(since, "--since") if since else None
    last_day = day(until, "--until") if until else datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    end = last_day + timedelta(days=1) - timedelta(microseconds=1)
    if start and start > end:
        raise ValueError("--since is after --until")
    return start, end


def keycloak_root(base_url: str) -> str:
    """UI host -> gateway Keycloak root. poc.jazzx.co serves only the static UI;
    the admin API is behind the gateway at poc-gw.jazzx.co/keycloak."""
    scheme, _, rest = base_url.rstrip("/").partition("://")
    host, _, _ = rest.partition("/")
    first, dot, domain = host.partition(".")
    if dot and not first.endswith("-gw"):
        host = f"{first}-gw.{domain}"
    return f"{scheme}://{host}/keycloak"


def is_internal(domain: str, internal: tuple[str, ...]) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in internal)


# ------------------------------------------------------------------ Keycloak API


class AdminLogin:
    """Password login for a Keycloak admin. Admin tokens from the master realm
    live about a minute, so a fresh one is fetched shortly before each expires."""

    def __init__(self, kc_url: str, username: str, password: str, realm: str, client_id: str,
                 timeout: float) -> None:
        self.url = f"{kc_url}/realms/{realm}/protocol/openid-connect/token"
        self.form = {"grant_type": "password", "client_id": client_id,
                     "username": username, "password": password}
        self.timeout = timeout
        self.token = ""
        self.expires_at = 0.0

    def get(self) -> str:
        if time.time() < self.expires_at - 15:
            return self.token
        import requests
        try:
            resp = requests.post(self.url, data=self.form, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ApiError(f"Keycloak login failed: {exc}") from exc
        if resp.status_code != 200:
            raise ApiError(f"Keycloak login returned HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        self.token = data["access_token"]
        self.expires_at = time.time() + int(data.get("expires_in", 60))
        return self.token


class KeycloakAdmin:
    def __init__(self, client: JazzXClient, realm: str, login: AdminLogin | None = None) -> None:
        self.client = client
        self.realm = realm
        self.login = login

    def _get(self, path: str, params: Any = None) -> Any:
        if self.login:
            self.client.session.headers["Authorization"] = f"Bearer {self.login.get()}"
        return self.client.request_json("GET", f"admin/realms/{self.realm}/{path}", params=params)

    def _paged(self, path: str, params: list[tuple[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        first = 0
        while True:
            page = self._get(path, params + [("first", first), ("max", PAGE_SIZE)])
            if not isinstance(page, list):
                raise ApiError(f"{path}: expected a list, got {type(page).__name__}")
            out.extend(page)
            if len(page) < PAGE_SIZE:
                return out
            first += len(page)

    def token_lifespan(self) -> int | None:
        """Access token lifetime in seconds, from the realm settings."""
        try:
            if self.login:
                self.client.session.headers["Authorization"] = f"Bearer {self.login.get()}"
            realm = self.client.request_json("GET", f"admin/realms/{self.realm}")
            return int(realm.get("accessTokenLifespan") or 0) or None
        except (ApiError, ValueError, TypeError):
            return None

    def events_config(self) -> dict[str, Any] | None:
        try:
            return self._get("events/config")
        except ApiError:
            return None  # needs manage-events; informational only

    def events(self) -> list[dict[str, Any]]:
        return self._paged("events", [("type", t) for t in EVENT_TYPES])

    def users(self) -> list[dict[str, Any]]:
        return self._paged("users", [("briefRepresentation", "false")])

    def user_sessions(self, user_id: str) -> list[dict[str, Any]]:
        return self._get(f"users/{user_id}/sessions") or []


# --------------------------------------------------------------------- analysis


def to_user(raw: dict[str, Any]) -> User:
    name = " ".join(p for p in (raw.get("firstName"), raw.get("lastName")) if p)
    return User(
        id=raw.get("id", ""),
        username=raw.get("username", ""),
        email=(raw.get("email") or "").lower(),
        name=name,
        created=ms_to_dt(raw.get("createdTimestamp")),
        enabled=bool(raw.get("enabled", True)),
    )


def build_sessions(
    events: list[dict[str, Any]],
    live_sessions: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[Session], dict[str, int]]:
    """Group events into sessions; also count failed logins per user id."""
    live_sessions = live_sessions or {}
    sessions: dict[str, Session] = {}
    pending: dict[str, list[dict[str, Any]]] = defaultdict(list)
    failed: dict[str, int] = defaultdict(int)

    for ev in sorted(events, key=lambda e: e.get("time", 0)):
        etype = ev.get("type")
        sid = ev.get("sessionId") or ""
        when = ms_to_dt(ev.get("time"))
        if when is None:
            continue
        if etype == "LOGIN_ERROR":
            failed[ev.get("userId") or ""] += 1
            continue
        if not sid:
            continue
        if etype == "LOGIN":
            if sid not in sessions:
                sessions[sid] = Session(
                    session_id=sid,
                    user_id=ev.get("userId") or "",
                    client=ev.get("clientId") or "",
                    ip=ev.get("ipAddress") or "",
                    start=when,
                )
            continue
        pending[sid].append({"type": etype, "time": when})

    for sid, items in pending.items():
        session = sessions.get(sid)
        if session is None:
            continue  # login is older than Keycloak's retained events
        for item in items:
            if item["type"] == "LOGOUT":
                session.end, session.end_basis = item["time"], "logout"
            elif item["type"] in ACTIVITY_TYPES:
                session.activity.append(item["time"])

    for session in sessions.values():
        live = live_sessions.get(session.session_id)
        if session.end_basis == "logout":
            continue
        if live:
            last = ms_to_dt(live.get("lastAccess"))
            if last:
                session.end, session.end_basis = last, "active"
                continue
        if session.activity:
            session.end, session.end_basis = max(session.activity), "last_activity"

    return sorted(sessions.values(), key=lambda s: s.start), dict(failed)


def sessions_from_app(rows: list[dict[str, Any]]) -> list[Session]:
    """Assistant user_session rows -> sessions."""
    out = []
    for row in rows:
        start = iso_to_dt(row.get("logon_at")) or iso_to_dt(row.get("created_at"))
        if start is None:
            continue
        s = Session(session_id=str(row.get("id", "")), user_id=str(row.get("user_id", "")),
                    client=row.get("user_agent") or "", ip=row.get("ip_address") or "", start=start)
        logoff = iso_to_dt(row.get("logoff_at"))
        updated = iso_to_dt(row.get("updated_at"))
        if logoff and logoff >= start:
            s.end, s.end_basis = logoff, "logout"
        elif updated and updated > start:
            s.end, s.end_basis = updated, "last_activity"
        out.append(s)
    return out


def merge_visits(sessions: list[Session], gap: timedelta) -> list[Session]:
    """Merge one user's sessions that overlap or are less than `gap` apart."""
    by_user: dict[str, list[Session]] = defaultdict(list)
    for s in sessions:
        by_user[s.user_id].append(s)
    visits: list[Session] = []
    for items in by_user.values():
        items.sort(key=lambda s: s.start)
        cur: Session | None = None
        for s in items:
            last = s.end or s.start
            if cur is not None and s.start - (cur.end or cur.start) <= gap:
                if last >= (cur.end or cur.start):
                    cur.end = last
                    cur.end_basis = s.end_basis if s.end else "last_activity"
                cur.parts += 1
                continue
            cur = Session(session_id=s.session_id, user_id=s.user_id, client=s.client, ip=s.ip,
                          start=s.start, end=s.end, end_basis=s.end_basis)
            visits.append(cur)
    return sorted(visits, key=lambda s: s.start)


class AppSessions:
    """Reads the Assistant service's user_session table through the gateway."""

    def __init__(self, gateway_url: str, token: str, timeout: float) -> None:
        self.client = JazzXClient(gateway_url=gateway_url, token=token, timeout=timeout)

    def all(self, page: int = 100) -> list[dict[str, Any]]:
        """Every login record, from the first one on. Reads until the API returns an empty
        page -- a short page is NOT treated as the last, because the server may cap the page
        size below `page`. Stops if a page repeats records (a server ignoring `offset`)."""
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        offset = 0
        while True:
            batch = self.client.request_json("GET", "assistant/api/v1/user_sessions",
                                             params={"limit": page, "offset": offset})
            if not isinstance(batch, list):
                raise ApiError(f"user_sessions: expected a list, got {type(batch).__name__}")
            new = [r for r in batch if str(r.get("id", id(r))) not in seen]
            if not new:
                return rows
            seen.update(str(r.get("id", id(r))) for r in new)
            rows.extend(new)
            offset += len(batch)


# ----------------------------------------------------------------------- output


def write_reports(
    *,
    out_dir: Path,
    label: str,
    tz: ZoneInfo,
    users: dict[str, User],
    sessions: list[Session],
    internal: tuple[str, ...],
    include_internal: bool,
    meta: dict[str, Any],
    since: datetime | None = None,
    until: datetime | None = None,
    markdown: bool = False,
) -> list[Path]:
    """One row per login by a customer user, newest first, between `since` (default: the
    first recorded login) and `until` (default: now). Always a CSV of the login history;
    the Markdown summary only when `markdown` is set."""
    out_dir.mkdir(parents=True, exist_ok=True)
    until = until or datetime.now(timezone.utc)

    def keep(user: User | None) -> bool:
        if user is None:
            return include_internal
        return include_internal or not is_internal(user.domain, internal)

    rows = []
    for s in sorted(sessions, key=lambda s: s.start, reverse=True):
        u = users.get(s.user_id)
        if not keep(u):
            continue
        if (since and s.start < since) or s.start > until:
            continue
        rows.append({
            "email": (u.email or u.username) if u else s.user_id,
            "name": u.name if u else "",
            "session": s,
        })

    # The UI stores the gateway URL in ip_address, so only show values that are IPs.
    show_ip = any(valid_ip(r["session"].ip) for r in rows)

    def ip_of(s: Session) -> str:
        return s.ip if valid_ip(s.ip) else ""

    def time_of(s: Session) -> str:
        if s.end_basis == "logout":
            return fmt_duration(s.duration)
        if s.end_basis == "active":
            return f"{fmt_duration(s.duration)} (still active)"
        return f"at least {fmt_duration(s.duration)}" if s.duration else "unknown"

    first = since or (rows[-1]["session"].start if rows else until)
    base = out_dir / (f"{label}-login-history-{first.astimezone(tz):%Y%m%d}"
                      f"-to-{until.astimezone(tz):%Y%m%d}")

    csv_path = base.with_suffix(".csv")
    with csv_path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["email", "name", f"login ({tz.key})", f"last activity ({tz.key})", "logins_in_visit",
                    "minutes_at_least", "time", "end_basis"] + (["ip"] if show_ip else []))
        for r in rows:
            s, d = r["session"], r["session"].duration
            w.writerow([r["email"], r["name"], fmt_dt(s.start, tz), fmt_dt(s.end, tz), s.parts,
                        round(d.total_seconds() / 60, 1) if d else "", time_of(s), s.end_basis]
                       + ([ip_of(s)] if show_ip else []))

    if not markdown:
        return [csv_path]

    md_path = base.with_suffix(".md")
    lines = [
        f"# Customer access report: {label}",
        "",
        f"- Instance: {meta['base_url']} (realm `{meta['realm']}`)",
        f"- Generated: {fmt_dt(datetime.now(timezone.utc), tz)} {tz.key}",
        f"- Source: {meta['source']}",
        f"- Period: {fmt_dt(first, tz)} to {fmt_dt(until, tz)} {tz.key}",
        f"- Customer visits: {len(rows)} by {len({r['email'] for r in rows})} users"
        + (f", from {fmt_dt(rows[-1]['session'].start, tz)} to {fmt_dt(rows[0]['session'].start, tz)}"
           if rows else ""),
        "",
    ]
    live = meta.get("live")
    if live is not None:
        lines += ["## Logged in now", ""]
        if live:
            lines += [f"| User | Logged in since ({tz.key}) | Last active | Time so far |", "|---|---|---|---|"]
            for ls in sorted(live, key=lambda x: x.get("lastAccess", 0), reverse=True):
                u = users.get(ls.get("userId", ""))
                who = (f"{u.name} ({u.email})" if u.name else u.email) if u else ls.get("username", "")
                start, last = ms_to_dt(ls.get("start")), ms_to_dt(ls.get("lastAccess"))
                span = last - start if start and last else None
                lines.append(f"| {who} | {fmt_dt(start, tz)} | {fmt_dt(last, tz)} | {fmt_duration(span)} |")
        else:
            lines.append("No customer user is logged in right now.")
        lines += ["", "## Login history", ""]
    lines += [
        f"| User | Login ({tz.key}) | Last activity | Logins in visit | Time |"
        + (" IP |" if show_ip else ""),
        "|---|---|---|---|---|" + ("---|" if show_ip else ""),
    ]
    for r in rows:
        s = r["session"]
        who = f"{r['name']} ({r['email']})" if r["name"] else r["email"]
        lines.append(f"| {who} | {fmt_dt(s.start, tz)} | {fmt_dt(s.end, tz) or '-'} | {s.parts} "
                     f"| {time_of(s)} |" + (f" {ip_of(s) or '-'} |" if show_ip else ""))
    lines += [
        "",
        "**Login times are exact. Times spent are minimums.** Users close the tab instead of "
        "logging out, so no visit has a recorded end. The app writes a record only when it "
        f"gets a new login token ({meta.get('token_note', 'tokens last up to an hour')}), so a "
        "visit with one login could have lasted anywhere up to the token lifetime. Logins "
        "less than the merge gap apart are shown as one visit.",
        "",
    ]
    md_path.write_text("\n".join(lines))
    return [csv_path, md_path]


# ------------------------------------------------------------------------- main


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default=os.getenv("JAZZX_BASE_URL"),
                   help="Instance URL, e.g. https://poc.jazzx.co (Keycloak is read from its -gw gateway host)")
    p.add_argument("--keycloak-url", default=os.getenv("JAZZX_KEYCLOAK_URL"),
                   help="Keycloak root if it is not <base-url>/keycloak")
    p.add_argument("--gateway-url", default=os.getenv("JAZZX_GATEWAY_URL"),
                   help="Gateway for the Assistant API (default: Keycloak URL without /keycloak)")
    p.add_argument("--gap-minutes", type=float, default=30.0,
                   help="Merge a user's logins less than this many minutes apart into one visit")
    p.add_argument("--realm", default=os.getenv("JAZZX_REALM", "jazzx-application"))
    p.add_argument("--token", default=os.getenv("JAZZX_TOKEN"),
                   help="Your UI bearer token, used to read the Assistant login records "
                        "(prefer the JAZZX_TOKEN env var to keep it out of shell history)")
    p.add_argument("--admin-user", default=os.getenv("KEYCLOAK_ADMIN_USER"),
                   help="Log in as this Keycloak admin instead of passing --token "
                        "(password from KEYCLOAK_ADMIN_PASSWORD)")
    p.add_argument("--admin-realm", default=os.getenv("KEYCLOAK_ADMIN_REALM", "master"),
                   help="Realm the admin user belongs to (default: master)")
    p.add_argument("--admin-client-id", default="admin-cli")
    p.add_argument("--label", help="Name used in report file names (default: instance host)")
    p.add_argument("--internal-domain", action="append", default=[],
                   help=f"Extra internal domain to exclude (always excluded: {', '.join(DEFAULT_INTERNAL_DOMAINS)})")
    p.add_argument("--include-internal", action="store_true", help="Keep JazzX/internal users in the report")
    p.add_argument("--timezone", default="America/Los_Angeles", help="Timezone for displayed times")
    p.add_argument("--skip-live-sessions", action="store_true",
                   help="Do not query each user's open sessions (faster on large realms)")
    p.add_argument("--since", help="First day to include, YYYY-MM-DD (default: the first recorded login)")
    p.add_argument("--until", help="Last day to include, YYYY-MM-DD (default: today), in --timezone")
    p.add_argument("--markdown", action="store_true",
                   help="Also write the Markdown summary (default: the login history CSV only)")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--raw", action="store_true", help="Also save the raw events and users JSON")
    p.add_argument("--timeout", type=float, default=60.0)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    login: AdminLogin | None = None
    try:
        if not (args.keycloak_url or args.base_url):
            raise ValueError("--base-url (or --keycloak-url) is required")
        kc_url = (args.keycloak_url or keycloak_root(args.base_url)).rstrip("/")
        gw_url = (args.gateway_url or kc_url.removesuffix("/keycloak")).rstrip("/")
        tz = ZoneInfo(args.timezone)
        since, until = report_window(args.since, args.until, tz)
        user_token = args.token.removeprefix("Bearer ").strip() if args.token else ""
        if args.admin_user:
            password = require_value(None, name="admin password", env_name="KEYCLOAK_ADMIN_PASSWORD")
            login = AdminLogin(kc_url, args.admin_user, password, args.admin_realm,
                               args.admin_client_id, args.timeout)
            kc_token = login.get()
            print(f"logged in to Keycloak as {args.admin_user} ({args.admin_realm} realm)", file=sys.stderr)
        else:
            kc_token = require_value(user_token, name="--token or --admin-user", env_name="JAZZX_TOKEN")
    except (ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except ApiError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    claims = decode_jwt_claims(user_token) if user_token else {}
    if claims:
        exp = ms_to_dt(claims.get("exp", 0) * 1000)
        print(f"token: {claims.get('preferred_username') or claims.get('sub')} "
              f"(expires {fmt_dt(exp, tz)} {tz.key})", file=sys.stderr)
        if exp and exp < datetime.now(timezone.utc):
            print("ERROR: the token has expired; copy a fresh one and run again.", file=sys.stderr)
            return 2
    elif not user_token:
        print("warning: no JAZZX_TOKEN, so the platform login records are skipped and only "
              "Keycloak events are used", file=sys.stderr)

    label = args.label or (args.base_url or kc_url).split("//", 1)[-1].split("/", 1)[0].split(".", 1)[0]
    internal = tuple(DEFAULT_INTERNAL_DOMAINS) + tuple(d.lower() for d in args.internal_domain)

    # Keycloak: users (always), events and live sessions (if the realm keeps them)
    print(f"keycloak: {kc_url} realm {args.realm}", file=sys.stderr)
    kc = KeycloakAdmin(JazzXClient(gateway_url=kc_url, token=kc_token, timeout=args.timeout),
                       args.realm, login)
    try:
        raw_users = kc.users()
        print(f"  {len(raw_users)} users", file=sys.stderr)
        raw_events = kc.events()
        cfg = kc.events_config()
    except ApiError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if "HTTP 401" in str(exc):
            print("The token was rejected; it may have expired. Copy a fresh one.", file=sys.stderr)
        elif "HTTP 403" in str(exc):
            print("Reading Keycloak users needs admin access: pass --admin-user admin and set "
                  "KEYCLOAK_ADMIN_PASSWORD.", file=sys.stderr)
        return 1
    users = {u.id: u for u in map(to_user, raw_users)}
    events_on = bool(cfg and cfg.get("eventsEnabled"))
    print(f"  {len(raw_events)} login events (event saving {'on' if events_on else 'OFF'})", file=sys.stderr)

    # Platform login records
    app_rows: list[dict[str, Any]] = []
    if user_token:
        print(f"platform: {gw_url}/assistant/api/v1/user_sessions", file=sys.stderr)
        try:
            app_rows = AppSessions(gw_url, user_token, args.timeout).all()
            print(f"  {len(app_rows)} login records", file=sys.stderr)
        except ApiError as exc:
            print(f"  warning: could not read platform login records: {exc}", file=sys.stderr)

    # Who is logged in right now: Keycloak's open sessions for customer users.
    live: dict[str, dict[str, Any]] = {}
    if not args.skip_live_sessions:
        for u in users.values():
            if args.include_internal or not is_internal(u.domain, internal):
                try:
                    for ls in kc.user_sessions(u.id):
                        live[ls.get("id", "")] = {**ls, "userId": u.id}
                except ApiError as exc:
                    print(f"  warning: open sessions for {u.email or u.username}: {exc}", file=sys.stderr)
        print(f"  {len(live)} customer sessions open now", file=sys.stderr)

    gap = timedelta(minutes=args.gap_minutes)
    if app_rows:
        sessions = merge_visits(sessions_from_app(app_rows), gap)
        source = (f"JazzX platform login records (assistant user_sessions), "
                  f"logins under {args.gap_minutes:g} min apart merged into one visit")
        earliest = min((s.start for s in sessions), default=None)
    else:
        sessions, _ = build_sessions(raw_events, live)
        if cfg is None:
            source = "Keycloak events (retention unknown)"
        elif not events_on:
            source = "Keycloak events -- event saving is DISABLED for this realm, so no history exists"
        else:
            days = int(cfg.get("eventsExpiration") or 0) // 86400
            source = f"Keycloak events (kept {f'{days} days' if days else 'forever'})"
        earliest = ms_to_dt(min((e.get("time", 0) for e in raw_events), default=None)) if raw_events else None

    lifespan = kc.token_lifespan()
    token_note = (f"on {label} a token lasts {lifespan // 60} minutes" if lifespan
                  else "tokens last up to an hour")

    if args.raw:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        (args.output_dir / f"{label}-{stamp}-raw.json").write_text(
            json.dumps({"user_sessions": app_rows, "events": raw_events, "users": raw_users}, indent=2))

    paths = write_reports(
        out_dir=args.output_dir, label=label, tz=tz, users=users, sessions=sessions,
        internal=internal, include_internal=args.include_internal,
        since=since, until=until, markdown=args.markdown,
        meta={"base_url": args.base_url or kc_url, "realm": args.realm,
              "source": source, "earliest_event": earliest, "token_note": token_note,
              "live": None if args.skip_live_sessions else list(live.values())},
    )
    with paths[0].open(newline="") as fh:
        logins = list(csv.DictReader(fh))
    period = f"{since.astimezone(tz):%Y-%m-%d}" if since else "the first recorded login"
    print(f"\nsource: {source}", file=sys.stderr)
    print(f"login history: {len(logins)} customer visits by {len({r['email'] for r in logins})} users, "
          f"{period} to {until.astimezone(tz):%Y-%m-%d} ({tz.key})", file=sys.stderr)
    print("report:" if len(paths) == 1 else "reports:", file=sys.stderr)
    for path in paths:
        print(f"  {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
