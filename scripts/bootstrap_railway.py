# /// script
# requires-python = ">=3.11"
# dependencies = ["cryptography>=44,<46"]
# ///
"""Prepare a Railway project for News Officer without exposing credentials.

The script intentionally stops before deployment.  It can create or link a
project, ensure the service and /data volume exist, and stage variables with
``--skip-deploys``.  Feishu's App Secret is resolved from lark-cli's current
profile and every secret is sent to Railway over stdin, never in argv.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

RAILWAY_NPX_PACKAGE = "@railway/cli@5.49.2"
LARK_KEYCHAIN_SERVICE = "lark-cli"
LARK_MASTER_KEY_ACCOUNT = "master.key"
LARK_MASTER_KEY_BYTES = 32
LARK_GCM_NONCE_BYTES = 12
GO_KEYRING_BASE64_PREFIX = b"go-keyring-base64:"


class BootstrapError(RuntimeError):
    """An expected, user-actionable bootstrap failure."""


@dataclass(frozen=True)
class LarkApp:
    app_id: str
    profile: str
    secret_spec: str | Mapping[str, Any]

    @property
    def secret_source(self) -> str:
        if isinstance(self.secret_spec, str):
            return "legacy config value"
        return str(self.secret_spec.get("source", "unknown"))


def default_lark_config_path(environment: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environment is None else environment
    config_dir = env.get("LARKSUITE_CLI_CONFIG_DIR", "").strip()
    if config_dir:
        return Path(config_dir).expanduser() / "config.json"
    return Path.home() / ".lark-cli" / "config.json"


def select_lark_app(data: Mapping[str, Any], profile: str | None = None) -> LarkApp:
    apps = data.get("apps")
    if not isinstance(apps, list) or not apps:
        raise BootstrapError("lark-cli 配置中没有可用应用，请先运行 config init。")

    selector = (profile or str(data.get("currentApp", ""))).strip()
    selected: Mapping[str, Any] | None = None
    if selector:
        # lark-cli resolves profile names before App IDs.
        selected = next(
            (
                app
                for app in apps
                if isinstance(app, Mapping) and app.get("name") == selector
            ),
            None,
        )
        if selected is None:
            selected = next(
                (
                    app
                    for app in apps
                    if isinstance(app, Mapping) and app.get("appId") == selector
                ),
                None,
            )
        if selected is None:
            raise BootstrapError(f"找不到 lark-cli profile：{selector}")
    elif isinstance(apps[0], Mapping):
        selected = apps[0]

    if selected is None:
        raise BootstrapError("lark-cli 当前应用配置格式无效。")
    app_id = str(selected.get("appId", "")).strip()
    secret_spec = selected.get("appSecret")
    if not app_id or not isinstance(secret_spec, (str, Mapping)):
        raise BootstrapError("lark-cli 当前应用缺少 App ID 或 App Secret 引用。")
    profile_name = str(selected.get("name") or app_id)
    return LarkApp(app_id=app_id, profile=profile_name, secret_spec=secret_spec)


def load_lark_app(config_path: Path, profile: str | None = None) -> LarkApp:
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise BootstrapError(f"找不到 lark-cli 配置：{config_path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise BootstrapError(f"无法读取 lark-cli 配置：{config_path}") from error
    if not isinstance(data, Mapping):
        raise BootstrapError("lark-cli 配置顶层必须是 JSON object。")
    return select_lark_app(data, profile)


def safe_keychain_filename(account: str) -> str:
    """Mirror lark-cli's macOS account-to-file mapping."""

    return re.sub(r"[^a-zA-Z0-9._-]", "_", account) + ".enc"


def _lark_keychain_storage_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / LARK_KEYCHAIN_SERVICE


def decode_security_password(value: bytes) -> bytearray:
    """Decode a binary password returned by macOS ``security -w``.

    go-keyring wraps binary values once before storing them; lark-cli itself
    stores its 32-byte master key as base64, so current installations require
    two decoding layers. Older/plain keychain representations remain accepted.
    """

    encoded = value.strip()
    if encoded.startswith(GO_KEYRING_BASE64_PREFIX):
        try:
            encoded = base64.b64decode(
                encoded[len(GO_KEYRING_BASE64_PREFIX) :], validate=True
            )
        except ValueError as error:
            raise BootstrapError("lark-cli 钥匙串包装格式无效。") from error
    if len(encoded) == LARK_MASTER_KEY_BYTES:
        return bytearray(encoded)
    try:
        return bytearray(base64.b64decode(encoded, validate=True))
    except ValueError as error:
        raise BootstrapError("lark-cli 钥匙串主密钥格式无效。") from error


def _load_lark_master_key(storage_dir: Path) -> bytearray:
    file_fallback = storage_dir / "master.key.file"
    if file_fallback.is_file():
        key = bytearray(file_fallback.read_bytes())
    else:
        if sys.platform != "darwin" or shutil.which("security") is None:
            raise BootstrapError(
                "此 lark-cli Secret 需要 macOS 系统钥匙串；请在原 Mac 上执行初始化。"
            )
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                LARK_KEYCHAIN_SERVICE,
                "-a",
                LARK_MASTER_KEY_ACCOUNT,
                "-w",
            ],
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            raise BootstrapError(
                "无法读取 lark-cli 系统钥匙串，请解锁钥匙串后重试。"
            )
        key = decode_security_password(result.stdout)

    if len(key) != LARK_MASTER_KEY_BYTES:
        _wipe(key)
        raise BootstrapError("lark-cli 钥匙串主密钥长度无效。")
    return key


def decrypt_lark_secret(ciphertext: bytes, master_key: bytearray) -> bytearray:
    if len(ciphertext) <= LARK_GCM_NONCE_BYTES + 16:
        raise BootstrapError("lark-cli Secret 文件内容无效。")
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as error:
        raise BootstrapError(
            "缺少 cryptography；请使用 `uv run scripts/bootstrap_railway.py`。"
        ) from error

    nonce = ciphertext[:LARK_GCM_NONCE_BYTES]
    payload = ciphertext[LARK_GCM_NONCE_BYTES:]
    try:
        plaintext = AESGCM(bytes(master_key)).decrypt(nonce, payload, None)
    except Exception as error:
        raise BootstrapError("无法解密 lark-cli App Secret。") from error
    if not plaintext:
        raise BootstrapError("lark-cli App Secret 为空。")
    return bytearray(plaintext)


def resolve_lark_secret(app: LarkApp, config_path: Path) -> bytearray:
    spec = app.secret_spec
    if isinstance(spec, str):
        if not spec:
            raise BootstrapError("lark-cli App Secret 为空。")
        return bytearray(spec.encode())

    source = str(spec.get("source", ""))
    secret_id = str(spec.get("id", ""))
    if not secret_id:
        raise BootstrapError("lark-cli App Secret 引用缺少 id。")
    if source == "file":
        path = Path(secret_id).expanduser()
        if not path.is_absolute():
            path = config_path.parent / path
        try:
            secret = bytearray(path.read_bytes().strip())
        except OSError as error:
            raise BootstrapError("无法读取 lark-cli App Secret 文件。") from error
        if not secret:
            raise BootstrapError("lark-cli App Secret 文件为空。")
        return secret
    if source != "keychain":
        raise BootstrapError(f"不支持的 lark-cli Secret 来源：{source or 'unknown'}")

    expected_id = f"appsecret:{app.app_id}"
    if secret_id != expected_id:
        raise BootstrapError("lark-cli App ID 与 App Secret 钥匙串引用不匹配。")
    storage_dir = _lark_keychain_storage_dir()
    secret_path = storage_dir / safe_keychain_filename(secret_id)
    try:
        ciphertext = secret_path.read_bytes()
    except OSError as error:
        raise BootstrapError("找不到 lark-cli 加密的 App Secret。") from error

    master_key = _load_lark_master_key(storage_dir)
    try:
        return decrypt_lark_secret(ciphertext, master_key)
    finally:
        _wipe(master_key)


def _wipe(value: bytearray | None) -> None:
    if value is not None:
        for index in range(len(value)):
            value[index] = 0


def railway_cli() -> list[str]:
    installed = shutil.which("railway")
    if installed:
        return [installed]
    npx = shutil.which("npx")
    if npx:
        return [npx, "--yes", RAILWAY_NPX_PACKAGE]
    raise BootstrapError("找不到 Railway CLI 或 npx。")


def _child_environment(sensitive_names: Iterable[str] = ()) -> dict[str, str]:
    environment = dict(os.environ)
    # Do not unnecessarily copy application secrets into Railway subprocesses.
    for name in {"OPENAI_API_KEY", "FEISHU_APP_SECRET", *sensitive_names}:
        environment.pop(name, None)
    return environment


class Railway:
    def __init__(
        self,
        command: Sequence[str],
        sensitive_environment_names: Iterable[str] = (),
    ) -> None:
        self.command = list(command)
        self.sensitive_environment_names = frozenset(sensitive_environment_names)

    def run(
        self,
        arguments: Sequence[str],
        *,
        secret_stdin: bytearray | None = None,
        quiet: bool = False,
    ) -> str:
        command = [*self.command, *arguments]
        if not quiet:
            print(f"→ {shlex.join(command)}")
        secret_copy = bytes(secret_stdin) if secret_stdin is not None else None
        result = subprocess.run(
            command,
            input=secret_copy,
            check=False,
            capture_output=True,
            env=_child_environment(self.sensitive_environment_names),
        )
        if result.returncode != 0:
            diagnostic = (result.stderr + result.stdout).decode(errors="replace")
            if secret_copy:
                diagnostic = diagnostic.replace(
                    secret_copy.decode(errors="replace"), "<redacted>"
                )
            diagnostic = diagnostic.strip()[-2000:]
            detail = f"\n{diagnostic}" if diagnostic else ""
            raise BootstrapError(
                f"Railway 命令失败（exit {result.returncode}）："
                f"{shlex.join(command)}{detail}"
            )
        return result.stdout.decode(errors="replace")


def parse_json_output(output: str) -> Any:
    text = output.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise BootstrapError("Railway CLI 未返回有效 JSON。") from error


def walk_objects(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from walk_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_objects(child)


def has_named_service(payload: Any, service_name: str) -> bool:
    return any(str(item.get("name", "")) == service_name for item in walk_objects(payload))


def has_mount_path(payload: Any, mount_path: str) -> bool:
    for item in walk_objects(payload):
        for key, value in item.items():
            normalized = str(key).replace("_", "").lower()
            if normalized == "mountpath" and str(value).rstrip("/") == mount_path:
                return True
    return False


def runtime_variables(args: argparse.Namespace, app_id: str) -> dict[str, str]:
    values = {
        "FEISHU_APP_ID": app_id,
        "OPENAI_MODEL": args.openai_model,
        "NEWS_OFFICER_DAILY_TIME": args.daily_time,
        "NEWS_OFFICER_TIMEZONE": args.timezone,
        "NEWS_OFFICER_LOOKBACK_HOURS": str(args.lookback_hours),
        "NEWS_OFFICER_MAX_SUMMARIES": str(args.max_summaries),
        "NEWS_OFFICER_MAX_CANDIDATES": str(args.max_candidates),
        "NEWS_OFFICER_DB_PATH": "/data/news-officer.sqlite3",
    }
    if args.user_open_ids.strip():
        values["FEISHU_USER_OPEN_IDS"] = args.user_open_ids.strip()
    if args.group_chat_ids.strip():
        values["FEISHU_GROUP_CHAT_IDS"] = args.group_chat_ids.strip()
    return values


def read_openai_key(args: argparse.Namespace) -> bytearray:
    if args.openai_key_stdin:
        secret = bytearray(sys.stdin.buffer.read().strip())
    else:
        value = os.environ.get(args.openai_key_env, "").strip()
        if not value:
            value = getpass.getpass("OPENAI_API_KEY（输入隐藏）：").strip()
        secret = bytearray(value.encode())
    if not secret:
        raise BootstrapError("OPENAI_API_KEY 不能为空。")
    return secret


def confirm_apply(args: argparse.Namespace, app: LarkApp) -> None:
    if args.yes:
        return
    project_action = (
        f"创建项目 {args.project_name}"
        if args.create_project
        else f"关联项目 {args.project_id}"
        if args.project_id
        else "使用当前已关联项目"
    )
    print(
        f"即将{project_action}，准备 service {args.service}、/data 卷及变量；"
        "不会部署，也不会变更 Railway 套餐。"
    )
    print(f"飞书应用：{app.app_id}（Secret 来源：{app.secret_source}）")
    answer = input("输入 APPLY 继续：").strip()
    if answer != "APPLY":
        raise BootstrapError("已取消，未修改 Railway。")


def ensure_project(railway: Railway, args: argparse.Namespace) -> None:
    if args.create_project:
        railway.run(["init", "--name", args.project_name, "--json"])
    elif args.project_id:
        railway.run(
            [
                "link",
                "--project",
                args.project_id,
                "--environment",
                args.environment,
                "--json",
            ]
        )
    else:
        railway.run(["status", "--json"], quiet=True)


def ensure_service(railway: Railway, service_name: str) -> None:
    services = parse_json_output(railway.run(["service", "list", "--json"], quiet=True))
    if not has_named_service(services, service_name):
        railway.run(["add", "--service", service_name, "--json"])
    railway.run(["service", "link", service_name])


def ensure_volume(railway: Railway, service_name: str) -> None:
    arguments = ["volume", "--service", service_name]
    volumes = parse_json_output(railway.run([*arguments, "list", "--json"], quiet=True))
    if not has_mount_path(volumes, "/data"):
        railway.run([*arguments, "add", "--mount-path", "/data", "--json"])


def set_variables(
    railway: Railway,
    service_name: str,
    variables: Mapping[str, str],
    feishu_secret: bytearray,
    openai_secret: bytearray,
) -> None:
    assignments = [f"{key}={value}" for key, value in sorted(variables.items())]
    railway.run(
        [
            "variable",
            "set",
            *assignments,
            "--service",
            service_name,
            "--skip-deploys",
            "--json",
        ]
    )
    for name, secret in (
        ("FEISHU_APP_SECRET", feishu_secret),
        ("OPENAI_API_KEY", openai_secret),
    ):
        railway.run(
            [
                "variable",
                "set",
                name,
                "--stdin",
                "--service",
                service_name,
                "--skip-deploys",
                "--json",
            ],
            secret_stdin=secret,
        )


def validate_args(args: argparse.Namespace) -> None:
    try:
        hours, minutes = (int(part) for part in args.daily_time.split(":"))
    except (TypeError, ValueError):
        hours, minutes = -1, -1
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise BootstrapError("--daily-time 必须是 HH:MM。")
    try:
        ZoneInfo(args.timezone)
    except ZoneInfoNotFoundError as error:
        raise BootstrapError(f"未知时区：{args.timezone}") from error
    if min(args.lookback_hours, args.max_summaries, args.max_candidates) < 1:
        raise BootstrapError("数值型运行参数必须大于 0。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="安全初始化新闻官的 Railway 项目（默认只预览，不部署）"
    )
    project = parser.add_mutually_exclusive_group()
    project.add_argument("--create-project", action="store_true")
    project.add_argument("--project-id")
    parser.add_argument("--project-name", default="news-officer")
    parser.add_argument("--service", default="news-officer")
    parser.add_argument("--environment", default="production")
    parser.add_argument("--lark-config", type=Path)
    parser.add_argument("--lark-profile")
    parser.add_argument("--openai-key-stdin", action="store_true")
    parser.add_argument("--openai-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--user-open-ids", default="")
    parser.add_argument("--group-chat-ids", default="")
    parser.add_argument("--openai-model", default="gpt-5.6-terra")
    parser.add_argument("--daily-time", default="08:30")
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--lookback-hours", type=int, default=72)
    parser.add_argument("--max-summaries", type=int, default=3)
    parser.add_argument("--max-candidates", type=int, default=16)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="执行初始化；省略时只显示计划，不访问密钥。",
    )
    parser.add_argument("--yes", action="store_true", help="跳过 APPLY 确认。")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        validate_args(args)
        config_path = (args.lark_config or default_lark_config_path()).expanduser()
        app = load_lark_app(config_path, args.lark_profile)
        project_action = (
            f"create {args.project_name}"
            if args.create_project
            else f"link {args.project_id}"
            if args.project_id
            else "use current link"
        )
        print(
            "计划："
            f"Railway {project_action} → service {args.service} → /data volume → "
            "non-secret variables → FEISHU_APP_SECRET/OPENAI_API_KEY via stdin"
        )
        print(f"飞书 App ID：{app.app_id}；Secret 来源：{app.secret_source}")
        print("本脚本不会部署服务，也不会创建或升级付费套餐。")
        if not args.apply:
            print("预览完成。确认后加 --apply；自动化场景可再加 --yes。")
            return 0

        confirm_apply(args, app)
        command = railway_cli()
        railway = Railway(command, {args.openai_key_env})
        railway.run(["whoami", "--json"], quiet=True)
        feishu_secret: bytearray | None = None
        openai_secret: bytearray | None = None
        try:
            # Resolve both credentials before the first Railway mutation so a
            # locked keychain or missing API key cannot leave partial setup.
            feishu_secret = resolve_lark_secret(app, config_path)
            openai_secret = read_openai_key(args)
            ensure_project(railway, args)
            ensure_service(railway, args.service)
            ensure_volume(railway, args.service)
            set_variables(
                railway,
                args.service,
                runtime_variables(args, app.app_id),
                feishu_secret,
                openai_secret,
            )
        finally:
            _wipe(feishu_secret)
            _wipe(openai_secret)

        print("Railway 初始化完成，尚未部署。")
        print(
            "下一步："
            f"{shlex.join([*command, 'up', '--service', args.service, '--detach'])}"
        )
        return 0
    except (BootstrapError, KeyboardInterrupt) as error:
        message = str(error) if str(error) else "已取消。"
        print(f"错误：{message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
