# AI Agent 框架调研笔记

> 本文件用于记录调研过程中的核心发现、对比、结论。

---

## 一、核心概念（Phase 1 产出）

### 1.1 什么是 AI Agent？

**最简定义**：能感知环境 → 自主决策 → 调用工具 → 达成目标的 LLM 系统。

关键特征：
- **自主性**：能在没有人为逐步指示的情况下完成任务
- **工具使用**：能调用外部 API、代码、文件等
- **循环执行**：Observe → Think → Act 循环
- **目标驱动**：以最终目标为导向，而非单轮响应

### 1.2 ReAct 范式

**论文**：ReAct: Synergizing Reasoning and Acting in Language Models (Yao et al., 2022)

核心思想：让 LLM 在思考时交替输出
- **Thought**（推理）：当前应该做什么
- **Action**（行动）：调用什么工具
- **Observation**（观察）：工具返回的结果

```
Thought 1: 需要查询北京天气
Action 1: search("北京天气")
Observation 1: 晴，25°C
Thought 2: 用户可能想知道穿衣建议
Action 2: llm("基于25度晴天给出穿衣建议")
Observation 2: 建议穿薄外套
Final Answer: ...
```

### 1.3 Function Calling / Tool Use

让 LLM 输出结构化的"工具调用请求"，由代码层执行。

OpenAI / Anthropic / Google 都已原生支持。

关键点：
- Tool Schema：函数名、参数、描述、必填项
- 模型选择：模型决定是否调用、调哪个、传什么参数
- 路由层：开发者解析模型输出 → 真正执行 → 返回结果

### 1.4 Agent vs Chain

| 维度 | Chain | Agent |
|------|-------|-------|
| 执行路径 | 预定义、固定 | 动态、模型决定 |
| 控制流 | 代码层 | 模型层 |
| 适用场景 | 已知流程 | 开放任务 |
| 可调试性 | 高 | 中~低 |

### 1.5 Memory 三层

- **Working Memory**：当前对话上下文（消息历史）
- **Episodic Memory**：过去的交互事件（"上次我帮你做了..."）
- **Semantic Memory**：长期知识（用户偏好、事实）

Letta/MemGPT 在这块做得最深。

### 1.6 Multi-Agent 模式

- **Supervisor（监督者）**：一个主 Agent 分配任务给子 Agent
- **Debate（辩论）**：多个 Agent 互相质疑，提升答案质量
- **Crew（角色协作）**：每个 Agent 有固定角色，分工合作（CrewAI 主打）
- **GroupChat**：所有 Agent 在群里讨论（AutoGen 主打）

---

## 二、主流框架横向对比（Phase 2 产出）

> 待填充。每研究一个框架就补充一节。

### 2.1 LangChain / LangGraph

**定位**：瑞士军刀，组件化最强

**核心抽象**：
- `Tool`：工具定义
- `AgentExecutor`：运行 Agent 的循环
- LangGraph：用图（节点+边）描述状态流转，取代老的 AgentExecutor

**优点**：
- 生态最大，几乎所有模型/工具都有集成
- LangGraph 适合复杂多步骤工作流
- LCEL 表达式语言简洁

**缺点**：
- 抽象层多，学习曲线陡
- 老版本 API 变化频繁，文档碎片化

**适用场景**：复杂 RAG + Agent 工作流、需要细粒度控制

---

### 2.2 OpenAI Agents SDK

**定位**：OpenAI 官方推出的轻量 Agent 框架（2025 年发布，前身是 Swarm）

**核心抽象**：
- `Agent`：指令 + 工具 + 模型
- `Runner`：执行 Agent（同步/异步/流式）
- `Handoffs`：Agent 之间的任务交接
- `Guardrails`：输入/输出校验

**优点**：
- API 设计简洁，开箱即用
- 原生支持 OpenAI 模型
- 内置 tracing（可视化追踪）

**缺点**：
- 主要绑定 OpenAI 模型
- 多 Agent 模式相对单一

**适用场景**：基于 GPT 系列快速搭建 Agent

---

### 2.3 Anthropic Claude Agent SDK

**定位**：Anthropic 官方，让 Claude 具备完整的 Agent 能力

**核心抽象**：
- `ClaudeSDKClient`：与 Claude 服务通信
- `tools`：定义可用工具
- 内置 Computer Use（操作 GUI）
- MCP 客户端

**优点**：
- Claude 3.5+ Sonnet 在 Computer Use 上最强
- 工具调用稳定可靠
- MCP 协议原生支持
- 适合复杂长任务

**缺点**：
- 主要绑定 Claude 模型
- Python/TS 都有，但生态比 LangChain 小

**适用场景**：需要 Computer Use、需要工具调用高可靠性

---

### 2.4 AutoGen (Microsoft)

**定位**：多 Agent 对话框架，学术气息浓

**核心抽象**：
- `AssistantAgent`：LLM 驱动的 Agent
- `UserProxyAgent`：可执行代码的人类代理
- `GroupChat`：多 Agent 群聊

**优点**：
- 多 Agent 模式最丰富
- 适合研究类项目

**缺点**：
- API 较复杂
- 文档偏学术

**适用场景**：多 Agent 协作研究、原型验证

---

### 2.5 CrewAI

**定位**：角色扮演型多 Agent 协作，类比公司团队

**核心抽象**：
- `Agent`：角色 + 目标 + 背景故事
- `Task`：具体任务
- `Crew`：组织 Agents 协作
- `Process`：执行顺序（顺序/层级）

**优点**：
- 概念直观（像管理一个团队）
- 上手快
- 适合业务流程类 Agent

**缺点**：
- 灵活度不如 LangGraph
- 复杂场景下扩展性受限

**适用场景**：营销文案、研究报告、流程型多 Agent 任务

---

### 2.6 MCP (Model Context Protocol)

**定位**：**不是框架，而是协议标准**（Anthropic 2024 年开源）

类比：Agent 界的"USB-C"，统一工具/上下文接入方式

**核心概念**：
- **MCP Server**：提供工具/资源/提示
- **MCP Client**：使用工具
- **Tools / Resources / Prompts**：三类可暴露的能力

**优点**：
- 一次开发，多 Agent 框架都能用
- 成为行业事实标准的趋势明显
- Claude Desktop、Cursor、Zed 已内置支持

**缺点**：
- 仍在演进
- 老框架支持度不一

**影响**：**学 Agent 必学 MCP**。可以基于 MCP 写自己的工具，然后任何兼容 MCP 的客户端都能用。

---

### 2.7 Letta（前 MemGPT）

**定位**：主打长期记忆的 Agent 框架

**核心抽象**：
- 分层记忆：core memory / archival memory / recall memory
- 类似操作系统的虚拟内存分页思路

**优点**：
- 长期记忆最成熟
- 适合需要"记住"用户偏好/历史的场景

**缺点**：
- 概念重，入门门槛高

**适用场景**：个性化助手、长期陪伴类 Agent

---

### 2.8 Pydantic AI

**定位**：类型安全优先的 Agent 框架（基于 Pydantic 作者）

**核心抽象**：
- 强类型 Tool 定义
- `Agent` 类，Pydantic 风格

**优点**：
- 类型安全、IDE 友好
- 学习曲线平缓
- 依赖注入清晰

**缺点**：
- 生态较新（2024 年发布）

**适用场景**：Python 团队、需要类型安全的生产项目

---

## 三、对比汇总表

| 框架 | 学习曲线 | 灵活性 | 多 Agent | MCP 支持 | 长期记忆 | 模型绑定 |
|------|---------|--------|---------|---------|---------|---------|
| LangChain/LangGraph | 高 | 极高 | 中 | 通过插件 | 中 | 多模型 |
| OpenAI Agents SDK | 低 | 中 | 中（Handoffs） | 部分 | 弱 | OpenAI |
| Claude Agent SDK | 中 | 高 | 中 | 原生 | 弱 | Claude |
| AutoGen | 高 | 高 | 极强 | 通过插件 | 弱 | 多模型 |
| CrewAI | 低 | 中 | 强（角色协作） | 通过插件 | 弱 | 多模型 |
| Letta | 高 | 中 | 弱 | 通过插件 | **极强** | 多模型 |
| Pydantic AI | 低 | 中 | 弱 | 通过插件 | 弱 | 多模型 |

---

## 四、动手实践记录（Phase 3 产出）

> 待填充：每个框架实现同一个任务，记录代码量和体验。

---

## 五、最终选定与决策（Phase 4 产出）

> 待填充：选定框架 + 理由

---

## 六、ziva 集成方案（Phase 5 产出）

> 待填充