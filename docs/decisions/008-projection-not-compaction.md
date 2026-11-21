# 008 Prompt is a projection, not a compaction

Do not concatenate full turn bodies into chat/completions. Do not LLM-summarize the log.

Traces stay append-only and complete. Assemble emits a bounded working set of frozen previews (768 chars). Overflow is `<ref digest=… session=… ts=…>` into that event, the same bytes every later turn.

Old large tool/command stdio is evicted first (`collapse_old_stdio`): pairing stays, body becomes a frozen `<stdio>` stub. Latest round keeps a preview. Small results stay. Then, if still over budget, oldest dialogue drops. No “N omitted” line.

NVIDIA NOOA pass-by-reference is the same idea: the model sees a preview, the body stays live in traces. Prefix cache still applies to the XML system block; a stub is stable once written.
