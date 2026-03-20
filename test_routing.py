#!/usr/bin/env python3
"""Regression tests for the declarative URL routing table in server.py.

Verifies that:
- Every route in _ROUTES has a corresponding handler method on the
  request handler class.
- No duplicate patterns exist (same method + pattern).
- The route index is consistent with the route table.
- Pattern matching works for both static and parameterized routes.
- Device routes trigger validation via the device_param flag.
- The param_types coercion is applied correctly.
- The unquote_params flag is applied correctly.

No network or hardware dependencies — all tests use source inspection
and the route table data structure directly.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import unittest
from typing import Any

from server import (
    _Route,
    _ROUTES,
    _ROUTE_INDEX,
    _PARAM_OPEN,
    _PARAM_CLOSE,
    DEVICE_RESOLVE_ERROR,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Methods the routing table covers.
SUPPORTED_METHODS: tuple[str, ...] = ("GET", "POST", "DELETE")


# ---------------------------------------------------------------------------
# Tests: Route table integrity
# ---------------------------------------------------------------------------

class TestRouteTableIntegrity(unittest.TestCase):
    """Verify the route table is internally consistent."""

    def test_routes_is_nonempty(self) -> None:
        """The route table must contain at least one route."""
        self.assertGreater(len(_ROUTES), 0)

    def test_all_routes_have_valid_methods(self) -> None:
        """Every route must use a recognized HTTP method."""
        for route in _ROUTES:
            self.assertIn(
                route.method, SUPPORTED_METHODS,
                f"Route {route.pattern} has invalid method '{route.method}'",
            )

    def test_no_duplicate_patterns(self) -> None:
        """No two routes may have the same (method, pattern) combination."""
        seen: set[tuple[str, tuple[str, ...]]] = set()
        for route in _ROUTES:
            key: tuple[str, tuple[str, ...]] = (route.method, route.pattern)
            self.assertNotIn(
                key, seen,
                f"Duplicate route: {route.method} {'/'.join(route.pattern)}",
            )
            seen.add(key)

    def test_route_index_covers_all_routes(self) -> None:
        """Every route in _ROUTES must appear in _ROUTE_INDEX."""
        for route in _ROUTES:
            key: tuple[str, int] = (route.method, len(route.pattern))
            self.assertIn(
                key, _ROUTE_INDEX,
                f"Route index missing key {key} for "
                f"{route.method} {'/'.join(route.pattern)}",
            )
            self.assertIn(
                route, _ROUTE_INDEX[key],
                f"Route not in index bucket for "
                f"{route.method} {'/'.join(route.pattern)}",
            )

    def test_route_index_has_no_extra_routes(self) -> None:
        """_ROUTE_INDEX must not contain routes absent from _ROUTES."""
        route_set: set[int] = {id(r) for r in _ROUTES}
        for key, bucket in _ROUTE_INDEX.items():
            for route in bucket:
                self.assertIn(
                    id(route), route_set,
                    f"Route index contains unregistered route at {key}",
                )


# ---------------------------------------------------------------------------
# Tests: Handler method existence
# ---------------------------------------------------------------------------

class TestHandlersExist(unittest.TestCase):
    """Verify every route's handler method exists on the request class."""

    def test_all_handlers_exist(self) -> None:
        """Every route.handler must be a method on GlowUpRequestHandler."""
        # Import here to avoid circular issues at module level.
        from server import GlowUpRequestHandler

        for route in _ROUTES:
            self.assertTrue(
                hasattr(GlowUpRequestHandler, route.handler),
                f"Handler '{route.handler}' not found on "
                f"GlowUpRequestHandler for route "
                f"{route.method} /{'/'.join(route.pattern)}",
            )

    def test_all_handlers_are_callable(self) -> None:
        """Every handler attribute must be callable."""
        from server import GlowUpRequestHandler

        for route in _ROUTES:
            handler: Any = getattr(GlowUpRequestHandler, route.handler, None)
            self.assertTrue(
                callable(handler),
                f"Handler '{route.handler}' is not callable",
            )


# ---------------------------------------------------------------------------
# Tests: Pattern matching logic
# ---------------------------------------------------------------------------

class TestPatternMatching(unittest.TestCase):
    """Verify pattern segments match correctly."""

    def test_static_route_matches_exact_path(self) -> None:
        """A static pattern must match only the exact path segments."""
        route: _Route = _Route("GET", ("api", "status"), "_handler")
        parts: list[str] = ["api", "status"]
        self.assertTrue(self._matches(route, parts))

    def test_static_route_rejects_wrong_path(self) -> None:
        """A static pattern must reject different path segments."""
        route: _Route = _Route("GET", ("api", "status"), "_handler")
        self.assertFalse(self._matches(route, ["api", "devices"]))

    def test_static_route_rejects_wrong_length(self) -> None:
        """A static pattern must reject paths of different length."""
        route: _Route = _Route("GET", ("api", "status"), "_handler")
        self.assertFalse(self._matches(route, ["api", "status", "extra"]))

    def test_param_route_captures_value(self) -> None:
        """A {param} placeholder must capture any segment value."""
        route: _Route = _Route("GET", ("api", "devices", "{id}", "status"),
                               "_handler")
        parts: list[str] = ["api", "devices", "10.0.0.5", "status"]
        params: dict[str, str] = {}
        self.assertTrue(self._matches(route, parts, params))
        self.assertEqual(params["id"], "10.0.0.5")

    def test_two_params_captured_in_order(self) -> None:
        """Multiple {param} placeholders capture values in order."""
        route: _Route = _Route(
            "POST",
            ("api", "assign", "{node_id}", "cancel", "{assignment_id}"),
            "_handler",
        )
        parts: list[str] = ["api", "assign", "judy", "cancel", "abc-123"]
        params: dict[str, str] = {}
        self.assertTrue(self._matches(route, parts, params))
        self.assertEqual(params["node_id"], "judy")
        self.assertEqual(params["assignment_id"], "abc-123")

    def test_param_route_rejects_wrong_literal(self) -> None:
        """A parameterized pattern must reject wrong literal segments."""
        route: _Route = _Route("POST", ("api", "devices", "{id}", "play"),
                               "_handler")
        parts: list[str] = ["api", "devices", "10.0.0.5", "stop"]
        self.assertFalse(self._matches(route, parts))

    @staticmethod
    def _matches(
        route: _Route,
        parts: list[str],
        params: dict[str, str] | None = None,
    ) -> bool:
        """Replicate the _dispatch matching logic for testing.

        Args:
            route:  Route to test against.
            parts:  URL path segments to match.
            params: If provided, populated with captured param values.

        Returns:
            ``True`` if the route matches the parts.
        """
        if len(route.pattern) != len(parts):
            return False
        if params is None:
            params = {}
        for seg, pat in zip(parts, route.pattern):
            if pat.startswith(_PARAM_OPEN) and pat.endswith(_PARAM_CLOSE):
                params[pat[1:-1]] = seg
            elif seg != pat:
                return False
        return True


# ---------------------------------------------------------------------------
# Tests: Route flags
# ---------------------------------------------------------------------------

class TestRouteFlags(unittest.TestCase):
    """Verify route flags are set correctly for known routes."""

    def test_dashboard_requires_no_auth(self) -> None:
        """The dashboard route must have requires_auth=False."""
        dashboard: list[_Route] = [
            r for r in _ROUTES
            if r.pattern == ("dashboard",)
        ]
        self.assertEqual(len(dashboard), 1, "Expected exactly 1 dashboard route")
        self.assertFalse(
            dashboard[0].requires_auth,
            "Dashboard route must not require auth",
        )

    def test_all_device_routes_have_device_param(self) -> None:
        """Routes matching /api/devices/{id}/... must set device_param."""
        for route in _ROUTES:
            if (len(route.pattern) >= 4
                    and route.pattern[0] == "api"
                    and route.pattern[1] == "devices"
                    and route.pattern[2].startswith(_PARAM_OPEN)):
                self.assertIsNotNone(
                    route.device_param,
                    f"Device route {route.method} "
                    f"/{'/'.join(route.pattern)} "
                    f"missing device_param flag",
                )

    def test_schedule_index_has_int_type(self) -> None:
        """POST /api/schedule/{index}/enabled must coerce index to int."""
        sched: list[_Route] = [
            r for r in _ROUTES
            if r.pattern == ("api", "schedule", "{index}", "enabled")
        ]
        self.assertEqual(len(sched), 1)
        self.assertIn("index", sched[0].param_types)
        self.assertEqual(sched[0].param_types["index"], int)

    def test_delete_routes_unquote_params(self) -> None:
        """DELETE routes with path params must specify unquote_params."""
        for route in _ROUTES:
            if route.method != "DELETE":
                continue
            # Count how many params this route has.
            param_names: list[str] = [
                pat[1:-1] for pat in route.pattern
                if pat.startswith(_PARAM_OPEN) and pat.endswith(_PARAM_CLOSE)
            ]
            if param_names:
                for pname in param_names:
                    # Every DELETE param should be unquoted (MACs have colons,
                    # device IDs may have encoded chars).
                    self.assertIn(
                        pname, route.unquote_params,
                        f"DELETE route /{'/'.join(route.pattern)} "
                        f"param '{pname}' not in unquote_params",
                    )

    def test_non_dashboard_routes_require_auth(self) -> None:
        """All routes except dashboard must require auth."""
        for route in _ROUTES:
            if route.pattern == ("dashboard",):
                continue
            self.assertTrue(
                route.requires_auth,
                f"Route {route.method} /{'/'.join(route.pattern)} "
                f"should require auth",
            )


# ---------------------------------------------------------------------------
# Tests: Route count sanity
# ---------------------------------------------------------------------------

class TestRouteCount(unittest.TestCase):
    """Sanity check that all expected routes are present."""

    def test_get_route_count(self) -> None:
        """GET routes should match the expected count."""
        get_routes: list[_Route] = [r for r in _ROUTES if r.method == "GET"]
        # 1 dashboard + 14 static + 3 device = 18
        self.assertEqual(
            len(get_routes), 18,
            f"Expected 18 GET routes, got {len(get_routes)}",
        )

    def test_post_route_count(self) -> None:
        """POST routes should match the expected count."""
        post_routes: list[_Route] = [r for r in _ROUTES if r.method == "POST"]
        # 7 device + 5 parameterized + 7 static = 19
        self.assertEqual(
            len(post_routes), 19,
            f"Expected 19 POST routes, got {len(post_routes)}",
        )

    def test_delete_route_count(self) -> None:
        """DELETE routes should match the expected count."""
        del_routes: list[_Route] = [r for r in _ROUTES if r.method == "DELETE"]
        self.assertEqual(
            len(del_routes), 2,
            f"Expected 2 DELETE routes, got {len(del_routes)}",
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
