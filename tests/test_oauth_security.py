from __future__ import annotations

import asyncio
import time
import unittest
from types import SimpleNamespace

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from fonbet_mcp.security import JWKSJWTVerifier, build_auth_kwargs

ISSUER = "https://auth.example.test"
RESOURCE = "https://fonbet.example.test/mcp"
JWKS = "https://auth.example.test/.well-known/jwks.json"


class OAuthConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.values = {
            "MCP_AUTH_MODE": "oauth",
            "MCP_OAUTH_ISSUER": ISSUER,
            "MCP_OAUTH_RESOURCE_URL": RESOURCE,
            "MCP_OAUTH_JWKS_URL": JWKS,
            "MCP_OAUTH_REQUIRED_SCOPES": "fonbet:read",
        }

    def test_default_is_unchanged_without_oauth(self):
        self.assertEqual(build_auth_kwargs({}), {})
        self.assertEqual(build_auth_kwargs({"MCP_AUTH_MODE": "none"}), {})

    def test_enabled_requires_complete_https_configuration(self):
        for name in ("MCP_OAUTH_ISSUER", "MCP_OAUTH_RESOURCE_URL", "MCP_OAUTH_JWKS_URL"):
            data = dict(self.values)
            del data[name]
            with self.subTest(field=name), self.assertRaises(ValueError):
                build_auth_kwargs(data)

        data = dict(self.values, MCP_OAUTH_ISSUER="http://auth.example.test")
        with self.assertRaises(ValueError):
            build_auth_kwargs(data)

        data = dict(self.values, MCP_OAUTH_JWKS_URL="https://username:pw@auth.example.test/key")
        with self.assertRaises(ValueError):
            build_auth_kwargs(data)

        data = dict(self.values, MCP_OAUTH_REQUIRED_SCOPES="")
        with self.assertRaises(ValueError):
            build_auth_kwargs(data)

        with self.assertRaises(ValueError):
            build_auth_kwargs({"MCP_AUTH_MODE": "unknown"})

    def test_enabled_configures_real_mcp_bearer_auth(self):
        config = build_auth_kwargs(self.values)
        self.assertIn("token_verifier", config)
        self.assertIn("auth", config)
        self.assertEqual(str(config["auth"].issuer_url).rstrip("/"), ISSUER)
        self.assertEqual(str(config["auth"].resource_server_url), RESOURCE)
        self.assertEqual(config["auth"].required_scopes, ["fonbet:read"])


class JWTVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_key = cls.private_key.public_key()

    def setUp(self):
        self.verifier = JWKSJWTVerifier(ISSUER, JWKS, RESOURCE, ["fonbet:read"])
        # Avoid external calls in the unit tests. Signature and claim checks
        # still run through PyJWT against a real generated public RSA key.
        self.verifier.jwks.get_signing_key_from_jwt = lambda _token: SimpleNamespace(key=self.public_key)

    def _jwt(self, **updates):
        payload = {
            "iss": ISSUER,
            "aud": RESOURCE,
            "sub": "user-1",
            "client_id": "chatgpt-client",
            "scope": "fonbet:read",
            "exp": int(time.time()) + 3600,
        }
        payload.update(updates)
        return jwt.encode(payload, self.private_key, algorithm="RS256")

    def _verify(self, token):
        return asyncio.run(self.verifier.verify_token(token))

    def test_valid_signed_token_with_required_scope(self):
        valid = self._verify(self._jwt())
        self.assertIsNotNone(valid)
        self.assertEqual(valid.client_id, "chatgpt-client")
        self.assertEqual(valid.resource, RESOURCE)
        self.assertEqual(valid.scopes, ["fonbet:read"])
        self.assertEqual(valid.subject, "user-1")

    def test_wrong_issuer_audience_expired_missing_scope_rejected(self):
        for modified in [
            {"iss": "https://attacker.test"},
            {"aud": "https://other.example.test/mcp"},
            {"exp": int(time.time()) - 3600},
            {"scope": "other:read"},
        ]:
            with self.subTest(modified=modified):
                self.assertIsNone(self._verify(self._jwt(**modified)))

    def test_tampered_signature_is_rejected(self):
        token = self._jwt()
        a, b, signature = token.split(".")
        signature = ("A" if signature[0] != "A" else "B") + signature[1:]
        self.assertIsNone(self._verify(".".join([a, b, signature])))

    def test_scp_list_and_no_client_id(self):
        payload = jwt.decode(
            self._jwt(), self.public_key, algorithms=["RS256"], audience=RESOURCE, issuer=ISSUER
        )
        del payload["scope"]
        del payload["client_id"]
        payload["scp"] = ["fonbet:read"]
        result = self._verify(jwt.encode(payload, self.private_key, algorithm="RS256"))
        self.assertIsNotNone(result)
        self.assertEqual(result.client_id, "user-1")

    def test_invalid_token_cannot_bypass_auth(self):
        self.assertIsNone(self._verify("not-a-jwt"))
        self.assertIsNone(self._verify(""))


if __name__ == "__main__":
    unittest.main()
