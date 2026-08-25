#!/usr/bin/env python3
"""Run one stateless OpenAI Responses API request and archive it without printing the body."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


API_URL = "https://api.openai.com/v1/responses"
KEYCHAIN_SERVICE = "chatgpt-pro-bridge-openai"
DEFAULT_KEY_FILE = Path("~/.config/chatgpt-pro-bridge/openai-api-key")
ALLOWED_MODELS = {
    "gpt-5.5-pro",
    "gpt-5.5-pro-2026-04-23",
    "gpt-5.6",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
}
ALLOWED_EFFORTS = {"none", "low", "medium", "high", "xhigh", "max"}
RESPONSE_ID = re.compile(r"^resp_[A-Za-z0-9_-]{4,200}$")
SAFE_ERROR_VALUE = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
USAGE_KEYS = {
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
}
PERSISTED_KEYS = {
    "api_credentials_included",
    "archive_contains_user_content",
    "completed_at",
    "format_version",
    "model_observed",
    "model_requested",
    "reasoning_effort_observed",
    "reasoning_effort_requested",
    "reasoning_mode_observed",
    "reasoning_mode_requested",
    "request_chars",
    "request_path",
    "request_sha256",
    "response_chars",
    "response_id",
    "response_path",
    "response_sha256",
    "response_status",
    "response_store_requested",
    "source",
    "sources_count",
    "sources_path",
    "sources_sha256",
    "usage",
    "web_search_context_size",
    "web_search_enabled",
}


class BridgeError(Exception):
    """A deliberately body-free error safe to emit to an agent context."""

    def __init__(
        self,
        code: str,
        *,
        http_status: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status
        self.request_id = safe_error_value(request_id)

    def summary(self) -> dict[str, object]:
        return {
            "ok": False,
            "error": self.code,
            "http_status": self.http_status,
            "request_id": self.request_id,
        }


def safe_error_value(value: object) -> str | None:
    if isinstance(value, str) and SAFE_ERROR_VALUE.fullmatch(value):
        return value
    return None


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def utc_timestamp(unix_seconds: object = None) -> str:
    if isinstance(unix_seconds, (int, float)) and not isinstance(unix_seconds, bool):
        value = datetime.fromtimestamp(unix_seconds, tz=timezone.utc)
    else:
        value = datetime.now(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def require_model(value: str) -> str:
    if value not in ALLOWED_MODELS:
        raise BridgeError("UNSUPPORTED_GPT_5_6_MODEL")
    return value


def require_effort(value: str, model: str) -> str:
    if value not in ALLOWED_EFFORTS:
        raise BridgeError("UNSUPPORTED_REASONING_EFFORT")
    if model.startswith("gpt-5.5-pro") and value not in {"medium", "high", "xhigh"}:
        raise BridgeError("UNSUPPORTED_REASONING_EFFORT_FOR_MODEL")
    return value


def build_request_payload(request_text: str, model: str, effort: str) -> dict[str, object]:
    if not request_text:
        raise BridgeError("EMPTY_REQUEST_ENVELOPE")
    model = require_model(model)
    return {
        "model": model,
        "reasoning": {
            "mode": "pro",
            "effort": require_effort(effort, model),
            "summary": "auto",
        },
        "input": request_text,
        "text": {"format": {"type": "text"}, "verbosity": "medium"},
        "tools": [
            {
                "type": "web_search",
                "user_location": {"type": "approximate"},
                "search_context_size": "high",
            }
        ],
        "include": ["web_search_call.action.sources"],
        "store": False,
    }


def load_api_key() -> tuple[str, str]:
    environment_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if environment_key:
        return environment_key, "environment"

    if os.environ.get("OPENAI_PRO_BRIDGE_DISABLE_HOST_KEY_LOOKUP") == "1":
        return "", "missing"

    key_file = Path(
        os.environ.get("OPENAI_PRO_BRIDGE_API_KEY_FILE", str(DEFAULT_KEY_FILE))
    ).expanduser()
    try:
        key_stat = key_file.stat()
        if key_stat.st_uid == os.getuid() and not key_stat.st_mode & 0o077:
            file_key = key_file.read_text(encoding="utf-8").strip()
            if file_key:
                return file_key, "private_file"
        else:
            return "", "private_file_permissions_invalid"
    except OSError:
        pass

    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                [
                    "security",
                    "find-generic-password",
                    "-a",
                    getpass.getuser(),
                    "-s",
                    KEYCHAIN_SERVICE,
                    "-w",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            result = None
        if result is not None and result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip(), "macos_keychain"

    return "", "missing"


def invoke_api(
    request_text: str,
    *,
    model: str,
    effort: str,
    timeout_seconds: int,
    api_key: str,
) -> dict[str, object]:
    if not api_key:
        raise BridgeError("OPENAI_API_KEY_MISSING")
    payload = json.dumps(
        build_request_payload(request_text, model, effort),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            request_id = response.headers.get("x-request-id")
            raw_response = response.read()
    except urllib.error.HTTPError as error:
        raise BridgeError(
            "OPENAI_API_HTTP_ERROR",
            http_status=error.code,
            request_id=error.headers.get("x-request-id") if error.headers else None,
        ) from None
    except (
        TimeoutError,
        socket.timeout,
        urllib.error.URLError,
        http.client.HTTPException,
        OSError,
    ):
        # The server may already have accepted a billable request. Never retry automatically.
        raise BridgeError("REQUEST_STATE_AMBIGUOUS_NO_RETRY") from None

    try:
        parsed = json.loads(raw_response)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BridgeError(
            "REQUEST_STATE_AMBIGUOUS_NO_RETRY",
            request_id=request_id,
        ) from None
    if not isinstance(parsed, dict):
        raise BridgeError("OPENAI_API_INVALID_RESPONSE", request_id=request_id)
    return parsed


def extract_output_text(response: dict[str, object]) -> str:
    output = response.get("output")
    if not isinstance(output, list):
        raise BridgeError("OPENAI_API_OUTPUT_MISSING")
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        if item.get("role") not in {None, "assistant"}:
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "output_text"
                and isinstance(block.get("text"), str)
            ):
                parts.append(str(block["text"]))
            elif (
                isinstance(block, dict)
                and block.get("type") == "refusal"
                and isinstance(block.get("refusal"), str)
            ):
                parts.append(str(block["refusal"]))
    text = "".join(parts)
    if not text:
        raise BridgeError("OPENAI_API_OUTPUT_TEXT_MISSING")
    return text


def optional_reasoning_value(response: dict[str, object], key: str) -> str | None:
    reasoning = response.get("reasoning")
    if not isinstance(reasoning, dict):
        return None
    value = reasoning.get(key)
    return value if isinstance(value, str) else None


def normalized_usage(response: dict[str, object]) -> dict[str, int | None]:
    usage = response.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    input_details = usage.get("input_tokens_details")
    if not isinstance(input_details, dict):
        input_details = {}
    output_details = usage.get("output_tokens_details")
    if not isinstance(output_details, dict):
        output_details = {}

    def integer(value: object) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    return {
        "input_tokens": integer(usage.get("input_tokens")),
        "cached_input_tokens": integer(input_details.get("cached_tokens")),
        "output_tokens": integer(usage.get("output_tokens")),
        "reasoning_tokens": integer(output_details.get("reasoning_tokens")),
        "total_tokens": integer(usage.get("total_tokens")),
    }


def extract_web_sources(response: dict[str, object]) -> list[dict[str, object]]:
    """Keep only reviewable citation fields, never the raw API response."""
    output = response.get("output")
    if not isinstance(output, list):
        return []
    sources: list[dict[str, object]] = []

    def add_source(candidate: object) -> None:
        if not isinstance(candidate, dict):
            return
        url = candidate.get("url")
        title = candidate.get("title")
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            return
        item: dict[str, object] = {"url": url}
        if isinstance(title, str) and title:
            item["title"] = title
        for key in ("start_index", "end_index"):
            value = candidate.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                item[key] = value
        if item not in sources:
            sources.append(item)

    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "web_search_call":
            action = item.get("action")
            if isinstance(action, dict) and isinstance(action.get("sources"), list):
                for source in action["sources"]:
                    add_source(source)
        if item.get("type") != "message" or not isinstance(item.get("content"), list):
            continue
        for block in item["content"]:
            if not isinstance(block, dict) or not isinstance(block.get("annotations"), list):
                continue
            for annotation in block["annotations"]:
                if isinstance(annotation, dict) and annotation.get("type") == "url_citation":
                    add_source(annotation)
    return sources


def stage(parent: Path, name: str, payload: str) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=parent, prefix=f".{name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def commit_write_once(payloads: dict[Path, str], metadata_path: Path) -> None:
    targets = list(payloads)
    parent = metadata_path.parent
    if any(target.parent != parent for target in targets):
        raise BridgeError("ARCHIVE_PATHS_MUST_SHARE_DIRECTORY")
    parent.mkdir(parents=True, exist_ok=True)
    if any(target.exists() for target in targets):
        raise BridgeError("ARCHIVE_TARGET_ALREADY_EXISTS")

    staged: dict[Path, Path] = {}
    committed: list[Path] = []
    try:
        for target, payload in payloads.items():
            staged[target] = stage(parent, target.name, payload)
        ordered = [target for target in targets if target != metadata_path] + [metadata_path]
        for target in ordered:
            os.link(staged[target], target)
            staged[target].unlink()
            committed.append(target)
    except BaseException:
        for target in committed:
            target.unlink(missing_ok=True)
        raise
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)


def archive_response(
    request_text: str,
    response: dict[str, object],
    *,
    output_dir: Path,
    model_requested: str,
    effort_requested: str,
) -> dict[str, object]:
    if response.get("status") != "completed":
        raise BridgeError("OPENAI_RESPONSE_NOT_COMPLETE")
    response_id = response.get("id")
    if not isinstance(response_id, str) or not RESPONSE_ID.fullmatch(response_id):
        raise BridgeError("OPENAI_RESPONSE_ID_INVALID")
    model_observed = response.get("model")
    if not isinstance(model_observed, str) or not model_observed:
        raise BridgeError("OPENAI_RESPONSE_MODEL_MISSING")

    response_text = extract_output_text(response)
    output_dir = output_dir.expanduser().resolve()
    metadata_path = output_dir / f"{response_id}.json"
    request_path = output_dir / f"{response_id}.request.txt"
    response_path = output_dir / f"{response_id}.response.txt"
    sources_path = output_dir / f"{response_id}.sources.json"
    sources = extract_web_sources(response)
    sources_payload = json.dumps(sources, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    metadata: dict[str, object] = {
        "api_credentials_included": False,
        "archive_contains_user_content": True,
        "completed_at": utc_timestamp(response.get("completed_at")),
        "format_version": 1,
        "model_observed": model_observed,
        "model_requested": model_requested,
        "reasoning_effort_observed": optional_reasoning_value(response, "effort"),
        "reasoning_effort_requested": effort_requested,
        "reasoning_mode_observed": optional_reasoning_value(response, "mode"),
        "reasoning_mode_requested": "pro",
        "request_chars": len(request_text),
        "request_path": str(request_path),
        "request_sha256": sha256(request_text),
        "response_chars": len(response_text),
        "response_id": response_id,
        "response_path": str(response_path),
        "response_sha256": sha256(response_text),
        "response_status": "completed",
        "response_store_requested": False,
        "source": "openai_responses_api",
        "sources_count": len(sources),
        "sources_path": str(sources_path),
        "sources_sha256": sha256(sources_payload),
        "usage": normalized_usage(response),
        "web_search_context_size": "high",
        "web_search_enabled": True,
    }
    commit_write_once(
        {
            request_path: request_text,
            response_path: response_text,
            sources_path: sources_payload,
            metadata_path: json.dumps(
                metadata, ensure_ascii=False, indent=2, sort_keys=True
            )
            + "\n",
        },
        metadata_path,
    )
    return verify_archive(metadata_path)


def verify_archive(metadata_path: Path) -> dict[str, object]:
    metadata_path = metadata_path.expanduser().resolve()
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise BridgeError("ARCHIVE_METADATA_INVALID") from None
    if not isinstance(metadata, dict) or set(metadata) != PERSISTED_KEYS:
        raise BridgeError("ARCHIVE_METADATA_FIELDS_INVALID")
    if metadata.get("format_version") != 1:
        raise BridgeError("ARCHIVE_VERSION_UNSUPPORTED")
    if metadata.get("source") != "openai_responses_api":
        raise BridgeError("ARCHIVE_SOURCE_INVALID")
    if metadata.get("api_credentials_included") is not False:
        raise BridgeError("ARCHIVE_CREDENTIAL_FLAG_INVALID")
    if metadata.get("archive_contains_user_content") is not True:
        raise BridgeError("ARCHIVE_CONTENT_FLAG_INVALID")
    if metadata.get("response_store_requested") is not False:
        raise BridgeError("ARCHIVE_STORE_FLAG_INVALID")
    response_id = metadata.get("response_id")
    if not isinstance(response_id, str) or not RESPONSE_ID.fullmatch(response_id):
        raise BridgeError("ARCHIVE_RESPONSE_ID_INVALID")
    if metadata.get("response_status") != "completed":
        raise BridgeError("ARCHIVE_RESPONSE_STATUS_INVALID")
    if metadata.get("reasoning_mode_requested") != "pro":
        raise BridgeError("ARCHIVE_REASONING_MODE_INVALID")
    model_requested = metadata.get("model_requested")
    effort_requested = metadata.get("reasoning_effort_requested")
    if not isinstance(model_requested, str) or not isinstance(effort_requested, str):
        raise BridgeError("ARCHIVE_REASONING_CONFIG_INVALID")
    require_model(model_requested)
    require_effort(effort_requested, model_requested)
    if not isinstance(metadata.get("model_observed"), str):
        raise BridgeError("ARCHIVE_MODEL_OBSERVED_INVALID")
    for key in ("reasoning_mode_observed", "reasoning_effort_observed"):
        if metadata.get(key) is not None and not isinstance(metadata.get(key), str):
            raise BridgeError("ARCHIVE_REASONING_OBSERVED_INVALID")
    completed_at = metadata.get("completed_at")
    if not isinstance(completed_at, str) or not RFC3339.fullmatch(completed_at):
        raise BridgeError("ARCHIVE_COMPLETED_AT_INVALID")
    try:
        parsed_completed_at = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    except ValueError:
        raise BridgeError("ARCHIVE_COMPLETED_AT_INVALID") from None
    if parsed_completed_at.tzinfo is None:
        raise BridgeError("ARCHIVE_COMPLETED_AT_INVALID")
    if metadata.get("web_search_enabled") is not True:
        raise BridgeError("ARCHIVE_WEB_SEARCH_FLAG_INVALID")
    if metadata.get("web_search_context_size") != "high":
        raise BridgeError("ARCHIVE_WEB_SEARCH_CONFIG_INVALID")
    usage = metadata.get("usage")
    if not isinstance(usage, dict) or set(usage) != USAGE_KEYS:
        raise BridgeError("ARCHIVE_USAGE_INVALID")
    if any(
        value is not None
        and (isinstance(value, bool) or not isinstance(value, int) or value < 0)
        for value in usage.values()
    ):
        raise BridgeError("ARCHIVE_USAGE_INVALID")

    for prefix in ("request", "response", "sources"):
        path_value = metadata.get(f"{prefix}_path")
        if not isinstance(path_value, str):
            raise BridgeError("ARCHIVE_CONTENT_PATH_INVALID")
        path = Path(path_value)
        if not path.is_absolute() or path.parent != metadata_path.parent:
            raise BridgeError("ARCHIVE_CONTENT_PATH_INVALID")
        expected_name = (
            f"{response_id}.{prefix}.txt"
            if prefix in {"request", "response"}
            else f"{response_id}.sources.json"
        )
        if path.name != expected_name:
            raise BridgeError("ARCHIVE_CONTENT_PATH_INVALID")
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            raise BridgeError("ARCHIVE_CONTENT_MISSING") from None
        expected_hash = metadata.get(f"{prefix}_sha256")
        if not isinstance(expected_hash, str) or not SHA256.fullmatch(expected_hash):
            raise BridgeError("ARCHIVE_HASH_INVALID")
        if sha256(text) != expected_hash:
            raise BridgeError("ARCHIVE_CONTENT_VERIFICATION_FAILED")
        if prefix != "sources" and len(text) != metadata.get(f"{prefix}_chars"):
            raise BridgeError("ARCHIVE_CONTENT_VERIFICATION_FAILED")
        if prefix == "sources":
            try:
                sources = json.loads(text)
            except json.JSONDecodeError:
                raise BridgeError("ARCHIVE_SOURCES_INVALID") from None
            if not isinstance(sources, list) or len(sources) != metadata.get("sources_count"):
                raise BridgeError("ARCHIVE_SOURCES_INVALID")
        if path.stat().st_mode & 0o077:
            raise BridgeError("ARCHIVE_PERMISSIONS_TOO_OPEN")
    if metadata_path.stat().st_mode & 0o077:
        raise BridgeError("ARCHIVE_PERMISSIONS_TOO_OPEN")

    return {
        "ok": True,
        "transport": "openai_responses_api",
        "response_id": metadata["response_id"],
        "response_status": metadata["response_status"],
        "model_requested": metadata["model_requested"],
        "model_observed": metadata["model_observed"],
        "reasoning_mode_requested": metadata["reasoning_mode_requested"],
        "reasoning_mode_observed": metadata["reasoning_mode_observed"],
        "reasoning_effort_requested": metadata["reasoning_effort_requested"],
        "reasoning_effort_observed": metadata["reasoning_effort_observed"],
        "metadata_path": str(metadata_path),
        "request_path": metadata["request_path"],
        "response_path": metadata["response_path"],
        "request_chars": metadata["request_chars"],
        "request_sha256": metadata["request_sha256"],
        "response_chars": metadata["response_chars"],
        "response_sha256": metadata["response_sha256"],
        "sources_path": metadata["sources_path"],
        "sources_count": metadata["sources_count"],
        "sources_sha256": metadata["sources_sha256"],
        "usage": metadata["usage"],
        "archive_verified": True,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run and archive a zero-body GPT-5.6 Pro Responses API request."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--request-file", required=True, type=Path)
    run_parser.add_argument("--output-dir", required=True, type=Path)
    run_parser.add_argument(
        "--model",
        default=os.environ.get(
            "OPENAI_PRO_BRIDGE_MODEL", "gpt-5.5-pro-2026-04-23"
        ),
    )
    run_parser.add_argument(
        "--effort", default=os.environ.get("OPENAI_PRO_BRIDGE_EFFORT", "xhigh")
    )
    run_parser.add_argument("--timeout-seconds", type=int, default=1800)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--metadata", required=True, type=Path)

    subparsers.add_parser("config")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "config":
            api_key, key_source = load_api_key()
            result = {
                "ok": bool(api_key),
                "transport": "openai_responses_api",
                "api_key": "present" if api_key else "missing",
                "api_key_source": key_source,
                "endpoint": API_URL,
                "model": os.environ.get(
                    "OPENAI_PRO_BRIDGE_MODEL", "gpt-5.5-pro-2026-04-23"
                ),
                "reasoning_mode": "pro",
                "reasoning_effort": os.environ.get(
                    "OPENAI_PRO_BRIDGE_EFFORT", "xhigh"
                ),
                "web_search": "enabled",
                "web_search_context_size": "high",
            }
        elif args.command == "verify":
            result = verify_archive(args.metadata)
        else:
            if not 1 <= args.timeout_seconds <= 3600:
                raise BridgeError("TIMEOUT_SECONDS_OUT_OF_RANGE")
            try:
                request_text = args.request_file.read_text(encoding="utf-8")
            except OSError:
                raise BridgeError("REQUEST_FILE_UNREADABLE") from None
            api_key, _ = load_api_key()
            response = invoke_api(
                request_text,
                model=args.model,
                effort=args.effort,
                timeout_seconds=args.timeout_seconds,
                api_key=api_key,
            )
            result = archive_response(
                request_text,
                response,
                output_dir=args.output_dir,
                model_requested=args.model,
                effort_requested=args.effort,
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("ok") else 2
    except BridgeError as error:
        print(json.dumps(error.summary(), sort_keys=True), file=sys.stderr)
        return 1
    except Exception:
        # Never allow a traceback or raw in-memory response to enter agent context.
        error = BridgeError("INTERNAL_ERROR_NO_BODY")
        print(json.dumps(error.summary(), sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
