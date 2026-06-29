# Claude Code + kimi-k2.6 Auto-Compact 问题

## 问题描述
使用 kimi-k2.6 模型时，`/compact` 命令报错：
```
Error during compaction: API Error: 400 Invalid request: 
Your request exceeded model token limit: 262144 (requested: 265358)
```

## 根本原因
- kimi-k2.6 上下文窗口：262144 tokens
- Claude Code 的 auto-compact 机制是为 Claude 模型设计的
- 对第三方模型没有可配置的 compact 阈值
- 当上下文超过限制时，连生成摘要的空间都没有

## 解决方案

### 1. 手动提前 compact
在上下文达到 ~200k tokens 时主动运行 `/compact`

### 2. 检查配置
```bash
claude config list | grep -i compact
```

### 3. 分段工作
- 把大任务拆分成多个小会话
- 每个会话处理独立的任务
- 完成后开启新会话

### 4. 保存上下文
如果会话卡住，手动保存重要信息到文件：
```bash
# 在新会话中引用
@context.md
```

## 当前配置（无 compact 相关设置）
```json
{
  "ANTHROPIC_MODEL": "kimi-k2.6",
  "ANTHROPIC_SMALL_FAST_MODEL": "kimi-k2.6"
}
```

## 状态
- [ ] 等待 Claude Code 支持第三方模型的 compact 阈值配置
- [ ] 临时方案：手动 compact
