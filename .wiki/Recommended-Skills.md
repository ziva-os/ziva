# 推荐 Skills

Skills 是 Ziva 的扩展能力模块，位于 `~/.ziva/skills/` 或 `~/.agents/skills/` 目录下。每个 Skill 是一个包含 `SKILL.md` 的目录，定义了 AI 在特定场景下的行为规范和可用工具。

---

## Skill 机制

### 安装位置
```
~/.ziva/skills/          # 用户级 Skills
~/.agents/skills/        # 全局 Skills
./plugins/               # 项目级 Skills（随项目走）
```

### 启用方式
Skills 默认全部可用（通过 `read_skill` 工具加载详情）。你也可以在 config.yaml 中显式启用：

```yaml
skill:
  enabled:
    - diagnosing-bugs
    - code-review
  extra_paths:
    - ~/.ziva/skills
    - ~/.agents/skills
```

---

## 精选推荐

### 开发工作流

| Skill | 用途 | 触发场景 |
|-------|------|---------|
| **tdd** | 测试驱动开发 | "用 TDD 实现..."、"red-green-refactor" |
| **diagnosing-bugs** | 系统性 Bug 诊断 | "诊断这个 bug"、"为什么报错" |
| **code-review** | 代码审查 | "审查这个 PR"、"review since main" |
| **implement** | 按 spec 实现 | "实现这个 ticket"、"按设计文档做" |
| **brainstorming** | 方案探索 | 任何创造性工作开始前 |

### 文档与写作

| Skill | 用途 |
|-------|------|
| **doc-coauthoring** | 协作撰写文档、提案、技术规范 |
| **internal-comms** | 内部沟通文档（周报、FAQ、事故报告等） |
| **research** | 针对高可信来源做调研并产出 Markdown |

### 文件处理

| Skill | 用途 |
|-------|------|
| **pdf** | PDF 读取、合并、拆分、OCR |
| **xlsx** | Excel/CSV/TSV 读写、公式、图表 |
| **docx** | Word 文档创建与编辑 |
| **pptx** | PowerPoint 演示文稿 |

### 前端与设计

| Skill | 用途 |
|-------|------|
| **frontend-design** | 前端 UI 设计指导 |
| **canvas-design** | 海报、图表等静态视觉设计 |
| **web-artifacts-builder** | 复杂 HTML 组件（React + Tailwind + shadcn/ui） |

### 架构与规划

| Skill | 用途 |
|-------|------|
| **planning-with-files** | Manus 风格的文件化任务规划 |
| **codebase-design** | 模块接口设计、深度模块化 |
| **domain-modeling** | 领域模型与架构决策记录 |
| **grill-me** | 对方案进行无情的压力测试 |

### 媒体创作

| Skill | 用途 |
|-------|------|
| **hyperframes** | 视频与动画制作（核心入口） |
| **motion-graphics** | 短动态图形 |
| **algorithmic-art** | p5.js 生成艺术 |
| **slack-gif-creator** | Slack 优化 GIF |

---

## 安装新 Skill

### 方式 1：直接放入目录

```bash
# 将 Skill 目录放入 ~/.ziva/skills/
cp -r my-skill ~/.ziva/skills/my-skill
```

Skill 目录结构：
```
my-skill/
├── SKILL.md          # 必须，Skill 定义文件
├── scripts/          # 可选，辅助脚本
└── references/       # 可选，参考资料
```

### 方式 2：使用 Skill 安装工具

如果安装了 `clawdhub` CLI：
```bash
clawdhub install <skill-name>
```

### 方式 3：创建自定义 Skill

使用 `skill-creator` skill 来引导创建：
```
请帮我创建一个新 skill，用于...
```

---

## Skill 与 System Prompt

Ziva 的 System Prompt 会动态注入已安装 Skill 的列表和路径（v1.2.0+）。这意味着 AI 知道哪些 Skill 可用，并能通过 `read_skill` 工具按需加载完整定义。

你不需要手动告诉 AI 使用某个 Skill——只要描述你的需求，AI 会自动匹配并加载合适的 Skill。
