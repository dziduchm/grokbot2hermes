#!/usr/bin/env python3
"""Read-only dashboard presentation layer for grokbot2hermes g2h_ui.py.

Pure display code: renders the observation snapshot to strict HTML and
serves it over a loopback-only GET server. It contains no gateway client,
no key handling, and no file writes; g2h.py owns observation and state,
this module owns presentation. Split out of g2h.py per the size rule.

No JavaScript, no cookies, no tokens, no forms: CSP default-src 'none';
style-src 'self', X-Content-Type-Options: nosniff, GET-only, Host
allowlist limited to the loopback host names for the bound port.
"""

import html as _html
import json
import socket
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CSP_POLICY = "default-src 'none'; style-src 'self'"

STYLE_CSS = """\
body { font: 14px/1.5 -apple-system, sans-serif; margin: 2rem auto;
  max-width: 72rem; padding: 0 1rem; color: #1a1a1a; }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; }
th, td { border: 1px solid #ccc; padding: .4rem .6rem; text-align: left;
  vertical-align: top; }
th { background: #f5f5f5; }
code { font-family: ui-monospace, monospace; font-size: .9em;
  word-break: break-all; }
pre { white-space: pre-wrap; word-break: break-word; background: #f8f8f8;
  border: 1px solid #e0e0e0; padding: .5rem; max-width: 60rem; }
.banner { padding: .6rem .8rem; margin: .6rem 0; border: 1px solid;
  font-weight: 600; }
.banner-error { color: #8a1f1f; background: #fdecec;
  border-color: #e0a0a0; }
.banner-warn { color: #7a5200; background: #fff7e0; border-color: #e6c96a; }
.statusline span { margin-right: 1.2rem; }
.blockers { margin: .2rem 0; padding-left: 1.1rem; }
.decision { margin: .3rem 0; }
.expiry, .note { font-size: .85rem; color: #555; }
.limits summary { cursor: pointer; font-weight: 600; }
footer { color: #666; font-size: .85rem; margin-top: 1.5rem; }
"""


def _render_details(row: dict, esc) -> str:
    chunk = ["<details><summary>expanded details</summary>"]
    output = row.get("output")
    chunk.append("<pre>%s</pre>" % esc(output)
                 if output else "<pre>(no output yet)</pre>")
    chunk.append("<dl>")
    pairs = (
        ("receipt", row.get("receipt")),
        ("run id", row.get("run_id")
         or "(none yet; dispatch may have failed pre-create)"),
        ("idempotency key", "present" if row.get("idem_present")
         else "absent"),
        ("created at", row.get("created_at")),
        ("updated at", row.get("updated_at")),
        ("last ok at", row.get("last_ok_at") or "never"),
        ("last error", row.get("last_error") or "none"),
    )
    for label, value in pairs:
        chunk.append("<dt>%s</dt><dd>%s</dd>" % (label, esc(value)))
    chunk.append("</dl>")
    usage = row.get("usage")
    if usage:
        try:
            usage_text = json.dumps(usage, sort_keys=True)
        except (TypeError, ValueError):
            usage_text = repr(usage)
        chunk.append("<div>usage: %s</div>" % esc(usage_text[:4000]))
    event = row.get("last_event")
    if event:
        chunk.append("<div>last event: %s at %s (%s)</div>"
                     % (esc(event.get("name") or "-"),
                        esc(event.get("at") or "-"),
                        esc(event.get("snippet") or "-")))
    chunk.append("</details>")
    return "".join(chunk)


def _render_row(row: dict, esc) -> list:
    fresh = row.get("freshness") or {}
    cells = ["<tr>"]
    cells.append("<td>%s%s</td>" % (esc(row.get("task")
                                            or "(no input text)"),
                                    _render_details(row, esc)))
    cells.append("<td>%s</td>" % esc(row.get("profile")))
    cells.append("<td>%s</td>" % esc(row.get("status") or "unknown"))
    cells.append("<td>%s</td>" % esc(row.get("current_work") or "-"))
    cells.append("<td>%s</td>" % esc(row.get("last_ok_at") or "never"))
    cells.append("<td>%s</td>" % esc(fresh.get("label")))
    blockers = row.get("blockers") or []
    if blockers:
        inner = "".join("<li>%s</li>" % esc(item) for item in blockers)
        cells.append("<td><ul class=\"blockers\">%s</ul></td>" % inner)
    else:
        cells.append("<td>-</td>")
    link = row.get("pr_link")
    if link:
        cells.append("<td><a href=\"%s\">pull %s</a> <span class=\"expiry\">"
                     "(%s)</span></td>"
                     % (esc(link.get("url")), esc(link.get("number")),
                        esc(link.get("derived_from"))))
    else:
        cells.append("<td>-</td>")
    decisions = row.get("decisions") or []
    if not decisions:
        cells.append("<td>-</td>")
    else:
        chunks = []
        for decision in decisions:
            generation = decision.get("execution_generation")
            chunks.append(
                "<div class=\"decision\"><code>%s</code> request <code>%s"
                "</code> generation <code>%s</code>"
                "<div class=\"expiry\">expiry: %s</div>"
                "<div class=\"note\">%s</div></div>"
                % (esc(decision.get("kind")),
                   esc(decision.get("request_id") or "(unknown)"),
                   esc(generation if generation is not None
                       else "(unknown)"),
                   esc(decision.get("expiry")),
                   esc(decision.get("note"))))
        cells.append("<td>%s</td>" % "".join(chunks))
    cells.append("</tr>")
    return cells


def render_html(snapshot: dict, error=None, refresh: float = 0.0) -> str:
    """Pure snapshot -> page. Every value is escaped; no script anywhere."""
    def esc(value) -> str:
        return _html.escape("" if value is None else str(value), quote=True)

    parts = [
        "<!doctype html><html lang=\"en\"><head>",
        "<meta charset=\"utf-8\">",
        "<meta name=\"robots\" content=\"noindex\">",
        "<title>g2h dashboard (read-only): profile %s</title>"
        % esc(snapshot.get("profile")),
    ]
    if refresh and refresh > 0:
        parts.append("<meta http-equiv=\"refresh\" content=\"%d\">"
                     % max(1, int(refresh)))
    parts.append("<link rel=\"stylesheet\" href=\"/style.css\">")
    parts.append("</head><body><h1>g2h dashboard (read-only)</h1>")
    if error:
        parts.append("<div class=\"banner banner-error\">observer error "
                     "during the latest pass: %s</div>" % esc(error))
    gateway = snapshot.get("gateway") or {}
    if gateway.get("state") == "failing":
        parts.append("<div class=\"banner banner-warn\">gateway unreachable:"
                     " every non-terminal run is marked stale; last "
                     "transition %s</div>"
                     % esc(gateway.get("last_transition_at") or "unknown"))
    parts.append("<div class=\"statusline\">")
    parts.append("<span>%s</span>" % esc(snapshot.get("notifier")))
    parts.append("<span>profile: %s</span>" % esc(snapshot.get("profile")))
    parts.append("<span>gateway: %s</span>"
                 % esc(snapshot.get("gateway_contact")
                       or gateway.get("state") or "ok"))
    parts.append("<span>generated at: %s</span>"
                 % esc(snapshot.get("generated_at")))
    parts.append("<span>freshness window: %s</span>"
                 % esc("%d s" % int(snapshot.get("freshness_window_seconds")
                                    or 0)))
    parts.append("</div>")
    counts = snapshot.get("counts") or {}
    shown = int(counts.get("shown") or 0)
    hidden = int(counts.get("hidden") or 0)
    if shown == 0:
        parts.append("<p>No receipts yet in %s.</p>"
                     % esc(snapshot.get("profile")))
    elif hidden:
        parts.append("<p>Showing %d of %d receipts (newest first; %d older "
                     "hidden by --max-runs)</p>"
                     % (shown, shown + hidden, hidden))
    else:
        parts.append("<p>Showing %d receipts (newest first; none hidden)"
                     "</p>" % shown)
    parts.append("<table><thead><tr><th>Task</th><th>Profile</th>"
                 "<th>Status</th><th>Current work</th><th>Last confirmed "
                 "update</th><th>Freshness</th><th>Blockers</th><th>PR "
                 "link</th><th>Decisions needed</th></tr></thead><tbody>")
    for row in snapshot.get("runs") or []:
        parts.extend(_render_row(row, esc))
    parts.append("</tbody></table>")
    parts.append("<details class=\"limits\"><summary>What this dashboard "
                 "cannot show</summary><ul>")
    for item in snapshot.get("limitations") or []:
        parts.append("<li>%s</li>" % esc(item))
    parts.append("</ul><p>Human-only approvals: this page has no approve, "
                 "deny, answer or execute control anywhere, and no "
                 "ready-to-run command. Identity fields are shown so a "
                 "human can answer in their own Hermes client.</p></details>")
    parts.append("<footer>g2h %s. Local read-only page: loopback server, "
                 "GET only, no cookies, no tokens. Data for %s</footer>"
                 % (esc(snapshot.get("tool_version")),
                    esc(snapshot.get("base_url"))))
    parts.append("</body></html>")
    return "".join(parts)


class _DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allowed_hosts: frozenset = frozenset()
    address_family = socket.AF_INET
    server_name: str

    def server_bind(self) -> None:
        # Bind without reverse DNS: BaseServer.server_bind calls
        # socket.getfqdn, which for 127.0.0.1 performs a live PTR query
        # (on macOS it resolves to an in-addr.arpa name) and stalls long
        # past the readiness budget on sandboxed runner resolvers. A
        # loopback dashboard needs no advertised hostname.
        if self.allow_reuse_address:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()
        self.server_name = str(self.server_address[0])
        self.server_port = self.server_address[1]


def serve_dashboard(port: int, version: str, allowed_hosts, page_fn) -> None:
    """Bind loopback and answer GETs until interrupted.

    page_fn(query: dict) returns the page body as bytes. Received-method
    names below are the stdlib dispatch contract for a local server; the
    server never sends requests anywhere, it only answers 405 to anything
    that is not a safe read.
    """
    class Handler(BaseHTTPRequestHandler):
        server_version = "g2h-dashboard/%s" % version

        def log_message(self, format, *args):  # noqa: A002 stdlib contract
            pass  # quiet by default; loopback traffic only

        def _reply(self, status: int, ctype: str, body: bytes,
                   allow_get: bool = False, cache: bool = False):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Security-Policy", CSP_POLICY)
            self.send_header("X-Content-Type-Options", "nosniff")
            if allow_get:
                self.send_header("Allow", "GET")
            if cache:
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                if body:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        def _host_allowed(self) -> bool:
            host = (self.headers.get("Host") or "").strip().lower()
            allowed = getattr(self.server, "allowed_hosts", frozenset())
            return host in allowed

        def do_GET(self):
            if not self._host_allowed():
                self._reply(403, "text/plain; charset=utf-8", b"")
                return
            parts = urllib.parse.urlsplit(self.path)
            if parts.path == "/style.css":
                self._reply(200, "text/css; charset=utf-8",
                            STYLE_CSS.encode("utf-8"), cache=True)
                return
            if parts.path != "/":
                self._reply(404, "text/plain; charset=utf-8",
                            b"not found\n")
                return
            query = urllib.parse.parse_qs(parts.query)
            self._reply(200, "text/html; charset=utf-8", page_fn(query),
                        cache=True)

        def do_HEAD(self):
            self._reply(405, "text/plain; charset=utf-8", b"",
                        allow_get=True)

        def do_PUT(self):
            self._reply(405, "text/plain; charset=utf-8", b"",
                        allow_get=True)

        def do_DELETE(self):
            self._reply(405, "text/plain; charset=utf-8", b"",
                        allow_get=True)

        def do_PATCH(self):
            self._reply(405, "text/plain; charset=utf-8", b"",
                        allow_get=True)

        def do_OPTIONS(self):
            self._reply(405, "text/plain; charset=utf-8", b"",
                        allow_get=True)

        def do_TRACE(self):
            self._reply(405, "text/plain; charset=utf-8", b"",
                        allow_get=True)

        def do_POST(self):
            self._reply(405, "text/plain; charset=utf-8", b"",
                        allow_get=True)

    sys.stderr.write("g2h-dashboard: binding 127.0.0.1:%d\n" % port)
    sys.stderr.flush()
    server = _DashboardServer(("127.0.0.1", port), Handler)
    sys.stderr.write("g2h-dashboard: listening on 127.0.0.1:%d\n" % port)
    sys.stderr.flush()
    server.allowed_hosts = frozenset(allowed_hosts)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
