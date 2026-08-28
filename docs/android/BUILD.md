# Ziva Android — 本地构建指南

APK = WebView 壳 + 离线 rootfs 资产（Ubuntu 24.04 arm64 + venv + Ziva 源码 + 前端产物）+ Termux proot（jniLibs）。

```
[前端] web/ --vite--> src/ziva/transports/desktop_api/static/
[后端] src/ziva/ (python)
          │
          ▼
scripts/build-android-rootfs.sh   →  android/app/src/main/assets/offline-rootfs.bin
scripts/fetch-proot.sh            →  android/app/src/main/jniLibs/arm64-v8a/*.so
          │
          ▼
android/ (gradle assembleRelease) →  app/build/outputs/apk/release/app-arm64-v8a-release.apk
```

两个二进制产物（rootfs、proot so）**不进 git**，由脚本本地生成或由 CI 自动产出。

## 方式一：CI 构建（推荐）

`.github/workflows/android-build.yml` 为 `workflow_dispatch` 手动触发，两段式：

1. `rootfs`（ubuntu-24.04-arm 原生 arm64）：跑 `build-android-rootfs.sh` + `fetch-proot.sh`，上传 artifact
2. `apk`（ubuntu-latest）：下载 artifact → `gradle :app:assembleRelease`，上传 `ziva-apk`

Release 签名需要仓库 secrets（不配则回退 debug 签名，仅测试装）：

| Secret | 说明 |
| --- | --- |
| `ZIVA_KEYSTORE_B64` | release keystore 的 base64（`base64 -i ziva.jks`） |
| `ZIVA_STORE_PASS` | keystore 口令（注入为 `ZIVA_KEYSTORE_PASSWORD`） |
| `ZIVA_KEY_PASS` | key 口令（注入为 `ZIVA_KEY_PASSWORD`） |
| `ZIVA_KEY_ALIAS` | key 别名 |

## 方式二：本地构建

要求：Linux arm64（或能跑 arm64 容器的机器，如 Apple Silicon Docker）+ JDK 17 + Android SDK（`sdkmanager` 可装 platform 34、build-tools 34）+ gradle 8.5+。

```bash
# 0. 前端（产物直出后端 static，随 src 一起进 rootfs）
cd web && npm ci && npm run build && cd ..

# 1. 离线 rootfs（~5-10 分钟；必须在 arm64 环境跑，CI 用的就是 arm runner）
sudo bash scripts/build-android-rootfs.sh
#    → android/app/src/main/assets/offline-rootfs.bin

# 2. Termux proot → jniLibs（Linux/macOS 均可，纯 python 解包）
bash scripts/fetch-proot.sh
#    → android/app/src/main/jniLibs/arm64-v8a/{libproot,libloader,libtalloc,libandroid-shmem}.so

# 3. APK
cd android
gradle :app:assembleRelease          # 或 ./gradlew（CI 上用预装 gradle）
# 签名走环境变量（缺失则自动回退 debug 签名）：
#   ZIVA_KEYSTORE=/path/ziva.jks ZIVA_KEYSTORE_PASSWORD=... ZIVA_KEY_PASSWORD=... ZIVA_KEY_ALIAS=ziva
```

> 注意：rootfs 脚本对 CPU 架构有硬性要求（arm64 原生执行 chroot 内的 aarch64 二进制）。
> x86 机器请勿用 qemu 模拟——会跑通但极慢，且 CI 已提供 arm64 runner。

## 首次安装后

1. 启动 App → ExtractActivity 解压 rootfs 到应用数据目录（一次性，几分钟）
2. 后端在 `127.0.0.1:4097` 起服务，WebView 加载；前台服务常驻 + 开机自启（可在系统设置关）
3. 数据目录：优先 `/sdcard/Documents/zivadata`（需要"所有文件访问"权限，Diagnostics 页会引导），否则应用私有目录
4. 日志：`/sdcard/Documents/zivadata/ziva-android.log`；应用菜单 ⋮ 提供「重启后端 / 运行自检 / 备份数据」

## 常见问题

- **解压后启动卡 bootStatus**：先跑菜单「运行自检」，看 proot probe / HTTP 哪项失败；日志文件里有后端 stdout/stderr。
- **proot 段错误**：确认 jniLibs 里的 `libproot.so` 来自 Termux fork（`scripts/fetch-proot.sh`），发行版 proot 在 Android 上不可用。
- **CVE/体积**：offline-rootfs.bin 约 220MB（APK 内不二次压缩），解压后 ~700MB。
