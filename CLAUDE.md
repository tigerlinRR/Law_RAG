# CLAUDE.md — project guide for Claude Code

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
- 8-K drafting: facts always from the source document (RAG), never fine-tuned in; every
  draft needs lawyer sign-off. The style adapter learns *how/what to disclose*, not facts.
- After editing `lawrag/*.py`: restart the web server and verify with a real HTTP
  request (no hot-reload).

See `progress.md` for everything else.
