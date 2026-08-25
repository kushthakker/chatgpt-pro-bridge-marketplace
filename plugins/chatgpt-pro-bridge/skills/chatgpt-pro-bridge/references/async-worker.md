# Asynchronous Worker Mode

Use this mode for every new bridge request initiated by a subagent-capable main or root agent. The main agent delegates even short calls so browser monitoring and response handling never occupy its context or checking loop. Inline execution is only the fallback for a spawned agent that is prohibited from creating descendants.

## Ownership

- Spawn one fresh dedicated worker subagent for each new bridge request. Do not reuse an unrelated or previously completed worker, and do not create a user-owned Codex task for this internal work.
- Keep that worker's identity stable for the whole request. Resume the same worker after confirmation, `GENERATION_STILL_ACTIVE`, or another explicitly resumable state; never replace it merely because the main agent moved on to other work.
- Use this delegation only from an agent that is authorized to create a subagent. A spawned worker performs the bridge inline and must not create a descendant.
- The worker exclusively owns the ChatGPT tab, clipboard attachment flow, completion monitoring, server-backed reread, and archive files. The main agent must not operate the same tab or run a second bridge worker concurrently.
- Keep shared writes collision-free: the worker receives one approved archive directory and lets the permanent ChatGPT conversation ID determine the three filenames.
- Preserve zero-body isolation. The worker returns only status, conversation URL and ID, model status, file paths, counts, hashes, and reread status—never request or response bodies.

## Delegation packet

Give the worker a self-contained packet containing:

- the exact complete request envelope;
- absolute image paths and verified MIME types, if any;
- the archive output directory;
- the requirement to create a new persistent ChatGPT chat with Pro visibly selected;
- the requirement to use clipboard paste for ChatGPT image attachments;
- the zero-body extraction and summary-only return contract;
- a prohibition on duplicate sends and on using `read_thread` in the main or worker context.

Do not send hidden system or developer instructions, unrelated conversation history, browser credentials, or implicit main-agent context.

## Send synchronization

1. The worker opens and verifies an empty new Pro chat, classifies the envelope and attachments under the Browser policy, enables completion monitoring, and calls `markHandoff()`.
2. For sensitive text or attachments, the worker must not read a file into the browser clipboard, paste, or type yet. It returns `AWAITING_SENSITIVE_SEND_CONFIRMATION` describing the destination, the specific data and filenames, why they are needed, and the grouped imminent actions: clipboard staging, paste/type, and Send. After the user confirms, resume the same worker to perform those actions and send exactly once without another pause.
3. For non-sensitive input, the worker may paste and verify attachments and fill the complete envelope. Immediately before Send, it calls `markHandoff()` again and returns `AWAITING_SEND_CONFIRMATION` with the destination, a concise description of the data, and attachment filenames.
4. The main agent asks for the applicable Browser-required action-time confirmation. This is the only expected human synchronization point; subagent delegation cannot bypass it.
5. After confirmation, resume the same worker with a follow-up instruction to complete the already described actions and send exactly once. Do not spawn a replacement worker or rebuild the chat unless the marked tab is unrecoverable.

## Background monitoring

- After Send, keep the worker running and let the main agent continue independent work. The main agent must not open the worker's tab, check ChatGPT completion UI, inspect archive files, or enter a repeated `wait`/status-polling loop.
- The worker monitors network completion signals in bounded intervals of at most 30 seconds, with the `Stop answering`/`Response actions` DOM state as fallback. Do not emit post-response DOM snapshots or response text.
- Set a monitoring deadline appropriate to the request; use 20 minutes when the packet gives no deadline. If the deadline expires while generation remains active, call `markHandoff()` again in the current worker turn immediately before returning `GENERATION_STILL_ACTIVE`, then allow the same worker to resume monitoring later. Do not cancel or resend.
- If the browser binding or tab becomes stale, recover only the worker's marked tab and inspect whether the conversation was already created before considering any retry. Never resend when completion state is ambiguous.
- On completion, the worker performs the zero-body server reread and archive flow, verifies the local archive, marks the permanent conversation tab as deliverable, and returns the summary-only result.
- The worker reports only meaningful state transitions: required confirmation, terminal success, terminal failure, `GENERATION_STILL_ACTIVE`, or `SEND_STATE_AMBIGUOUS`. It does not send per-poll updates.
- The main agent relies on the worker's completion notification and summary instead of checking whether output has arrived. If the user explicitly asks for status, take at most one compact immediate worker snapshot; do not begin a polling loop.

## Main-agent handoff

- Dispatch the worker asynchronously, then continue the user's independent work immediately.
- When the worker completes, it informs the main agent with the summary-only result. The main agent integrates that result at the next natural boundary without reading the response file.
- If no independent work remains and the user is waiting specifically for this result, one bounded wait is acceptable; repeated checking is not.

## Completion and failure

- Success requires one new ChatGPT conversation, one user turn, one completed assistant turn, a verified archive, and no response body in agent messages or tool output.
- If image paste produces no preview, stop after the single paste attempt and report the attachment blocker without trying the chooser.
- If the worker cannot prove whether Send occurred, call `markHandoff()` in the current turn, report `SEND_STATE_AMBIGUOUS`, and preserve the tab for inspection. Do not retry automatically.
- If generation fails definitively before producing an assistant turn, report the observed failure and preserve the conversation URL when available.
