# Linux 桌面集成

## GNOME / IBus

没有 fcitx5 插件时，Linux 宿主自动选择 Desktop Portal 后端，保留现有 IBus 输入法。运行依赖为 `python3`、`python3-gi`、`gir1.2-glib-2.0`、`xdg-desktop-portal` 和支持 RemoteDesktop、Clipboard 的桌面后端；Ubuntu GNOME 使用 `xdg-desktop-portal-gnome`。

1. 打开 OpenLess 的 **设置 → 录音与输入 → 桌面语音输入**，点击 **连接 / 重新授权**。
2. 在系统对话框中允许键盘和剪贴板访问；支持全局快捷键 Portal 的桌面还会请求快捷键授权。应用不请求屏幕采集。
3. 将光标放到目标输入框，先完成已有的拼音组词，再使用系统分配的快捷键开始/停止录音。默认请求 `Ctrl+Alt+Space`，取消请求 `Ctrl+Alt+Escape`；实际绑定可通过 **配置系统快捷键** 修改。
4. 结果以完整文本自动粘贴到处理完成时的当前光标处，并保留在剪贴板中。普通输入框使用 `Ctrl+V`；终端通常选择 `Ctrl+Shift+V`。

Ubuntu 24.04 / GNOME 46 没有全局快捷键 Portal。此时应用在输入权限获准后添加两条 GNOME 自定义快捷键，沿用上述默认按键，不覆盖已有自定义快捷键。点击 **配置系统快捷键** 会打开 GNOME 键盘设置，在自定义快捷键中修改 OpenLess 的按键；如果默认按键与已有系统或自定义快捷键冲突，应用会保留原绑定，并提示在那里为 OpenLess 分配按键。断开连接或正常退出时移除 OpenLess 的激活条目，保留你选定的按键供下次连接使用。快捷键只向运行中的 OpenLess 转发命令，应用退出后不会因残留快捷键而自行录音。

旧版 Portal 缺少应用身份注册接口时使用它自带的客户端识别机制；授权拒绝不会触发降级。新版桌面继续使用 GlobalShortcuts Portal。

该后端采用切换录音和一次性粘贴，不提供原生 IME 提交、逐字流式写入或选区替换。录音和处理过程中请保持输入框焦点；系统确认按键已发送不等于目标应用已接受文本，历史会记录 `PasteSent`。粘贴失败时不自动重发，以免重复输入。拒绝授权仍可使用设置、历史和应用内录音入口。

已授权的桌面会话会尝试在下次启动时恢复，系统可能重新请求授权。断开、桌面服务重启或权限撤销后可在设置中重新连接。桌面恢复状态单独存储在应用数据目录的 `desktop-portal.json`，不包含模型凭据。

可用 `OPENLESS_INPUT_BACKEND=portal openless` 强制使用桌面后端，或 `OPENLESS_INPUT_BACKEND=fcitx5 openless` 使用原有插件。默认 `auto` 优先使用能响应的 fcitx5 插件，其余环境选择 Portal。

从源码直接运行前，在用户目录安装 Portal 所需的应用身份文件（deb 已包含）：

```sh
# 在 openless-all/app/ 下执行；不修改输入法配置。
cargo build --locked --release -p openless-linux-egui
python3 - <<'PY'
import os
from pathlib import Path

binary = Path("target/release/openless-linux-egui").resolve(strict=True)
entry = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "applications/top.openless.OpenLess.desktop"
entry.parent.mkdir(parents=True, exist_ok=True)
source = Path("linux-egui/packaging/top.openless.OpenLess.desktop").read_text()
entry.write_text(source.replace("Exec=openless\n", f"Exec={binary}\n"))
PY
```

源码运行的身份文件必须指向已存在的可执行文件；仅复制包含 `Exec=openless` 的打包模板可能无法被桌面识别。如果之前安装了上述用户身份文件，改用 deb 时应移除该文件，让桌面使用包内 `/usr/share/applications/top.openless.OpenLess.desktop`，避免继续引用源码目录。

### Ubuntu 24.04 兼容包

在 `openless-all/app/` 下执行以下命令（需要 Docker），会用 Ubuntu 24.04 的系统库构建 amd64 包：

```sh
bash scripts/build-linux-portal-ubuntu24.sh
```

产物位于 `target/ubuntu24/linux-egui-packages/`，包含 `.deb` 和 `SHA256SUMS`。构建脚本检查实际 ELF 的 glibc 要求不高于 2.39，避免将高版本主机生成的程序误标为 24.04 兼容。系统软件源需提供 GNOME 46 的 Portal 后端、Python/GIO 和运行库；无需 fcitx5。

容器检查能验证依赖安装、二进制加载和兼容逻辑，不能替代目标桌面上的麦克风、快捷键及实际粘贴验收。

### 本机包

仅面向本机系统版本的安装包可从 `openless-all/app/` 构建：

```sh
cargo build --locked --release -p openless-linux-egui
OPENLESS_LINUX_INPUT_BACKEND=portal \
OPENLESS_LINUX_VERSION="$(node -p "require('./package.json').version.split('+')[0]")-$(cat linux-egui/package-revision)" \
bash scripts/package-linux-egui.sh
```

输出为 `target/linux-egui-packages/OpenLess-Linux-portal-*.deb`，不依赖、不安装、不重启 fcitx5。默认打包命令继续生成原有 fcitx5 deb/rpm。两种 deb 使用同一 `openless` 包名，选择一种安装。

本地包的 glibc 最低版本由实际二进制符号生成；在 Ubuntu 26.04 上构建的包不应直接当作 Ubuntu 24.04 的发布包。

开发验证：

```sh
python3 linux-egui/tests/portal_bridge_test.py
node scripts/linux-portal-package.test.mjs
cargo test --locked -p openless-linux-egui --all-targets
```

自动检查覆盖授权拒绝、会话代次、取消、按键清理及包装依赖；输入效果仍需在实际 GTK、浏览器、Electron 和终端应用中验证。

## 桌面扩展

桥协议为 `org.openless.Desktop1` v1，与 Core 业务数据版本独立。X11 直接使用 X11 接口；Wayland 使用桌面组件提供前台窗口、屏幕工作区、焦点恢复、浮窗定位和全局快捷键。

安装包附带 `openless-desktop-install`。在用户会话中运行：

```sh
openless-desktop-install install
openless-desktop-install enable
openless-desktop-install uninstall
```

GNOME 42–44 和 45+ 使用不同模块入口。首次安装 GNOME 扩展后可能需要注销并重新登录，再执行 `enable`。KDE 使用 KWin 脚本与 KGlobalAccel 辅助程序；安装时写入用户的脚本、D-Bus 服务和登录启动项。安装后重启 OpenLess，使当前设置重新注册到桌面桥。

组件只运行在当前用户的会话中，不要求 root，不读取密码输入框。fcitx5 输入目标使用 UUID 与会话票据；桌面窗口身份用于定位与恢复焦点，不代替写入前的文本目标校验。

开发构建：

```sh
node gnome/build.mjs
cmake -S kde -B kde/build -DCMAKE_BUILD_TYPE=Release
cmake --build kde/build --parallel
```

KDE 构建支持 Qt 5 / KF5 与 Qt 6 / KF6。桌面组件的实际交互验收见 Linux 交接清单；编译通过不代表已经在相应桌面版本上完成验收。

接口参考：[GNOME 扩展](https://gjs.guide/extensions/)、[KWin](https://develop.kde.org/docs/plasma/kwin/api/)、[KGlobalAccel](https://api.kde.org/kglobalaccel.html)、[AT-SPI Text](https://gnome.pages.gitlab.gnome.org/at-spi2-core/libatspi/iface.Text.html)。
