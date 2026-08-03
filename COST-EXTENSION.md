# Extension « coût par question » — `analyze_jsonl`

Attribue un **coût** et un **type** à chaque question utilisateur d'une session Claude Code,
avec le décompte des **agents** et **commandes** lancés, exposé en CLI, via l'API HTTP
(`/cost`) et dans l'interface web.

## Ce qui a été ajouté

- **Découpage par question.** Une question = un enregistrement `user` porteur de texte ; son
  coût agrège les `message.usage` de tous les `assistant` qui suivent jusqu'à la question
  suivante. Chaque enregistrement est facturé au tarif de **son propre** `message.model`.
- **Classification en 6 types** : `code_writing` (écriture de code), `code_analysis`
  (analyse de code), `web_deepsearch` (deepsearch web), `agent_launch` (lancement d'agents),
  `tool_onboarding` (prise en main / documentation), `technical_conversation` (repli).
  Signal **primaire** = outils appelés (`Write`/`Edit`→écriture, `Read`/`Grep`→analyse,
  `WebSearch`/`WebFetch`→web, `Task`→agents) ; signal **secondaire** = mots-clés du texte.
- **Compteurs** : `agents` = appels à l'outil `Task` ; `commands` = appels à `Bash`.
- **Coût USD** par les quatre types de tokens (input, output, cache write, cache read).

## CLI

```
python3 analyze_jsonl.py --cost                 # rapport agrégé (par type, par modèle, par session)
python3 analyze_jsonl.py --cost --session <id>  # détail par question d'une session
python3 analyze_jsonl.py --cost --source backup # sur les sessions archivées
```

## API HTTP

```
GET /cost[?source=claude|backup&session=<id>]
```

Retourne `totals`, `by_type`, `by_model`, `by_day`, `per_session`, la table `pricing` et
`pricing_source` (`config` ou `builtin_default`). Avec `session=<id>`, ajoute le détail par
question (`session.turns`). Les tours humains de `GET /sessions/<id>` sont désormais enrichis
(`category`, `cost`, `agents`, `commands`, `token_subtotal`, `model`).

## Configuration (`analyze_jsonl_config.json`)

Deux blocs **optionnels** (repli sur les valeurs par défaut du script sinon) :

- `pricing` — tarifs USD par million de tokens, par préfixe de modèle (correspondance par
  préfixe le plus long ; clé `default` en repli). **Les valeurs livrées sont des exemples** :
  vérifie-les sur <https://platform.claude.com/docs/en/about-claude/pricing>.
- `classification.keywords` — mots-clés secondaires par type de question.

> Si tu avais déjà personnalisé ta config, **fusionne** ces deux blocs plutôt que d'écraser.

## Exécution régulière via un hook Claude Code

Le coût est recalculé à la lecture ; le rôle du hook est surtout d'**archiver** chaque session
terminée (le dossier `.claude` live peut être purgé) pour conserver l'historique du benchmark.

Le plus simple : `/hooks` dans Claude Code, puis ajouter sur l'événement **`Stop`** (ou
`SessionEnd`) une commande `python3 /chemin/analyze_jsonl.py --backup`. Équivalent dans
`~/.claude/settings.json` (schéma **représentatif** — laisse `/hooks` écrire la forme exacte) :

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          { "type": "command", "command": "python3 /chemin/vers/analyze_jsonl.py --backup" }
        ]
      }
    ]
  }
}
```

Pour journaliser aussi un récap de coût par session :
`python3 /chemin/analyze_jsonl.py --cost >> ~/.claude/cost.log`.

Pour une cadence **horloge** (indépendante de l'activité Claude Code), préfère un `cron` /
timer `systemd` appelant le script ou interrogeant l'API `--serve`.

## Sécurité

Les transcripts peuvent contenir des secrets/flags. L'API n'écoute que sur `127.0.0.1` par
défaut : ne pas ouvrir `--host` au-delà sans authentification en amont.
