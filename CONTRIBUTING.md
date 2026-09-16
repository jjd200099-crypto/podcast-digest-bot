# 贡献指南

感谢参与情报官。这个项目最重要的边界是：只有取得完整、可核验的文字稿，才允许生成摘要。贡献代码时，请同时保护这一内容质量门槛和飞书、OpenAI 凭证的安全。

## 开发流程

1. 仓库成员从最新 `main` 创建分支，例如 `feat/add-podcast-source`、`fix/transcript-parser` 或 `docs/deployment-guide`。
2. 保持一次 PR 只解决一个清晰问题；如果修改行为，请补充或更新测试。
3. 提交前运行与 CI 相同的检查：

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install -r requirements.txt
   python -m pip install ruff
   ruff check src tests scripts
   python -m unittest discover -s tests -v
   python -m compileall -q src tests scripts
   ```

4. 推送分支并向 `main` 提交 PR。说明改动动机、验证方式和对生产配置的影响。
5. 等待 CI 通过和至少一位维护者审阅后再合并。推荐使用 squash merge，让 `main` 历史保持清楚。

## 内容与来源要求

- 新增节目优先使用官方 RSS 发现新集，优先获取官方 transcript，其次是出版方逐字稿或公开视频完整字幕。
- 不得仅凭标题、节目简介、章节、搜索摘要或片段生成内容摘要。
- 文字稿完整性判断、来源回退和失败路径都应有测试。无法取得完整文字稿时，必须跳过并明确说明。
- `episode_transcripts` 中的原始核验全文及 SHA-256 必须逐字保留，问答也只能使用这一证据层。用户附件是独立的精编阅读层：只允许删除高置信度广告、机械噪声、纯语气词和无信息量重复，不得覆盖原稿，不得删除实质问答、数字、异议或限定词，也不得把摘要或节选冒充全文。
- 播客问答只能引用已归档的完整文字稿。新增检索或交互行为时，应测试引用有效性、歧义选择、会话隔离，以及找不到依据时的安全失败。
- 摘要输出保持恰好 10 条短要点；嘉宾预测、公司主张和模型估算要明确归因。
- 修改 `feeds.json` 时，请在 PR 中附上节目官网或官方 RSS 链接，并说明为什么值得加入。

## 安全与配置

- 不得提交 `.env`、App Secret、API Key、访问令牌、真实用户 ID、真实群聊 ID 或本地数据库。
- 示例配置只能使用明显的占位符，例如 `replace_me`、`ou_xxx` 和 `oc_xxx`。
- 日志不得输出凭证、完整授权 URL 或带敏感参数的请求。
- 当前文件上传使用 `im:resource` 应用身份权限。新增飞书能力时只申请完成该能力所需的最小权限，并在 PR 中说明用途。
- 如果怀疑密钥已经进入提交历史，请立即停止推送并按 [SECURITY.md](SECURITY.md) 联系维护者。

## PR 审阅重点

维护者会重点检查：完整文字稿门槛是否被削弱、失败是否安全、幂等与持久化是否仍然成立、测试是否覆盖关键边界，以及是否引入了额外飞书权限或新的密钥暴露面。
