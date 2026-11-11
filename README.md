# Claude Box

**把 Claude 放进独立容器里运行，减少它接触宿主机文件的机会，防止不相关的本机内容被意外读取或泄露。**

Claude 经常需要查看代码、执行命令、安装依赖。直接在日常使用的电脑上运行，它可能接触到与当前任务无关的文件、配置和凭据。

Claude Box 为 Claude 准备了一套独立的 Linux 开发环境：**Claude 在容器里工作，宿主机上的项目目录按需共享，网络访问走独立的代理出口。** 你仍然可以正常写代码、运行工具和使用终端，而不必把整个宿主机环境交给 Claude。

## 有什么用？

- **隔离文件访问**：使用 Rootless Podman 运行 Claude。容器拥有独立的家目录，只有你主动挂载的宿主目录才会额外开放给它。
- **隔离网络**：容器拥有独立的网络命名空间，通过专用 Xray TUN 出网；代理不可用时，不会自动改走宿主机的普通网络连接。
- **按需使用宿主能力**：需要图形程序时可以开启 Wayland，需要虚拟机加速时可以开启 KVM。不使用时可以关闭。
- **保持开发体验**：可以在容器内使用 Claude、Git、编译工具和 SSH，项目文件也能按需挂载。
- **一个配置文件**：端口、网络参数、目录挂载和 Xray 分享链接都写在 `config.toml` 中。

> 请只共享愿意让 Claude 访问的项目目录。开启挂载或设备共享后，容器内的程序也会获得对应访问能力。

## 快速开始

目前项目管理的是**已经创建好的 Podman 容器及其网络环境**，不会自动创建新容器。需要 Linux、Rootless Podman、Python 3.11+，以及准备好的 Claude 开发容器和项目专用 Xray 可执行文件。

### 1. 配置

在项目目录执行：

```bash
cp config.toml.example config.toml
chmod 600 config.toml
```

编辑 `config.toml`，至少将 `[xray]` 下的 `url` 换成自己的 **VLESS Reality 分享链接**，并根据本机实际情况修改 `[ssh]` 的监听地址、端口等参数。

```toml
[xray]
url = "vless://UUID@server.example.com:443?type=tcp&security=reality&..."
log_level = "info"
```

当前支持 VLESS + TCP + Reality。Claude Box 会根据链接自动生成 Xray 配置，**无需再维护一份独立的代理 JSON**。

`config.toml` 含有连接凭据，默认不提交到 Git。

### 2. 启动并进入容器

```bash
./control.sh start
./enter.sh
```

进入容器后，直接运行：

```bash
claude
```

`control.sh start` 负责启动或恢复隔离网络与代理；`enter.sh` 打开容器终端。

### 3. 查看状态或停止

```bash
./control.sh status
./control.sh stop
```

如果启用了项目自带的 systemd 用户服务，`stop` 会同时停止对应的 SSH 服务。

## 让 Claude 使用你的项目

默认不需要把整个宿主机家目录交给 Claude。只需在 `config.toml` 中添加希望开放的目录：

```toml
[[mounts]]
source = "/home/user/projects/my-app"
target = "/workspace/my-app"
```

配置后执行：

```bash
./control.sh mount apply
./control.sh mount list
```

现在 Claude 就可以在容器内访问 `/workspace/my-app`。宿主机的其他目录不会因为添加这一条挂载而自动共享。

不再需要时，可以卸载：

```bash
./control.sh mount remove /workspace/my-app
```

请先卸载，再从 `config.toml` 中删除对应的 `[[mounts]]` 配置。容器会在下次启动时恢复仍然配置的挂载。

## 可选功能

### 图形界面（Wayland）

需要在容器里打开图形程序时：

```bash
./control.sh wayland enable
./control.sh wayland status
./control.sh wayland disable
```

开启后只共享选定的 Wayland socket，不会顺带共享整个宿主桌面会话、音频服务或 GPU 设备。新进入的容器终端会自动设置 Wayland 环境变量。

### 虚拟机加速（KVM）

需要在容器里运行使用 KVM 的 QEMU 时：

```bash
./control.sh kvm enable
./control.sh kvm status
./control.sh kvm disable
```

这只向现有容器开放 `/dev/kvm`，不会把容器改成特权模式。

### 通过 SSH 进入

项目提供 `claude-netns.service` 用户服务，用于同时启动容器网络与 SSH 转发。部署好该 unit 后可以运行：

```bash
systemctl --user start claude-netns.service
systemctl --user status claude-netns.service
```

从其他设备连接时，使用 `config.toml` 中的 SSH 监听地址、端口，以及**容器内已有的用户名**：

```bash
ssh -p <监听端口> <容器用户名>@<宿主机地址>
```

将尖括号中的占位符替换成实际值。SSH 登录使用的是容器账户，不需要与宿主机用户名相同。容器内的 SSH 仅使用公钥认证，实际监听在独立网络命名空间的回环地址上，外部连接由宿主机转发。

**注意**：当前 unit 中的工作目录仍对应原来的本地部署路径。如果移动了仓库目录，启用服务前需要同步调整 unit 的路径。

## 常用命令

| 想做什么 | 命令 |
| --- | --- |
| 启动或恢复容器网络 | `./control.sh start` |
| 进入容器终端 | `./enter.sh` |
| 查看状态 | `./control.sh status` |
| 停止环境 | `./control.sh stop` |
| 应用共享目录 | `./control.sh mount apply` |
| 查看共享目录 | `./control.sh mount list` |
| 关闭所有配置的动态挂载 | `./control.sh mount remove-all` |
| 开启图形支持 | `./control.sh wayland enable` |
| 开启 KVM | `./control.sh kvm enable` |
| 构建项目专用 Xray | `./control.sh build-xray` |

项目只有 `control.sh` 和 `enter.sh` 两个 Shell 入口，主要逻辑使用 Python 实现。

## 数据保存在哪里？

- `config.toml`：唯一需要手动维护的配置文件，包括代理分享链接。
- `home/`：容器使用的家目录，文件保存在宿主机项目目录内。
- `runtime/`：运行时状态，以及自动生成的 `runtime/xray.json`。
- `logs/`：网关和 SSH 运行日志。
- `config.toml.example`：不含真实凭据的配置模板。

`config.toml`、`home/`、`runtime/`、`logs/` 等本地数据已排除在 Git 之外。Xray 构建产物位于 `build/xray`；构建命令不会直接替换正在运行的 `./xray`。

---

Claude Box 的目标很简单：**让 Claude 在自己的盒子里完成开发任务，只把必要的宿主资源交给它。**
