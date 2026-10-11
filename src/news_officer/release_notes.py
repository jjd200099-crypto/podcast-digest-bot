"""User-visible changes shipped with the application, not GitHub/PR activity."""

# Add a stable ID and the earliest eligible Beijing morning date for each
# meaningful deployed feature. Never advertise unimplemented plans or secrets.
RELEASE_NOTES = (
    {
        "id": "2026-10-11-full-reading-documents",
        "date": "2026-10-11",
        "text": "“值得看全文”和“值得编译”的节目都会附飞书精读链接；文末附完整中文对谈，去掉时间戳，姓名加粗，按发言轮次整理。日报仍为一条消息。",
    },
    {
        "id": "2026-10-10-single-reader-edition",
        "date": "2026-10-10",
        "text": "日报改为一条消息，正文只保留推荐内容和精读链接；补齐的旧节目并入后续日报。",
    },
    {
        "id": "2026-10-08-podwise-rate-limit-recovery",
        "date": "2026-10-08",
        "text": (
            "Podwise 限流时会共享冷却并保存待办，不再连续重复请求；其他可用全文仍可整理。"
            "历史补抓分批处理，正式日报优先。另增加了运行在 GitHub 的独立日报送达检查。"
        ),
    },
    {
        "id": "2026-10-04-editorial-and-reliability",
        "date": "2026-10-04",
        "text": (
            "日报现在只展示达到推荐门槛的内容，无关节目仍保留全文归档，不占正文。"
            "连续追问会重新核对原文；也可以在对话中登记长期表达或选题偏好，先供审阅，不直接改动全群规则。"
            "新增早报前的有限预处理和送达状态检查，减少重复处理并发现积压。"
        ),
    },
    {
        "id": "2026-10-04-podwise-discovery",
        "date": "2026-10-04",
        "text": (
            "新增 Podwise 扩展发现：除了原有关注列表，也会寻找其他节目的优质 AI 访谈。"
            "取得并核验全文后，按相关度和信息密度筛选；入选内容会标注“Podwise 扩展发现”。"
            "这是扩大搜索范围，不代表每天都一定有列表外节目入选。"
        ),
    },
)


def render_release_notes(notes: list[dict]) -> str:
    if not notes:
        return ""
    return "功能更新\n\n" + "\n\n".join(note["text"] for note in notes)
