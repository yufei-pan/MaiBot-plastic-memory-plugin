# MaiBot Plugin Dev — Documentation Map

Read files with the Read tool when implementing or debugging. Paths are relative to workspace roots.

## Primary: `docs/develop/plugin-dev/`

| File | Read when |
|------|-----------|
| `index.md` | First visit; architecture (Host/Runner IPC), quick start, component overview |
| `vibe-coding.md` | **Always** before AI-assisted plugin work; boundaries, skeleton, prompts, checklist |
| `manifest.md` | Writing or validating `_manifest.json` |
| `lifecycle.md` | `on_load` / `on_unload` / `on_config_update`, config hot-reload |
| `tools.md` | `@Tool`, `ToolParameterInfo`, `core_tool`, return shapes |
| `commands.md` | `@Command`, regex `pattern`, return tuple |
| `hooks.md` | `@HookHandler`, blocking vs observe |
| `event-handlers.md` | `@EventHandler`, message/workflow events |
| `api-components.md` | `@API`, inter-plugin calls |
| `message-gateway.md` | `@MessageGateway`, platform adapters |
| `llmprovider.md` | `@LLMProvider`, custom model clients |
| `config.md` | `PluginConfigBase`, `Field`, WebUI schema |
| `actions.md` | Legacy `@Action` only — do not use for new plugins |
| `api-reference.md` | Quick SDK API index (links to detailed topics) |

## SDK reference: `maibot-plugin-sdk/docs/`

| File | Read when |
|------|-----------|
| `guide.md` | **Definitive API reference** — decorators, capability proxies, types, lifecycle, debugging, publishing (~1800 lines; read relevant sections) |
| `migration-guide.md` | Porting pre-IPC / legacy plugins |

## SDK source (when docs are insufficient)

| Path | Contents |
|------|----------|
| `maibot-plugin-sdk/maibot_sdk/__init__.py` | Public exports |
| `maibot-plugin-sdk/maibot_sdk/plugin.py` | `MaiBotPlugin` base class |
| `maibot-plugin-sdk/maibot_sdk/components.py` | Decorator implementations |
| `maibot-plugin-sdk/maibot_sdk/context.py` | `PluginContext` |
| `maibot-plugin-sdk/maibot_sdk/capabilities/` | Capability proxy stubs (one file per proxy) |
| `maibot-plugin-sdk/maibot_sdk/types.py` | `ToolParamType`, parameter/return types |
| `maibot-plugin-sdk/maibot_sdk/messages.py` | Message models |
| `maibot-plugin-sdk/tests/test_sdk.py` | Usage examples |

## MaiBot Host source (read-only unless permitted)

| Path | Contents |
|------|----------|
| `MaiBot/src/plugin_runtime/` | Host-side runtime: supervisors, RPC, component registry, hook dispatch |
| `MaiBot/src/plugins/built_in/` | Built-in plugin examples |
| `MaiBot/plugins/` | Third-party plugin install location (runtime) |

## Broader MaiBot docs: `docs/develop/`

| File | Read when |
|------|-----------|
| `index.md` | Tech stack, project structure, dev environment (`uv sync`, `uv run python bot.py`) |
| `architecture.md` | Runner/Worker process model, message pipeline overview |
| `architecture/message-pipeline.md` | How messages flow through hooks and HeartFlow |
| `architecture/maisaka-reasoning.md` | LLM planner, tool invocation context |
| `architecture/memory-system.md` | A-Memorix; relevant if plugin uses `ctx.knowledge` |
| `architecture/webui-internals.md` | Plugin management UI, config hot-reload |
| `contributing.md` | Main-program contribution rules, code style |

## User manual (behavior context)

| Path | Read when |
|------|-----------|
| `docs/manual/features/` | End-user feature descriptions (memory, message pipeline, etc.) |
| `docs/manual/webui/` | WebUI plugin management from user perspective |

## Topic → doc routing

```
New plugin task?
  → vibe-coding.md → index.md → (component doc)

_manifest.json error?
  → manifest.md → MaiBot plugin_runtime loader validation

Tool not discovered by LLM?
  → tools.md → architecture/maisaka-reasoning.md

Hook not firing?
  → hooks.md → MaiBot/src/plugin_runtime/ hook dispatch

ctx.* API signature?
  → maibot-plugin-sdk/docs/guide.md (§能力代理)
  → maibot_sdk/capabilities/<name>.py

Config not in WebUI?
  → config.md → PluginConfigBase + __ui_*__ attrs

Legacy plugin migration?
  → migration-guide.md → actions.md (compat only)
```

## External links

- SDK guide (GitHub): https://github.com/Mai-with-u/maibot-plugin-sdk/blob/main/docs/guide.md
- Plugin repo contributing: https://github.com/Mai-with-u/plugin-repo/blob/main/CONTRIBUTING.md
