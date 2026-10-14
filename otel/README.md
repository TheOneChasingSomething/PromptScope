# OTel layer — opencode → Collector → Phoenix

This folder extends the `analyze_jsonl` project with a telemetry layer that
captures **live** opencode sessions and sends them, secret-scrubbed, to a
self-hosted **Arize Phoenix** instance.

```
 opencode ──OTLP/HTTP──▶ otel-collector ──redaction──▶ phoenix
 (plugin)                (control point)               (traces + eval UI)
```

Phoenix is deliberate: the same instance is reused later for **RAG tracing and
evaluation during the thesis**, so the observability backend is set up once.

## Why a collector in the middle

The collector is the control point. It:

1. receives raw OTLP from the opencode OTel plugin;
2. **redacts** CTF flags and credentials out of span values before they are
   stored (see `otel-collector-config.yaml` → `redaction`);
3. forwards clean traces to Phoenix, and can fan out to other backends later
   without touching opencode.

## Endpoints

| Name                     | Address                               | Purpose                                  |
| ------------------------ | ------------------------------------- | ---------------------------------------- |
| collector-otlp-http      | `http://localhost:4318`               | opencode sends OTLP here (`/v1/traces`)  |
| collector-otlp-grpc      | `localhost:4317`                      | optional gRPC alternative for opencode   |
| phoenix-otlp-http        | `http://phoenix:6006/v1/traces`       | collector → Phoenix (compose network)    |
| phoenix-ui               | `http://localhost:6006`               | Phoenix web UI (traces, projects, evals) |

## Run

Prerequisites: Docker + Docker Compose, and opencode installed.

```bash
# 1. Start Phoenix + the collector
docker compose -f otel/docker-compose.yml up -d

# 2. Register the opencode plugin (merge into ~/.config/opencode/opencode.json)
cat otel/opencode.config.example.json

# 3. Point opencode at the collector, then launch it
source otel/opencode.env.example
opencode

# 4. Open the UI
open http://localhost:6006
```

> The OTel plugin (`@devtheops/opencode-plugin-otel`) is **community**, not
> official. Review its source before trusting it with transcripts that may
> contain flags or secrets. Redaction here is defence in depth, not a reason
> to skip that review.

## Redaction

`redaction` masks any attribute **value** matching a pattern, regardless of the
key the plugin used — robust to not knowing the plugin's schema. Out of the box
it covers CTF flags (`flag{…}`, `hackropole{…}`, generic `NAME{…}`), API keys
(Anthropic, OpenAI-style, AWS, GitHub, bearer) and PEM private keys. Add your
own patterns under `blocked_values`. To keep correlation without cleartext,
enable `hash_function: sha256`. For full prompt/response removal, enable the
optional `transform` block.

## Reuse for the thesis (RAG)

Instrument the RAG pipeline with OpenTelemetry / OpenInference and point it at
the **same collector** (`http://localhost:4318`). Retrieval and generation
spans land in Phoenix next to the opencode traces, ready for Phoenix's
evaluation tooling — one backend for both agent-cost observability and RAG
quality evaluation.

## Also feeding this collector: the analyzer (historical + typed questions)

The live plugin does not produce the project's per-question **type**
classification, nor does it cover sessions recorded before it was enabled. The
`analyze_jsonl` tool can emit its own OTLP spans (one trace per session, one
span per question, with `tokens` / `cost` / `category` / `agents` / `commands`
attributes) to the same `http://localhost:4318` endpoint — not yet wired here.

---

# Evidence layer (`evidence/`)

Phoenix is for **exploring and comparing** runs. It is not an evidence store.
The evidence spec requires a separate, tamper-evident archive, produced by a
supervisor the agents cannot modify, that lets a third party verify results
**without trusting the agent's own report and without depending on Phoenix's
current state**.

## The boundary that shapes everything

The collector sits **after redaction**. So anything it writes — including the
new `file/evidence` exporter — is **sanitized telemetry**, i.e. the
*analysis view*, never the *restricted reference archive*. Three things stay
distinct and must not be conflated:

| Concept | Where it is captured | What you may claim |
| --- | --- | --- |
| What the agent **declares** | session exports (`sessions/`) | "the agent reported X" |
| What the environment **observed** | tool executor + `auditd` | "the executor/kernel recorded X" |
| What was **preserved unchanged** | sealed archive (`seal-run.sh`) | "these bytes have not changed since sealing" |

A signature proves byte integrity. It does not turn an inaccurate declaration
into a reliable observation, nor does it prove the collection was complete.

## Architecture

```
 OpenCode ──► tool executor ──► program under test
    │              │
    │ events       │ commands, outputs, return codes
    └──────┬───────┘
           ▼
   Operator-controlled collection   (evidence/run-experiment.sh)
           │
   ┌───────┴────────┐
   ▼                ▼
 Protected run      Transform / redaction
 archive (sealed)        │
 seal-run.sh             ▼
                 Collector ──► Phoenix   (+ file/evidence: sanitized copy)
```

## Files

| File | Role | Runs as / where |
| --- | --- | --- |
| `run-experiment.sh` | Mints `run_id` **before** the agent; captures inputs, environment, events, sessions (+children), artifacts; writes an honest `completeness.json`. | Operator; base-level collection |
| `seal-run.sh` | Freezes a run, builds `manifest.json` (path/size/sha256/**origin**), then encrypts + signs. | **Archival host/account**, never the agent |
| `auditd-scope.sh` | Adds/verifies/removes a **targeted** `auditd` rule for the experiment UID (execve/fork/clone family). | Operator (sudo) |
| `completeness.example.json`, `manifest.example.json` | Proposed schemas. | reference |

## What the collector change adds

`file/evidence` writes a **sanitized** copy of the trace stream to a
collector-only volume (`evidence-staging`, not mounted into any agent). Known
limits of the `fileexporter`: no `fsync`, no `sending_queue`/`retry_on_failure`,
and one JSON line **per OTLP batch** (not per span) — so an abrupt stop can lose
the tail. Treat it as a delivery copy; the operator transfers **closed** files
into the sealed archive. Option names were validated against
`otelcol-contrib 0.116.0`; re-validate if you change the image.

## Minimal protocol

1. **Before** opencode: `auditd-scope.sh add <lab-user>` then `witness`.
2. **During**: `run-experiment.sh "<task>"` keeps OTel/Phoenix + `run --format
   json` + DEBUG logs. Avoid recursive `strace` when GDB is in the chain
   (`ptrace` attach conflict) — prefer `auditd`/eBPF.
3. **After**: `auditd-scope.sh collect`, freeze the folder, then — on the
   archival host — `seal-run.sh <run_dir>`, and copy the sealed package to a
   second, separately-administered location (ideally WORM / S3 Object Lock).

Test the rig on four cases before any campaign: normal run, abrupt interruption,
Phoenix unavailable, and a tampered archived copy (integrity check must fail
while the protected original survives).

## Reconciling Phoenix with the archive

Carry only join keys in Phoenix — `audit.run_id`, `audit.artifact_id`,
`audit.collection_status` (custom attributes, not OpenInference fields). The
protected registry links `artifact_id` → archive hash → location. Verify
collection by **matching ids** (calls started vs completed, sessions discovered
vs exported, files expected vs present), not by counting spans — OTLP may
deliver duplicates after a retry.
