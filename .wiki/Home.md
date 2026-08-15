# Ziva Desktop Wiki

欢迎！这里是 Ziva 的详细文档。如果你是第一次使用，建议按顺序阅读以下页面。

## 📖 文档索引

| 页面 | 内容 |
|------|------|
| [前置依赖与安装](Prerequisites) | 系统要求、必装组件、推荐工具 |
| [配置指南](Configuration) | config.yaml 完整说明、Provider 配置、推荐配置示例 |
| [浏览器自动化](Browser-Setup) | Chrome DevTools MCP 配置、内置浏览器使用 |
| [推荐 Skills](Recommended-Skills) | 精选技能安装与使用 |
| [常见问题](FAQ) | 报错排查、已知问题 |

## 🚀 快速开始（5 分钟）

```bash
# 1. 克隆项目
git clone https://github.com/ziva-os/ziva.git
cd ziva

# 2. 安装后端
pip install -e ".[all]"

# 3. 安装前端
cd web && npm install && cd ..

# 4. 安装 Electron 壳
cd electron && npm install && cd ..

# 5. 启动
cd electron && npm start
```

首次启动后，Ziva 会自动在 `~/.ziva/config.yaml` 创建一份模板配置。打开它，填入你的 API Key 即可开始使用。

> 详细的系统要求和前置依赖请见 [前置依赖与安装](Prerequisites)。
