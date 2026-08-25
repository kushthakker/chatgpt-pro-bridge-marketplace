# ChatGPT Pro Bridge

Public Codex plugin marketplace for `chatgpt-pro-bridge`.

The plugin sends a complete context envelope through either a fresh signed-in ChatGPT Pro browser chat or a headless OpenAI Responses API request, then stores the verified response outside the agent context window. Browser credentials, API keys, and profile data are never included in this repository.

## Install

```sh
codex plugin marketplace add kushthakker/chatgpt-pro-bridge-marketplace --ref v0.1.0-codex.20260825114737
codex plugin add chatgpt-pro-bridge@kush-chatgpt-pro-bridge
```

GUI/browser mode remains the default when the Codex in-app Browser and its signed-in ChatGPT session work; API is only a pre-Send fallback there. Headless hosts default to API mode. API credentials may come from `OPENAI_API_KEY`, a user-owned `0600` file at `~/.config/chatgpt-pro-bridge/openai-api-key`, or macOS Keychain service `chatgpt-pro-bridge-openai`. API mode defaults to `gpt-5.5-pro-2026-04-23`, `reasoning.mode=pro`, `reasoning.effort=xhigh`, and high-context Web Search. API use is billed separately from a ChatGPT subscription.

The API runner uses `store=false`, starts an independent request every time, saves response text and extracted citations locally as private `0600` files, and never prints the response body to Codex.
