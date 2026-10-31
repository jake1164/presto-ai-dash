"""The PyPI and GHCR adapters: what they ask, what they accept, what they refuse.

A fake transport stands in for the network, so nothing here contacts a
registry. The real ``fetch`` is exercised against a throwaway loopback server.
"""

import http.server
import json
from pathlib import Path
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash import release_registry as registry
from presto_ai_dash.release_registry import (Cancelled, GhcrAdapter, PypiAdapter,
                                             RegistryError, Response)

IMAGE = "jake1164/presto-ai-dash"
TAGS = "https://ghcr.io/v2/%s/tags/list" % IMAGE
FIRST = TAGS + "?n=100"
TOKEN_URL = "https://ghcr.io/token?service=ghcr.io&scope=repository%%3A%s%%3Apull" % IMAGE.replace("/", "%2F")
CHALLENGE = ('Bearer realm="https://ghcr.io/token",service="ghcr.io",'
             'scope="repository:%s:pull"' % IMAGE)


def reply(status=200, body=None, headers=None, raw=None):
    data = raw if raw is not None else json.dumps(body).encode("utf-8")
    return Response(status, headers or {}, data)


class Net(object):
    """A scripted transport: maps a URL to a reply, a list of replies, or an error."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.requests = []

    def __call__(self, url, headers, timeout, max_bytes):
        self.requests.append((url, dict(headers), timeout, max_bytes))
        value = self.routes.get(url)
        if isinstance(value, list):
            value = value.pop(0) if len(value) > 1 else value[0]
        if value is None:
            return reply(404, {"message": "not found"})
        if isinstance(value, Exception):
            raise value
        return value

    @property
    def urls(self):
        return [item[0] for item in self.requests]


def pypi_body(releases, name="presto-ai-dash"):
    return {"info": {"name": name, "version": "0"}, "releases": releases}


def wheel(yanked=False):
    return [{"filename": "presto_ai_dash-1-py3-none-any.whl", "yanked": yanked}]


PYPI_URL = "https://pypi.org/pypi/presto-ai-dash/json"


class PypiTests(unittest.TestCase):
    def labels(self, net):
        return sorted(item.label for item in PypiAdapter("presto-ai-dash", transport=net).releases())

    def test_it_asks_pypi_and_only_pypi(self):
        net = Net({PYPI_URL: reply(body=pypi_body({"0.50.0": wheel(), "0.51.0rc1": wheel()}))})
        self.assertEqual(self.labels(net), ["0.50.0", "0.51.0rc1"])
        self.assertEqual(net.urls, [PYPI_URL])

    def test_yanked_empty_and_invalid_releases_are_ignored(self):
        net = Net({PYPI_URL: reply(body=pypi_body({
            "0.49.0": wheel(), "0.50.0": wheel(yanked=True), "0.50.1": [],
            "0.50.2": [{"filename": "x.txt"}], "0.50.3": [{"yanked": False}],
            "0.50.4": None, "0.50.5.post1": wheel(), "latest": wheel(), "0.60.0.dev1": wheel(),
            "0.50.6": [{"filename": "p-1.tar.gz", "yanked": False}]}))})
        self.assertEqual(self.labels(net), ["0.49.0", "0.50.6"])

    def test_a_release_with_one_good_file_is_usable_despite_a_yanked_one(self):
        files = wheel(yanked=True) + [{"filename": "p-1.tar.gz", "yanked": False}]
        net = Net({PYPI_URL: reply(body=pypi_body({"0.50.0": files}))})
        self.assertEqual(self.labels(net), ["0.50.0"])

    def test_no_usable_release_is_not_current(self):
        net = Net({PYPI_URL: reply(body=pypi_body({"0.50.0": wheel(yanked=True)}))})
        with self.assertRaises(RegistryError) as caught:
            PypiAdapter("presto-ai-dash", transport=net).releases()
        self.assertEqual(caught.exception.code, "no_eligible_release")

    def check(self, response, code, adapter=None):
        net = Net({PYPI_URL: response})
        with self.assertRaises(RegistryError) as caught:
            (adapter or PypiAdapter("presto-ai-dash", transport=net)).releases()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_malformed_metadata_is_refused(self):
        for body in ([], "text", {"info": {}}, {"releases": {}},
                     {"info": {"name": "other"}, "releases": {}},
                     {"info": {"name": "presto-ai-dash"}, "releases": []},
                     {"info": [], "releases": {}}, None):
            with self.subTest(body=body):
                self.check(reply(body=body), "invalid_metadata")

    def test_invalid_json_is_refused(self):
        self.check(reply(raw=b"{not json"), "invalid_metadata")
        self.check(reply(raw=b"\xff\xfe"), "invalid_metadata")
        self.check(reply(raw=b"[" * 100000), "invalid_metadata")

    def test_the_project_name_may_be_spelled_either_way(self):
        net = Net({PYPI_URL: reply(body=pypi_body({"0.50.0": wheel()}, name="Presto_AI.Dash"))})
        self.assertEqual(self.labels(net), ["0.50.0"])

    def test_an_error_status_is_an_http_error(self):
        for status in (404, 500, 503, 401, 301):
            with self.subTest(status=status):
                self.check(reply(status, {}), "http_error")

    def test_rate_limits_carry_a_bounded_retry_after(self):
        error = self.check(reply(429, {}, {"Retry-After": "120"}), "rate_limited")
        self.assertEqual(error.retry_after, 120)
        self.assertEqual(self.check(reply(429, {}, {"Retry-After": "5"}),
                                    "rate_limited").retry_after, 60)
        self.assertEqual(self.check(reply(429, {}, {"Retry-After": "99999999"}),
                                    "rate_limited").retry_after, 86400)
        self.assertIsNone(self.check(reply(429, {}, {"Retry-After": "tomorrow"}),
                                     "rate_limited").retry_after)
        self.check(reply(403, {}, {"X-RateLimit-Remaining": "0"}), "rate_limited")

    def test_transport_failures_pass_their_code_through(self):
        for code in ("timeout", "network_error"):
            with self.subTest(code=code):
                self.check(RegistryError(code), code)

    def test_a_redirect_to_the_same_host_is_followed_a_bounded_number_of_times(self):
        target = "https://pypi.org/pypi/presto-ai-dash/json/"
        net = Net({PYPI_URL: reply(301, {}, {"Location": target}),
                   target: reply(body=pypi_body({"0.50.0": wheel()}))})
        self.assertEqual(self.labels(net), ["0.50.0"])
        loop = Net({PYPI_URL: reply(302, {}, {"Location": PYPI_URL})})
        with self.assertRaises(RegistryError) as caught:
            PypiAdapter("presto-ai-dash", transport=loop).releases()
        self.assertEqual(caught.exception.code, "http_error")
        self.assertEqual(len(loop.requests), registry.MAX_REDIRECTS + 1)

    def test_a_redirect_to_another_host_or_scheme_is_refused_unrequested(self):
        for target in ("https://test.pypi.org/pypi/presto-ai-dash/json",
                       "http://pypi.org/pypi/presto-ai-dash/json",
                       "https://evil.example/pypi", "https://pypi.org:8443/x",
                       "https://user:pw@pypi.org/x", "ftp://pypi.org/x"):
            with self.subTest(target=target):
                net = Net({PYPI_URL: reply(301, {}, {"Location": target})})
                with self.assertRaises(RegistryError):
                    PypiAdapter("presto-ai-dash", transport=net).releases()
                self.assertEqual(net.urls, [PYPI_URL])

    def test_it_never_asks_testpypi_or_another_registry(self):
        net = Net({PYPI_URL: reply(500, {})})
        with self.assertRaises(RegistryError):
            PypiAdapter("presto-ai-dash", transport=net).releases()
        self.assertEqual(net.urls, [PYPI_URL])

    def test_the_request_is_bounded(self):
        net = Net({PYPI_URL: reply(body=pypi_body({"0.50.0": wheel()}))})
        PypiAdapter("presto-ai-dash", transport=net).releases()
        _, headers, timeout, limit = net.requests[0]
        self.assertLessEqual(timeout, registry.REQUEST_TIMEOUT_S)
        self.assertEqual(limit, registry.PYPI_MAX_BYTES)
        self.assertNotIn("Authorization", headers)

    def test_an_enormous_release_table_is_not_trusted(self):
        table = {"0.%d.0" % number: wheel() for number in range(registry.MAX_PYPI_RELEASES + 1)}
        self.check(reply(body=pypi_body(table)), "incomplete_listing")

    def test_the_package_urls_are_generated_here(self):
        adapter = PypiAdapter("presto-ai-dash")
        self.assertEqual(adapter.package_url(), "https://pypi.org/project/presto-ai-dash/")
        self.assertEqual(adapter.package_url("0.50.0rc5"),
                         "https://pypi.org/project/presto-ai-dash/0.50.0rc5/")


def page(tags, name=IMAGE):
    return {"name": name, "tags": tags}


def authed(net_routes):
    """Routes for a registry that answers the first request with a challenge."""
    return net_routes


class GhcrTests(unittest.TestCase):
    def adapter(self, net):
        return GhcrAdapter(IMAGE, transport=net)

    def labels(self, net):
        return sorted(item.label for item in self.adapter(net).releases())

    def check(self, net, code):
        with self.assertRaises(RegistryError) as caught:
            self.adapter(net).releases()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def challenged(self, tags_reply, extra=None):
        routes = {FIRST: [reply(401, {}, {"WWW-Authenticate": CHALLENGE}), tags_reply],
                  TOKEN_URL: reply(body={"token": "abc.def-1"})}
        routes.update(extra or {})
        return Net(routes)

    def test_it_authenticates_anonymously_and_lists_numbered_tags(self):
        net = self.challenged(reply(body=page(
            ["latest", "0.50", "0.50.0-rc3", "0.50.0-rc4", "sha-abc", "v1.0.0", "0.51.0"])))
        self.assertEqual(self.labels(net), ["0.50.0-rc3", "0.50.0-rc4", "0.51.0"])
        self.assertEqual(net.urls, [FIRST, TOKEN_URL, FIRST])
        self.assertEqual(net.requests[2][1]["Authorization"], "Bearer abc.def-1")
        self.assertNotIn("Authorization", net.requests[0][1])
        self.assertNotIn("Authorization", net.requests[1][1])

    def test_it_asks_ghcr_and_only_ghcr(self):
        net = self.challenged(reply(body=page(["0.50.0"])))
        self.labels(net)
        self.assertTrue(all(url.startswith("https://ghcr.io/") for url in net.urls))

    def test_a_registry_that_needs_no_token_is_fine(self):
        net = Net({FIRST: reply(body=page(["0.50.0"]))})
        self.assertEqual(self.labels(net), ["0.50.0"])
        self.assertEqual(len(net.requests), 1)

    def test_floating_aliases_alone_are_not_a_release(self):
        net = self.challenged(reply(body=page(["latest", "0", "0.50", "main"])))
        self.check(net, "no_eligible_release")

    def test_an_empty_repository_is_not_current(self):
        net = self.challenged(reply(body=page(None)))
        self.check(net, "no_eligible_release")

    def test_all_pages_are_read(self):
        second = TAGS + "?last=0.50.0-rc2&n=100"
        third = TAGS + "?last=0.50.0-rc9&n=100"
        net = self.challenged(
            reply(body=page(["0.50.0-rc1", "0.50.0-rc2"]),
                  headers={"Link": '<%s>; rel="next"' % "/v2/%s/tags/list?last=0.50.0-rc2&n=100" % IMAGE}),
            {second: reply(body=page(["0.50.0-rc9"]),
                           headers={"Link": '<%s>; rel="next"' % third}),
             third: reply(body=page(["0.50.0"]))})
        self.assertEqual(self.labels(net), ["0.50.0", "0.50.0-rc1", "0.50.0-rc2", "0.50.0-rc9"])

    def test_the_newest_tag_on_a_later_page_is_not_missed(self):
        second = TAGS + "?last=a&n=100"
        net = self.challenged(
            reply(body=page(["0.50.0"]), headers={"Link": '<%s>; rel="next"' % second}),
            {second: reply(500, {})})
        # Page two failed: an incomplete listing must never read as "current".
        self.check(net, "http_error")

    def test_a_next_link_to_elsewhere_is_an_incomplete_listing(self):
        for link in ("<https://evil.example/v2/%s/tags/list?last=a>" % IMAGE,
                     "<http://ghcr.io/v2/%s/tags/list?last=a>" % IMAGE,
                     "</v2/other/image/tags/list?last=a>",
                     "</v2/%s/tags/list?last=a&token=1>" % IMAGE,
                     "<https://ghcr.io:444/v2/%s/tags/list?last=a>" % IMAGE,
                     "<https://u:p@ghcr.io/v2/%s/tags/list?last=a>" % IMAGE,
                     "</v2/%s/tags/list?last=a&last=b>" % IMAGE,
                     "</v2/%s/tags/list?last=a#frag>" % IMAGE):
            with self.subTest(link=link):
                net = self.challenged(reply(body=page(["0.50.0"]),
                                            headers={"Link": link + '; rel="next"'}))
                self.check(net, "incomplete_listing")
                self.assertEqual(len(net.requests), 3)

    def test_a_repeating_next_link_is_an_incomplete_listing(self):
        net = self.challenged(reply(body=page(["0.50.0"]), headers={
            "Link": '</v2/%s/tags/list?n=100>; rel="next"' % IMAGE}))
        self.check(net, "incomplete_listing")

    def test_the_page_limit_is_a_hard_stop(self):
        routes = {}
        for number in range(registry.MAX_PAGES + 2):
            here = FIRST if number == 0 else TAGS + "?last=p%d&n=100" % number
            routes[here] = reply(body=page(["0.1.%d" % number]), headers={
                "Link": '<%s>; rel="next"' % (TAGS + "?last=p%d&n=100" % (number + 1))})
        net = Net(routes)
        self.check(net, "incomplete_listing")
        self.assertLessEqual(len(net.requests), registry.MAX_PAGES)

    def test_too_many_tags_is_an_incomplete_listing(self):
        tags = ["0.1.%d" % number for number in range(registry.MAX_TAGS + 1)]
        self.check(Net({FIRST: reply(body=page(tags))}), "incomplete_listing")

    def test_the_wrong_repository_or_shape_is_refused(self):
        for body in (page(["0.50.0"], name="other/image"), {"tags": ["0.50.0"]},
                     {"name": IMAGE, "tags": "0.50.0"}, {"name": IMAGE, "tags": [1, 2]}, []):
            with self.subTest(body=body):
                self.check(self.challenged(reply(body=body)), "invalid_metadata")

    def test_challenges_are_validated_not_followed(self):
        bad = (
            'Bearer realm="https://evil.example/token",service="ghcr.io",scope="repository:%s:pull"' % IMAGE,
            'Bearer realm="http://ghcr.io/token",service="ghcr.io",scope="repository:%s:pull"' % IMAGE,
            'Bearer realm="https://ghcr.io/other",service="ghcr.io",scope="repository:%s:pull"' % IMAGE,
            'Bearer realm="https://ghcr.io/token",service="evil",scope="repository:%s:pull"' % IMAGE,
            'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:%s:push"' % IMAGE,
            'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:other/x:pull"',
            'Basic realm="ghcr.io"', "", "Bearer")
        for header in bad:
            with self.subTest(header=header):
                net = Net({FIRST: reply(401, {}, {"WWW-Authenticate": header})})
                self.check(net, "auth_challenge")
                self.assertEqual(net.urls, [FIRST])

    def test_a_bad_token_answer_is_an_auth_error(self):
        for answer in (reply(401, {}), reply(403, {}), reply(body={}),
                       reply(body={"token": ""}), reply(body={"token": "has space"}),
                       reply(body={"token": 5}), reply(body={"token": "a" * 9000}),
                       reply(raw=b"nope")):
            with self.subTest(answer=answer.status):
                net = Net({FIRST: reply(401, {}, {"WWW-Authenticate": CHALLENGE}),
                           TOKEN_URL: answer})
                with self.assertRaises(RegistryError) as caught:
                    self.adapter(net).releases()
                self.assertIn(caught.exception.code, ("auth_challenge", "invalid_metadata"))

    def test_an_access_token_field_is_accepted(self):
        net = Net({FIRST: [reply(401, {}, {"WWW-Authenticate": CHALLENGE}),
                           reply(body=page(["0.50.0"]))],
                   TOKEN_URL: reply(body={"access_token": "tok123"})})
        self.assertEqual(self.labels(net), ["0.50.0"])

    def test_a_second_challenge_after_a_token_is_an_auth_error(self):
        net = Net({FIRST: reply(401, {}, {"WWW-Authenticate": CHALLENGE}),
                   TOKEN_URL: reply(body={"token": "tok"})})
        self.check(net, "auth_challenge")
        self.assertEqual(net.urls, [FIRST, TOKEN_URL, FIRST])

    def test_a_redirect_is_never_followed(self):
        net = Net({FIRST: reply(302, {}, {"Location": "https://ghcr.io/elsewhere"})})
        self.check(net, "http_error")
        self.assertEqual(net.urls, [FIRST])

    def test_rate_limit_timeout_and_http_errors_have_their_codes(self):
        self.check(Net({FIRST: reply(429, {}, {"Retry-After": "300"})}), "rate_limited")
        self.check(Net({FIRST: reply(500, {})}), "http_error")
        self.check(Net({FIRST: RegistryError("timeout")}), "timeout")
        self.check(Net({FIRST: RegistryError("network_error")}), "network_error")

    def test_a_failure_never_falls_back_to_pypi(self):
        net = Net({FIRST: RegistryError("network_error")})
        self.check(net, "network_error")
        self.assertEqual(net.urls, [FIRST])

    def test_the_overall_deadline_stops_a_slow_listing(self):
        clock = {"now": 0.0}

        def slow(url, headers, timeout, max_bytes):
            clock["now"] += 20.0
            return reply(body=page(["0.50.0"]), headers={
                "Link": '</v2/%s/tags/list?last=%s&n=100>; rel="next"' % (IMAGE, clock["now"])})

        adapter = GhcrAdapter(IMAGE, transport=slow, clock=lambda: clock["now"])
        with self.assertRaises(RegistryError) as caught:
            adapter.releases()
        self.assertEqual(caught.exception.code, "timeout")

    def test_cancellation_stops_before_the_next_request(self):
        adapter = GhcrAdapter(IMAGE, transport=Net({FIRST: reply(body=page(["0.50.0"]))}),
                              cancelled=lambda: True)
        with self.assertRaises(Cancelled):
            adapter.releases()

    def test_a_bad_image_name_is_refused(self):
        for name in ("", "UPPER/case", "a b/c", "../x", "a//b", "x" * 300):
            with self.assertRaises(ValueError, msg=name):
                GhcrAdapter(name)

    def test_the_package_urls_are_generated_here(self):
        adapter = GhcrAdapter(IMAGE)
        self.assertEqual(adapter.package_url(),
                         "https://github.com/jake1164/presto-ai-dash/pkgs/container/presto-ai-dash")
        self.assertEqual(adapter.package_url("0.50.0-rc4"),
                         "https://github.com/jake1164/presto-ai-dash/releases/tag/v0.50.0-rc4")


class LinkHeaderTests(unittest.TestCase):
    def test_next_is_found_wherever_rel_appears_among_the_parameters(self):
        for header in ('<https://ghcr.io/a?last=1>; rel="next"',
                       '<https://ghcr.io/a?last=1>; type="application/json"; rel="next"',
                       '<https://ghcr.io/a?last=1>;title="x";rel=next',
                       '<https://ghcr.io/a?last=1>; REL="Next"; anchor="#a"',
                       '</p>; rel="prev", <https://ghcr.io/a?last=1>; rel="next"',
                       '<https://ghcr.io/a?last=1>; rel="prev next"',
                       '<https://ghcr.io/a?last=1> ;  rel = "next"'):
            with self.subTest(header=header):
                self.assertEqual(registry.next_link(header), "https://ghcr.io/a?last=1")

    def test_a_quoted_parameter_may_contain_separators(self):
        self.assertEqual(registry.next_link('<https://ghcr.io/a>; title="a;b,c>"; rel="next"'),
                         "https://ghcr.io/a")

    def test_other_relations_are_not_next(self):
        for header in ('</p>; rel="prev"', '</p>; rel="nextish"', '</p>; type="next"', '</p>'):
            with self.subTest(header=header):
                self.assertIsNone(registry.next_link(header))

    def test_an_unreadable_header_is_an_incomplete_listing_not_a_missing_one(self):
        for header in ("garbage", "<https://ghcr.io/a", '<https://ghcr.io/a>; rel="next',
                       "<https://ghcr.io/a> rel=next", "<https://ghcr.io/a>; =x"):
            with self.subTest(header=header):
                with self.assertRaises(RegistryError) as caught:
                    registry.next_link(header)
                self.assertEqual(caught.exception.code, "incomplete_listing")

    def test_a_next_page_behind_an_earlier_parameter_is_still_read(self):
        second = TAGS + "?last=a&n=100"
        net = Net({FIRST: reply(body=page(["0.50.0-rc1"]), headers={
            "Link": '</v2/%s/tags/list?last=a&n=100>; type="x"; rel="next"' % IMAGE}),
            second: reply(body=page(["0.50.0-rc9"]))})
        found = sorted(item.label for item in GhcrAdapter(IMAGE, transport=net).releases())
        self.assertEqual(found, ["0.50.0-rc1", "0.50.0-rc9"])
        self.assertEqual(net.urls, [FIRST, second])

    def test_a_malformed_link_header_never_reads_as_current(self):
        net = Net({FIRST: reply(body=page(["0.50.0"]), headers={"Link": "<oops"})})
        with self.assertRaises(RegistryError) as caught:
            GhcrAdapter(IMAGE, transport=net).releases()
        self.assertEqual(caught.exception.code, "incomplete_listing")


class RetryAfterDateTests(unittest.TestCase):
    NOW = 1792567200  # Wed, 21 Oct 2026 07:20:00 GMT

    def test_seconds_and_http_dates_are_both_read_and_bounded(self):
        parse = registry.retry_after_seconds
        self.assertEqual(parse("7200", self.NOW), 7200)
        self.assertEqual(parse("Wed, 21 Oct 2026 08:20:00 GMT", self.NOW), 3600)
        self.assertEqual(parse("Wed, 21 Oct 2026 07:20:10 GMT", self.NOW), 60)
        self.assertEqual(parse("Wed, 21 Oct 2026 06:00:00 GMT", self.NOW), 60)
        self.assertEqual(parse("Fri, 30 Oct 2026 07:20:00 GMT", self.NOW), 86400)
        self.assertEqual(parse("Wednesday, 21-Oct-26 08:20:00 GMT", self.NOW), 3600)

    def test_unusable_values_are_ignored(self):
        for value in (None, "", "tomorrow", "-5", "1.5", "Wed, 21 Oct 2026 08:20:00",
                      "99999999999999", "Wed, 99 Foo 2026 08:20:00 GMT"):
            with self.subTest(value=value):
                self.assertIsNone(registry.retry_after_seconds(value, self.NOW))

    def test_a_date_reaches_the_error_the_adapter_raises(self):
        header = {"Retry-After": "Wed, 21 Oct 2026 09:20:00 GMT"}
        net = Net({FIRST: reply(429, {}, header)})
        with self.assertRaises(RegistryError) as caught:
            GhcrAdapter(IMAGE, transport=net).releases()
        self.assertEqual(caught.exception.code, "rate_limited")
        # The date is far from "now" in a test run, so only the bound is certain.
        self.assertIn(caught.exception.retry_after, (60, 86400))


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        mode = self.path.strip("/")
        if mode == "slow":
            threading.Event().wait(2)
            return
        if mode in ("slowstatus", "slowheaders"):
            # The status line or the headers a byte at a time, each byte well
            # inside any socket timeout.
            head = b"HTTP/1.1 200 OK\r\n" if mode == "slowheaders" else b""
            slow = (b"HTTP/1.1 200 OK\r\n" if mode == "slowstatus" else b"") + \
                b"X-Pad: " + b"a" * 200 + b"\r\nContent-Length: 2\r\n\r\n{}"
            try:
                self.wfile.write(head)
                self.wfile.flush()
                for byte in slow:
                    self.wfile.write(bytes((byte,)))
                    self.wfile.flush()
                    threading.Event().wait(0.05)
            except OSError:
                pass
            self.close_connection = True
            return
        if mode == "trickle":
            self.send_response(200)
            self.send_header("Content-Length", "400")
            self.end_headers()
            try:
                for _ in range(400):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    threading.Event().wait(0.05)
            except OSError:
                pass
            return
        if mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b"x" * 5000 if mode == "big" else b'{"ok": true}'
        status = 429 if mode == "limited" else 200
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Retry-After", "30")
        self.end_headers()
        self.wfile.write(body)


class RealFetchTests(unittest.TestCase):
    """The real transport against a loopback server."""

    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, args=(0.02,), daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_a_response_comes_back_with_its_status_and_headers(self):
        response = registry.fetch(self.base + "/ok", {"User-Agent": "t"}, 3, 1000)
        self.assertEqual((response.status, response.body), (200, b'{"ok": true}'))
        limited = registry.fetch(self.base + "/limited", {}, 3, 1000)
        self.assertEqual((limited.status, limited.headers["retry-after"]), (429, "30"))

    def test_an_oversized_body_is_refused(self):
        with self.assertRaises(RegistryError) as caught:
            registry.fetch(self.base + "/big", {}, 3, 1000)
        self.assertEqual(caught.exception.code, "invalid_metadata")

    def test_a_slow_server_times_out(self):
        with self.assertRaises(RegistryError) as caught:
            registry.fetch(self.base + "/slow", {}, 0.3, 1000)
        self.assertEqual(caught.exception.code, "timeout")

    def test_a_body_that_trickles_is_cut_off_by_the_deadline(self):
        # One byte every 50 ms never trips a socket timeout, but the whole
        # request has a budget.
        started = time.monotonic()
        with self.assertRaises(RegistryError) as caught:
            registry.fetch(self.base + "/trickle", {}, 0.3, 1000)
        self.assertEqual(caught.exception.code, "timeout")
        self.assertLess(time.monotonic() - started, 1.5)

    def test_slowly_streamed_headers_are_cut_off_by_the_deadline(self):
        # The reviewer's case: a 150 ms budget must not stay blocked for over a
        # second while the server dribbles out its headers.
        for mode in ("slowstatus", "slowheaders"):
            with self.subTest(mode=mode):
                started = time.monotonic()
                with self.assertRaises(RegistryError) as caught:
                    registry.fetch(self.base + "/" + mode, {}, 0.15, 1000)
                self.assertEqual(caught.exception.code, "timeout")
                self.assertLess(time.monotonic() - started, 0.6)

    def test_the_deadline_timer_does_not_outlive_a_normal_request(self):
        before = threading.active_count()
        for _ in range(5):
            self.assertEqual(registry.fetch(self.base + "/ok", {}, 3, 1000).status, 200)
        time.sleep(0.05)
        self.assertLessEqual(threading.active_count(), before + 1)

    def test_a_deadline_that_has_already_passed_never_connects(self):
        deadline = registry._Deadline(0)
        with deadline:
            time.sleep(0.05)
            with self.assertRaises(socket.timeout):
                deadline.track(object())

    def test_the_overall_deadline_bounds_a_trickling_listing(self):
        adapter = PypiAdapter("presto-ai-dash")
        adapter._cancelled = None
        session = registry._Session(("127.0.0.1",), overall_timeout=0.3)
        started = time.monotonic()
        with self.assertRaises(RegistryError):
            # The real transport, through the session's own budget.
            registry.fetch(self.base + "/trickle", {}, min(registry.REQUEST_TIMEOUT_S,
                                                          session.deadline - time.monotonic()), 1000)
        self.assertLess(time.monotonic() - started, 1.5)

    def test_a_redirect_is_returned_not_followed(self):
        response = registry.fetch(self.base + "/redirect", {}, 3, 1000)
        self.assertEqual((response.status, response.headers["location"]), (302, "/ok"))

    def test_an_unreachable_host_is_a_network_error(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        with self.assertRaises(RegistryError) as caught:
            registry.fetch("http://127.0.0.1:%d/" % port, {}, 8, 1000)
        self.assertEqual(caught.exception.code, "network_error")


if __name__ == "__main__":
    unittest.main()
