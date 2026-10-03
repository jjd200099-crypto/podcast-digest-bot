"""User-visible changes shipped with the application, not GitHub/PR activity."""

# Add a stable ID and the earliest eligible Beijing morning date for each
# meaningful deployed feature. Never advertise unimplemented plans or secrets.
RELEASE_NOTES = (
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
