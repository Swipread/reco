# Base de données de la reco

Le schéma `app`, l'extension `vector` et le schéma `reco` sont créés par les migrations Flyway du backend : ils font foi, et sont décrits dans [`docs/database.md` de Swipread/api](https://github.com/Swipread/api/blob/main/docs/database.md). Ce document liste seulement ce que la reco lit dans `app`, les tables qu'elle crée dans `reco` et les droits dont elle a besoin. État aligné sur Swipread/api au commit 48055c3 (migrations V1 à V8).

Pour les tests, `sql/app_schema.sql` recopie ces migrations ; il est à tenir à jour à chaque nouvelle migration du backend.

## Ce que la reco lit dans `app`

En lecture seule ; la reco n'écrit jamais dans `app`.

| Table | Colonnes lues | Usage |
| --- | --- | --- |
| `app.users` | `id`, `locale` | Utilisateur connu (404 sinon), langue par défaut ; purge des lots des comptes supprimés |
| `app.topics` | `id`, `slug`, `label` | Liste des thèmes, slugs des listes `fallback:{slug}` |
| `app.user_topics` | `user_id`, `topic_id` | Thèmes choisis |
| `app.sources` | `id`, `active` | Seuls les articles des sources actives sont recommandables |
| `app.articles` | `id`, `source_id`, `title`, `summary`, `language`, `word_count`, `published_at`, `ingested_at` | Catalogue, encodage, titres des routes d'explication. `embedding` n'est ni lu ni écrit |
| `app.article_topics` | `article_id`, `topic_id`, `confidence` | Thème principal d'un article |
| `app.events` | `event_id`, `user_id`, `type`, `article_id`, `payload`, `occurred_at`, `received_at` | Rejeu du profil et repère de `reco.user_profiles`. Types lus : `view`, `open`, `read`, `like`, `unlike`, `save`, `unsave`, `share`, `hide` ; les lignes sans `article_id` sont ignorées |
| `app.user_articles` | `user_id`, `article_id` | Articles déjà vus par le lecteur, exclus des lots |

Le backend n'a ni statut d'article ni `cluster_id` : tout article d'une source active est actif, et la règle « même actualité » repose sur la seule ressemblance (`algo_reco.md`, 7.2).

## Tables de `reco`

Créées par le service au démarrage (`PgStore.ensure_schema`), si elles n'existent pas. Le schéma `reco` doit exister (migration V1 du backend) : le service ne le crée pas et s'arrête avec un message explicite s'il manque, si le type `vector` manque, ou si `reco.user_profiles` est encore l'ancienne table du backend (créée par V1, supprimée par sa migration V8 : lancer les migrations du backend d'abord).

```sql
CREATE TABLE IF NOT EXISTS reco.feed_requests (
  request_id      text PRIMARY KEY,            -- r_...
  user_id         text NOT NULL,
  session_id      text,
  model_version   text NOT NULL,
  requested_count int  NOT NULL,
  seed            bigint NOT NULL,
  profile         jsonb NOT NULL,
  params          jsonb NOT NULL,
  generated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS feed_requests_user_idx
  ON reco.feed_requests (user_id, generated_at DESC);

CREATE TABLE IF NOT EXISTS reco.feed_items (
  request_id  text REFERENCES reco.feed_requests(request_id) ON DELETE CASCADE,
  position    smallint,
  article_id  text NOT NULL,
  topic_id    int  NOT NULL,
  reason      text NOT NULL,
  score       real NOT NULL,
  explanation jsonb NOT NULL,
  PRIMARY KEY (request_id, position)
);

CREATE TABLE IF NOT EXISTS reco.user_profiles (
  user_id          text PRIMARY KEY,
  basis            jsonb NOT NULL,             -- ce dont dépend le rejeu, hors événements
  state            jsonb NOT NULL,             -- résultat du rejeu
  events_count     int  NOT NULL,              -- repère : nombre d'événements rejoués
  last_occurred_at timestamptz,                -- repère : dernier événement (contrat v1.5)
  last_event_id    text,
  computed_at      timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS reco.article_embeddings (
  article_id  text PRIMARY KEY,                -- pas de clé étrangère : aucun droit REFERENCES sur app
  embedding   vector(384) NOT NULL,
  model       text NOT NULL,
  embedded_at timestamptz NOT NULL
);
```

- `feed_requests` et `feed_items` : chaque lot calculé et ses cartes ; contenu des colonnes `jsonb` dans `algo_reco.md`, section 9.
- `article_embeddings` : un embedding par article, écrit par le worker. Seules les lignes du modèle courant (`multilingual-e5-small`) sont lues ; une ligne d'un autre modèle est réencodée et remplacée (`algo_reco.md`, 8.4).
- Purge horaire (`algo_reco.md`, 8.7) : lots de plus de 30 jours ou d'utilisateurs absents de `app.users`, embeddings d'articles absents de `app.articles`.

- `user_profiles` (contrat v1.5, `algo_reco.md` 8.6) : le profil de chaque lecteur, tenu à jour par le worker depuis le flux `events` et vérifié à chaque demande. Il n'est utilisé que si son repère et sa base sont ceux du moment, sinon il est rejoué depuis `app.events` et remplacé.
  - Repère : `events_count`, `last_occurred_at` et `last_event_id` des événements du lecteur qui comptent pour le profil (types de `algo_reco.md` 4.1, avec article).
  - `basis` : `{ "modelVersion": …, "params": {paramètres de la section 10}, "topics": [ids de app.topics], "chosen": [ids choisis, triés] }`.
  - `state` : `{ "weights": [[topicId, poids]…], "liked": [[articleId, force, dernierÉvénement]…], "rejected": […], "touched": [articleIds], "history": [[eventId, type, articleId, topicId, δ, poidsAprès, occurredAt]…] }`. Des listes plutôt que des objets là où l'ordre compte : `jsonb` ne garde pas l'ordre des clés. Les forces sont celles d'avant le facteur d'ancienneté ; `history` garde les 50 derniers changements de poids.
  - Supprimé tout de suite sur `user_deleted`, et à la purge horaire si le lecteur n'est plus dans `app.users`.

Le backend ne crée aucune table dans `reco` depuis sa migration V8, qui a supprimé les `reco.user_profiles` et `reco.model_runs` de sa migration V1, jamais utilisées.

## Droits

Le service se connecte avec un rôle membre du rôle de groupe `swipread_reco`, créé par la migration V5 du backend :

```sql
CREATE ROLE reco_service LOGIN PASSWORD '…' IN ROLE swipread_reco;
```

| Schéma | Droits de `swipread_reco` (V5) | Utilisés par la reco |
| --- | --- | --- |
| `app` | `USAGE` ; `SELECT` sur toutes les tables, sauf `app.users` limité à `id`, `locale`, `created_at` ; rien sur `app.event_outbox` ni `app.refresh_tokens` | `SELECT` sur les colonnes listées plus haut |
| `reco` | `USAGE`, `CREATE` ; `SELECT`, `INSERT`, `UPDATE`, `DELETE` sur les tables | Création de ses quatre tables, lecture et écriture |
| base | aucun `CREATE` | aucun : le service ne crée ni schéma ni extension |

Les tests d'intégration le vérifient : ils se connectent avec un rôle `reco_test` membre de `swipread_reco`, sur une copie des migrations du backend.
