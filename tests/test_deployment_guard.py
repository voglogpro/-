import unittest

from scripts.deployment_guard import require_immutable_image


class DeploymentGuardTests(unittest.TestCase):
    def test_accepts_sha256_digest(self):
        digest = "a" * 64
        self.assertEqual(
            require_immutable_image(f"ghcr.io/example/bibitasks@sha256:{digest}"),
            f"ghcr.io/example/bibitasks@sha256:{digest}",
        )

    def test_rejects_mutable_tag(self):
        with self.assertRaisesRegex(RuntimeError, "immutable image reference"):
            require_immutable_image("ghcr.io/example/bibitasks:latest")

    def test_rejects_missing_or_malformed_digest(self):
        for value in (None, "", "ghcr.io/example/bibitasks@sha256:ABC", "repo@sha256:" + "g" * 64):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                require_immutable_image(value)
