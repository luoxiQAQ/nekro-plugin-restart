"""Nekro Agent restart plugin."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import time
from datetime import datetime
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import aiodocker
import psutil
from croniter import croniter
from pydantic import Field

from nekro_agent.api.plugin import (
    Arg,
    CmdCtl,
    CommandExecutionContext,
    CommandPermission,
    CommandResponse,
    ConfigBase,
    NekroPlugin,
)
from nekro_agent.api.schemas import AgentCtx

plugin = NekroPlugin(
    name="重启管理",
    module_name="nekro_restart",
    description="通过管理员命令或 Cron 计划安全重启 Nekro Agent",
    version="1.0.5",
    author="luoxiQAQ",
    url="https://github.com/luoxiQAQ/nekro-plugin-restart",
    allow_sleep=False,
    sleep_brief="系统管理插件，提供受权限保护的进程重启和定时重启能力。",
)


class RestartMode:
    AUTO = "auto"
    DOCKER = "docker"
    PROCESS = "process"
    VALUES = {AUTO, DOCKER, PROCESS}


@plugin.mount_config()
class RestartConfig(ConfigBase):
    RESTART_MODE: str = Field(
        default=RestartMode.AUTO,
        title="重启模式",
        description="auto: 优先 Docker，失败时退出进程；docker: 重启当前容器；process: 退出当前进程",
    )
    DEFAULT_DELAY_SECONDS: int = Field(
        default=3,
        ge=1,
        le=300,
        title="手动重启延迟（秒）",
        description="返回命令响应后等待多久再执行重启",
    )
    ENABLE_SCHEDULED_RESTART: bool = Field(
        default=False,
        title="启用定时重启",
        description="启用后按照 Cron 表达式自动重启",
    )
    RESTART_CRON: str = Field(
        default="0 5 * * *",
        title="定时重启 Cron",
        description="五段 Cron 表达式：分 时 日 月 周",
    )
    TIMEZONE: str = Field(
        default="Asia/Shanghai",
        title="定时时区",
        description="IANA 时区，例如 Asia/Shanghai、UTC",
    )
    SCHEDULE_NOTIFY_CHAT_KEY: str = Field(
        default="",
        title="定时重启通知频道",
        description="可选。填写 chat_key 后，定时重启完成会向该频道发送通知",
    )
    DOCKER_SOCKET_PATH: str = Field(
        default="/var/run/docker.sock",
        title="Docker Socket 路径",
        description="官方 Docker Compose 默认已挂载此 Socket",
    )
    DOCKER_STOP_TIMEOUT_SECONDS: int = Field(
        default=10,
        ge=1,
        le=120,
        title="Docker 停止超时（秒）",
    )
    ACTION_TIMEOUT_SECONDS: int = Field(
        default=20,
        ge=3,
        le=120,
        title="重启动作超时（秒）",
    )
    PROCESS_FORCE_EXIT_SECONDS: int = Field(
        default=10,
        ge=1,
        le=60,
        title="进程强制退出等待（秒）",
        description="发送 SIGTERM 后仍未退出时，使用非零状态强制结束进程",
    )
    NOTICE_RETRY_SECONDS: int = Field(
        default=60,
        ge=5,
        le=300,
        title="完成通知重试时间（秒）",
    )
    SHOW_MEMORY_INFO: bool = Field(
        default=True,
        title="完成通知显示内存",
    )


config = plugin.get_config(RestartConfig)
store = plugin.store

_PENDING_KEY = "pending_restart"
_LAST_RESULT_KEY = "last_restart_result"
_restart_task: asyncio.Task[None] | None = None
_scheduler_task: asyncio.Task[None] | None = None
_notice_task: asyncio.Task[None] | None = None


def _track_task(task: asyncio.Task[None], label: str) -> asyncio.Task[None]:
    def _done(completed: asyncio.Task[None]) -> None:
        if completed.cancelled():
            return
        try:
            completed.result()
        except Exception:
            plugin.logger.exception(f"后台任务执行失败: {label}")

    task.add_done_callback(_done)
    return task


def _create_task(coro: Any, name: str) -> asyncio.Task[None]:
    return _track_task(asyncio.create_task(coro, name=name), name)


async def _cancel_task(task: asyncio.Task[None] | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _validate_mode(mode: str) -> str:
    normalized = mode.strip().lower()
    if normalized not in RestartMode.VALUES:
        raise ValueError("RESTART_MODE 必须是 auto、docker 或 process")
    return normalized


def _validate_schedule() -> tuple[str, ZoneInfo]:
    cron_expr = config.RESTART_CRON.strip()
    timezone = ZoneInfo(config.TIMEZONE.strip())
    croniter(cron_expr, datetime.now(timezone)).get_next(datetime)
    return cron_expr, timezone


def _next_run_time() -> datetime | None:
    if not config.ENABLE_SCHEDULED_RESTART:
        return None
    cron_expr, timezone = _validate_schedule()
    return croniter(cron_expr, datetime.now(timezone)).get_next(datetime)


def _memory_info() -> str:
    memory = psutil.virtual_memory()
    used = memory.total - memory.available
    gib = 1024**3
    return f"{used / gib:.1f}GB/{memory.total / gib:.1f}GB ({memory.percent:.1f}%)"


async def _set_json(key: str, value: dict[str, Any]) -> None:
    await store.set(store_key=key, value=json.dumps(value, ensure_ascii=False))


async def _get_json(key: str) -> dict[str, Any] | None:
    raw = await store.get(store_key=key)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        plugin.logger.warning(f"忽略损坏的插件状态: {key}")
        await store.delete(store_key=key)
        return None
    return value if isinstance(value, dict) else None


async def _clear_pending() -> None:
    await store.delete(store_key=_PENDING_KEY)


async def _send_chat_text(chat_key: str, content: str) -> None:
    ctx = await AgentCtx.create_by_chat_key(chat_key)
    await ctx.send_text(content, record=False)


async def _notify_restart_complete() -> None:
    pending = await _get_json(_PENDING_KEY)
    if not pending:
        return

    start_ts = float(pending.get("start_ts", 0) or 0)
    if start_ts <= 0 or time.time() - start_ts > 3600:
        await _clear_pending()
        return

    elapsed = max(0.0, time.time() - start_ts)
    message = f"Nekro Agent 重启完成（耗时 {elapsed:.2f} 秒）"
    if config.SHOW_MEMORY_INFO:
        message += f"\n内存：{_memory_info()}"

    chat_key = str(pending.get("chat_key", "")).strip()
    deadline = time.monotonic() + config.NOTICE_RETRY_SECONDS
    notified = not chat_key
    while chat_key and time.monotonic() < deadline:
        try:
            await _send_chat_text(chat_key, message)
            notified = True
            break
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(2)

    await _set_json(
        _LAST_RESULT_KEY,
        {
            "status": "success",
            "reason": pending.get("reason", "unknown"),
            "mode": pending.get("mode", "unknown"),
            "started_at": start_ts,
            "completed_at": time.time(),
            "elapsed_seconds": elapsed,
            "notified": notified,
        },
    )
    await _clear_pending()
    if chat_key and not notified:
        plugin.logger.warning(f"重启完成，但通知频道在重试期限内不可用: {chat_key}")


async def _restart_current_container() -> None:
    socket_path = config.DOCKER_SOCKET_PATH.strip()
    if not socket_path or not os.path.exists(socket_path):
        raise RuntimeError(f"Docker Socket 不存在: {socket_path or '<empty>'}")

    container_ref = os.environ.get("HOSTNAME", "").strip() or socket.gethostname().strip()
    if not container_ref:
        raise RuntimeError("无法识别当前容器 ID")

    docker = aiodocker.Docker(url=f"unix://{socket_path}")
    try:
        container = await asyncio.wait_for(
            docker.containers.get(container_ref),
            timeout=config.ACTION_TIMEOUT_SECONDS,
        )
        await asyncio.wait_for(
            container.restart(t=config.DOCKER_STOP_TIMEOUT_SECONDS),
            timeout=config.ACTION_TIMEOUT_SECONDS,
        )
    finally:
        await docker.close()


async def _terminate_current_process() -> None:
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.sleep(config.PROCESS_FORCE_EXIT_SECONDS)
    os._exit(1)


async def _perform_restart(mode: str) -> str:
    selected = _validate_mode(mode)
    if selected == RestartMode.DOCKER:
        await _restart_current_container()
        return RestartMode.DOCKER
    if selected == RestartMode.PROCESS:
        await _terminate_current_process()
        return RestartMode.PROCESS

    socket_path = config.DOCKER_SOCKET_PATH.strip()
    if socket_path and os.path.exists(socket_path):
        try:
            await _restart_current_container()
            return RestartMode.DOCKER
        except asyncio.CancelledError:
            raise
        except Exception:
            plugin.logger.exception("Docker 重启失败，回退为进程退出模式")

    await _terminate_current_process()
    return RestartMode.PROCESS


async def _restart_after_delay(delay: int, reason: str, chat_key: str) -> None:
    global _restart_task

    try:
        await asyncio.sleep(delay)
        mode = _validate_mode(config.RESTART_MODE)
        await _set_json(
            _PENDING_KEY,
            {
                "chat_key": chat_key,
                "start_ts": time.time(),
                "reason": reason,
                "mode": mode,
            },
        )
        await _perform_restart(mode)
        raise RuntimeError("重启动作已返回，但 Nekro Agent 仍在运行")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        plugin.logger.exception("Nekro Agent 重启失败")
        await _set_json(
            _LAST_RESULT_KEY,
            {
                "status": "failed",
                "reason": reason,
                "completed_at": time.time(),
                "error": str(exc),
            },
        )
        await _clear_pending()
        if chat_key:
            try:
                await _send_chat_text(chat_key, f"Nekro Agent 重启失败：{exc}")
            except Exception:
                plugin.logger.exception("发送重启失败通知时出错")
    finally:
        _restart_task = None


def _schedule_restart(delay: int, reason: str, chat_key: str = "") -> bool:
    global _restart_task

    if _restart_task and not _restart_task.done():
        return False
    _restart_task = _create_task(
        _restart_after_delay(delay, reason, chat_key),
        "nekro-restart-action",
    )
    return True


async def _scheduler_loop() -> None:
    last_error = ""
    while True:
        try:
            if not config.ENABLE_SCHEDULED_RESTART:
                await asyncio.sleep(15)
                continue

            cron_expr, timezone = _validate_schedule()
            next_run = croniter(cron_expr, datetime.now(timezone)).get_next(datetime)
            signature = (cron_expr, str(timezone))
            last_error = ""

            while config.ENABLE_SCHEDULED_RESTART:
                current_signature = (config.RESTART_CRON.strip(), config.TIMEZONE.strip())
                if current_signature != signature:
                    break
                remaining = (next_run - datetime.now(timezone)).total_seconds()
                if remaining <= 0:
                    _schedule_restart(
                        0,
                        "scheduled",
                        config.SCHEDULE_NOTIFY_CHAT_KEY.strip(),
                    )
                    await asyncio.sleep(1)
                    break
                await asyncio.sleep(min(remaining, 15))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = str(exc)
            if error != last_error:
                plugin.logger.error(f"定时重启配置无效: {error}")
                last_error = error
            await asyncio.sleep(30)


@plugin.mount_init_method()
async def init() -> None:
    global _notice_task, _scheduler_task

    _validate_mode(config.RESTART_MODE)
    if config.ENABLE_SCHEDULED_RESTART:
        _validate_schedule()
    _scheduler_task = _create_task(_scheduler_loop(), "nekro-restart-scheduler")
    _notice_task = _create_task(_notify_restart_complete(), "nekro-restart-notice")
    plugin.logger.info("重启管理插件已初始化")


@plugin.mount_cleanup_method()
async def cleanup() -> None:
    global _notice_task, _restart_task, _scheduler_task

    await _cancel_task(_scheduler_task)
    await _cancel_task(_notice_task)
    await _cancel_task(_restart_task)
    _scheduler_task = None
    _notice_task = None
    _restart_task = None
    plugin.logger.info("重启管理插件已清理")


@plugin.mount_command(
    name="na",
    description="管理 Nekro Agent 重启和定时重启",
    usage="na <重启|定时重启|重启状态> [参数]",
    permission=CommandPermission.SUPER_USER,
    category="system",
    tags=["restart", "cron", "admin", "system"],
)
async def na_command(
    context: CommandExecutionContext,
    action: Annotated[
        str,
        Arg("操作", positional=True),
    ],
    value: Annotated[str, Arg("可选参数", positional=True)] = "",
) -> CommandResponse:
    if action not in {"重启", "定时重启", "重启状态"}:
        return CmdCtl.failed("正确格式：/na 重启 [秒数] | /na 定时重启 开|关|状态 | /na 重启状态")

    if action == "重启":
        try:
            delay = int(value) if value.strip() else config.DEFAULT_DELAY_SECONDS
        except ValueError:
            return CmdCtl.failed("延迟秒数必须是整数")
        if not 0 <= delay <= 300:
            return CmdCtl.failed("延迟秒数必须在 0 到 300 之间")
        effective_delay = delay or config.DEFAULT_DELAY_SECONDS
        if not _schedule_restart(effective_delay, "manual", context.chat_key):
            return CmdCtl.failed("已有重启任务正在等待或执行")
        return CmdCtl.success(f"Nekro Agent 将在 {effective_delay} 秒后重启")

    if action == "定时重启":
        mode = value.strip() or "状态"
        if mode in {"开", "on"}:
            try:
                _validate_schedule()
                config.ENABLE_SCHEDULED_RESTART = True
                plugin.save_config(config)
                next_run = _next_run_time()
            except Exception as exc:
                return CmdCtl.failed(f"无法开启定时重启：{exc}")
            return CmdCtl.success(f"已开启定时重启，下次执行：{next_run.isoformat() if next_run else '未知'}")

        if mode in {"关", "off"}:
            config.ENABLE_SCHEDULED_RESTART = False
            plugin.save_config(config)
            return CmdCtl.success("已关闭定时重启")

        if mode not in {"状态", "status"}:
            return CmdCtl.failed("正确格式：/na 定时重启 开|关|状态")
        try:
            next_run = _next_run_time()
        except Exception as exc:
            return CmdCtl.failed(f"定时重启配置无效：{exc}")
        if next_run is None:
            return CmdCtl.success("定时重启已关闭")
        return CmdCtl.success(
            f"定时重启已开启\nCron：{config.RESTART_CRON}\n时区：{config.TIMEZONE}\n下次执行：{next_run.isoformat()}"
        )

    pending = await _get_json(_PENDING_KEY)
    last_result = await _get_json(_LAST_RESULT_KEY)
    try:
        next_run = _next_run_time()
        schedule_status = next_run.isoformat() if next_run else "关闭"
    except Exception as exc:
        schedule_status = f"配置错误：{exc}"

    data = {
        "mode": config.RESTART_MODE,
        "restart_pending": bool(_restart_task and not _restart_task.done()),
        "scheduled_restart": schedule_status,
        "pending_record": pending,
        "last_result": last_result,
    }
    return CmdCtl.success(json.dumps(data, ensure_ascii=False, indent=2), data=data)