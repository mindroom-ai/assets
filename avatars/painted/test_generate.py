"""Offline checks for the stock avatar regeneration script."""

import base64
import contextlib
import errno
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import OpenAI

SCRIPT = Path(__file__).with_name("generate.py")
SPEC = importlib.util.spec_from_file_location("generate", SCRIPT)
generate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generate)
PNG = Path(__file__).with_name("reference.png").read_bytes()


class GenerateTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        self.output = self.root / "output"
        self.reference = self.root / "reference.png"
        self.reference.write_bytes(PNG)
        self.entries = [
            {"name": "router", "prompt": "Paint a signpost."},
            {"name": "mind", "prompt": "Paint a brain."},
        ]
        self.manifest = self.root / "prompts.json"
        self.write_manifest()
        self.requests = []
        self.response = {"data": [{"b64_json": base64.b64encode(PNG).decode()}]}
        self.api_status = 200
        self.addCleanup(patch.stopall)
        patch.object(generate, "ROOT", self.root).start()
        patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=True).start()
        self.factory = patch.object(
            generate, "OpenAI", side_effect=self.make_client
        ).start()

    def make_client(self, **kwargs):
        return OpenAI(
            **kwargs,
            max_retries=0,
            http_client=httpx.Client(
                transport=httpx.MockTransport(self.handle_request)
            ),
        )

    def write_manifest(self):
        self.manifest.write_text(
            json.dumps({"reference": "reference.png", "avatars": self.entries})
        )

    def handle_request(self, request):
        self.requests.append(request)
        return httpx.Response(self.api_status, json=self.response)

    def run_script(self, *arguments):
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return generate.main([*arguments, "--output-dir", str(self.output)])

    def test_selected_avatar_sends_saved_prompt_and_reference_through_sdk(self):
        self.run_script("router")
        self.assertEqual([p.name for p in self.output.iterdir()], ["router.png"])
        self.assertEqual((self.output / "router.png").read_bytes(), PNG)
        self.assertEqual(self.reference.read_bytes(), PNG)
        request = self.requests[0]
        self.assertEqual(request.url.path, "/v1/images/edits")
        self.assertEqual(request.headers["authorization"], "Bearer test-key")
        for value in (
            b"gpt-image-2.5-sunburst",
            b"Paint a signpost.",
            b"1024x1024",
            b"high",
            b"png",
            b'filename="reference.png"',
            PNG,
        ):
            self.assertIn(value, request.content)
        self.assertNotIn(b"Paint a brain.", request.content)

    def test_no_names_generates_all_entries_and_existing_outputs_are_skipped(self):
        self.output.mkdir()
        (self.output / "router.png").write_bytes(b"keep existing image")
        self.run_script()
        self.assertEqual(len(self.requests), 1)
        self.assertIn(b"Paint a brain.", self.requests[0].content)
        self.assertEqual(
            (self.output / "router.png").read_bytes(), b"keep existing image"
        )
        self.assertTrue((self.output / "mind.png").is_file())

    def test_force_overwrites_only_selected_output(self):
        self.output.mkdir()
        (self.output / "router.png").write_bytes(b"old")
        self.run_script("router", "--force")
        self.assertEqual((self.output / "router.png").read_bytes(), PNG)
        self.assertEqual(len(self.requests), 1)

    def test_write_failure_preserves_existing_image_and_removes_partial_output(self):
        self.output.mkdir()
        destination = self.output / "router.png"
        original_open = Path.open

        @contextlib.contextmanager
        def failing_open(path, mode="r", *args, **kwargs):
            with original_open(path, mode, *args, **kwargs) as stream:
                if mode in ("wb", "xb"):
                    stream.write(b"partial")
                    raise OSError(errno.ENOSPC, "No space left on device")
                yield stream

        for force in (True, False):
            with self.subTest(force=force):
                destination.unlink(missing_ok=True)
                if force:
                    destination.write_bytes(b"keep")
                arguments = ["router", "--force"] if force else ["router"]
                with (
                    patch.object(Path, "open", failing_open),
                    self.assertRaises(SystemExit) as error,
                ):
                    self.run_script(*arguments)
                self.assertEqual(error.exception.code, 1)
                if force:
                    self.assertEqual(destination.read_bytes(), b"keep")
                    destination.unlink()
                self.assertEqual(list(self.output.iterdir()), [])

    def test_concurrent_output_is_not_overwritten_without_force(self):
        destination = self.output / "router.png"
        validate = generate.png_bytes

        def concurrent_save(response):
            destination.write_bytes(b"another run")
            return validate(response)

        with (
            patch.object(generate, "png_bytes", side_effect=concurrent_save),
            self.assertRaises(SystemExit) as error,
        ):
            self.run_script("router")
        self.assertEqual(error.exception.code, 1)
        self.assertEqual(destination.read_bytes(), b"another run")
        self.assertEqual(list(self.output.iterdir()), [destination])

    def test_failed_responses_preserve_existing_output(self):
        self.output.mkdir()
        destination = self.output / "router.png"
        for response in (
            {"data": []},
            {"data": [{"b64_json": "invalid base64!"}]},
            {"data": [{"b64_json": base64.b64encode(b"not a PNG").decode()}]},
        ):
            with self.subTest(response=response):
                destination.write_bytes(b"keep")
                self.response = response
                with self.assertRaises(SystemExit) as error:
                    self.run_script("router", "--force")
                self.assertEqual(error.exception.code, 1)
                self.assertEqual(destination.read_bytes(), b"keep")

    def test_dry_run_needs_no_key_or_network_and_creates_no_output(self):
        os.environ.clear()
        self.run_script("--dry-run")
        self.factory.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_all_skipped_needs_no_key_or_network(self):
        self.output.mkdir()
        (self.output / "router.png").write_bytes(PNG)
        os.environ.clear()
        self.run_script("router")
        self.factory.assert_not_called()

    def test_unknown_name_is_rejected_before_network_or_writes(self):
        with self.assertRaises(SystemExit):
            self.run_script("rouetr")
        self.factory.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_invalid_manifest_is_rejected_before_network(self):
        for entries in (
            [{"name": "../outside", "prompt": "Paint."}],
            [{"name": "router", "prompt": ""}],
            [self.entries[0], self.entries[0]],
        ):
            with self.subTest(entries=entries):
                self.entries = entries
                self.write_manifest()
                with self.assertRaises(SystemExit):
                    self.run_script()
                self.factory.assert_not_called()
                self.assertFalse(self.output.exists())

    def test_key_file_is_trimmed(self):
        key_file = self.root / "api-key"
        key_file.write_text(" file-key\n")
        os.environ.clear()
        os.environ["OPENAI_API_KEY_FILE"] = str(key_file)
        self.run_script("router")
        self.assertEqual(self.factory.call_args.kwargs["api_key"], "file-key")

    def test_direct_key_takes_precedence_over_key_file(self):
        os.environ["OPENAI_API_KEY_FILE"] = str(self.root / "missing-key-file")
        self.run_script("router")
        self.assertEqual(self.factory.call_args.kwargs["api_key"], "test-key")

    def test_missing_key_fails_without_creating_output(self):
        os.environ.clear()
        with self.assertRaises(SystemExit) as error:
            self.run_script("router")
        self.assertEqual(error.exception.code, 1)
        self.factory.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_api_error_exits_unsuccessfully_without_writing_an_image(self):
        self.api_status = 403
        self.response = {
            "error": {"message": "Model unavailable", "type": "access_error"}
        }
        with self.assertRaises(SystemExit) as error:
            self.run_script("router")
        self.assertEqual(error.exception.code, 1)
        self.assertFalse((self.output / "router.png").exists())


if __name__ == "__main__":
    unittest.main()
