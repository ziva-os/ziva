# 浏览器自动化设置

Ziva 内置了一个基于 Chromium 的浏览器，AI 可以通过 Chrome DevTools Protocol (CDP) 进行网页导航、交互、截图、性能分析等操作。

---

## 工作原理

```
Ziva Electron 进程
  ├── 启动 Chrome（--remote-debugging-port=9222）
  ├── chrome-devtools-mcp ← 连接到 Chrome 9222 端口
  └── 内置浏览器 UI（WebContentsView）
      └── AI 通过 MCP 工具控制浏览器
```

Ziva 启动时自动完成所有连接，用户无需手动配置。

---

## 必须安装 Chrome

```bash
brew install --cask google-chrome
```

或从 [google.com/chrome](https://www.google.com/chrome/) 下载。

验证安装：
```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --version
```

> ⚠️ Ziva 需要独立的 Chrome 实例用于调试。如果你日常使用的 Chrome 已经在运行，建议先退出，否则端口 9222 可能被占用。

---

## 配置确认

`~/.ziva/config.yaml` 中需要确保以下配置：

```yaml
mcp:
  enabled: true
  servers:
    chrome-devtools:
      type: local
      command: npx -y chrome-devtools-mcp --browserUrl http://127.0.0.1:9222
      environment: {}
      enabled: true
```

如果你修改了 `ZIVA_CDP_PORT` 环境变量，需要同步修改 `--browserUrl` 中的端口。

---

## AI 可用的浏览器工具

启用 chrome-devtools-mcp 后，AI 可以使用以下工具：

### 导航类
| 工具 | 功能 |
|------|------|
| `navigate_page` | 打开 URL、前进、后退、刷新 |
| `new_page` | 新开标签页 |
| `close_page` | 关闭标签页 |
| `list_pages` | 列出所有标签页 |

### 交互类
| 工具 | 功能 |
|------|------|
| `click` | 点击元素 |
| `fill` / `fill_form` | 输入文本 / 批量填表 |
| `hover` | 鼠标悬停 |
| `drag` | 拖拽元素 |
| `press_key` | 按键（Enter、Tab、快捷键等） |
| `type_text` | 在已聚焦元素中输入 |
| `upload_file` | 上传文件 |

### 检查类
| 工具 | 功能 |
|------|------|
| `take_snapshot` | 获取页面 a11y 树快照（推荐，比截图更快） |
| `take_screenshot` | 页面或元素截图 |
| `evaluate_script` | 执行 JavaScript |

### 调试类
| 工具 | 功能 |
|------|------|
| `list_console_messages` | 查看控制台日志 |
| `list_network_requests` | 查看网络请求 |
| `get_network_request` | 查看请求/响应详情 |
| `lighthouse_audit` | Lighthouse 审计（SEO、可访问性、最佳实践） |
| `performance_start_trace` | 性能追踪 |

---

## 内置浏览器 UI

Ziva 界面左侧有一个浏览器面板，你可以：

- **输入 URL 导航**：支持 `http://`、`https://`、`file://` 等协议
- **多标签页**：点击 + 号新建标签，切换标签页
- **刷新恢复**：Ziva 刷新或重启后自动恢复之前的标签页状态（v1.2.0+）

---

## 常见问题

### Chrome 没有自动启动

检查 Chrome 是否安装在标准位置。Ziva 在以下路径搜索 Chrome：
1. `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`
2. `~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`

### 端口 9222 被占用

```bash
# 查看占用
lsof -i :9222

# 可以修改端口
export ZIVA_CDP_PORT=9223
# 同时更新 config.yaml 中的 --browserUrl
```

### file:// 协议无法打开

v1.2.0 已修复。之前版本在地址栏输入 `file:///path/to/file.html` 会被误判为搜索关键词。升级后所有带 scheme 的 URL（`file://`、`ftp://` 等）都能正确打开。

### 浏览器抢占 Ziva 界面焦点

v1.2.0 已修复。之前版本在刷新或重开窗口时，浏览器标签页会抢占 Ziva 的输入焦点。现在通过 localStorage 持久化活动标签状态，实现精准恢复。
