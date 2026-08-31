# 配置指南

Ziva 的所有配置都在 `~/.ziva/config.yaml` 中。首次启动时会自动从模板创建一份，你只需要填入 API Key。

---

## 快速上手

```bash
# 查看当前配置
cat ~/.ziva/config.yaml

# 编辑配置（修改后重启 Ziva 生效）
# 在 Ziva 设置面板中修改，或直接编辑文件
```

完整的推荐配置示例见项目根目录 `.ziva/config.yaml.example`。

---

## 核心配置块

### 1. model — 当前使用的模型

```yaml
model:
  name: MiniMax-M3              # 模型名称（必须匹配下方 providers 中的模型）
  provider_name: MiniMax         # 供应商名称（解决重名模型冲突）
  max_tokens: 64000              # 最大输出 token 数
  thinking_mode: high            # 推理深度：disabled / low / medium / high / xhigh / max
  thinking_budget_tokens: 4000   # 推理 token 预算（部分模型支持）
```

> ⚠️ `provider_name` 是 v1.2.0 新增的关键字段。当多个供应商下有同名模型时，它用于精准路由。省略会导致歧义。

### 2. providers — 模型供应商列表

每个供应商支持 `openai_compatible` 或 `anthropic` 两种 API 类型：

```yaml
providers:
  # OpenAI 兼容格式（MiniMax、DeepSeek、GLM、OpenRouter 等）
  - name: MiniMax
    api_type: openai_compatible
    api_key: sk-your-key-here
    base_url: https://api.minimaxi.com/v1
    models:
      - name: MiniMax-M3
        capabilities:
          vision: true           # 是否支持图片输入

  # Anthropic 兼容格式（Kimi、智谱 GLM 等）
  - name: Kimi
    api_type: anthropic
    api_key: sk-your-key-here
    base_url: https://api.kimi.com/coding/
    models:
      - name: kimi-k2.6
        capabilities:
          vision: true
```

**常见供应商 base_url 参考**：

| 供应商 | api_type | base_url |
|--------|----------|----------|
| MiniMax | openai_compatible | `https://api.minimaxi.com/v1` |
| Kimi (月之暗面) | anthropic | `https://api.kimi.com/coding/` |
| 智谱 GLM | anthropic | `https://open.bigmodel.cn/api/anthropic` |
| OpenCode 聚合 | openai_compatible | `https://opencode.ai/zen/go/v1` |

### 3. mcp — MCP 服务器

```yaml
mcp:
  enabled: true
  servers:
    chrome-devtools:             # 浏览器自动化（强烈推荐）
      type: local
      command: npx -y chrome-devtools-mcp --browserUrl http://127.0.0.1:9222
      environment: {}
      enabled: true
    MiniMax:                     # MiniMax 搜索 + 图片理解
      type: local
      command: uvx minimax-coding-plan-mcp
      environment:
        MINIMAX_API_KEY: sk-your-key
        MINIMAX_API_HOST: https://api.minimaxi.com
      enabled: true
```

### 4. approval — 审批策略

```yaml
approval:
  policy: full-auto              # full-auto（读写分离审批） / auto（全自动）
  allow_without_prompt: []       # 免审批工具列表
```

### 5. agents — 子 Agent 定义

```yaml
agents:
  explore:                       # 只读搜索 agent
    instructions: |
      You are a read-only exploration agent...
    tools: [list, grep, glob, read_file, read_skill]
    background: false
  general-purpose:               # 全能 agent（可写可执行）
    instructions: |
      You are a general-purpose agent...
    tools: [read_file, write_file, edit_file, grep, glob, list, shell, ...]
    background: false
```

---

## 其他配置块

### tool — 工具白/黑名单
```yaml
tool:
  allow: []                      # 空 = 全部允许
  deny: []                       # 黑名单优先
  max_rounds: 0                  # 0 = 无限轮
```

### skill — 技能路径
```yaml
skill:
  enabled: []                    # 启用的技能名列表
  extra_paths:                   # 技能搜索路径
    - ~/.ziva/skills
    - ~/.agents/skills
```

### memory — 上下文记忆
```yaml
memory:
  backend: markdown              # markdown（单文件 MEMORY.md）
  context_window_tokens: 200000  # 上下文窗口大小
```

### sandbox — 沙箱
```yaml
sandbox:
  mode: 'off'                    # off / docker / chroot
  writable_dirs: []
  blocked_commands: []
```

### ui — 界面
```yaml
ui:
  lang: zh                       # zh / en
```

---

## 通过设置面板修改

除了直接编辑 YAML，你也可以在 Ziva 界面中点击右上角 **⚙️ 设置** 按钮修改配置。设置面板支持：

- Provider / Model 增删改
- 默认模型选择
- 审批策略切换
- MCP 服务器管理
- Hooks 配置
- 自定义 System Prompt

保存后改动会**同步到后端运行时**，无需重启（v1.2.0+ 修复）。
