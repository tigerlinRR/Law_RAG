"""Law_RAG — a fully local, private document knowledge base for a law firm.

Phase 1 scope: ingest PDF/Word documents with metadata, then retrieve them via
hybrid search (semantic vector + keyword) with source citations. No AI drafting.

All heavy ML (embeddings) runs in local vLLM docker containers; this package is
lightweight host-side code (parse / chunk / store / retrieve) with no torch dep.
"""

__version__ = "0.1.0"
