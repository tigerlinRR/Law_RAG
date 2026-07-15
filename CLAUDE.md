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
- **Never commit**: `data/`, `storage/`, `README.zh-CN.md` (English `README.md` only to
  GitHub), or `Richtech Materials for Potential AI Training.docx`. (All gitignored.)
- 8-K drafting: **facts never come from the model** — they come from the source document
  (extraction/delex + deterministic backfill), and the `lawrag.guardrail` RED-blocks any
  figure not grounded in the source. The fine-tuned adapter provides only *structure/style*
  (v4/v5 delex adapters emit typed placeholders, never real values). Every draft needs
  lawyer sign-off. **Live model + drafting-mode status changes often — check `progress.md`
  for which model is served (v2 interim vs v4/v5 delex) and the current default `mode`.**
- After editing `lawrag/*.py`: restart the web server and verify with a real HTTP
  request (no hot-reload).

See `progress.md` for everything else.
