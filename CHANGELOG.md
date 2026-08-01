# Changelog

本文件记录 MaiBot-plastic-memory-plugin（可塑性记忆）的版本变更。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [0.4.4] - 2026-08-01

### 变更

- 将带注释的配置模板改为 `config.default.toml`，运行期 `config.toml` 不再入库
- 在 `create_plugin` / `on_load` 中从模板补齐或恢复 Runner 生成的空壳配置

## [0.4.3] - 2026-07-20

### 新增

- `[memory] llm_rpc_timeout_ms`：后台 `llm.generate`（rewrite / compact）的 cap.call RPC 超时（毫秒），默认 120000；实际取 `max(本值, hook_timeout_ms)`；compact 此前未传超时会命中 Host 30s 默认；`config_version` 升至 1.6.0

## [0.4.2] - 2026-07-11

### 修复

- WebUI 清空可选数值 / 布尔字段时按默认值处理，避免空字符串触发 pydantic 校验错误

## [0.4.1] - 2026-07-10

### 变更

- 默认 compact / rewrite prompt 以具名 AI 生命体的「可塑性记忆」模块自居；旧身份表述保留为 legacy，已有默认配置自动跟随

## [0.4.0] - 2026-07-03

### 变更

- 注入块去掉冗长自我演化前言，仅保留【自我回馈全局/本聊天流记录】标题、正文与用量计数
- 接口指引与写入注意仅保留在 append / rewrite / compact 工具描述中，缩小每次请求体积
- 旧冗长默认模板保留为 legacy；`config_version` 升至 1.5.0

## [0.3.3] - 2026-06-27

### 变更

- rewrite / compact 默认模板引导结构化 Markdown（面向 `.md` 记忆文件），同时禁止围栏代码块包裹

## [0.3.2] - 2026-06-27

### 变更

- append / rewrite 先立即落盘原始内容，再由 replyer 异步润色，避免同步 `cap.call` 重试触发 `plugin.invoke_tool` 60s 超时

## [0.3.1] - 2026-06-27

### 修复

- 阻止 Host 上下文泄漏进 LLM rewrite prompt
- 空写入护栏、非阻塞 compact 与安全回写、读取错误时部分注入
- 明确 LLM 回退提示；`NoteStore` I/O 错误完整上浮

## [0.3.0] - 2026-06-27

### 变更

- 默认通过 replyer 归一化 append / rewrite 写入（`llm_rewrite_writes`），带重试与合并回退
- compact / rewrite 模型改为 replyer；`content` 可选
- 运行时代码中剩余「便利贴」用语替换为「演化指令」

## [0.2.0] - 2026-06-27

### 变更

- 提示与文档改以「自我演化接口 / 演化指令」表述，替代便利贴语义
- `append_note` / `rewrite_note` / `compact_notes` 重命名为 `append_instruction` / `rewrite_instruction` / `compact_instructions`，运行时迁移旧模板

## [0.1.9] - 2026-06-13

### 修复

- 升级时不再剥离配置键，避免 Runner 清空 `config.toml`
- 1.2.0 烘焙值仅在运行时解析；WebUI 重置后恢复带注释的 `config.default.toml`

## [0.1.8] - 2026-06-13

### 修复

- 持久化配置时省略 `None`，兼容 tomlkit

## [0.1.7] - 2026-06-13

### 变更

- 未设置的配置字段跟随代码默认值，便于升级改内置值而无需用户改 `config.toml`
- 从 1.2.0 迁移时保留用户自定义项

## [0.1.6] - 2026-06-11

### 变更

- 拆分 replyer 注入模板（相对 planner）：去掉工具维护与次级指令表述（replyer 不能调工具或改笔记）

## [0.1.5] - 2026-06-11

### 变更

- 明确笔记为可自修改的次级指令；更新注入模板与 append / rewrite 工具描述

## [0.1.4] - 2026-06-11

### 新增

- `append_note` 支持 `insert_after_string`，在全局或流作用域笔记中按首次匹配锚定插入（默认仍追加到末尾）

## [0.1.3] - 2026-06-10

### 新增

- 按聊天流作用域的笔记（`chat_notes/`）、作用域感知工具、合并注入与 nickname 占位符

### 文档

- 精简默认 prompt 并刷新 README

## [0.1.2] - 2026-06-07

### 新增

- 添加 MIT LICENSE

## [0.1.1] - 2026-06-07

### 变更

- 更新 manifest `id` 与仓库 URL

## [0.1.0] - 2026-06-07

### 新增

- 首次发布：可自管理的粘性笔记（可塑性记忆），含 compact 鉴权与推理模型长度重试改进
