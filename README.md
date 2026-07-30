# Nekro Agent 重启管理插件

适用于 [KroMiose/nekro-agent](https://github.com/KroMiose/nekro-agent) 的安全重启插件。

## 功能

- 仅超级管理员可执行重启命令。
- 支持 Docker 容器重启和进程退出两种模式。
- `auto` 模式优先重启当前 Docker 容器，失败后回退为进程退出。
- 支持五段 Cron 定时重启，默认关闭。
- 防止重复创建重启任务或重复调度器。
- 手动重启后向原频道发送完成耗时和内存信息。
- 定时重启可配置通知频道。
- 支持 Nekro Agent 已接入的多种消息适配器。

## 安装

在 Nekro Agent 插件市场中添加或安装以下 Git 仓库：

```text
https://github.com/luoxiQAQ/nekro-plugin-restart.git
```

仓库根目录直接包含 `__init__.py`，市场模块名为 `nekro_restart`。安装后在 WebUI 插件管理中启用“重启管理”。

手动安装时，也可以将仓库内容放到 `plugins/workdir/nekro_restart/` 后重启 Nekro Agent。

官方 Docker Compose 已将 `/var/run/docker.sock` 挂载到主服务，并配置了 `restart: unless-stopped`，默认 `auto` 模式可直接使用。

## 命令

命令均要求超级管理员权限。

| 命令 | 说明 |
| --- | --- |
| `/na 重启` | 按配置延迟重启 |
| `/na 重启 10` | 10 秒后重启 |
| `/na 定时重启 开` | 开启 Cron 定时重启 |
| `/na 定时重启 关` | 关闭 Cron 定时重启 |
| `/na 定时重启 状态` | 查看定时重启状态 |
| `/na 重启状态` | 查看当前任务和上次重启结果 |

如果命令系统启用了插件命名空间，也可以使用 `/nekro_restart:na 重启`。

## 主要配置

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `RESTART_MODE` | `auto` | `auto`、`docker` 或 `process` |
| `DEFAULT_DELAY_SECONDS` | `3` | 手动命令默认延迟 |
| `ENABLE_SCHEDULED_RESTART` | `false` | 是否启用定时重启 |
| `RESTART_CRON` | `0 5 * * *` | 每天 05:00 |
| `TIMEZONE` | `Asia/Shanghai` | Cron 使用的 IANA 时区 |
| `SCHEDULE_NOTIFY_CHAT_KEY` | 空 | 定时重启完成通知频道 |
| `DOCKER_SOCKET_PATH` | `/var/run/docker.sock` | Docker Socket 路径 |
| `SHOW_MEMORY_INFO` | `true` | 完成通知是否显示内存 |

## 重启模式

### `auto`

检测到 Docker Socket 时，通过 Docker API 重启当前容器；否则向当前进程发送 `SIGTERM`。Docker 重启失败也会回退到进程模式。

### `docker`

只允许 Docker 重启。插件使用容器内的 `HOSTNAME` 识别当前容器，不接受任意容器名称，避免通过插件控制其他容器。

### `process`

向 Nekro Agent 当前进程发送 `SIGTERM`。必须由 Docker、systemd、Supervisor 或其他进程管理器配置自动拉起，否则服务只会退出而不会重新启动。

## 安全说明

- 插件不会执行 Shell 命令。
- 插件不会开放给 Agent 自主调用的重启工具。
- Docker 模式只重启当前容器。
- 所有聊天命令均使用 `CommandPermission.SUPER_USER`。
- 默认关闭定时重启，安装后不会自行重启服务。

## 兼容性

插件依据 Nekro Agent `main` 分支在 2026-07-30 的插件 API 编写，要求支持以下接口：

- `NekroPlugin.mount_command`
- `NekroPlugin.mount_init_method`
- `NekroPlugin.mount_cleanup_method`
- `AgentCtx.create_by_chat_key`
- `CommandPermission.SUPER_USER`