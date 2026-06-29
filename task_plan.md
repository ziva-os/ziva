# AI Agent 框架学习调研 Plan

## 🎯 目标

系统性地学习和调研当前主流 AI Agent 框架，理解核心设计理念、各框架优缺点，并能在 ziva 项目中实践至少一个 Agent 框架。

## 📅 时间规划

建议总周期：2~3 周（业余时间），每天 1~2 小时。

## 🗂️ 调研范围

### 主流框架（必看）

- [ ] **LangChain / LangGraph** —— 生态最大，组件化思维
- [ ] **OpenAI Agents SDK** —— OpenAI 官方推出的 Agent 框架（原 Swarm 升级版）
- [ ] **Anthropic Claude Agent SDK** —— Claude 原生 Agent 能力，Computer Use
- [ ] **AutoGen** (Microsoft) —— 多代理对话框架
- [ ] **CrewAI** —— 角色扮演型多 Agent 协作
- [ ] **MCP (Model Context Protocol)** —— Anthropic 提出的工具/上下文协议标准
- [ ] **Letta** (前 MemGPT) —— 长期记忆 Agent
- [ ] **Pydantic AI** —— 类型安全 Agent 框架（Python）

### 核心概念（底层）

- [ ] ReAct (Reason + Act) 范式
- [ ] Function Calling / Tool Use
- [ ] Planning & Reflection
- [ ] Memory 系统（短期/长期/情景记忆）
- [ ] Multi-Agent 协作模式（Supervisor、Debate、Crew）
- [ ] RAG 与 Agent 结合
- [ ] Human-in-the-loop

---

## 🪜 阶段规划

### Phase 1：基础理论（3~4 天）

**目标**：理解 Agent 的核心概念和经典论文

- [ ] 阅读 ReAct 论文（Yao et al., 2022）
- [ ] 阅读 Toolformer 论文要点
- [ ] 阅读 Anthropic 的 "Building Effective Agents" 文章
- [ ] 阅读 Lilian Weng 的 Agent 综述博客
- [ ] 整理核心概念脑图到 `findings.md`

**产出**：`findings.md` 第一部分（核心概念）

---

### Phase 2：框架横向调研（5~7 天）

**目标**：逐一过一遍主流框架，记录各自定位、优缺点、适用场景

- [ ] LangChain / LangGraph：核心抽象、LCEL、状态图
- [ ] OpenAI Agents SDK：Runner、Tools、Guardrails、Handoffs
- [ ] Claude Agent SDK：Computer Use、MCP 集成
- [ ] AutoGen：GroupChat、UserProxyAgent
- [ ] CrewAI：Role/Goal/Backstory、Tasks、Crew
- [ ] Letta：记忆系统设计
- [ ] Pydantic AI：类型安全思路

**产出**：每个框架一节总结到 `findings.md`，形成对比表

---

### Phase 3：动手实践对比（3~4 天）

**目标**：用 2~3 个框架实现同一个简单任务（推荐任务：联网搜索 + 写摘要 + 保存文件）

- [ ] 选择简单任务：基于 LangGraph / OpenAI Agents SDK / Claude Agent SDK 各实现一遍
- [ ] 对比代码量、学习曲线、可调试性
- [ ] 记录到 `findings.md` 对比章节

**产出**：`/scripts/agent-comparison/` 目录下三个实现版本

---

### Phase 4：选定框架深入（3~5 天）

**目标**：选定一个框架（推荐 LangGraph 或 Claude Agent SDK）深入学习

- [ ] 阅读官方文档全篇
- [ ] 学习高级特性（子图、人在环、持久化、流式输出）
- [ ] 实现一个稍复杂的 Agent（例如：自动研究某个主题 → 生成报告 → 保存到本地）

**产出**：一个可工作的 Agent 项目代码

---

### Phase 5：接入 ziva 项目（2~3 天）

**目标**：把 Agent 能力接入到现有的 ziva Desktop 项目中

- [ ] 分析 ziva 现有架构（`electron/`、`web/`、`src/`、`plugins/`）
- [ ] 评估在哪个层面集成最合理
- [ ] 设计集成方案（插件化？新模块？）
- [ ] 实现一个最小可用集成

**产出**：ziva 项目中可运行的 Agent 功能 + 文档

---

## ❓ 待决问题（开始调研前需要确认）

1. **Python 还是 TypeScript？** —— 多数框架有 Python SDK，部分有 TS（LangChain.js、Claude Agent SDK 有 TS 版）。建议 Python 优先。
2. **是否要本地模型支持？** —— Ollama / vLLM 等；如要，关注 LangChain 和 Letta。
3. **是否关注多模态 Agent？** —— Claude Computer Use、OpenAI CUA 等。
4. **预算：** LLM API 是否有额度？默认按有额度规划。

---

## 📁 文件组织

```
/Users/wangxinxin/code/ziva/
├── task_plan.md          ← 当前文件（进度跟踪）
├── findings.md           ← 调研笔记
├── progress.md           ← 会话日志
└── research/
    ├── papers/           ← 论文笔记
    ├── frameworks/       ← 各框架详细笔记
    └── experiments/      ← 实验代码
```

---

## ✅ 验收标准

- 能用自己的话讲清楚 ReAct、Function Calling、Multi-Agent 三大概念
- 能横向对比至少 5 个主流框架
- 有一个完整可运行的 Agent 项目
- 把 Agent 集成进 ziva 项目，至少跑通一个用例