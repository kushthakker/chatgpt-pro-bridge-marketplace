# ChatGPT Pro Bridge

Private Codex plugin marketplace for `chatgpt-pro-bridge`.

The plugin starts a fresh signed-in ChatGPT Pro browser chat, sends a complete context envelope, and stores the verified response outside the agent context window. Browser credentials and profile data are never included in this repository.

## Install

```sh
codex plugin marketplace add kushthakker/chatgpt-pro-bridge-marketplace --ref main
codex plugin add chatgpt-pro-bridge@kush-chatgpt-pro-bridge
```

The destination host must provide the Codex in-app Browser and have its own signed-in ChatGPT session. Installing the plugin does not transfer browser cookies or credentials between hosts.
