from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import socket
import stat
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "run_responses_api.py"
SPEC = importlib.util.spec_from_file_location("run_responses_api", SCRIPT)
assert SPEC and SPEC.loader
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class FakeResponse:
    def __init__(self, body: dict[str, object]) -> None:
        self.body = json.dumps(body).encode("utf-8")
        self.headers = {"x-request-id": "req_test"}

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def completed_response(marker: str = "SECRET_RESPONSE_BODY") -> dict[str, object]:
    return {
        "id": "resp_fixture_1234",
        "status": "completed",
        "completed_at": 1_800_000_000,
        "model": "gpt-5.6-sol",
        "reasoning": {"mode": "pro", "effort": "medium"},
        "output": [
            {
                "type": "web_search_call",
                "action": {
                    "sources": [
                        {"title": "Example", "url": "https://example.com/source"}
                    ]
                },
            },
            {"type": "reasoning", "summary": []},
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": marker},
                ],
            },
        ],
        "usage": {
            "input_tokens": 7,
            "input_tokens_details": {"cached_tokens": 1},
            "output_tokens": 12,
            "output_tokens_details": {"reasoning_tokens": 4},
            "total_tokens": 19,
        },
    }


class ResponsesApiBridgeTests(unittest.TestCase):
    def test_payload_is_stateless_pro_with_web_search(self) -> None:
        payload = bridge.build_request_payload(
            "TASK", "gpt-5.5-pro-2026-04-23", "xhigh"
        )
        self.assertEqual(payload["model"], "gpt-5.5-pro-2026-04-23")
        self.assertEqual(
            payload["reasoning"],
            {"mode": "pro", "effort": "xhigh", "summary": "auto"},
        )
        self.assertEqual(payload["input"], "TASK")
        self.assertIs(payload["store"], False)
        self.assertEqual(payload["tools"][0]["type"], "web_search")
        self.assertEqual(payload["tools"][0]["search_context_size"], "high")
        self.assertEqual(payload["include"], ["web_search_call.action.sources"])
        self.assertNotIn("previous_response_id", payload)
        self.assertNotIn("conversation", payload)

    def test_run_archives_body_but_stdout_remains_summary_only(self) -> None:
        marker = "SECRET_RESPONSE_BODY"
        observed_request = None

        def fake_urlopen(request: object, timeout: int) -> FakeResponse:
            nonlocal observed_request
            observed_request = request
            self.assertEqual(timeout, 30)
            return FakeResponse(completed_response(marker))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request_path = root / "request.txt"
            request_path.write_text("COMPLETE REQUEST", encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.object(bridge.urllib.request, "urlopen", fake_urlopen),
                mock.patch.object(bridge, "load_api_key", return_value=("test-key", "test")),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                exit_code = bridge.main(
                    [
                        "run",
                        "--request-file",
                        str(request_path),
                        "--output-dir",
                        str(root / "archive"),
                        "--timeout-seconds",
                        "30",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertNotIn(marker, stdout.getvalue())
            summary = json.loads(stdout.getvalue())
            response_path = Path(summary["response_path"])
            self.assertEqual(response_path.read_text(encoding="utf-8"), marker)
            self.assertEqual(Path(summary["request_path"]).read_text(), "COMPLETE REQUEST")
            for path in (response_path, Path(summary["request_path"]), Path(summary["metadata_path"])):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            sources_path = Path(summary["sources_path"])
            self.assertEqual(stat.S_IMODE(sources_path.stat().st_mode), 0o600)
            self.assertEqual(
                json.loads(sources_path.read_text(encoding="utf-8")),
                [{"title": "Example", "url": "https://example.com/source"}],
            )

            self.assertIsNotNone(observed_request)
            payload = json.loads(observed_request.data)
            self.assertIs(payload["store"], False)
            self.assertEqual(payload["reasoning"]["mode"], "pro")
            self.assertEqual(payload["tools"][0]["type"], "web_search")
            self.assertEqual(observed_request.get_header("Authorization"), "Bearer test-key")

    def test_timeout_is_ambiguous_and_never_retried(self) -> None:
        calls = 0

        def fail_once(*_: object, **__: object) -> object:
            nonlocal calls
            calls += 1
            raise socket.timeout()

        with mock.patch.object(bridge.urllib.request, "urlopen", fail_once):
            with self.assertRaises(bridge.BridgeError) as raised:
                bridge.invoke_api(
                    "TASK",
                    model="gpt-5.6",
                    effort="medium",
                    timeout_seconds=30,
                    api_key="test-key",
                )
        self.assertEqual(calls, 1)
        self.assertEqual(raised.exception.code, "REQUEST_STATE_AMBIGUOUS_NO_RETRY")

    def test_incomplete_body_read_is_ambiguous_and_never_retried(self) -> None:
        calls = 0

        class IncompleteResponse(FakeResponse):
            def read(self) -> bytes:
                raise bridge.http.client.IncompleteRead(b"partial")

        def incomplete_once(*_: object, **__: object) -> IncompleteResponse:
            nonlocal calls
            calls += 1
            return IncompleteResponse(completed_response())

        with mock.patch.object(bridge.urllib.request, "urlopen", incomplete_once):
            with self.assertRaises(bridge.BridgeError) as raised:
                bridge.invoke_api(
                    "TASK",
                    model="gpt-5.5-pro-2026-04-23",
                    effort="xhigh",
                    timeout_seconds=30,
                    api_key="test-key",
                )
        self.assertEqual(calls, 1)
        self.assertEqual(raised.exception.code, "REQUEST_STATE_AMBIGUOUS_NO_RETRY")

    def test_http_error_body_is_never_emitted(self) -> None:
        marker = "SECRET_ERROR_BODY"
        error = urllib.error.HTTPError(
            bridge.API_URL,
            400,
            "bad request",
            {"x-request-id": "req_fixture"},
            io.BytesIO(marker.encode("utf-8")),
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(bridge.urllib.request, "urlopen", side_effect=error),
            mock.patch.object(bridge, "load_api_key", return_value=("test-key", "test")),
            tempfile.TemporaryDirectory() as directory,
        ):
            request_path = Path(directory) / "request.txt"
            request_path.write_text("TASK", encoding="utf-8")
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = bridge.main(
                    [
                        "run",
                        "--request-file",
                        str(request_path),
                        "--output-dir",
                        str(Path(directory) / "archive"),
                    ]
                )
        self.assertEqual(exit_code, 1)
        self.assertNotIn(marker, stdout.getvalue() + stderr.getvalue())
        self.assertEqual(json.loads(stderr.getvalue())["http_status"], 400)

    def test_multiple_output_blocks_are_joined_without_reasoning(self) -> None:
        response = completed_response("first")
        response["output"].append(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": " second"}],
            }
        )
        self.assertEqual(bridge.extract_output_text(response), "first second")

    def test_refusal_only_response_is_archived_as_complete_output(self) -> None:
        response = completed_response()
        response["output"] = [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "refusal", "refusal": "Cannot comply."}],
            }
        ]
        self.assertEqual(bridge.extract_output_text(response), "Cannot comply.")

    def test_duplicate_archive_and_open_permissions_are_rejected(self) -> None:
        response = completed_response()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary = bridge.archive_response(
                "TASK",
                response,
                output_dir=root,
                model_requested="gpt-5.5-pro-2026-04-23",
                effort_requested="xhigh",
            )
            with self.assertRaises(bridge.BridgeError) as duplicate:
                bridge.archive_response(
                    "TASK",
                    response,
                    output_dir=root,
                    model_requested="gpt-5.5-pro-2026-04-23",
                    effort_requested="xhigh",
                )
            self.assertEqual(duplicate.exception.code, "ARCHIVE_TARGET_ALREADY_EXISTS")

            Path(summary["response_path"]).chmod(0o644)
            with self.assertRaises(bridge.BridgeError) as permissions:
                bridge.verify_archive(Path(summary["metadata_path"]))
            self.assertEqual(permissions.exception.code, "ARCHIVE_PERMISSIONS_TOO_OPEN")

    def test_tampered_metadata_is_rejected(self) -> None:
        response = completed_response()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary = bridge.archive_response(
                "TASK",
                response,
                output_dir=root,
                model_requested="gpt-5.5-pro-2026-04-23",
                effort_requested="xhigh",
            )
            metadata_path = Path(summary["metadata_path"])
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["sources_count"] = 99
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            metadata_path.chmod(0o600)
            with self.assertRaises(bridge.BridgeError) as tampered:
                bridge.verify_archive(metadata_path)
            self.assertEqual(tampered.exception.code, "ARCHIVE_SOURCES_INVALID")

    def test_incomplete_response_is_not_archived(self) -> None:
        response = completed_response()
        response["status"] = "incomplete"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(bridge.BridgeError) as raised:
                bridge.archive_response(
                    "TASK",
                    response,
                    output_dir=Path(directory),
                    model_requested="gpt-5.6",
                    effort_requested="medium",
                )
            self.assertEqual(raised.exception.code, "OPENAI_RESPONSE_NOT_COMPLETE")
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_missing_key_config_is_body_free(self) -> None:
        stdout = io.StringIO()
        with (
            mock.patch.object(bridge, "load_api_key", return_value=("", "missing")),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = bridge.main(["config"])
        self.assertEqual(exit_code, 2)
        self.assertEqual(json.loads(stdout.getvalue())["api_key"], "missing")


if __name__ == "__main__":
    unittest.main()
