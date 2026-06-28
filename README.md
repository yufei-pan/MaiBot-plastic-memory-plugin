# ~~塑料内存条~~ · ~~便利贴笔记~~ · 自我演化接口 (Plastic Memory)

MaiBot 的记忆系统没法让麦麦在运行时反向改写自身——写一段会随每次 LLM 调用注入的演化指令，并在之后每次思考、回复时一起带上。这个插件提供的就是这条**自我演化接口**：插件负责造端口，麦麦往里写入**演化指令**。

- **全局自我演化指令**（`my_memory.md`）：（全局便利贴）所有聊天流共享同一份全局演化指令
- **本聊天流自我演化指令**（`chat_notes/<stream_id>.md`）：（聊天便利贴）仅在当前聊天流注入对应的演化指令

> [!NOTE]
> 过了 81920 小时也不会自动清空。

---

## 能做什么

**自动注入**  
在 planner（规划）、时机判断、replyer（回复）请求里，插件会在系统提示词之后插入演化指令，并附带已用/上限等用量信息。

**三个维护工具**  
麦麦可以用 `append_instruction`、`rewrite_instruction`、`compact_instructions` 向自我演化接口写入或维护演化指令。三个工具都支持可选参数 `scope`：

- `global`（默认）→ 全局自我演化接口
- `stream` → 仅当前聊天流的自我演化接口（stream id 由插件从上下文解析，**不能**指定其他聊天流）

**LLM 写入整理（默认开启，后台异步）**  
`append_instruction` / `rewrite_instruction` 默认会先将 planner 传入的实质正文（`content` / `instruction` / `note` / `text` 等）**原样落盘**，再在后台交给 **replyer** 模型整理并替换为演化指令正文——与超限 **compact** 相同，**不会阻塞工具返回**，因此不会触发 Host 60s 的 `plugin.invoke_tool` 超时。Host 注入的 `stream_id` 等会话上下文字段**不会**进入整理 prompt。若无实质正文则拒绝写入。后台整理失败时保留已落盘的原始正文。可通过 `llm_rewrite_writes = false` 关闭整理，仅直写落盘。

> [!NOTE]
> 从 v0.3.0 起 `llm_rewrite_writes` 默认 `true`。若你希望 planner 写入内容原样落盘（不经 LLM 整理），请在 `config.toml` 中显式设置 `llm_rewrite_writes = false`。

**自动压缩**  
演化指令超过字符上限时，会调用 **replyer** 模型按麦麦的人格与表达风格压缩重写。超限时 `append_instruction` / `rewrite_instruction` 会在后台异步触发压缩；也可以主动调用 `compact_instructions` 同步等待结果。

> [!TIP]
> 演化指令建议用 Markdown 书写（标题、列表等），工具提示里也会这样引导，但插件不强制校验格式。

> [!WARNING]
> 压缩与写入整理默认走 `replyer` 任务模型，可在配置里改成 `planner`、`utils` 等。若 `size_limit` 设得很大，又用的是思考模型，请注意 token 消耗。

---

## 安装

1. 把本仓库放到 MaiBot 的 `plugins/` 目录，例如 `plugins/MaiBot-plastic-memory-plugin/`
2. 启动 MaiBot，插件会自动加载
3. 也可在 WebUI → 插件管理 里启用、改配置

除 `maibot-plugin-sdk` 外无其他第三方依赖。

---

## 工具说明

### 参数 `scope`

- **`scope="global"`**（默认）— 操作 `my_memory.md` 中的全局演化指令
- **`scope="stream"`** — 操作当前聊天流自我演化接口中的演化指令；stream id 由插件从上下文解析，工具不接受 stream id 参数，无法改动其他聊天流

> [!NOTE]
> 实现层还静默接受 `scope="chat"` 作为 `stream` 的别名，但 LLM 工具描述与注入提示里不会提到这一点。

### `append_instruction(content="", scope="global", insert_after_string="")`

向自我演化接口写入演化指令。`content` 可省略；默认经 LLM 整理其余工具参数（见上文）。若结尾没有换行会自动补上。超过上限时后台异步压缩，工具立即返回。

- 省略 **`insert_after_string`** — 追加到演化指令**末尾**（与原先行为相同）
- 指定 **`insert_after_string`** — 插入到该字符串**第一次出现**的位置之后；找不到则不写入

### `rewrite_instruction(content="", scope="global")`

用整理后的内容 **完全覆盖**演化指令。`content` 可省略。超过上限时同样在后台异步压缩。

### `compact_instructions(scope="global")`

主动压缩演化指令。与超限自动压缩不同，本工具**同步阻塞**，会等压缩完成并返回当前字符数与剩余空间；未超限时不会改动内容。

---

## 配置（`config.toml`）

配置文件位于插件目录。也可在 WebUI 插件管理里编辑。下面按区块说明各字段含义与默认值。

### `[plugin]` — 插件开关

```toml
[plugin]
enabled = true
config_version = "1.4.0"
```

**`enabled`** — 是否启用插件  
**`config_version`** — 配置版本号

### `[memory]`

`[memory]` 下所有字段见下方完整示例；下文按用途分段说明。

```toml
[memory]
# ── 存储与上限 ──
size_limit = 8192
note_file = "my_memory.md"
per_chat_size_limit = 4096
per_chat_note_folder = "chat_notes"

# ── 注入 ──
inject_when_empty = true
inject_to_planner = true
inject_to_replyer = true
hook_timeout_ms = 60000
# 占位符：{nickname} {note} {used} {free} {size_limit} {stream_section}
# injection_template = """..."""          # planner / 时机判断
# replyer_injection_template = """..."""  # replyer（默认不提维护工具）
# 占位符：{stream_note} {stream_used} {stream_free} {stream_size_limit}
# stream_injection_section = """..."""

# ── 压缩 ──
max_compact_attempts = 3
compact_model = "replyer"
compact_temperature = 0.3
compact_max_tokens = 0          # 0 = size_limit × 8

# ── 写入整理 ──
llm_rewrite_writes = true
rewrite_model = "replyer"
rewrite_temperature = 0.3
rewrite_max_tokens = 0          # 0 = size_limit × 8
max_rewrite_attempts = 3
# 占位符：{nickname} {personality} {reply_style} {note_scope} {size_limit} {used} {note}
# compact_prompt_template = """..."""
```

#### 存储与上限

**`size_limit`** — 全局演化指令字符上限（按字符计，非字节）。默认 `8192`  
**`note_file`** — 全局自我演化接口存储路径；相对路径基于插件目录。默认 `my_memory.md`  
**`per_chat_size_limit`** — 本聊天流演化指令字符上限。默认 `4096`  
**`per_chat_note_folder`** — 本聊天流自我演化接口目录；每流一个 `<stream_id>.md`。默认 `chat_notes`

#### 注入

**`inject_when_empty`** — 演化指令为空时是否仍注入提示（让麦麦知道可以向接口写入指令）。默认 `true`  
**`inject_to_planner`** / **`inject_to_replyer`** — 是否在 planner / 时机判断 / replyer 中注入。默认均为 `true`  
**`hook_timeout_ms`** — 注入 Hook 处理器超时（毫秒）。默认 `60000`（**不是**压缩 LLM 的超时）  
**`injection_template`** — planner / 时机判断注入模板；含自我演化接口维护工具说明。默认把含 `{nickname}` 的说明放在【全局演化指令】之前，便于模型缓存  
**`replyer_injection_template`** — replyer 专用注入模板；默认仅提示参考先前写入的演化指令，不提及维护工具或自我演化接口定位（replyer 无工具调用能力）  
**`stream_injection_section`** — 本聊天流演化指令子模板，渲染后填入 `{stream_section}`

模板字符串较长，完整默认值见仓库内 `config.default.toml`。

> [!NOTE]
> 当前 `ctx.llm.generate` 未暴露单次 LLM 超时参数，插件无法单独配置压缩调用的超时。

#### 压缩

**`max_compact_attempts`** — 单次压缩仍超限时，最多递归重试次数。默认 `3`  
**`compact_model`** — 压缩使用的 LLM 任务名。默认 `replyer`  
**`compact_temperature`** — 压缩采样温度。默认 `0.3`  
**`compact_max_tokens`** — 压缩 `max_tokens`；`0` 表示按对应 `size_limit × 8` 自动计算。默认 `0`  
**`compact_prompt_template`** — 压缩时发给 LLM 的提示词模板（默认引导输出精炼 Markdown，保留标题/列表结构）

#### 写入整理

**`llm_rewrite_writes`** — `append_instruction` / `rewrite_instruction` 写入前是否经 LLM 整理。默认 `true`（v0.3.0 起；升级后行为变化，若需原样落盘请设为 `false`）  
**`rewrite_model`** — 写入整理使用的 LLM 任务名。默认 `replyer`  
**`rewrite_temperature`** — 写入整理采样温度。默认 `0.3`  
**`rewrite_max_tokens`** — 写入整理 `max_tokens`；`0` 表示按对应 `size_limit × 8` 自动计算（与 compact 相同）。默认 `0`  
**`max_rewrite_attempts`** — LLM 返回空时的重试次数；仍失败则回退原始参数合并。默认 `3`  
**`rewrite_prompt_template`** — 写入整理时发给 LLM 的提示词模板（默认引导输出 Markdown 正文，勿用代码块包裹整篇）

---

## 工作原理

注入靠两个 Hook：

- `maisaka.planner.before_request` — planner 主流程与时机判断
- `maisaka.replyer.before_model_request` — replyer

插件读取全局与本聊天流的演化指令，合成一条 `user` 消息，插在最后一条 `system` 消息之后。聊天流 id 从 Hook 上下文里的 `session_id` 解析。

压缩时会读取 `bot.nickname`、`personality.personality`、`personality.reply_style`，让 LLM 以麦麦的身份重写演化指令。

### 压缩时的 `max_tokens` 与重试

```toml
compact_max_tokens = 0   # 默认：初始 max_tokens = size_limit × 8
```

- **`compact_max_tokens = 0`** — 为推理模型的 reasoning token 留余量，自动按 `size_limit × 8` 计算
- **`compact_max_tokens > 0`** — 使用固定值；若小于 `size_limit`，日志会告警

**长度触顶重试** — 每次压缩后判断是否可能被截断：

- 优先看 Host 返回的 `finish_reason`
- 若无该字段：返回字符数 **< `size_limit` 的 80%** 时视为可能被截断

需要重试时，临时将 `max_tokens` 翻倍，最多重试 **3** 次（加首次共 **4** 次调用），最终取**最长**的那次响应写入。

---

## 常见问题

> **演化指令存在哪？**

- 全局：`my_memory.md`（可用 `note_file` 改路径）
- 本聊天流：`chat_notes/` 下，每个聊天流一个文件

以上文件已在 `.gitignore` 中，不会进版本库。

> **全局和本聊天流自我演化接口有什么区别？**

全局接口在所有聊天里都会注入其演化指令；本聊天流接口只在对应聊天流里出现，适合写入与该群/私聊相关的演化指令。

> **为什么 8192 字符的文件更大？**

`size_limit` 按**字符**计。中文 UTF-8 编码下每字约 3 字节，文件字节数大于字符数是正常现象。

> **压缩会死循环吗？**

不会。有 `max_compact_attempts` 上限；多次仍超限也会写入目前最短的结果后停止。

---

## 许可证

MIT
