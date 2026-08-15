# 常见问题

---

## 安装与启动

### Q: 启动后白屏 / 后端无法连接

检查后端日志：
```bash
cat ~/.ziva/backend.log | tail -50
```

常见原因：
- Python 版本不对（需要 3.10+，打包需要 3.11）
- 依赖未安装完整（`pip install -e ".[all]"`）
- 端口 4097 被占用（`lsof -i :4097`）

### Q: 首次打开 DMG 提示"无法验证开发者"

```bash
# 移除隔离属性
xattr -dr com.apple.quarantine /Applications/Ziva.app

# 或右键 → 打开
```

### Q: 国内安装 Electron 依赖超时

```bash
export ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/
export ELECTRON_BUILDER_BINARIES_MIRROR=https://npmmirror.com/mirrors/electron-builder-binaries/
cd electron && npm install
```

---

## 模型与 API

### Q: `Error: Model not listed in any provider`

模型名称不匹配。检查 config.yaml 中 `model.name` 是否精确匹配 `providers` 下某个供应商的 `models[].name`。

如果多个供应商有同名模型，必须设置 `model.provider_name` 来消歧（v1.2.0+）。

### Q: `peer closed connection without sending complete message body`

服务端在流式响应中途关闭了连接（常见于 DeepSeek 长输出场景）。

v1.2.0+ 已修复——Ziva 会保留已接收的部分内容，优雅结束流式读取，不再导致整轮对话失败。

如果频繁发生：
- 检查网络稳定性
- 尝试减少 `max_tokens`
- 切换到其他模型或供应商

### Q: 设置面板保存后不生效

v1.2.0 已修复。之前版本存在配置保存后前端 DOM 不刷新的问题。

### Q: 新增 Provider 后默认模型被篡改

v1.2.0 已修复。之前版本的保存逻辑会硬编码 `newProviders[0].models[0]` 作为默认模型，导致新增供应商时覆盖已有设置。

---

## 浏览器

### Q: 地址栏输入 `file://` 跳转到 Google 搜索

v1.2.0 已修复。之前版本的 URL 判断逻辑只识别 `http(s)://`，其他协议被误判为搜索关键词。

### Q: 浏览器标签页抢占 Ziva 界面焦点

v1.2.0 已修复。现在通过 localStorage 持久化活动标签状态，刷新或重开窗口时精准恢复。

### Q: Chrome 没有自动启动

确认 Chrome 安装在标准路径：
```bash
ls "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
```

如果没有，安装：
```bash
brew install --cask google-chrome
```

---

## 文件操作

### Q: `read_file` / `write_file` 报路径错误

v1.2.0 已修复 `~` 路径展开问题。之前版本 `Path("~/.ziva/...")` 不会自动展开为绝对路径。

如果仍然报错，确认文件确实存在：
```bash
ls -la ~/.ziva/your-file
```

---

## 子 Agent

### Q: 子 Agent 报 `Model not listed in any provider`

v1.2.0 已修复。之前版本子 Agent (`_child_turn`) 只继承父级的 `model_name` 但不继承 `provider_name`，导致重名模型无法路由。

---

## 其他

### Q: 如何重启 Ziva 后端？

在 Ziva 界面输入 `/restart`，或通过菜单重启，或：
```bash
ziva desktop restart
```

### Q: 如何查看 Ziva 版本？

菜单 → About Ziva，或：
```bash
cat electron/package.json | grep version
```

### Q: 如何清理缓存？

```bash
# 清理后端日志
rm ~/.ziva/backend.log

# 清理会话数据（谨慎）
rm -rf ~/.ziva/sessions/

# 注意：不要删除 ~/.ziva/config.yaml
```

---

## 获取帮助

- [GitHub Issues](https://github.com/ziva-os/ziva/issues) — 报告 Bug 或提出功能需求
- [GitHub Discussions](https://github.com/ziva-os/ziva/discussions) — 使用讨论与问答
