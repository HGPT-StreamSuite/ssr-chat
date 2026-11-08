from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ssr_chat.cli import build_parser, main


class IntegratedLiveCliTests(unittest.TestCase):
    def test_twitch_live_delegates_without_building_fixture_runtime(self):
        with patch("ssr_chat.twitch_live.main", return_value=3) as live, patch(
            "ssr_chat.cli.build_runtime"
        ) as build:
            self.assertEqual(main(["twitch-live", "listen", "--config", "external.json"]), 3)
        live.assert_called_once_with(["listen", "--config", "external.json"])
        build.assert_not_called()

    def test_live_help_does_not_create_auth_or_connect(self):
        with patch("ssr_chat.twitch_live._make_auth") as auth, contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as result:
                main(["twitch-live", "--help"])
        self.assertEqual(result.exception.code, 0)
        self.assertIn("login", output.getvalue())
        auth.assert_not_called()

    def test_lifecycle_default_is_opt_in(self):
        self.assertIsNone(build_parser().parse_args(["serve"]).lifecycle_dir)

    def test_serve_passes_explicit_lifecycle_directory(self):
        root = Path(__file__).resolve().parents[1] / ".test-temp"
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory, patch("ssr_chat.cli.serve") as serve:
            self.assertEqual(main(["serve", "--state-dir", directory,
                                   "--lifecycle-dir", str(Path(directory) / "control")]), 0)
        self.assertEqual(serve.call_args.kwargs["lifecycle_dir"], str(Path(directory) / "control"))


if __name__ == "__main__":
    unittest.main()
