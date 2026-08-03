#!/usr/bin/env python3
"""
Analyse les fichiers JSONL de Claude Code.

Modes :
  (défaut)          résumé de ~/.claude/history.jsonl
  --list-sessions   liste les sessions disponibles
  --session <id>    affiche le détail d'une session (commandes + thinking + texte)

Sauvegarde des sessions :
  --backup          force une sauvegarde des sessions avant l'exécution
  --no-backup       désactive la sauvegarde automatique pour cette exécution
  --backup-dir DIR  répertoire de sauvegarde (remplace la config)

  La sauvegarde automatique (à chaque lancement du script, quel que soit le
  mode) est pilotée par analyze_jsonl_config.json, à côté de ce script.

API HTTP :
  --serve           démarre le serveur API au lieu du mode CLI
  --host HOST       adresse d'écoute (défaut : 127.0.0.1, local uniquement)
  --port PORT       port d'écoute (défaut : 8765)

  Endpoints : GET /health, GET /config, GET /sources, GET /history,
              GET /sessions, GET /sessions/<id>[?tool=NOM&no_output=1],
              GET /cost[?session=<id>], POST /backup

Coûts par question :
  --cost            rapport de coûts par type de question (agrège toutes les sessions)
  --cost --session <id>   détail par question d'une session (type, coût, agents, commandes)

  Chaque question utilisateur est classée (écriture de code, analyse de code,
  conversation technique, prise en main/documentation, deepsearch web, lancement
  d'agents) et chiffrée en USD à partir des tokens et des tarifs par modèle
  (analyze_jsonl_config.json → "pricing" / "classification").
  Les transcripts pouvant contenir des secrets/flags, ne pas exposer
  --host au-delà de 127.0.0.1 sans authentification en amont.

  La langue de l'interface web (web/index.html) est pilotée par le champ
  "language" ("fr" ou "en", défaut "fr") de analyze_jsonl_config.json,
  servi via GET /config — pas de sélecteur de langue côté client.
"""

import json
import sys
import shutil
import argparse
from pathlib import Path
from datetime import datetime
from collections import defaultdict, Counter

HISTORY_FILE = Path.home() / ".claude" / "history.jsonl"
SESSIONS_DIR = Path.home() / ".claude" / "projects" / "-workspace"
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "analyze_jsonl_config.json"
DEFAULT_CONFIG = {
    "auto_backup": True,
    "backup_dir": "session_backups",
    "language": "fr",
}

SUPPORTED_LANGUAGES = ("fr", "en")


def resolve_language(config: dict) -> str:
    lang = config.get("language", "fr")
    return lang if lang in SUPPORTED_LANGUAGES else "fr"

# Couleurs ANSI
C = {
    "reset":   "\033[0m",
    "bold":    "\033[1m",
    "dim":     "\033[2m",
    "cyan":    "\033[36m",
    "yellow":  "\033[33m",
    "green":   "\033[32m",
    "magenta": "\033[35m",
    "blue":    "\033[34m",
    "red":     "\033[31m",
}


def color(text: str, *keys) -> str:
    if not sys.stdout.isatty():
        return text
    codes = "".join(C[k] for k in keys)
    return f"{codes}{text}{C['reset']}"


def load_jsonl(path: Path) -> list[dict]:
    records = []
    with open(path) as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"[WARN] ligne {i} invalide : {e}", file=sys.stderr)
    return records


def fmt_ts(ts) -> str:
    """Accepte ms (int) ou ISO-8601 (str)."""
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return ts
    return str(ts)


def get_session_file(session_id: str, sessions_dir: Path = SESSIONS_DIR) -> Path | None:
    """Trouve le fichier .jsonl d'une session par préfixe ou ID complet."""
    if not sessions_dir.exists():
        return None
    for p in sessions_dir.glob("*.jsonl"):
        if p.stem.startswith(session_id):
            return p
    return None


# ─── MODE : liste des sessions ───────────────────────────────────────────────

def list_sessions(sessions_dir: Path = SESSIONS_DIR) -> None:
    records = load_jsonl(HISTORY_FILE)

    sessions: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        sid = r.get("sessionId")
        if sid:
            sessions[sid].append(r)

    print(color(f"Sessions disponibles (source : {sessions_dir}) :", "bold"))
    print(f"  {'ID (8 car.)':<12}  {'Début':<20}  {'Prompts':>7}  {'Fichier session'}")
    print("  " + "─" * 70)

    for sid, msgs in sorted(sessions.items(), key=lambda x: min(r.get("timestamp", 0) for r in x[1])):
        first_ts = min(r.get("timestamp", 0) for r in msgs)
        ts = fmt_ts(first_ts) if first_ts else "?"
        sf = get_session_file(sid, sessions_dir)
        sf_label = color("✓ " + sf.name[:36], "green") if sf else color("✗ (pas de détails)", "dim")
        print(f"  {sid[:8]:<12}  {ts:<20}  {len(msgs):>7}  {sf_label}")

    print()
    print("Utilisation : python3 analyze_jsonl.py --session <8-premiers-caractères>")


# ─── MODE : détail d'une session ─────────────────────────────────────────────

def load_session_records(
    session_id: str, sessions_dir: Path = SESSIONS_DIR
) -> tuple[Path, list[dict], dict[str, str]] | None:
    """Charge une session et indexe ses tool_result par tool_use_id.

    Partagé entre le rendu CLI (render_session) et l'API JSON (session_detail_json)
    pour ne parser le fichier qu'une fois par appel.
    """
    sf = get_session_file(session_id, sessions_dir)
    if sf is None:
        return None

    records = load_jsonl(sf)

    tool_results: dict[str, str] = {}
    for r in records:
        if r.get("type") == "user":
            content = r.get("message", {}).get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tid = block.get("tool_use_id", "")
                        result_content = block.get("content", "")
                        if isinstance(result_content, list):
                            result_content = "\n".join(
                                b.get("text", "") for b in result_content if isinstance(b, dict)
                            )
                        tool_results[tid] = str(result_content)

    return sf, records, tool_results


def render_session(
    session_id: str, tool_filter: str | None, no_output: bool, sessions_dir: Path = SESSIONS_DIR
) -> None:
    loaded = load_session_records(session_id, sessions_dir)
    if loaded is None:
        print(f"Aucun fichier de session trouvé pour : {session_id} (source : {sessions_dir})", file=sys.stderr)
        sys.exit(1)
    sf, records, tool_results = loaded

    # Filtrer uniquement les prompts utilisateur et réponses assistant
    print(color(f"=== Session {session_id[:8]}… — {sf.name} ===", "bold", "cyan"))
    print()

    turn = 0
    for r in records:
        rtype = r.get("type")

        # ── Prompt utilisateur ──
        if rtype == "user":
            msg = r.get("message", {})
            content = msg.get("content", "")
            ts = fmt_ts(r.get("timestamp", ""))

            # Ignorer les messages purement technique (tool_result only)
            if isinstance(content, list):
                texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                if not texts:
                    continue
                text = "\n".join(texts)
            elif isinstance(content, str):
                text = content
            else:
                continue

            text = text.strip()
            if not text:
                continue

            turn += 1
            print(color(f"[{ts}] ", "dim") + color(f"HUMAIN #{turn}", "bold", "yellow"))
            print(f"  {text}")
            print()

        # ── Réponse assistant ──
        elif rtype == "assistant":
            ts = fmt_ts(r.get("timestamp", ""))
            content = r.get("message", {}).get("content", [])
            if not isinstance(content, list):
                continue

            has_output = False
            blocks_output = []

            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")

                if btype == "thinking":
                    thinking_text = block.get("thinking", "").strip()
                    if thinking_text:
                        blocks_output.append(("thinking", None, thinking_text))
                    else:
                        blocks_output.append(("thinking_encrypted", None, None))

                elif btype == "text":
                    text = block.get("text", "").strip()
                    if text:
                        blocks_output.append(("text", None, text))

                elif btype == "tool_use":
                    tool_name = block.get("name", "?")
                    tool_id = block.get("id", "")
                    inp = block.get("input", {})

                    # Filtrer par outil si demandé
                    if tool_filter and tool_name.lower() != tool_filter.lower():
                        continue

                    result = tool_results.get(tool_id, "")
                    blocks_output.append(("tool_use", tool_name, (tool_id, inp, result)))

            if not blocks_output:
                continue

            print(color(f"[{ts}] ", "dim") + color("ASSISTANT", "bold", "green"))

            for btype, tool_name, data in blocks_output:
                has_output = True

                if btype == "thinking_encrypted":
                    print(color("  [thinking — chiffré]", "dim"))

                elif btype == "thinking":
                    print(color("  ┌─ THINKING ─────────────────────────────", "magenta"))
                    for line in data.splitlines():
                        print(color(f"  │ ", "magenta") + color(line, "dim"))
                    print(color("  └─────────────────────────────────────────", "magenta"))

                elif btype == "text":
                    for line in data.splitlines():
                        print(f"  {line}")

                elif btype == "tool_use":
                    tool_id, inp, result = data
                    cmd = inp.get("command", "")
                    desc = inp.get("description", "")
                    # Pour les outils non-Bash, afficher l'input complet
                    if not cmd:
                        cmd = json.dumps(inp, ensure_ascii=False)

                    print(color(f"  ┌─ {tool_name} ", "blue") + color(f"─ {desc}", "dim") if desc else color(f"  ┌─ {tool_name}", "blue"))
                    print(color("  │ ", "blue") + color("$ ", "bold") + cmd)

                    if not no_output and result:
                        result_lines = result.splitlines()
                        max_lines = 30
                        print(color("  ├─ output:", "dim"))
                        for line in result_lines[:max_lines]:
                            print(color("  │ ", "dim") + line)
                        if len(result_lines) > max_lines:
                            print(color(f"  │ … ({len(result_lines) - max_lines} lignes supplémentaires)", "dim"))
                    print(color("  └─────────────────────────────────────────", "blue"))

            print()


# ─── MODE : résumé de history.jsonl ──────────────────────────────────────────

def fmt_ts_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def analyze_history(verbose: bool = False, sessions_dir: Path = SESSIONS_DIR) -> None:
    records = load_jsonl(HISTORY_FILE)
    print(color(f"=== history.jsonl — {len(records)} enregistrements (source sessions : {sessions_dir}) ===", "bold"), "\n")

    all_fields = Counter()
    for r in records:
        all_fields.update(r.keys())
    print("Champs présents :")
    for field, count in all_fields.most_common():
        print(f"  {field:<25} {count:>4}")

    ts_records = [r for r in records if "timestamp" in r]
    if ts_records:
        ts_sorted = sorted(ts_records, key=lambda r: r["timestamp"])
        print(f"\nPériode :")
        print(f"  Premier : {fmt_ts_ms(ts_sorted[0]['timestamp'])}")
        print(f"  Dernier : {fmt_ts_ms(ts_sorted[-1]['timestamp'])}")

    sessions: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        if "sessionId" in r:
            sessions[r["sessionId"]].append(r)
    if sessions:
        print(f"\nSessions ({len(sessions)}) :")
        for sid, msgs in sorted(sessions.items(), key=lambda x: min(r.get("timestamp", 0) for r in x[1])):
            first_ts = min(r.get("timestamp", 0) for r in msgs)
            ts = fmt_ts_ms(first_ts)
            sf = get_session_file(sid, sessions_dir)
            has_file = color("✓", "green") if sf else color("✗", "dim")
            print(f"  {has_file} {sid[:8]}…  {ts}  ({len(msgs)} prompts)")

    displays = [r for r in records if "display" in r]
    if displays:
        print(f"\nPrompts ({len(displays)}) :")
        for r in sorted(displays, key=lambda r: r.get("timestamp", 0)):
            ts = fmt_ts_ms(r["timestamp"]) if "timestamp" in r else "?"
            text = r["display"][:110].replace("\n", " ")
            sid = r.get("sessionId", "")[:8]
            print(f"  [{ts}] {color(sid, 'dim')}  {text}")

    print(f"\nAstuce : python3 analyze_jsonl.py --list-sessions")
    print(f"         python3 analyze_jsonl.py --session <id>")


# ─── VERSIONS JSON (partagées CLI/API) ───────────────────────────────────────

def sessions_list_json(sessions_dir: Path = SESSIONS_DIR) -> list[dict]:
    records = load_jsonl(HISTORY_FILE)
    sessions: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        sid = r.get("sessionId")
        if sid:
            sessions[sid].append(r)

    result = []
    for sid, msgs in sorted(sessions.items(), key=lambda x: min(r.get("timestamp", 0) for r in x[1])):
        first_ts = min(r.get("timestamp", 0) for r in msgs)
        sf = get_session_file(sid, sessions_dir)
        result.append({
            "session_id": sid,
            "first_seen_ms": first_ts,
            "prompt_count": len(msgs),
            "has_transcript": sf is not None,
            "transcript_file": sf.name if sf else None,
        })
    return result


def history_summary_json(sessions_dir: Path = SESSIONS_DIR) -> dict:
    records = load_jsonl(HISTORY_FILE)

    all_fields = Counter()
    for r in records:
        all_fields.update(r.keys())

    ts_records = [r for r in records if "timestamp" in r]
    period = None
    if ts_records:
        ts_sorted = sorted(ts_records, key=lambda r: r["timestamp"])
        period = {"first_ms": ts_sorted[0]["timestamp"], "last_ms": ts_sorted[-1]["timestamp"]}

    return {
        "record_count": len(records),
        "fields": dict(all_fields),
        "period": period,
        "sessions": sessions_list_json(sessions_dir),
    }


def session_detail_json(
    session_id: str,
    tool_filter: str | None = None,
    no_output: bool = False,
    sessions_dir: Path = SESSIONS_DIR,
) -> dict | None:
    loaded = load_session_records(session_id, sessions_dir)
    if loaded is None:
        return None
    sf, records, tool_results = loaded

    turns = []
    turn_num = 0
    usage_totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    for r in records:
        rtype = r.get("type")

        if rtype == "assistant":
            usage = r.get("message", {}).get("usage")
            if isinstance(usage, dict):
                for key in usage_totals:
                    usage_totals[key] += usage.get(key, 0) or 0

        if rtype == "user":
            content = r.get("message", {}).get("content", "")
            if isinstance(content, list):
                texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                if not texts:
                    continue
                text = "\n".join(texts)
            elif isinstance(content, str):
                text = content
            else:
                continue
            text = text.strip()
            if not text:
                continue
            turn_num += 1
            turns.append({
                "role": "human",
                "turn": turn_num,
                "timestamp": r.get("timestamp"),
                "text": text,
            })

        elif rtype == "assistant":
            content = r.get("message", {}).get("content", [])
            if not isinstance(content, list):
                continue

            blocks = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")

                if btype == "thinking":
                    thinking_text = block.get("thinking", "").strip()
                    if thinking_text:
                        blocks.append({"type": "thinking", "text": thinking_text})
                    else:
                        blocks.append({"type": "thinking_encrypted"})

                elif btype == "text":
                    text = block.get("text", "").strip()
                    if text:
                        blocks.append({"type": "text", "text": text})

                elif btype == "tool_use":
                    tool_name = block.get("name", "?")
                    if tool_filter and tool_name.lower() != tool_filter.lower():
                        continue
                    tool_id = block.get("id", "")
                    entry = {"type": "tool_use", "tool": tool_name, "input": block.get("input", {})}
                    if not no_output:
                        entry["output"] = tool_results.get(tool_id, "")
                    blocks.append(entry)

            if blocks:
                turns.append({
                    "role": "assistant",
                    "timestamp": r.get("timestamp"),
                    "blocks": blocks,
                })

    usage_totals["total_tokens"] = sum(usage_totals.values())

    # Enrichit chaque tour humain avec coût / type / compteurs (mêmes bornes de
    # tour que compute_turn_costs → alignement 1:1 dans l'ordre d'apparition).
    _config = load_config()
    _pricing, _ = load_pricing(_config)
    cost_turns = compute_turn_costs(records, _pricing, load_keywords(_config))
    human_turns = [t for t in turns if t.get("role") == "human"]
    for human, ct in zip(human_turns, cost_turns):
        human["category"] = ct["category"]
        human["cost"] = ct["cost"]
        human["cost_input"] = ct["cost_input"]
        human["cost_output"] = ct["cost_output"]
        human["token_subtotal"] = ct["total_tokens"]
        human["agents"] = ct["agents"]
        human["commands"] = ct["commands"]
        human["model"] = ct["model"]

    return {
        "session_id": session_id,
        "transcript_file": sf.name,
        "transcript_path": str(sf),
        "usage": usage_totals,
        "turns": turns,
    }


# ─── SAUVEGARDE DES SESSIONS ─────────────────────────────────────────────────

def load_config() -> dict:
    """Charge analyze_jsonl_config.json à côté du script (valeurs par défaut si absent/invalide)."""
    if not CONFIG_FILE.exists():
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[WARN] config invalide ({CONFIG_FILE}) : {e}", file=sys.stderr)
        return dict(DEFAULT_CONFIG)
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    return merged


def resolve_backup_dir(raw: str) -> Path:
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = SCRIPT_DIR / p
    return p


SOURCES = ("claude", "backup")


def resolve_source_dir(source: str | None) -> Path:
    """Choisit le dossier de sessions à interroger : le dossier .claude live
    (source="claude", défaut) ou le dossier de sauvegarde (source="backup")."""
    if source == "backup":
        config = load_config()
        return resolve_backup_dir(config.get("backup_dir", "session_backups"))
    if source not in (None, "claude"):
        raise ValueError(f"source inconnue : {source!r} (attendu : {', '.join(SOURCES)})")
    return SESSIONS_DIR


def backup_sessions(backup_dir: Path, quiet: bool = False) -> int:
    """Copie les fichiers/dossiers de session vers backup_dir (incrémental).

    Ne recopie que les fichiers absents ou dont la taille/date de modification
    a changé, pour rester rapide même appelé à chaque lancement du script.
    Retourne le nombre d'éléments copiés.
    """
    if not SESSIONS_DIR.exists():
        return 0
    backup_dir.mkdir(parents=True, exist_ok=True)
    copied = 0

    for p in SESSIONS_DIR.glob("*.jsonl"):
        dest = backup_dir / p.name
        src_stat = p.stat()
        if dest.exists():
            dest_stat = dest.stat()
            if dest_stat.st_size == src_stat.st_size and dest_stat.st_mtime >= src_stat.st_mtime:
                continue
        shutil.copy2(p, dest)
        copied += 1

    for d in SESSIONS_DIR.iterdir():
        if d.is_dir() and d.name != "memory":
            dest_dir = backup_dir / d.name
            if not dest_dir.exists():
                shutil.copytree(d, dest_dir)
                copied += 1

    if not quiet and copied:
        print(
            color(f"[backup] {copied} élément(s) sauvegardé(s) vers {backup_dir}", "dim", "green"),
            file=sys.stderr,
        )
    return copied


# ─── COÛTS & CLASSIFICATION DES QUESTIONS ────────────────────────────────────
#
# Attribue un COÛT et un TYPE à chaque question utilisateur (« tour »).
#
# Un tour = un enregistrement `user` porteur de texte. Son coût = somme des
# `message.usage` de tous les enregistrements `assistant` qui suivent, jusqu'au
# tour humain suivant. Chaque enregistrement assistant est facturé au tarif de
# SON propre `message.model` (gère les changements de modèle en cours de session).
#
# Les tarifs (USD par million de tokens) et les mots-clés de classification sont
# pilotés par analyze_jsonl_config.json ("pricing" et "classification") ; à
# défaut, les tables DEFAULT_* ci-dessous s'appliquent.
#
# ⚠ Les tarifs par défaut sont des EXEMPLES fondés sur les multiplicateurs de
#   cache standard (écriture ≈ 1,25× input, lecture ≈ 0,1× input). Vérifie et
#   ajuste-les via la config : https://platform.claude.com/docs/en/about-claude/pricing

DEFAULT_PRICING = {
    # clé = préfixe de la chaîne `model` ; correspondance par préfixe le plus long
    "claude-opus-4":    {"input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_read": 1.50},
    "claude-sonnet-4":  {"input": 3.0,  "output": 15.0, "cache_write": 3.75,  "cache_read": 0.30},
    "claude-haiku-4":   {"input": 1.0,  "output": 5.0,  "cache_write": 1.25,  "cache_read": 0.10},
    "claude-3-5-haiku": {"input": 0.80, "output": 4.0,  "cache_write": 1.00,  "cache_read": 0.08},
    "default":          {"input": 3.0,  "output": 15.0, "cache_write": 3.75,  "cache_read": 0.30},
}

# Catégories de question demandées, dans l'ordre de priorité de départage.
CATEGORY_ORDER = [
    "web_deepsearch",         # deepsearch sur le web
    "agent_launch",           # lancement d'agents
    "code_writing",           # écriture de code
    "code_analysis",          # analyse de code
    "tool_onboarding",        # prise en main d'un outil / documentation
    "technical_conversation", # conversation technique (fallback)
]

# Signal PRIMAIRE : les outils appelés par l'assistant durant le tour.
# (catégorie, poids par appel)
TOOL_CATEGORY_WEIGHTS = {
    "Write": ("code_writing", 3), "Edit": ("code_writing", 3),
    "MultiEdit": ("code_writing", 3), "NotebookEdit": ("code_writing", 3),
    "Create": ("code_writing", 2),
    "Read": ("code_analysis", 2), "Grep": ("code_analysis", 2), "Glob": ("code_analysis", 1),
    "WebSearch": ("web_deepsearch", 3), "WebFetch": ("web_deepsearch", 3),
    "Task": ("agent_launch", 4),
    "Bash": ("code_analysis", 1),  # ambigu : petit poids
}

# Signal SECONDAIRE : mots-clés dans le texte de la question (poids 1, plafonné).
DEFAULT_KEYWORDS = {
    "code_writing": ["écris", "ecris", "implémente", "implemente", "ajoute", "crée une fonction",
                     "cree une fonction", "corrige", "refactor", "write", "implement", "add a", "fix the"],
    "code_analysis": ["analyse", "revue", "review", "explique ce code", "comprendre", "audit",
                      "que fait", "vulnérab", "vulnerab", "explain this code", "trace"],
    "tool_onboarding": ["comment utiliser", "documentation", "installe", "configurer", "config",
                        "prise en main", "how to use", "setup", "install", "getting started", "docs"],
    "web_deepsearch": ["cherche sur le web", "recherche web", "deepsearch", "deep search",
                       "search the web", "look up online", "sur internet"],
    "agent_launch": ["lance des agents", "sous-agent", "sous-agents", "subagent", "subagents",
                     "en parallèle", "spawn agents", "plusieurs agents"],
    "technical_conversation": [],
}

TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def load_pricing(config: dict) -> tuple[dict, str]:
    """Retourne (table_de_prix, source) où source ∈ {'config','builtin_default'}."""
    pricing = {k: dict(v) for k, v in DEFAULT_PRICING.items()}
    user_pricing = config.get("pricing")
    source = "builtin_default"
    if isinstance(user_pricing, dict):
        source = "config"
        for key, rate in user_pricing.items():
            if key.startswith("_"):
                continue
            if isinstance(rate, dict):
                pricing[key] = rate
    return pricing, source


def load_keywords(config: dict) -> dict:
    kw = {k: list(v) for k, v in DEFAULT_KEYWORDS.items()}
    cls = config.get("classification")
    if isinstance(cls, dict) and isinstance(cls.get("keywords"), dict):
        for cat, words in cls["keywords"].items():
            if cat in kw and isinstance(words, list):
                kw[cat] = words
    return kw


def price_for_model(model: str | None, pricing: dict) -> dict:
    """Correspondance par préfixe le plus long ; repli sur 'default'."""
    if model:
        best = None
        for key in pricing:
            if key == "default":
                continue
            if model.startswith(key) and (best is None or len(key) > len(best)):
                best = key
        if best:
            return pricing[best]
    return pricing.get("default", DEFAULT_PRICING["default"])


def usage_cost(usage: dict, rate: dict) -> tuple[float, float]:
    """Retourne (coût_input, coût_output) en USD. L'input inclut cache write/read."""
    it = usage.get("input_tokens", 0) or 0
    ot = usage.get("output_tokens", 0) or 0
    cw = usage.get("cache_creation_input_tokens", 0) or 0
    cr = usage.get("cache_read_input_tokens", 0) or 0
    cost_in = (it * rate["input"] + cw * rate["cache_write"] + cr * rate["cache_read"]) / 1_000_000
    cost_out = (ot * rate["output"]) / 1_000_000
    return cost_in, cost_out


def classify_turn(text: str, tools: "Counter", keywords: dict) -> tuple[str, dict]:
    """Score chaque catégorie (outils = signal primaire, mots-clés = secondaire)."""
    scores = {c: 0.0 for c in CATEGORY_ORDER}
    for tool, n in tools.items():
        mapping = TOOL_CATEGORY_WEIGHTS.get(tool)
        if mapping:
            cat, w = mapping
            if cat in scores:
                scores[cat] += w * n
    low = (text or "").lower()
    for cat, words in keywords.items():
        if cat in scores and words:
            hits = sum(1 for kw in words if kw in low)
            scores[cat] += min(hits, 3)
    best, best_score = "technical_conversation", -1.0
    for cat in CATEGORY_ORDER:              # ordre = priorité de départage
        if scores[cat] > best_score:
            best, best_score = cat, scores[cat]
    if best_score <= 0:
        best = "technical_conversation"
    return best, {k: round(v, 1) for k, v in scores.items()}


def _day_of(ts) -> str:
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except ValueError:
            return ts[:10]
    return "?"


def compute_turn_costs(records: list[dict], pricing: dict, keywords: dict) -> list[dict]:
    """Découpe une session en questions et calcule coût + type + compteurs par question."""
    turns: list[dict] = []
    current: dict | None = None

    def flush():
        nonlocal current
        if current is None:
            return
        cat, scores = classify_turn(current["text"], current["_tools"], keywords)
        current["category"] = cat
        current["scores"] = scores
        current["total_tokens"] = sum(current["usage"].values())
        current["cost_input"] = round(current["cost_input"], 6)
        current["cost_output"] = round(current["cost_output"], 6)
        current["cost"] = round(current["cost_input"] + current["cost_output"], 6)
        current["tools"] = dict(current["_tools"])
        current["model"] = current["_models"].most_common(1)[0][0] if current["_models"] else None
        del current["_tools"], current["_models"]
        turns.append(current)
        current = None

    for r in records:
        rtype = r.get("type")

        if rtype == "user":
            content = r.get("message", {}).get("content", "")
            if isinstance(content, list):
                texts = [b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text"]
                text = "\n".join(t for t in texts if t)
            elif isinstance(content, str):
                text = content
            else:
                text = ""
            text = text.strip()
            if not text:
                continue  # tool_result seul : rattaché au tour courant
            flush()
            current = {
                "turn": len(turns) + 1,
                "timestamp": r.get("timestamp"),
                "text": text[:500],
                "usage": {k: 0 for k in TOKEN_KEYS},
                "cost_input": 0.0, "cost_output": 0.0,
                "agents": 0, "commands": 0,
                "_tools": Counter(), "_models": Counter(),
            }

        elif rtype == "assistant":
            if current is None:
                continue  # sortie assistant avant toute question : ignorée (alignement)
            msg = r.get("message", {})
            usage = msg.get("usage")
            model = msg.get("model", "")
            if isinstance(usage, dict):
                for k in TOKEN_KEYS:
                    current["usage"][k] += usage.get(k, 0) or 0
                ci, co = usage_cost(usage, price_for_model(model, pricing))
                current["cost_input"] += ci
                current["cost_output"] += co
                if model:
                    current["_models"][model] += 1
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        name = block.get("name", "?")
                        current["_tools"][name] += 1
                        if name == "Task":
                            current["agents"] += 1
                        elif name == "Bash":
                            current["commands"] += 1
    flush()
    return turns


def cost_report_json(sessions_dir: Path = SESSIONS_DIR, session: str | None = None) -> dict:
    """Agrège coûts et types sur toutes les sessions (option : détail d'une session)."""
    config = load_config()
    pricing, pricing_source = load_pricing(config)
    keywords = load_keywords(config)

    def bucket():
        return {"turns": 0, "tokens": 0, "cost": 0.0}

    per_session, by_type, by_model, by_day = [], defaultdict(bucket), defaultdict(bucket), defaultdict(bucket)
    totals = {"turns": 0, "tokens": 0, "cost": 0.0, "cost_input": 0.0,
              "cost_output": 0.0, "agents": 0, "commands": 0, "sessions": 0}
    target = None

    for meta in sessions_list_json(sessions_dir):
        if not meta["has_transcript"]:
            continue
        loaded = load_session_records(meta["session_id"], sessions_dir)
        if loaded is None:
            continue
        _, records, _ = loaded
        turns = compute_turn_costs(records, pricing, keywords)
        if not turns:
            continue
        totals["sessions"] += 1
        s_cost = s_tokens = 0
        s_agents = s_commands = 0
        s_by_type = defaultdict(bucket)
        for tr in turns:
            c, tok = tr["cost"], tr["total_tokens"]
            s_cost += c; s_tokens += tok
            s_agents += tr["agents"]; s_commands += tr["commands"]
            for agg, key in ((by_type, tr["category"]), (by_model, tr["model"] or "?"), (by_day, _day_of(tr["timestamp"]))):
                agg[key]["turns"] += 1; agg[key]["tokens"] += tok; agg[key]["cost"] += c
            s_by_type[tr["category"]]["turns"] += 1
            s_by_type[tr["category"]]["cost"] += c
            totals["turns"] += 1; totals["tokens"] += tok; totals["cost"] += c
            totals["cost_input"] += tr["cost_input"]; totals["cost_output"] += tr["cost_output"]
        totals["agents"] += s_agents; totals["commands"] += s_commands
        per_session.append({
            "session_id": meta["session_id"],
            "first_seen_ms": meta["first_seen_ms"],
            "transcript_file": meta["transcript_file"],
            "turns": len(turns), "tokens": s_tokens, "cost": round(s_cost, 6),
            "agents": s_agents, "commands": s_commands,
            "by_type": {k: {"turns": v["turns"], "cost": round(v["cost"], 6)} for k, v in s_by_type.items()},
        })
        if session and meta["session_id"].startswith(session):
            target = {"session_id": meta["session_id"], "turns": turns}

    def round_agg(d):
        return {k: {"turns": v["turns"], "tokens": v["tokens"], "cost": round(v["cost"], 6)}
                for k, v in sorted(d.items(), key=lambda kv: kv[1]["cost"], reverse=True)}

    for key in ("cost", "cost_input", "cost_output"):
        totals[key] = round(totals[key], 6)

    result = {
        "currency": "USD",
        "pricing": pricing,
        "pricing_source": pricing_source,
        "categories": CATEGORY_ORDER,
        "totals": totals,
        "by_type": round_agg(by_type),
        "by_model": round_agg(by_model),
        "by_day": {k: {"turns": v["turns"], "tokens": v["tokens"], "cost": round(v["cost"], 6)}
                   for k, v in sorted(by_day.items())},
        "per_session": per_session,
    }
    if target is not None:
        result["session"] = target
    return result


def print_cost_report(sessions_dir: Path = SESSIONS_DIR, session: str | None = None) -> None:
    data = cost_report_json(sessions_dir, session=session)
    tot = data["totals"]
    note = color("(tarifs config)", "dim") if data["pricing_source"] == "config" else color("(tarifs par défaut — à ajuster)", "dim", "yellow")
    print(color(f"=== Coûts — {data['currency']} {note} ===", "bold"))
    print(f"  Sessions : {tot['sessions']}   Questions : {tot['turns']}   "
          f"Tokens : {tot['tokens']:,}   Agents : {tot['agents']}   Commandes : {tot['commands']}")
    print(color(f"  Coût total : ${tot['cost']:.4f}  ", "bold", "green")
          + color(f"(input ${tot['cost_input']:.4f} / output ${tot['cost_output']:.4f})", "dim"))

    print(color("\n  Par type de question :", "bold"))
    for cat in CATEGORY_ORDER:
        v = data["by_type"].get(cat)
        if v:
            print(f"    {cat:<24} {v['turns']:>4} q   ${v['cost']:.4f}")

    if data["by_model"]:
        print(color("\n  Par modèle :", "bold"))
        for model, v in data["by_model"].items():
            print(f"    {model:<32} {v['turns']:>4} q   ${v['cost']:.4f}")

    if "session" in data:
        print(color(f"\n  Détail session {data['session']['session_id'][:8]}… :", "bold", "cyan"))
        for tr in data["session"]["turns"]:
            head = f"    #{tr['turn']:<3} [{tr['category']:<22}] ${tr['cost']:.4f}"
            extra = f"  ({tr['agents']} agents, {tr['commands']} cmd)" if (tr['agents'] or tr['commands']) else ""
            print(color(head, "green") + color(extra, "dim"))
            print(color(f"        {tr['text'][:90]}", "dim"))



# ─── API HTTP ─────────────────────────────────────────────────────────────────
#
# Endpoints (JSON) :
#   GET  /health
#   GET  /config
#   GET  /sources
#   GET  /history[?source=claude|backup]
#   GET  /sessions[?source=claude|backup]
#   GET  /sessions/<id>[?source=claude|backup&tool=NOM&no_output=1]
#   POST /backup   (corps JSON optionnel : {"backup_dir": "..."})
#
# Le paramètre "source" choisit le dossier de sessions interrogé : le dossier
# .claude live (défaut) ou le dossier de sauvegarde (analyze_jsonl_config.json).
#
# La langue de l'interface (fr/en, servie par /config) est pilotée par le champ
# "language" de analyze_jsonl_config.json — pas de sélecteur côté client.
#
# Par sécurité (les transcripts peuvent contenir des flags/secrets), le
# serveur n'écoute que sur 127.0.0.1 par défaut — utiliser --host pour l'ouvrir.

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

API_ENDPOINTS = [
    "GET /health",
    "GET /config",
    "GET /sources",
    "GET /history[?source=claude|backup]",
    "GET /sessions[?source=claude|backup]",
    "GET /sessions/<id>[?source=claude|backup&tool=NOM&no_output=1]",
    "GET /cost[?source=claude|backup&session=<id>]",
    "POST /backup",
]


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "AnalyzeJSONL/1.0"

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[api] " + (fmt % args) + "\n")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        qs = parse_qs(parsed.query)

        try:
            source = qs.get("source", [None])[0]
            try:
                sessions_dir = resolve_source_dir(source)
            except ValueError as e:
                self._send_json(400, {"error": str(e)})
                return

            if parts == ["health"]:
                self._send_json(200, {"status": "ok"})
            elif parts == ["config"]:
                config = load_config()
                self._send_json(200, {"language": resolve_language(config)})
            elif parts == ["sources"]:
                config = load_config()
                self._send_json(200, {
                    "available": list(SOURCES),
                    "default": "claude",
                    "dirs": {
                        "claude": str(SESSIONS_DIR),
                        "backup": str(resolve_backup_dir(config.get("backup_dir", "session_backups"))),
                    },
                })
            elif parts == ["history"]:
                self._send_json(200, history_summary_json(sessions_dir))
            elif parts == ["cost"]:
                session = qs.get("session", [None])[0]
                self._send_json(200, cost_report_json(sessions_dir, session=session))
            elif parts == ["sessions"]:
                self._send_json(200, sessions_list_json(sessions_dir))
            elif len(parts) == 2 and parts[0] == "sessions":
                tool_filter = qs.get("tool", [None])[0]
                no_output = qs.get("no_output", ["0"])[0].lower() in ("1", "true", "yes")
                data = session_detail_json(
                    parts[1], tool_filter=tool_filter, no_output=no_output, sessions_dir=sessions_dir
                )
                if data is None:
                    self._send_json(404, {"error": f"session inconnue : {parts[1]} (source : {sessions_dir})"})
                else:
                    self._send_json(200, data)
            else:
                self._send_json(404, {"error": "endpoint inconnu", "endpoints": API_ENDPOINTS})
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]

        if parts != ["backup"]:
            self._send_json(404, {"error": "endpoint inconnu", "endpoints": API_ENDPOINTS})
            return

        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw_body = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw_body) if raw_body else {}
            except json.JSONDecodeError:
                body = {}

            config = load_config()
            backup_dir = resolve_backup_dir(body.get("backup_dir") or config.get("backup_dir", "session_backups"))
            copied = backup_sessions(backup_dir, quiet=True)
            self._send_json(200, {"copied": copied, "backup_dir": str(backup_dir)})
        except Exception as e:
            self._send_json(500, {"error": str(e)})


def serve_api(host: str, port: int) -> None:
    httpd = ThreadingHTTPServer((host, port), ApiHandler)
    print(color(f"API en écoute sur http://{host}:{port}", "bold", "green"))
    print("Endpoints : " + ", ".join(API_ENDPOINTS))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt du serveur.")
        httpd.shutdown()


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Analyse les fichiers JSONL de Claude Code",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--list-sessions", "-l", action="store_true",
                        help="Lister les sessions disponibles")
    parser.add_argument("--session", "-s", metavar="ID",
                        help="Afficher le détail d'une session (préfixe de 8 car. suffit)")
    parser.add_argument("--tool", "-t", metavar="NOM",
                        help="Filtrer par outil (ex: Bash, Read, Write, Edit)")
    parser.add_argument("--no-output", action="store_true",
                        help="Masquer les outputs des outils")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Mode verbeux pour le résumé")
    parser.add_argument("--backup", action="store_true",
                        help="Forcer une sauvegarde des sessions avant l'exécution")
    parser.add_argument("--no-backup", action="store_true",
                        help="Désactiver la sauvegarde automatique pour cette exécution")
    parser.add_argument("--backup-dir", metavar="DIR",
                        help="Répertoire de sauvegarde (remplace la config)")
    parser.add_argument("--cost", action="store_true",
                        help="Rapport de coûts par type de question (combinable avec --session <id> pour le détail)")
    parser.add_argument("--serve", action="store_true",
                        help="Démarrer le serveur API HTTP au lieu du mode CLI")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Adresse d'écoute de l'API (défaut : 127.0.0.1, local uniquement)")
    parser.add_argument("--port", type=int, default=8765,
                        help="Port d'écoute de l'API (défaut : 8765)")
    parser.add_argument("--source", choices=SOURCES, default="claude",
                        help="Dossier de sessions interrogé en mode CLI : "
                             "'claude' (dossier .claude live, défaut) ou 'backup' (dossier de sauvegarde)")
    args = parser.parse_args()

    config = load_config()
    should_backup = args.backup or (config.get("auto_backup", False) and not args.no_backup)
    if should_backup:
        backup_dir = resolve_backup_dir(args.backup_dir or config.get("backup_dir", "session_backups"))
        backup_sessions(backup_dir)

    if args.serve:
        serve_api(args.host, args.port)
        return

    sessions_dir = resolve_source_dir(args.source)
    if args.cost:
        print_cost_report(sessions_dir, session=args.session)
    elif args.list_sessions:
        list_sessions(sessions_dir)
    elif args.session:
        render_session(args.session, tool_filter=args.tool, no_output=args.no_output, sessions_dir=sessions_dir)
    else:
        analyze_history(verbose=args.verbose, sessions_dir=sessions_dir)


if __name__ == "__main__":
    main()
