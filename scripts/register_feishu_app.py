"""Create the minimal Feishu app and store its secret without printing it."""

import argparse
import subprocess

import lark_oapi as lark


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--allow-existing",
        action="store_true",
        help="Allow selecting an existing app instead of forcing a new app.",
    )
    parser.add_argument(
        "--app-id",
        help="Bind one known existing app directly (implies --allow-existing).",
    )
    return parser.parse_args()


def show_confirmation(info: dict) -> None:
    print("请在浏览器打开以下飞书官方确认页：")
    print(info["url"])


args = parse_args()

result = lark.register_app(
    on_qr_code=show_confirmation,
    app_preset={
        "name": "情报官",
        "desc": "通过飞书收发播客与科技情报，分析和存储由独立云服务完成。",
    },
    addons={
        "preset": False,
        "scopes": {
            "tenant": [
                "im:message.p2p_msg:readonly",
                "im:message.group_at_msg:readonly",
                "im:message:send_as_bot",
            ],
            "user": [],
        },
        "events": {"items": {"tenant": ["im.message.receive_v1"], "user": []}},
        "callbacks": {"items": []},
    },
    create_only=not (args.allow_existing or args.app_id),
    app_id=args.app_id,
)

app_id = str(result["client_id"])
app_secret = str(result["client_secret"])

# Keep lark-cli usable for smoke tests while avoiding a secret in argv/output.
subprocess.run(
    [
        "lark-cli",
        "config",
        "init",
        "--app-id",
        app_id,
        "--app-secret-stdin",
        "--brand",
        "feishu",
    ],
    input=app_secret,
    text=True,
    check=True,
    stdout=subprocess.DEVNULL,
)

user_info = result.get("user_info") or {}
print(f"情报官应用已创建：{app_id}")
if user_info.get("open_id"):
    print(f"创建者 open_id：{user_info['open_id']}")
print("App Secret 已由 lark-cli 安全存入系统钥匙串，未显示明文。")
