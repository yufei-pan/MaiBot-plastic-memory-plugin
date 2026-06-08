# ~~塑料内存条~~ 便利贴笔记 (Plastic Memory)

一个 MaiBot 第三方插件，为麦麦提供一张**可以自己管理的便利贴笔记**（`my_memory.md`）。

麦麦现有的记忆系统不允许它直接写一段"便条"，并在每次思考/回复时随请求一起带上。这个插件就是给 LLM 的一张便利贴：它可以随时给自己留言备忘，并在之后的对话中看到这些内容。

过了81920小时并不会自动清空

## 功能特性

- **自动注入**：在每次 planner（规划）/ 时机判断 / replyer（回复）请求的**系统提示词之后**，自动把整张便利贴的内容注入进去，同时告知还剩多少可用空间（`size_limit - 当前字符数`）。
- **自管理工具**：麦麦可通过三个工具自行维护便利贴：
  - `append_note`：在便利贴末尾追加内容。
  - `rewrite_note`：用新内容完全覆盖整张便利贴。
  - `compact_notes`：主动压缩便利贴（同步阻塞）。
- **自动压缩**：当便利贴超过字符上限时，会自动触发一次基于 LLM 的压缩重写，让它回到上限以内。压缩时会以麦麦的人格与表达风格进行。

> 笔记内容建议使用 Markdown 书写，工具提示中也会这样引导麦麦，但插件**不强制**校验格式。

## 安装

1. 将本插件目录放入 MaiBot 的 `plugins/` 目录下，例如 `plugins/MaiBot-plastic-memory-plugin/`。
2. 启动 MaiBot，插件会被自动发现并加载。
3. 也可在 WebUI 的插件管理中查看、启用并编辑配置。

本插件除 `maibot-plugin-sdk` 外没有额外的第三方依赖。

## 所需能力（`_manifest.json`）

插件在 `_manifest.json` 中声明以下 Host 能力，用于压缩时读取全局人格配置并调用 LLM：

- **`config.get`** — 读取 `bot.nickname`、`personality.personality`、`personality.reply_style`
- **`llm.generate`** — 执行便利贴压缩重写

若 `capabilities` 为空，Host 不会为该插件签发能力令牌，后台/同步压缩调用 `ctx.config` 或 `ctx.llm` 时会报 `E_CAPABILITY_DENIED`。

## 工具说明

### `append_note(content)`

把 `content` 追加到便利贴末尾。如果内容结尾没有换行，会自动补一个换行。
追加后若总字符数超过上限，会在**后台异步**触发一次压缩（工具立即返回）。

### `rewrite_note(content)`

用 `content` **完全覆盖**整张便利贴，旧内容会被清空。
如果新内容超过上限，会在**后台异步**触发一次压缩。

### `compact_notes()`

主动压缩便利贴。与超限自动触发的压缩不同，本工具是**同步阻塞**的：会等压缩完成，并返回压缩后的字符数与剩余可用字符数。若当前未超限则不改动内容。

## 配置项（`config.toml`）

`[plugin]`

- `**enabled`** — 是否启用插件。默认 `true`。
- `**config_version`** — 配置版本。默认 `"1.0.0"`。

`[memory]`

- `**size_limit**` — 便利贴字符数上限（按**字符**计，不是字节；UTF-8 落盘后的字节数可能更大）。默认 `8192`。
- `**note_file`** — 便利贴文件名/路径；相对路径基于插件目录解析。默认 `"my_memory.md"`。
- `**inject_when_empty`** — 便利贴为空时是否仍注入提示。默认 `true`。
- `**inject_to_planner**` — 是否在 planner / 时机判断请求中注入便利贴。默认 `true`。
- `**inject_to_replyer**` — 是否在 replyer 回复请求中注入便利贴。默认 `true`。
- `**max_compact_attempts**` — 单次压缩中结果仍超限时允许递归重压缩的最大次数（防止死循环）。默认 `3`。
- `**compact_model**` — 执行压缩时使用的 LLM 模型任务名。默认 `"planner"`（即复用 planner 任务的模型）。
- `**compact_temperature**` — 执行压缩时的采样温度。默认 `0.3`。
- `**compact_max_tokens**` — 压缩 LLM 调用的最大 token 数。默认 `0`，表示自动按 `size_limit` 的**八倍**计算（为推理模型的 reasoning token 预留空间）；若手动设置的值小于 `size_limit`，会在日志中打印告警。
- `**hook_timeout_ms**` — 注入 Hook 处理器的超时时间（毫秒）。默认 `60000`（60 秒）。
- `**injection_template**` — 注入到系统提示词之后的模板。占位符：`{note}`、`{used}`、`{free}`、`{size_limit}`。
- `**compact_prompt_template**` — 压缩时发给 LLM 的提示词模板。占位符：`{nickname}`、`{personality}`、`{reply_style}`、`{size_limit}`、`{used}`、`{note}`。

> **关于 LLM 调用超时**：当前 SDK / 宿主的 LLM 能力（`ctx.llm.generate`）只接受 `model`、`temperature`、`max_tokens`，**没有暴露单次 LLM 调用的超时参数**，因此本插件无法自定义压缩调用的 LLM 超时。`hook_timeout_ms` 控制的是注入 Hook 处理器自身的超时，而非压缩 LLM 调用的超时。

## 工作原理

- 注入通过两个命名 Hook 完成：
  - `maisaka.planner.before_request`（同时覆盖 planner 主流程与"时机判断"子代理）；
  - `maisaka.replyer.before_model_request`（覆盖 replyer）。
  插件在这两个 Hook 中读取便利贴，并把它作为一条 `user` 消息插入到最后一条 `system` 消息之后。
- 压缩时，插件从宿主全局配置读取 `bot.nickname`、`personality.personality`、`personality.reply_style`，让 LLM 以麦麦的身份与风格重写笔记。

### 压缩时的 max_tokens 与长度重试

- **`compact_max_tokens = 0`（默认）**：自动使用 `size_limit × 8` 作为本次压缩 LLM 调用的初始 `max_tokens` 上限（推理模型可能在 `reasoning` 阶段消耗大量 token）。
- **`compact_max_tokens > 0`**：使用你配置的固定值；若小于 `size_limit`，会在日志中告警。
- **长度触顶重试**：每次压缩 LLM 调用后，插件会尝试判断是否因输出被截断而需要重试：
  - 若 Host 返回了 `finish_reason`（当前 MaiBot 能力层通常**尚未暴露**该字段），则优先依据 `finish_reason`（如 `length`、`max_tokens`）判断；
  - 否则使用启发式：若返回笔记字符数 **小于 `size_limit` 的 80%**，视为可能被截断。
- 若判定需要重试，插件会**临时将本次调用的 `max_tokens` 翻倍**并用相同 prompt **重试**，最多 **3** 次（加上首次调用，最多共 **4** 次）。若多次调用都未得到理想结果，最终**选用字符数最长的那次响应**写入便利贴。该放大仅作用于当前这一次压缩调用，不会写回 `config.toml`。

## 常见问题

**便利贴文件存在哪里？**
默认在插件目录下的 `my_memory.md`（可通过 `note_file` 修改）。该文件已加入 `.gitignore`，不会进入版本库。

**为什么 8192 字符的笔记，文件大小却更大？**
`size_limit` 按字符计；中文在 UTF-8 下每个字符约占 3 字节，因此文件字节数可能明显大于字符数，这是正常现象。

**压缩会一直循环吗？**
不会。压缩存在 `max_compact_attempts` 上限；即使多次压缩仍超限，也会写入"目前最短"的结果后停止。

**插件能读取 LLM 的 finish_reason 吗？**
当前 MaiBot 的 `ctx.llm.generate` 能力返回值通常**不包含** `finish_reason`。插件已实现对该字段的优先检测（以便 Host 将来暴露时自动生效），并在缺失时回退到「返回笔记少于 `size_limit` 的 80% 则视为可能被截断」的启发式；若判定需要重试，会临时翻倍 `max_tokens` 并重试（最多 3 次，共最多 4 次调用），最终选用最长响应。

## 许可证

MIT