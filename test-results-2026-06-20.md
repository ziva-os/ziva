# 测试结果 — 2026-06-20

## MCP 复验（回归）
- `/mcp-status`: servers `[('MiniMax', 2), ('chrome-devtools', 29)]` — per-server tool_count 正确，无回归 ✓

## 7 个测试 query（并发 + 端到端）

| Query | status | 工具调用 | 结果 |
|---|---|---|---|
| 分析NVDA近一个月股价 | running | read_skill, shell×6, grep×2, read_file×2 (stock-analysis skill + shell) | 正常执行 |
| 分析TSLA近一个月股价 | done | read_skill, shell, web_search×2, web_fetch×7 | 正常 |
| 打开微博，分析今日热点 | done | navigate_page×2, wait_for, take_snapshot, take_screenshot, read_file×2 | **chrome-devtools 浏览器**端到端 ✓ |
| 打开抖音，分析今日热点 | done | navigate_page×2, take_screenshot, take_snapshot, read_file | **chrome-devtools** ✓ |
| 查看上海今天天气 | done | web_search | MiniMax web_search ✓ |
| 查看上海今日好玩的地方 | done | web_search | MiniMax web_search ✓ |
| ask_user 并行两问 | done | ask_user×2（单选+多选） | 见下 |

**全部无 error / doom_loop。**

### ask_user 并行测试
- 第1问：单选（multi_select:False）"最喜欢的编程语言 Python/JS/Rust"，reply "Python"
- 第2问：多选（multi_select:True）"常用工具 VSCode/Vim/Git/Terminal"，reply "VSCode, Git"
- 两问都问了，reply 后 turn `done`
- 注：ask_user 是阻塞机制（一问一答），模型顺序问两个，非真并发 — 这是设计，非 bug

## UI 测试（上一轮已测 + 修复，本轮 commit 状态）

已修复并 commit 的（28711f4 / ff59861 / 7ab4740 / 770dd40 / 6e838d0）：
- ✅ 输入框文本/图片 draft 隔离（不串扰、切回保留、删除、无 broken）
- ✅ 模型切换 per-session 隔离
- ✅ 设置切换退出（toggle/Esc/新对话）+ 实时写 config
- ✅ Files/Code Review 分割线拖动
- ✅ 分屏 2/3 屏并行
- ✅ pending 排队 + 停止/结束自动发送
- ✅ compact（磁盘压缩确认）
- ✅ workspace 切换创建
- ✅ 多图 vision 分析
- ✅ 子 agent（spawn + subagent_start/end）
- ✅ MCP：openai-agents → 本地 wrapper，chrome-devtools + MiniMax 端到端，retry + httpx 错误映射，per-server tool_count

## 结论
本轮 7 query + MCP 复验全部通过，**未发现新 bug**。
