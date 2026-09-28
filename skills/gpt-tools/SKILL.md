---
name: gpt-tools
description: How to call the gpt-tools MCP server well — query and length constraints for gpt_search, batch vs shared-context choices for gpt_image_gen, save locations and filenames. Load before crafting a gpt_search research query, running multiple searches or image generations, or any gpt_image_gen call.
---

# Using gpt_search / gpt_image_gen

Server internals, launchd wiring, and failure modes live in
`~/Customization/gpt_tool_use/AGENTS.md` — this file is call-time usage only.

## gpt_search

- Craft the query as a full prompt — there is no system prompt; the string goes to ChatGPT
  as-is, so you control the format of what comes back.
- Always include an explicit format/length constraint, matched to the actual need: quick
  fact lookup → "answer in under 100 words" or a one-line answer; comparison or explainer →
  "short bullet points, ~250 words"; deep dive (architecture survey, library evaluation) →
  "thorough but no filler, up to ~800 words." Let the question drive length, not a reflex
  to be terse.
- Multiple `gpt_search` calls in one assistant message get serialized by the MCP harness —
  and serialized calls into one ChatGPT account are what trip its rate limit. For
  independent queries use `gpt_search_batch` (fans out concurrently, per-item `label`
  names each heading); fold related questions into one `gpt_search` prompt when they share
  context.

## gpt_image_gen

- Default save location: `<cwd>/generated/` (created if missing). Pass `save_dir` to
  override.
- Pick descriptive `filename_prefix` values so saved images stay findable across sessions
  (e.g. `style-v2-contact-sheet`); omitting it lands files under a hash-derived stem.
- Phrase the prompt as a full image-gen request — style lineage, palette with hex codes,
  typography mood, what to avoid. Asking for N images should yield N saved files.
- `embed_images=True` (default) returns the images in the response so you can analyze them
  and give feedback. During tight iteration loops where image blocks would flood context,
  set `embed_images=False` — paths only, then `Read` selectively.
- **Mode-picking for multiple images.** Every `gpt_image_gen` call and every
  `gpt_image_gen_batch` item opens a fresh ChatGPT chat. Images that should share design
  context (pages of one design system, variations on a theme) → put all requests in a
  **single `gpt_image_gen` call's prompt**, one chat with shared context. Isolated designs
  (independent A/B variants that shouldn't influence each other) → `gpt_image_gen_batch`,
  one item per design, run concurrently in fresh chats. Never issue multiple separate
  `gpt_image_gen` calls from one assistant message — the MCP harness serializes them.
  Pass `requests=[{prompt, filename_prefix?, save_dir?}, ...]` and batch-level
  `embed_images`. Recommended batch size: 2–3. Failures isolate per item.
