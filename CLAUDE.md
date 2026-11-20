# Swipread reco

Service de recommandation de Swipread : compose pour chaque lecteur un lot d'articles (`POST /v1/recommendations`) appelé par le backend. Python 3.12, uv, FastAPI, psycopg 3 + pgvector, redis-py, numpy, sentence-transformers. Paquet `swipread_reco`, disposition `src/`.

## Sources de vérité

- Algorithme, paramètres, tests d'acceptation : `docs/algorithm.md` (« la spec » des commentaires du code, dont les numéros de section sont stables). Tout changement d'algorithme y est reporté.
- La spécification d'origine, en français, est archivée dans `notes/archive/algo_reco-5897d27.md` (document interne, ignoré par git, périmé) ; la version française à jour est `../docs/07-algorithme-reco.md`.
- Échanges avec le backend : `notes/Contrat backend reco.md` (copie du contrat de l'API, ignorée par git). Ne jamais changer un format du contrat sans accord explicite ; signaler toute ambiguïté au lieu de trancher seul.
- Schéma `app` : les migrations Flyway de Swipread/api font foi. `sql/app_schema.sql` les recopie pour les tests d'intégration : le tenir à jour à chaque nouvelle migration du backend, sans rien y ajouter qu'elles ne créent.

## Architecture

- Deux processus du même paquet : `api` (routes HTTP, catalogue en mémoire) et `worker` (encodage, listes de repli, purge, flux `events`).
- Le calcul (profil, score, répartition, assemblage) reste en fonctions pures, sans base, Redis ni horloge : ces dépendances sont passées en argument.
- Tout le SQL est dans `store.py`, derrière le protocole `Store` ; `tests/fakes.py` en a une version en mémoire, à tenir à jour avec lui.
- La reco n'écrit jamais dans `app` : son rôle (`swipread_reco`, migration V5 du backend) n'y a que `SELECT`. Elle crée ses tables dans `reco` (`PgStore.ensure_schema`), jamais de schéma ni d'extension.
- `app.events` est la source de vérité du profil ; `reco.user_profiles` n'en est qu'une copie vérifiée à chaque demande.

## Commandes

- Dépendances : `uv sync`
- Tests sans infrastructure : `uv run pytest -m "not perf"` ; lint : `uv run ruff check`
- Intégration : `docker compose up -d` puis `RECO_TEST_DSN=postgresql://postgres:postgres@localhost:5433/postgres uv run pytest tests/integration -q` (la base pointée est vidée)
- Performance (A13) : `uv run pytest -m perf`
- Bout en bout avec le vrai backend : `SWIPREAD_API_DIR=../API scripts/e2e.sh`
- Lancer : `uv run python -m swipread_reco api` et `uv run python -m swipread_reco worker` (variables dans le README)

## Conventions

- Code, commentaires et documentation (`README.md`, `docs/`) en anglais.
- Paramètres de l'algorithme uniquement dans `params.py` (section 10 de la spec), surchargeables par `RECO_<NOM>`.
- Tests à côté de leur sujet : `tests/unit`, `tests/api`, `tests/acceptance`, `tests/integration`, `tests/e2e`. Ruff (`line-length = 100`) doit passer.
- Le dossier `notes/` (documents internes) n'est jamais commité.

## Git

- Commits petits et fréquents, au format Conventional Commits (`feat(worker): …`, `fix(store): …`, `test(e2e): …`, `docs: …`).
- Pas de ligne `Co-Authored-By` dans les commits.
- Pousser seulement sur demande.
