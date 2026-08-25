import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import path from "node:path";
import { fileURLToPath } from "node:url";

const CONVERSATION_ID = /^[A-Za-z0-9:-]{8,128}$/;
const ARCHIVER = fileURLToPath(new URL("./save_chat_record.py", import.meta.url));

function sha256(text) {
  return createHash("sha256").update(text, "utf8").digest("hex");
}

function requireText(value, name) {
  if (typeof value !== "string" || value.length === 0) {
    throw new TypeError(`${name} must be a non-empty string`);
  }
  return value;
}

function runPython(args, stdin = "") {
  return new Promise((resolve, reject) => {
    const child = spawn("python3", [ARCHIVER, ...args], {
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";

    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("error", reject);
    child.stdin.on("error", reject);
    child.on("close", (code) => {
      if (code !== 0) {
        reject(new Error(`archive command failed with exit ${code}: ${stderr.trim()}`));
        return;
      }
      try {
        resolve(JSON.parse(stdout));
      } catch (error) {
        reject(new Error(`archive command returned invalid JSON: ${error.message}`));
      }
    });
    child.stdin.end(stdin, "utf8");
  });
}

export async function archiveZeroBody({
  outputDir,
  conversationId,
  conversationUrl,
  requestText,
  responseText,
  modelSlug = "unverified",
  modelVerification = "unverified",
  completedAt = new Date().toISOString(),
  threadKind = "chatgpt",
  rereadStatus = "unavailable",
  rereadAt,
  rereadResponseText,
}) {
  requireText(outputDir, "outputDir");
  requireText(conversationId, "conversationId");
  if (!CONVERSATION_ID.test(conversationId)) {
    throw new TypeError("conversationId has an unexpected format");
  }
  requireText(conversationUrl, "conversationUrl");
  requireText(requestText, "requestText");
  requireText(responseText, "responseText");

  const absoluteOutputDir = path.resolve(outputDir);
  const metadataPath = path.join(absoluteOutputDir, `${conversationId}.json`);
  const requestPath = path.join(absoluteOutputDir, `${conversationId}.request.txt`);
  const responsePath = path.join(absoluteOutputDir, `${conversationId}.response.txt`);
  const responseHash = sha256(responseText);
  const record = {
    conversation_id: conversationId,
    conversation_url: conversationUrl,
    prompt: requestText,
    response: responseText,
    model_slug: modelSlug,
    model_verification: modelVerification,
    completed_at: completedAt,
    source: "chatgpt_in_app_browser",
    thread_kind: threadKind,
    reread_status: rereadStatus,
  };

  if (rereadStatus !== "unavailable") {
    requireText(rereadAt, "rereadAt");
    requireText(rereadResponseText, "rereadResponseText");
    record.reread_at = rereadAt;
    record.reread_response_sha256 = sha256(rereadResponseText);
  }

  const saved = await runPython(
    [
      "save",
      "--metadata",
      metadataPath,
      "--request",
      requestPath,
      "--response",
      responsePath,
    ],
    JSON.stringify(record),
  );
  const verified = await runPython(["verify", "--metadata", metadataPath]);
  if (saved.response_sha256 !== responseHash || verified.response_sha256 !== responseHash) {
    throw new Error("archive response hash did not match the in-memory response");
  }

  return {
    conversation_id: conversationId,
    conversation_url: conversationUrl,
    model_slug: modelSlug,
    model_verification: modelVerification,
    reread_status: rereadStatus,
    metadata_path: saved.metadata_path,
    request_path: saved.request_path,
    response_path: saved.response_path,
    request_chars: [...requestText].length,
    request_sha256: saved.request_sha256,
    response_chars: [...responseText].length,
    response_sha256: responseHash,
    archive_verified: verified.verified === true,
  };
}
