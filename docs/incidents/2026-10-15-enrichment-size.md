# Daily enrichment size incident — 2026-10-15

## Root cause and evidence limits

The send path rendered a schema-2.0 interactive card and tried full size, then compaction levels 1–3. It measured only the initial body, without future 精读 links. Enrichment reconstructed the full frozen section templates, added ready document URLs, and called the renderer with `allow_compaction=False`. Thus it could reject both a barely-fitting full initial card and an initial card that had already needed compaction. The failure happened before an edit intent or PATCH; the original message was preserved.

The 29,000-byte guard is **UTF-8 bytes of the compact JSON serialized send envelope**, including `receive_id` (conservatively 128 characters), `msg_type`, the JSON card serialized again as the `content` string, `uuid` (36 characters), and `reply_in_thread`. It is neither visible text length nor raw card JSON bytes. PATCH only sends `content`; using the send-envelope guard there is conservative. The approximately 30 KB documented card-content/request-body limit mentioned in the incident is distinct from this application's 29,000-byte safety budget; this change does not raise the budget. Vendor documentation was not fetched under the no-contact constraint.

Local evidence checked: `/workspace/daily-check-2026-10-15/result.md` and available local SQLite snapshots. The report records nine episodes, an untouched interactive receipt, and the oversized-enrichment error; it does not save the frozen send envelope, original templates, or source transcript. Available snapshots do not contain today's daily job. **Whether today's initial send used full size or compaction, and its exact remaining bytes, cannot be established from these artifacts.** The expanded ~39.6 KB readback does not answer either question. A successful interactive send implies the selected rendition passed the conservative ≤29,000-byte guard. No production access was attempted.

## Changes

* Initial v4 rendering measures the complete frozen templates with a realistic `https://xxxx.feishu.cn/docx/` + 27-character URL for every eligible episode. It chooses compaction using that reserved rendition, then removes future link lines before sending. Placeholders never reach recipients. Existing indivisible-content send fallback remains available when even maximum compaction cannot form a card.
* Enrichment tries full size followed by the same levels 1–3, requiring exactly one interactive card. It fails safely only when maximum compaction cannot fit. V4 compaction retains headings, metadata, the complete 简介, 要点总结 structure, and every URL line; it removes optional advice and then whole trailing takeaways. Source/listening and 精读 links are never sacrificed to fit.
* Receipt identity, audience, outbox acknowledgement, external-edit detection, opaque-card revision checks, durable intents/receipts, and the 20-edit safety budget remain in force.
* Deepread model parsing and validation normalize every nested `evidence_ids` list: extract `C\d{4}` tokens from composite elements, deduplicate in order, and validate the resulting references against the actual supplied evidence. Core references can contain all recovered IDs. Elements with no valid ID remain invalid rather than disappearing.

The composite `C0062','C0063','C0070` was being treated as one nonexistent key. This alone **does not prove that the three individual IDs existed** in that production request. Their production transcript/batch is unavailable locally, so their existence cannot be confirmed. Source evidence generation is lossless; generation uses batches of at most five evidence blocks and consolidation uses full evidence. These three nonadjacent IDs cannot all be in one such batch; if they were batch-crossing references, normalization correctly still rejects the out-of-batch IDs. Tests cover both existing composite references and truly nonexistent extracted IDs. There is no evidence here that truncation caused this error.

## How today's existing message recovers after deployment

Previously `publish_enrichment` saved `daily_document_ready`, called `update_daily_message`, and completed even when individual targets failed (warning `daily_enrichment_target_failed`). Document recovery skipped completed jobs. There was no independent periodic edit retry or admin command calling this update path.

The existing scheduler runs every 30 seconds. With reader mode, the document compiler, active intended recipients, and a fully acknowledged original outbox, `enqueue_enrichment` now checks today's bundles for ready documents missing from per-message edit receipts. It queues an edit-only `daily_edit_retry` document job. The single document worker calls `update_daily_message` with the existing base job and existing receipt; it does not publish documents or send a new message.

Deploying on **2026-10-15 in the bot's configured local timezone**, with the existing database and ready documents retained, triggers recovery on the next scheduler scan (then worker availability). It does not require a new document to finish or an operator backfill. Successful receipts suppress further recovery. Each ready-document set gets at most three durable recovery attempts; new ready documents create a new bounded budget. Attempt markers survive restarts and are saved before network work. Prior-day bundles do not receive these new edit-only retries. Safety checks may still block a human-modified/ambiguous receipt or exhausted edit budget; those protections are intentional.

No deployment, push, Feishu/Railway contact, or message send was performed in this work.

## Validation

`PYTHONPATH=src .venv/bin/python -m pytest -q`: **996 passed, 1 skipped, 556 subtests passed**, with two dependency deprecation warnings (63.72 seconds). `git diff --check` passed.

Regression coverage includes realistic reserved-link sizing at the byte boundary, runtime reservation for every eligible section, placeholder exclusion from initial delivery, compaction with all links/layout/metadata/简介 retained, maximum-compaction rejection without editing, normalized evidence parsing before model validation, unknown-ID rejection, and 2026-10-15 edit-only retry exhaustion/new-document budgets/success suppression through the real worker. Existing receipt identity, human edits, opaque revisions, audience, and edit-budget tests also pass.
