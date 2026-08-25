# Headless Responses API Mode

Use this mode when the host has no in-app Browser or a GUI Browser attempt fails definitively before Send. A GUI host with working Browser remains Browser-first. Authentication is resolved in this order: `OPENAI_API_KEY`, the private key file `~/.config/chatgpt-pro-bridge/openai-api-key`, then macOS Keychain service `chatgpt-pro-bridge-openai`. A private file must be owned by the current user and expose no group/other permissions. Never accept the key as a command argument, write it to a request/archive file, print it, or repurpose Codex or ChatGPT login credentials as API authentication.

## Exact execution profile

The bundled runner defaults to the quality-first profile requested for this bridge:

- model: `gpt-5.5-pro-2026-04-23`
- reasoning: `mode=pro`, `effort=xhigh`, `summary=auto`
- text: plain text with `verbosity=medium`
- Web Search: enabled, approximate user location, `search_context_size=high`
- storage: `store=false`

`OPENAI_PRO_BRIDGE_MODEL` may select `gpt-5.5-pro`, its `2026-04-23` snapshot, or a GPT-5.6 family model. `OPENAI_PRO_BRIDGE_EFFORT` may override the effort; GPT-5.5 Pro accepts `medium`, `high`, or `xhigh`. Keep the defaults when the user asks for reasoning similar to the configured Pro sample.

Every run is a new, independent API response. Pass the complete task envelope as the new user input; do not reuse a prior response ID, conversation, reasoning item, or encrypted reasoning blob. `store=false` intentionally prevents later API retrieval, because the exact final response and citations are written locally. This is the API equivalent of starting a fresh chat, but it does not create a ChatGPT sidebar conversation.

## Run

The worker owns the API call and archive end to end:

```sh
python3 scripts/run_responses_api.py config
python3 scripts/run_responses_api.py run \
  --request-file /absolute/path/to/complete-request.txt \
  --output-dir /absolute/path/to/archive
```

Stage the complete request file with mode `0600` when it contains private user context. The runner writes its durable archived request copy at `0600`.

The user explicitly asking the bridge to run, or explicitly configuring API as the fallback for a Browser-first run, authorizes one request through the already configured key; do not add a second plugin-specific confirmation step. Continue to follow any system or runtime authorization requirements. The request may incur API and Web Search charges.

The synchronous request timeout defaults to 30 minutes because Pro reasoning may take several minutes. Never retry automatically after a timeout, socket reset, body-read failure, or other ambiguous post-dispatch state: the first request may already be billable. Return the runner's fixed body-free error summary.

## Archive and output contract

On success, the runner creates four private `0600` files named from the validated `resp_*` ID:

- metadata JSON;
- exact complete request envelope;
- response-only text;
- extracted Web Search citation sources JSON.

The raw API response, reasoning items, encrypted reasoning, headers, and credentials are not persisted. The runner writes metadata last, verifies all four hashes and permissions, and prints one summary JSON object. Never read the response-only file in the bridge task unless the user explicitly asks to bring its content into context.

Report only the transport, response ID, requested and observed model/reasoning fields, archive paths, counts, hashes, usage, source count, and verification state. A missing key is `OPENAI_API_KEY_MISSING`; an ambiguous dispatched request is `REQUEST_STATE_AMBIGUOUS_NO_RETRY`.
