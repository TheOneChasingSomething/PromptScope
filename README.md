# PromptScope

Analyze Claude Code session logs (JSONL): summaries, per-session transcripts,
per-question **cost and type** breakdowns, a local web viewer, and an optional
**OpenTelemetry → Phoenix** observability layer with a tamper-evident evidence
pipeline for OpenCode.

## Components

| Path | What it is |
| --- | --- |
| `analyze_jsonl.py` | CLI + local HTTP API over `~/.claude` logs. Pure Python stdlib. |
| `analyze_jsonl_config.json` | Backup, UI language, pricing table, classification keywords. |
| `index.html` | Static web viewer that talks to the API. |
| `COST-EXTENSION.md` | Design notes for the per-question cost/type extension. |
| `otel/` | OpenCode → Collector (redaction) → Phoenix stack + evidence tooling. |

## Requirements

- **Python 3.10+** (the analyzer uses `X | None` annotations). No third-party
  packages — standard library only.
- **Docker + Docker Compose** — only for the `otel/` layer.

## Install & run (fresh machine)

```bash
git clone https://github.com/TheOneChasingSomething/promptscope.git
cd promptscope
python3 analyze_jsonl.py --serve --host 127.0.0.1 --port 8765
```

Open `index.html` in a browser; it defaults to `http://127.0.0.1:8765`.

> The analyzer reads `~/.claude/history.jsonl` and
> `~/.claude/projects/-workspace` — i.e. **Claude Code** logs. On a fresh
> machine there is nothing to analyze until Claude Code has run (or point the
> viewer at a backup via the source selector). OpenCode uses a different
> storage format; use the `otel/` layer to observe OpenCode.

## CLI

```bash
python3 analyze_jsonl.py                     # summary of history.jsonl
python3 analyze_jsonl.py --list-sessions     # list sessions
python3 analyze_jsonl.py --session <id>      # one session's transcript
python3 analyze_jsonl.py --cost              # cost by question type / model / session
python3 analyze_jsonl.py --cost --session <id>   # per-question detail
```

## Cost & classification

Each user question is priced from token usage at its model's rate and tagged
with a type (`code_writing`, `code_analysis`, `web_deepsearch`, `agent_launch`,
`tool_onboarding`, `technical_conversation`), with agent/command counts.
Pricing and keywords live in `analyze_jsonl_config.json`. **The default rates
are examples** — verify them at
<https://platform.claude.com/docs/en/about-claude/pricing>. See
`COST-EXTENSION.md`.

## OTel / Phoenix observability (`otel/`)

```
OpenCode ──OTLP──▶ otel-collector ──redaction──▶ Phoenix
```

Captures live OpenCode sessions, scrubs secrets/flags in the collector, and
sends sanitized traces to a self-hosted Phoenix (reused later for RAG tracing
and evaluation). An evidence layer (`otel/evidence/`) adds an operator-run
supervisor, GPG sealing, and targeted `auditd` scoping for reproducible,
verifiable runs. See `otel/README.md`.

```bash
cd otel && docker compose up -d       # Phoenix :6006, collector :4318/:4317
```

## Security

Transcripts may contain secrets/flags. The API binds to `127.0.0.1` by default
— do not expose `--host` beyond localhost without authentication in front.
