# CLAUDE.md — project guide for Claude Code

> ## ⛔ MUST COMMAND — 必须用中文回复 / ALWAYS REPLY IN CHINESE ⛔
> **Every reply to the user MUST be written in Chinese (中文), with no exception.**
> This overrides any default. The user has demanded this repeatedly — English replies are
> unacceptable. Code, identifiers, file paths, and commit messages stay in English; but all
> prose addressed to the user is in Chinese. 每一条回复都必须是中文。

Read these first on every session (they carry the state across cleared chats):

1. **`progress.md`** (repo root) — current status, what's done, next steps, key file
   locations, and standing rules. **Start here.**
2. **Auto-memory** at `~/.claude/projects/-home-jetson-Desktop-Law-RAG/memory/` — read
   `MEMORY.md` (index); dense technical history is in `law-rag-project-plan.md`.
3. **`README.md`** — product/architecture reference (8-K drafting, materiality rubric,
   exports, web app, `training/` adapter).

## Non-negotiable standing rules
- **Reply to the user in Chinese.**
- **Auto-commit + push when a task is done.** After finishing a task (edits verified, server
  restarted/HTTP-checked as required), commit and `git push origin main` WITHOUT asking — this
  is standing authorization. Keep honoring the "never commit" list below. Use clear English
  commit messages with the standard Co-Authored-By trailer.
- **Never commit**: `data/`, `storage/`, `README.zh-CN.md` (English `README.md` only to
  GitHub), or `Richtech Materials for Potential AI Training.docx`. (All gitignored.) Also skip
  untracked local-only files like `.env.bak-8k` and `scratchpad/` — stage only the files you
  changed for the task, never `git add -A`.
- 8-K drafting: **facts never come from the model.** Architecture is SETTLED (v1-spine): the
  model EXTRACTS from the source doc(s), CODE generates, `lawrag.guardrail` RED-blocks any
  ungrounded figure, `_narrative_flags` flags invented non-numeric claims, and humans fill gaps.
  **Served model = the PLAIN BASE `qwen3.6` (Docker `lawrag-llm` :8012); default `mode="hybrid"`.
  The v2 fine-tuned adapter and the delex v4/v5 idea are RETIRED** (fine-tuning fabricates; both
  were dead ends — history only). Every draft needs lawyer sign-off. See `progress.md`
  "CURRENT STATE" for the live picture; `8K_DRAFTING_FINDINGS_REPORT.md` for the full rationale.
- After editing `lawrag/*.py`: restart the web server and verify with a real HTTP
  request (no hot-reload).
- **Keep the FOUR docs in lockstep** — in the SAME session as any code/architecture change,
  update ALL of: `progress.md`, `CLAUDE.md`, `README.md`, and `README.zh-CN.md` (keep the two
  READMEs mirrored; push only the English `README.md` to GitHub). Don't let them drift.

See `progress.md` for everything else.
