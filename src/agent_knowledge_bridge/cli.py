from __future__ import annotations

import argparse
import hashlib
import getpass
import importlib.util
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Sequence

from agent_knowledge_bridge import __version__
from agent_knowledge_bridge.agent_registry import discover_agents
from agent_knowledge_bridge.learning import LearningStore
from agent_knowledge_bridge.paths import memweave_home
from agent_knowledge_bridge import provider


CONFIG_VERSION = 1


def _paths() -> dict[str, Path]:
    home = memweave_home()
    return {
        "home": home,
        "config": home / "config.json",
        "state": home / "runtime-state.json",
        "logs": home / "logs",
        "database": home / "data" / "knowledge.db",
    }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    provider.atomic_json(path, value)


def _load_config() -> dict[str, Any]:
    path = _paths()["config"]
    if not path.is_file():
        raise RuntimeError("MemWeave 尚未配置，请先运行: memweave setup")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or value.get("config_version") != CONFIG_VERSION:
        raise RuntimeError("MemWeave 配置版本不可识别，请备份后重新运行: memweave init --force")
    return value


def _initialize(args: argparse.Namespace) -> int:
    paths = _paths()
    if paths["config"].exists() and not args.force:
        config = _load_config()
        database = Path(config["database_path"])
        LearningStore(database)
        print(f"MemWeave 已初始化: {paths['home']}")
        print(f"数据库: {database}")
        return 0

    # Preserve an earlier launcher/database when introducing the new CLI.
    previous = _read_state(paths['state']) or {}
    database = Path(args.database).expanduser().resolve() if args.database else (
        Path(previous['database_path']) if previous.get('database_path') else paths['database'])
    database.parent.mkdir(parents=True, exist_ok=True)
    LearningStore(database)
    config = {
        "config_version": CONFIG_VERSION,
        "memweave_version": __version__,
        "database_path": str(database),
        "default_project": previous.get('project_key') or args.project,
        "runtime_port": args.port,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    _write_json(paths["config"], config)
    installed = [item["display_name"] for item in discover_agents() if item["installed"]]
    print(f"MemWeave 初始化完成: {paths['home']}")
    print(f"数据库: {database}")
    print(f"默认项目: {args.project}")
    print(f"检测到本机 Agent: {', '.join(installed) if installed else '无'}")
    print("下一步: memweave setup（配置模型、创建桌面入口）")
    return 0


def _status(_args: argparse.Namespace) -> int:
    config = _load_config()
    database = Path(config["database_path"])
    store = LearningStore(database)
    project = str(config["default_project"])
    registered = store.knowledge.list_agents(include_disabled=True)
    enabled = [item for item in registered if item["enabled"]]
    agent_ids = list(dict.fromkeys(
        [item["agent_id"] for item in enabled] + ["claude-code", "codex"]
    ))[:10]
    overview = store.knowledge.overview(
        project_key=project, agent_ids=agent_ids, limit=2000
    )
    metrics = store.metrics(project)
    summary = overview["summary"]
    statuses = summary["status_counts"]
    print(f"MemWeave {__version__}")
    model = provider.public_settings()
    print(f"学习模型: {model['model']} / " + ('已配置' if model['configured'] else '未配置，请运行 memweave setup'))
    print(f"数据目录: {memweave_home()}")
    print(f"数据库: {database}")
    print(f"当前项目: {project}")
    print(f"Agent: {len(enabled)} 已启用 / {len(registered)} 已登记")
    if enabled:
        print("已启用: " + ", ".join(item["display_name"] for item in enabled))
    print(
        "知识: "
        f"{summary['total']} 总计 / {statuses.get('active', 0)} Active / "
        f"{statuses.get('candidate', 0)} Candidate / {statuses.get('stale', 0)} Stale / "
        f"{statuses.get('archived', 0)} Archived / {statuses.get('quarantined', 0)} Quarantined"
    )
    print(
        "跨 Agent: "
        f"{summary['available_shared']} 可共享 / {summary['confirmed_shared']} 有双方反馈"
    )
    print(
        "学习: "
        f"{metrics['learning']['runs']} 次编译 / {metrics['learning']['promoted']} 条晋升"
    )
    print(
        "召回: "
        f"{metrics['recall']['attempts']} 次 / {metrics['recall']['hit_rate']:.1%} 命中率 / "
        f"平均 {metrics['recall']['average_latency_ms']:.2f} ms"
    )
    return 0


def _source_revision() -> str:
    package = Path(__file__).resolve().parent
    material: list[str] = []
    for path in sorted(package.rglob('*'), key=lambda item: str(item)):
        if path.is_file() and path.suffix in {".py", ".html"}:
            material.append(f"{path.relative_to(package)}:{hashlib.sha256(path.read_bytes()).hexdigest()}")
    return hashlib.sha256("\n".join(material).encode("utf-8")).hexdigest()


def _read_state(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _request_json(url: str, token: str, timeout: float = 1.5) -> dict[str, Any] | None:
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
            return value if isinstance(value, dict) else None
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None


def _runtime_healthy(state: dict[str, Any] | None, revision: str) -> bool:
    if not state or state.get("source_revision") != revision:
        return False
    url, token = state.get("url"), state.get("token")
    if not isinstance(url, str) or not isinstance(token, str):
        return False
    health = _request_json(f"{url}/v1/health", token)
    agents = _request_json(f"{url}/v1/agents?include_disabled=true", token)
    return bool(health and health.get("status") == "ok" and agents is not None)


def _free_port(preferred: int) -> int:
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return int(sock.getsockname()[1])
    raise RuntimeError("无法分配本地 Runtime 端口")


def _stop_owned_runtime(state: dict[str, Any] | None) -> None:
    if not state:
        return
    url, token, pid = state.get("url"), state.get("token"), state.get("pid")
    if not isinstance(url, str) or not isinstance(token, str) or not isinstance(pid, int):
        return
    # A valid authenticated health response proves this is the Runtime recorded
    # in our private state file before its PID is touched.
    health = _request_json(f"{url}/v1/health", token)
    if not health or health.get('pid') != pid:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def _start_runtime(config: dict[str, Any], revision: str) -> dict[str, Any]:
    if importlib.util.find_spec("fastapi") is None or importlib.util.find_spec("uvicorn") is None:
        raise RuntimeError('UI 需要 Runtime 依赖，请运行: pip install "memweave-runtime[runtime]"')
    paths = _paths()
    paths["logs"].mkdir(parents=True, exist_ok=True)
    port = _free_port(int(config.get("runtime_port") or 8765))
    token = secrets.token_hex(24)
    url = f"http://127.0.0.1:{port}"
    environment = {
        **os.environ,
        "MW_DB_PATH": str(config["database_path"]),
        "MW_DAEMON_TOKEN": token,
        # The hooks read these three. Without them the hook falls back to
        # opening its own SQLite connection and re-running the whole schema on
        # every prompt, which is the slow path the daemon exists to avoid.
        "MW_DAEMON_URL": url,
        "MW_PROJECT_KEY": str(config.get("default_project") or ""),
        "MEMWEAVE_ROOT": str(Path(__file__).resolve().parents[2]),
        "PYTHONUTF8": "1",
    }
    stdout = open(paths["logs"] / "runtime.stdout.log", "ab", buffering=0)
    stderr = open(paths["logs"] / "runtime.stderr.log", "ab", buffering=0)
    options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": stdout,
        "stderr": stderr,
        "env": environment,
        "close_fds": True,
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "agent_knowledge_bridge.daemon", "--port", str(port)],
            **options,
        )
    finally:
        stdout.close()
        stderr.close()
    state = {
        "pid": process.pid,
        "url": url,
        "token": token,
        "database_path": str(config["database_path"]),
        "project_key": str(config.get("default_project") or ""),
        "source_revision": revision,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError(f"Runtime 启动失败，请查看日志: {paths['logs'] / 'runtime.stderr.log'}")
        if _runtime_healthy(state, revision):
            # Windows virtualenv python.exe may be a redirector. Store the
            # authenticated server PID, not just the transient launcher PID.
            health = _request_json(f'{url}/v1/health', token)
            if health and isinstance(health.get('pid'), int):
                state['launcher_pid'] = process.pid
                state['pid'] = health['pid']
            _write_json(paths["state"], state)
            return state
        time.sleep(0.1)
    process.terminate()
    raise RuntimeError(f"Runtime 启动超时，请查看日志: {paths['logs'] / 'runtime.stderr.log'}")


def _ui(args: argparse.Namespace) -> int:
    if not _paths()['config'].exists() or not provider.public_settings()['configured']:
        if sys.stdin is not None and sys.stdin.isatty() and not getattr(args, 'no_setup', False):
            _setup(argparse.Namespace(non_interactive=False, base_url=None, model=None,
                                     api_key_env=None, no_shortcut=False, desktop=None, check=False))
        elif not _paths()['config'].exists():
            raise RuntimeError('首次使用请在终端运行 memweave setup')
        else:
            print('尚未配置学习 API：当前可查看已有知识，运行 memweave setup 开启学习。')
    config = _load_config()
    paths = _paths()
    revision = _source_revision()
    from .process_lock import startup_lock
    with startup_lock():
        state = _read_state(paths["state"])
        reused = _runtime_healthy(state, revision)
        if not reused:
            _stop_owned_runtime(state)
            state = _start_runtime(config, revision)
    assert state is not None
    dashboard = (
        f"{state['url']}/knowledge#token={state['token']}"
        f"&project={urllib.parse.quote(config['default_project'])}"
    )
    if not args.no_open:
        webbrowser.open(dashboard)
    print(f"MemWeave UI: {state['url']}/knowledge")
    print("Runtime: " + ("已复用" if reused else "已在后台启动"))
    return 0


def _shortcut(args: argparse.Namespace) -> int:
    from .desktop import create_shortcut
    target = create_shortcut(Path(args.desktop).expanduser().resolve() if args.desktop else None)
    print(f'桌面入口已创建: {target}')
    return 0


def _setup(args: argparse.Namespace) -> int:
    existing = provider.settings()
    interactive = not args.non_interactive
    if interactive and not (sys.stdin is not None and sys.stdin.isatty()):
        raise RuntimeError('请在交互终端运行 memweave setup；自动部署请使用 --non-interactive --api-key-env 环境变量名')
    print('MemWeave 首次配置 / 修改配置')
    print('API 用于会话结束后的知识提炼；召回在本机执行。仅接入你在管理页主动添加的 Agent。')
    print('提炼会向所选服务商发送经脱敏的会话摘要；请使用你信任的服务商。')
    base_url = args.base_url
    model = args.model
    if interactive:
        base_url = base_url or input(f"API Base URL [{existing['base_url']}]: ").strip() or existing['base_url']
        model = model or input(f"模型名称 [{existing['model']}]: ").strip() or existing['model']
    else:
        base_url = base_url or existing['base_url']
        model = model or existing['model']
    key = None
    if args.api_key_env:
        key = os.getenv(args.api_key_env)
        if not key:
            raise ValueError('指定的 API Key 环境变量为空')
    elif interactive:
        key = getpass.getpass('API Key（不回显；同地址留空保留已有 Key）: ').strip() or None
    saved = provider.save(base_url=base_url, model=model, api_key=key)
    _initialize(argparse.Namespace(force=False, database=None, project='default', port=8765))
    config = _load_config()
    config['setup_complete'] = True
    _write_json(_paths()['config'], config)
    print('API 已保存（Key 不回显）。' + ('Windows 当前用户 DPAPI 加密。' if os.name == 'nt' else '独立凭据文件权限为 0600；不是加密存储。'))
    if args.check:
        provider.check_connection()
        print('API 实际请求检查通过。')
    else:
        print('未发起模型请求；可运行 memweave doctor --check-api 检查，可能产生少量费用。')
    if not args.no_shortcut:
        if os.name == 'nt':
            _shortcut(args)
        else:
            print('当前系统请使用 memweave ui 打开管理页。')
    print('配置完成。双击桌面 MemWeave知识管理，或运行 memweave ui；在 Agent 维护中选择加入。')
    return 0


def _doctor(args: argparse.Namespace) -> int:
    config = _load_config()
    value = provider.public_settings()
    print(f"Python: {sys.executable}")
    print(f"数据库: {'存在' if Path(config['database_path']).is_file() else '缺失'}")
    print(f"API: {'已配置' if value['configured'] else '未配置'} / 模型: {value['model']} / 凭据: {value['storage']}")
    print('Runtime 依赖: ' + ('完整' if all(importlib.util.find_spec(x) for x in ('fastapi','uvicorn')) else '缺少，请安装 memweave-runtime[runtime]'))
    print('Runtime: ' + ('运行中' if _runtime_healthy(_read_state(_paths()['state']), _source_revision()) else '未启动或需更新，运行 memweave ui'))
    if args.check_api:
        provider.check_connection()
        print('API 实际请求检查通过')
    return 0


def _workspace(args: argparse.Namespace) -> int:
    from .store import KnowledgeStore
    config = _load_config()
    KnowledgeStore._validate_project_key(args.project)
    path = Path(args.path).expanduser().resolve(strict=True)
    config.setdefault('workspace_projects', {})[str(path)] = args.project
    _write_json(_paths()['config'], config)
    print(f'工作区已绑定: {path} → {args.project}')
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memweave", description="MemWeave 本地知识运行时")
    parser.add_argument('--version', action='version', version=__version__)
    subcommands = parser.add_subparsers(dest="command")
    for command in ('setup', 'configure'):
        setup = subcommands.add_parser(command, help='配置 API、初始化并创建桌面入口')
        setup.add_argument('--base-url')
        setup.add_argument('--model')
        setup.add_argument('--api-key-env', help='读取指定环境变量，不在命令行暴露 Key')
        setup.add_argument('--non-interactive', action='store_true')
        setup.add_argument('--no-shortcut', action='store_true')
        setup.add_argument('--desktop', help=argparse.SUPPRESS)
        setup.add_argument('--check', action='store_true', help='实际请求模型检查连通性，可能产生少量费用')
        setup.set_defaults(handler=_setup)
    shortcut = subcommands.add_parser('shortcut', help='重建桌面知识管理入口')
    shortcut.add_argument('--desktop', help=argparse.SUPPRESS)
    shortcut.set_defaults(handler=_shortcut)
    doctor = subcommands.add_parser('doctor', help='检查配置与 Runtime 状态')
    doctor.add_argument('--check-api', action='store_true', help='实际请求模型，可能产生少量费用')
    doctor.set_defaults(handler=_doctor)
    workspace = subcommands.add_parser('workspace', help='将指定工作区绑定到既有项目（显式共享）')
    workspace.add_argument('--path', required=True)
    workspace.add_argument('--project', required=True)
    workspace.set_defaults(handler=_workspace)
    init = subcommands.add_parser("init", help="初始化本地知识库")
    init.add_argument("--project", default="default", help="默认项目键")
    init.add_argument("--database", help="自定义 SQLite 路径")
    init.add_argument("--port", type=int, default=8765, help="首选 Runtime 端口")
    init.add_argument("--force", action="store_true", help="重写本地配置")
    init.set_defaults(handler=_initialize)
    status = subcommands.add_parser("status", help="查看 Agent、知识、学习和召回统计")
    status.set_defaults(handler=_status)
    ui = subcommands.add_parser("ui", help="后台启动 Runtime 并打开管理页面")
    ui.add_argument("--no-open", action="store_true", help=argparse.SUPPRESS)
    ui.add_argument('--no-setup', action='store_true', help=argparse.SUPPRESS)
    ui.set_defaults(handler=_ui)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if not args.command:
            args = parser.parse_args(['ui'])
        return int(args.handler(args))
    except (KeyboardInterrupt, EOFError):
        parser.exit(1, '\n配置已取消；已有知识库未删除。\n')
    except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as error:
        parser.exit(1, f"错误: {error}\n")


if __name__ == "__main__":
    main()
