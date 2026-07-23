import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.routing import APIRoute
from starlette.requests import Request

from api import _shared, auth, captcha, exporters, platforms


def _request(*, authorization: str = "") -> Request:
    headers = []
    if authorization:
        headers.append((b"authorization", authorization.encode("ascii")))
    return Request({"type": "http", "headers": headers, "query_string": b""})


class ConsoleAuthProtectionTests(unittest.TestCase):
    def test_login_and_debug_require_the_configured_password(self):
        with (
            patch.object(_shared, "CONSOLE_PASSWORD", "test-console-password"),
            patch.object(auth, "CONSOLE_PASSWORD", "test-console-password"),
        ):
            self.assertEqual(
                auth.api_login({"password": "test-console-password"}),
                {"success": True},
            )
            with self.assertRaises(HTTPException) as wrong_login:
                auth.api_login({"password": "wrong"})
            self.assertEqual(wrong_login.exception.status_code, 401)

            with self.assertRaises(HTTPException) as anonymous_debug:
                auth.api_auth_debug(_request())
            self.assertEqual(anonymous_debug.exception.status_code, 401)

            result = auth.api_auth_debug(
                _request(authorization="Bearer test-console-password")
            )
            self.assertTrue(result["password_configured"])

    def test_sensitive_plugin_routers_have_console_auth_dependency(self):
        for router in (captcha.router, exporters.router, platforms.router):
            routes = [route for route in router.routes if isinstance(route, APIRoute)]
            self.assertTrue(routes)
            for route in routes:
                dependencies = [dependency.call for dependency in route.dependant.dependencies]
                self.assertIn(_shared.check_auth, dependencies, route.path)


if __name__ == "__main__":
    unittest.main()
