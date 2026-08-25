#!/usr/bin/env python3
"""Write and verify a private, browser-credential-omitting ChatGPT archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


CONVERSATION_ID = re.compile(r"^[A-Za-z0-9:-]{8,128}$")
MODEL_SLUG = re.compile(r"^[a-z0-9._-]{2,128}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
INPUT_KEYS = {
    "conversation_id",
    "conversation_url",
    "prompt",
    "response",
    "model_slug",
    "model_verification",
    "completed_at",
    "source",
    "thread_kind",
    "reread_status",
    "reread_at",
    "reread_response_sha256",
}
REQUIRED_INPUT_KEYS = {
    "conversation_id",
    "conversation_url",
    "prompt",
    "response",
    "model_slug",
    "model_verification",
    "completed_at",
    "source",
    "thread_kind",
    "reread_status",
}
MODEL_VERIFICATIONS = {"observed_conversation_payload", "ui_only", "unverified"}
REREAD_STATUSES = {"verified", "mismatch", "unavailable"}
PERSISTED_KEYS = {
    "archive_contains_user_content",
    "browser_credentials_included",
    "completed_at",
    "conversation_id",
    "conversation_url",
    "format_version",
    "model_slug",
    "model_verification",
    "request_chars",
    "request_path",
    "request_sha256",
    "response_chars",
    "response_path",
    "response_sha256",
    "reread_at",
    "reread_response_sha256",
    "reread_status",
    "source",
    "thread_kind",
}


def require_string(record: dict[str, object], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


def require_rfc3339(value: str, key: str) -> None:
    if not RFC3339.fullmatch(value):
        raise ValueError(f"{key} must be an RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{key} must be an RFC 3339 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{key} must include a timezone")


def validate_conversation_fields(record: dict[str, object]) -> None:
    conversation_id = require_string(record, "conversation_id")
    if not CONVERSATION_ID.fullmatch(conversation_id):
        raise ValueError("conversation_id has an unexpected format")

    conversation_url = require_string(record, "conversation_url")
    parsed = urlparse(conversation_url)
    if parsed.scheme != "https" or parsed.netloc != "chatgpt.com":
        raise ValueError("conversation_url must be an https://chatgpt.com URL")
    if (
        parsed.path.rstrip("/") != f"/c/{conversation_id}"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("conversation_url must be the canonical URL for conversation_id")

    model_slug = require_string(record, "model_slug")
    if model_slug != "unverified" and not MODEL_SLUG.fullmatch(model_slug):
        raise ValueError("model_slug has an unexpected format")

    model_verification = require_string(record, "model_verification")
    if model_verification not in MODEL_VERIFICATIONS:
        raise ValueError("model_verification has an unsupported value")
    if model_verification == "unverified" and model_slug != "unverified":
        raise ValueError("an unverified model must use model_slug='unverified'")

    require_rfc3339(require_string(record, "completed_at"), "completed_at")
    if require_string(record, "source") != "chatgpt_in_app_browser":
        raise ValueError("source must be chatgpt_in_app_browser")
    if require_string(record, "thread_kind") not in {"chatgpt", "unknown"}:
        raise ValueError("thread_kind must be chatgpt or unknown")

    reread_status = require_string(record, "reread_status")
    if reread_status not in REREAD_STATUSES:
        raise ValueError("reread_status has an unsupported value")
    reread_at = record.get("reread_at")
    reread_hash = record.get("reread_response_sha256")
    if reread_status == "unavailable":
        if reread_at is not None or reread_hash is not None:
            raise ValueError("unavailable reread must not include reread evidence")
    else:
        if reread_status == "verified" and record["thread_kind"] != "chatgpt":
            raise ValueError("verified reread requires thread_kind='chatgpt'")
        if not isinstance(reread_at, str):
            raise ValueError("verified or mismatched reread requires reread_at")
        require_rfc3339(reread_at, "reread_at")
        if not isinstance(reread_hash, str) or not SHA256.fullmatch(reread_hash):
            raise ValueError("verified or mismatched reread requires a SHA-256 hash")


def validate_input(record: object) -> dict[str, object]:
    if not isinstance(record, dict):
        raise ValueError("record must be a JSON object")
    unknown = set(record) - INPUT_KEYS
    missing = REQUIRED_INPUT_KEYS - set(record)
    if unknown:
        raise ValueError(f"unsupported fields: {', '.join(sorted(unknown))}")
    if missing:
        raise ValueError(f"missing fields: {', '.join(sorted(missing))}")

    require_string(record, "prompt")
    require_string(record, "response")
    validate_conversation_fields(record)
    return dict(record)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
    parents = {target.parent for target in targets}
    if len(parents) != 1:
        raise ValueError("metadata, request, and response files must share one directory")
    parent = parents.pop()
    parent.mkdir(parents=True, exist_ok=True)
    existing = [target for target in targets if target.exists()]
    if existing:
        raise FileExistsError(f"archive target already exists: {existing[0]}")

    staged: dict[Path, Path] = {}
    committed: list[Path] = []
    try:
        for target, payload in payloads.items():
            staged[target] = stage(parent, target.name, payload)

        ordered = [target for target in targets if target != metadata_path] + [metadata_path]
        for target in ordered:
            temporary = staged[target]
            os.link(temporary, target)
            temporary.unlink()
            committed.append(target)

        try:
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    except BaseException:
        for target in committed:
            target.unlink(missing_ok=True)
        raise
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)


def save_bundle(
    record: dict[str, object],
    metadata_path: Path,
    request_path: Path,
    response_path: Path,
) -> dict[str, object]:
    metadata_path = metadata_path.expanduser().resolve()
    request_path = request_path.expanduser().resolve()
    response_path = response_path.expanduser().resolve()
    if len({metadata_path, request_path, response_path}) != 3:
        raise ValueError("metadata, request, and response paths must differ")

    request = str(record.pop("prompt"))
    response = str(record.pop("response"))
    response_hash = digest(response)
    reread_status = str(record["reread_status"])
    reread_hash = record.get("reread_response_sha256")
    if reread_status == "verified" and reread_hash != response_hash:
        raise ValueError("verified reread hash does not match the response")
    if reread_status == "mismatch" and reread_hash == response_hash:
        raise ValueError("mismatched reread hash unexpectedly matches the response")

    metadata: dict[str, object] = {
        **record,
        "reread_at": record.get("reread_at"),
        "reread_response_sha256": record.get("reread_response_sha256"),
        "format_version": 1,
        "browser_credentials_included": False,
        "archive_contains_user_content": True,
        "request_path": str(request_path),
        "request_chars": len(request),
        "request_sha256": digest(request),
        "response_path": str(response_path),
        "response_chars": len(response),
        "response_sha256": response_hash,
    }
    metadata_payload = json.dumps(metadata, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    commit_write_once(
        {
            request_path: request,
            response_path: response,
            metadata_path: metadata_payload,
        },
        metadata_path,
    )
    return metadata


def validate_saved(metadata_path: Path) -> dict[str, object]:
    metadata_path = metadata_path.expanduser().resolve()
    record = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(record, dict) or set(record) != PERSISTED_KEYS:
        raise ValueError("persisted metadata fields do not match format version 1")
    if record.get("format_version") != 1:
        raise ValueError("unsupported format_version")
    if record.get("browser_credentials_included") is not False:
        raise ValueError("browser_credentials_included must be false")
    if record.get("archive_contains_user_content") is not True:
        raise ValueError("archive_contains_user_content must be true")
    validate_conversation_fields(record)

    for prefix in ("request", "response"):
        path_value = record.get(f"{prefix}_path")
        if not isinstance(path_value, str):
            raise ValueError(f"{prefix}_path must be a string")
        path = Path(path_value)
        if not path.is_absolute() or path.parent != metadata_path.parent:
            raise ValueError(f"{prefix}_path must be beside the metadata file")
        text = path.read_text(encoding="utf-8")
        char_count = record.get(f"{prefix}_chars")
        if isinstance(char_count, bool) or not isinstance(char_count, int):
            raise ValueError(f"{prefix}_chars must be an integer")
        if char_count != len(text):
            raise ValueError(f"{prefix}_chars mismatch")
        expected_hash = record.get(f"{prefix}_sha256")
        if not isinstance(expected_hash, str) or not SHA256.fullmatch(expected_hash):
            raise ValueError(f"{prefix}_sha256 is invalid")
        if expected_hash != digest(text):
            raise ValueError(f"{prefix}_sha256 mismatch")

    if record["reread_status"] == "verified":
        if record["reread_response_sha256"] != record["response_sha256"]:
            raise ValueError("verified reread hash does not match stored response")
    return record


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Write or verify a private ChatGPT request/response archive."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    save_parser = subparsers.add_parser("save")
    save_parser.add_argument("--metadata", required=True, type=Path)
    save_parser.add_argument("--request", required=True, type=Path)
    save_parser.add_argument("--response", required=True, type=Path)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--metadata", required=True, type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "save":
        record = validate_input(json.load(sys.stdin))
        metadata = save_bundle(record, args.metadata, args.request, args.response)
        result = {
            "metadata_path": str(args.metadata.expanduser().resolve()),
            "request_path": metadata["request_path"],
            "response_path": metadata["response_path"],
            "request_sha256": metadata["request_sha256"],
            "response_sha256": metadata["response_sha256"],
        }
    else:
        metadata = validate_saved(args.metadata)
        result = {
            "verified": True,
            "metadata_path": str(args.metadata.expanduser().resolve()),
            "request_path": metadata["request_path"],
            "response_path": metadata["response_path"],
            "request_sha256": metadata["request_sha256"],
            "response_sha256": metadata["response_sha256"],
        }
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
