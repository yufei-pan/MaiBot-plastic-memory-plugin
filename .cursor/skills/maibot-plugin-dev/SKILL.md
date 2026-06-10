---
name: maibot-plugin-dev
description: Develop MaiBot third-party plugins using maibot-plugin-sdk. Navigates workspace documentation (docs/develop/plugin-dev, SDK docs) and source repos (MaiBot, maibot-plugin-sdk). Use when writing, modifying, debugging, or reviewing MaiBot plugins, _manifest.json, plugin.py, @Tool/@Command/@HookHandler components, or when the user mentions MaiBot, 麦麦, MaiSaka, or maibot-plugin-sdk.
---

# MaiBot Plugin Development

MaiBot（麦麦 / MaiSaka）是基于 LLM 的交互式智能体。插件采用 **Host/Runner IPC** 架构：插件在独立子进程中运行，通过 msgpack RPC 与主进程通信。

## Workspace layout

This multi-repo workspace typically contains:

| Folder | Role |
|--------|------|
| `docs/` | VitePress 文档站点源码（首选插件开发文档） |
| `MaiBot/` | 主程序源码；`src/plugin_runtime/` 为插件运行时 |
| `maibot-plugin-sdk/` | SDK 源码；`docs/guide.md` 为 API 完整参考 |
| `plugins/<name>/` 或独立插件仓库 | 第三方插件目录 |

安装包名 `maibot-plugin-sdk`，导入名 `maibot_sdk`。

## Before writing code

1. **Read** `docs/develop/plugin-dev/vibe-coding.md` — AI 开发边界、骨架、检查清单
2. **Skim** `docs/develop/plugin-dev/index.md` — 架构概览与快速开始
3. **Consult topic docs** (see [reference.md](reference.md)) for the component you are implementing
4. **Look up API details** in `maibot-plugin-sdk/docs/guide.md` when types, signatures, or capability proxies are unclear
5. **Inspect Host implementation** in `MaiBot/src/plugin_runtime/` only when debugging IPC, loading, or Hook dispatch — do not modify without permission

## AI task briefing

Paste or internalize this context at the start of plugin tasks:

```text
你正在为 MaiBot 编写第三方插件。插件必须放在 plugins/<plugin-name>/ 下，不要修改 MaiBot 主程序代码，除非我明确许可。请使用 maibot-plugin-sdk，入口文件为 plugin.py，元信息文件为 _manifest.json。必须实现 on_load、on_unload、on_config_update 和 create_plugin。优先使用 @Tool、@Command、@HookHandler、@EventHandler、@API、@MessageGateway；不要给新插件使用 @Action。所有用户可见文本优先使用简体中文。请保持改动边界清晰，并给出测试方式。
```

## Development boundaries

- Plugin root: `plugins/<plugin-name>/` with `plugin.py`, `_manifest.json`, optional `config.toml`
- Factory: `create_plugin()` returns a `MaiBotPlugin` subclass
- Lifecycle: `on_load()`, `on_unload()`, `on_config_update()` are **required**
- Do **not** modify `MaiBot/src/`, `dashboard/`, root `.gitignore` without explicit permission
- Plugin-specific `.gitignore` goes inside the plugin directory
- Dependencies declared in `_manifest.json` (`dependencies`); sync `pyproject.toml` only when maintaining MaiBot itself
- Use `stream_id` from context; do not compute session IDs manually
- User-facing text: 简体中文

## Component selection

| Need | Decorator | Doc |
|------|-----------|-----|
| LLM-callable capability | `@Tool` | `docs/develop/plugin-dev/tools.md` |
| Slash command | `@Command` | `docs/develop/plugin-dev/commands.md` |
| Intercept/observe pipeline | `@HookHandler` | `docs/develop/plugin-dev/hooks.md` |
| Message/lifecycle events | `@EventHandler` | `docs/develop/plugin-dev/event-handlers.md` |
| Cross-plugin API | `@API` | `docs/develop/plugin-dev/api-components.md` |
| Platform adapter | `@MessageGateway` | `docs/develop/plugin-dev/message-gateway.md` |
| Custom LLM provider | `@LLMProvider` | `docs/develop/plugin-dev/llmprovider.md` |
| Legacy only | `@Action` | avoid for new plugins |

## Capability proxies (`self.ctx`)

`send`, `db`, `llm`, `config`, `message`, `chat`, `person`, `emoji`, `gateway`, `api`, `component`, `frequency`, `render`, `knowledge`, `tool`, `maisaka`, `logger` — full signatures in `maibot-plugin-sdk/docs/guide.md` §能力代理.

## Post-change checklist

- `_manifest.json`: `manifest_version` 2, semver, valid URLs, `host_application` + `sdk` version ranges
- `plugin.py`: imports only stdlib, third-party, `maibot_sdk`; lifecycle + `create_plugin()` present
- No secrets, absolute paths, or main-program edits
- README covers install, config, commands, testing

## Additional resources

- Full doc map and source-code pointers: [reference.md](reference.md)
- SDK migration from legacy plugins: `maibot-plugin-sdk/docs/migration-guide.md`
- MaiBot architecture (Host side): `docs/develop/architecture.md`, `MaiBot/src/plugin_runtime/`
- Plugin publishing: https://github.com/Mai-with-u/plugin-repo/blob/main/CONTRIBUTING.md
