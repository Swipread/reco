# Contrat backend / reco

Oct 12, 2026 · @William

## Principes

Le backend et le service de reco communiquent par deux canaux : un appel REST pour obtenir des recommandations, et une file de messages pour les événements. Une fois ce contrat figé, chaque partie peut avancer seule.

- **La reco renvoie des IDs, jamais du contenu.** Le backend hydrate les articles depuis la base.
- **Le backend est le seul à écrire les données métier** (users, articles, événements). La reco lit la base en lecture seule, sauf les colonnes d'embedding des articles qu'elle calcule, et possède ses propres données (schéma `reco`).
- **L'utilisateur n'attend jamais la reco.** Le feed est servi depuis un stock Redis, rempli en arrière-plan.
- **Toute panne de la reco a un repli.** Le backend sert un feed de secours.
- **Les formats sont versionnés.** Tout changement cassant passe par `/v2` ou un champ `version`.

Conventions : JSON en camelCase, dates en ISO 8601 UTC, IDs préfixés (`u_` pour un utilisateur, `a_` pour un article).

## Endpoint POST /v1/recommendations

Appelé par le backend quand le stock d'un utilisateur passe sous le seuil, ou après une invalidation. Timeout côté backend : 300 ms.

### Requête

```json
{
  "userId": "u_42",
  "count": 50,
  "exclude": ["a_101", "a_102"],
  "context": {
    "sessionId": "s_8f3a",
    "locale": "fr-FR",
    "device": "mobile"
  }
}
```

| Champ | Type | Requis | Rôle |
| --- | --- | --- | --- |
| userId | string | oui | Utilisateur cible |
| count | int (1 à 100) | oui | Nombre d'articles voulus |
| exclude | string\[\] | non | Articles déjà en stock dans Redis, à ne pas renvoyer |
| context.sessionId | string | non | Permet de pondérer les signaux de la session en cours |
| context.locale | string | non | Langue préférée des articles |
| context.device | string | non | mobile ou desktop |

La reco exclut aussi d'elle-même les articles déjà vus, à partir de l'historique en base et de `seen:{userId}`. Un nouvel inscrit qui a choisi ses thèmes reçoit un vrai lot dès le premier appel.

### Réponse 200

```json
{
  "requestId": "r_7c21",
  "modelVersion": "content-v1",
  "generatedAt": "2026-10-12T10:40:00Z",
  "items": [
    { "articleId": "a_387", "score": 0.91, "reason": "similar" },
    { "articleId": "a_512", "score": 0.40, "reason": "explore" }
  ]
}
```

| Champ | Rôle |
| --- | --- |
| requestId | À recopier dans les événements pour mesurer la qualité de chaque lot |
| modelVersion | Version de l'algo, utile pour comparer deux versions (A/B test) |
| items\[\].score | Entre 0 et 1, la liste est déjà triée par ordre d'affichage |
| items\[\].reason | `similar`, `collaborative`, `explore`, `trending` ou `onboarding` |

### Erreurs

| Code | Cas | Réaction du backend |
| --- | --- | --- |
| 400 | Requête invalide | Log, pas de retry |
| 404 | userId absent de `app.users` | Feed de repli basé sur l'onboarding |
| 503 ou timeout | Reco indisponible | Feed de repli, nouvel essai au prochain seuil |

La réponse peut contenir moins de `count` articles si le catalogue est épuisé pour cet utilisateur : le backend complète avec le repli.

## Événements

Le front envoie les événements par lots au backend, qui les valide, les écrit en base puis les publie sur la file `events` (Redis Streams). La v1 de l'algo ne consomme pas cette file : elle relit `app.events` à chaque demande de lot (voir [algo_reco.md](algo_reco.md)).

### Front vers backend : POST /v1/events

Envoi toutes les 5 secondes, ou dès 20 événements, ou à la fermeture de l'app.

```json
{
  "sessionId": "s_8f3a",
  "events": [
    {
      "eventId": "e_01J9X",
      "type": "view",
      "articleId": "a_387",
      "requestId": "r_7c21",
      "position": 3,
      "occurredAt": "2026-10-12T10:41:12Z",
      "payload": { "dwellMs": 4200 }
    }
  ]
}
```

Le `userId` n'est pas envoyé par le front : le backend le déduit du token. L'`eventId` est généré côté front (UUID ou ULID) pour dédoublonner les renvois.

### Types d'événements

| type | Quand | payload | Signal |
| --- | --- | --- | --- |
| impression | Carte affichée à l'écran | aucun | Neutre, sert de dénominateur |
| view | Carte quittée | dwellMs | Positif si long, négatif si skip rapide |
| open | Article ouvert | aucun | Positif |
| read | Article quitté | readMs, scrollDepth (0 à 1) | Fort si lecture complète |
| like / unlike | Bouton like | aucun | Fort |
| save | Ajout aux sauvegardés | aucun | Fort |
| share | Partage | channel | Fort |
| hide | « Pas intéressé » | reason (optionnel) | Fort négatif |

Les événements forts (`like`, `save`, `share`, `hide`, `read` avec scrollDepth supérieur à 0,8) déclenchent l'invalidation du stock Redis de l'utilisateur. L'invalidation démarre après la validation en base du lot d'événements, pour que la reco voie l'événement quand elle recalcule.

### Backend vers file : message `events`

Même structure, enrichie par le backend :

```json
{
  "version": 1,
  "eventId": "e_01J9X",
  "userId": "u_42",
  "sessionId": "s_8f3a",
  "type": "view",
  "articleId": "a_387",
  "requestId": "r_7c21",
  "position": 3,
  "occurredAt": "2026-10-12T10:41:12Z",
  "receivedAt": "2026-10-12T10:41:15Z",
  "payload": { "dwellMs": 4200, "articleWordCount": 850 }
}
```

`articleWordCount` est ajouté par le backend pour que la reco normalise le temps passé par la longueur de l'article. La reco doit être idempotente sur `eventId`.

## Redis : clés et règles de stock

Chaque utilisateur a un stock d'au plus 50 articles pré-calculés. Le backend sert 10 articles par page et redemande 50 articles à la reco quand le stock passe sous 20.

| Clé | Type | Écrite par | TTL | Contenu |
| --- | --- | --- | --- | --- |
| feed:{userId} | List | backend | 6 h | IDs d'articles dans l'ordre d'affichage |
| feed:{userId}:req | String | backend | 6 h | requestId du dernier lot |
| feed:{userId}:lock | String | backend | 5 s | Empêche deux remplissages simultanés |
| seen:{userId} | Set | backend | 30 j | Articles déjà servis, pour l'exclusion rapide ; lu aussi par la reco |
| fallback:{topic} | List | reco (batch) | 1 h | Articles récents par thème ; `{topic}` est le slug du thème |
| events | Stream | backend | 7 j | File des événements |

Règles :

1. Lecture d'une page : `LPOP feed:{userId} 10`, puis ajout des IDs dans `seen:{userId}`.
2. Si la longueur restante est inférieure à 20 et que le verrou est libre, le backend lance le remplissage en tâche de fond.
3. Remplissage : appel à `/v1/recommendations` avec le contenu actuel du stock dans `exclude`, puis `RPUSH` des nouveaux IDs.
4. Invalidation (événement fort à la position `p` de la page, index à partir de 0) : le backend garde les `p` premiers éléments du stock (`LTRIM feed:{userId} 0 p-1`, ou `DEL` si `p = 0`) et relance un remplissage. Avec les cartes déjà sur le téléphone, 9 cartes calculées avant l'événement restent à servir : une interaction sur la carte n se voit à partir de la carte n+10.

## Repli et développement en parallèle

### Feed de repli

Utilisé quand la reco renvoie 404 ou 503, dépasse le timeout, ou renvoie moins d'articles que demandé.

1. Le backend lit `fallback:{topic}` pour chaque thème choisi à l'onboarding.
2. Il mélange les listes, retire les articles présents dans `seen:{userId}`.
3. Si `fallback:*` est vide (reco jamais lancée), il prend les 50 articles les plus récents en base.

Les articles servis en repli portent `reason: "fallback"` dans les événements, pour mesurer l'écart de qualité avec la vraie reco.

### Faux service de reco

Pour que le backend avance sans attendre l'algo, un mock respecte le contrat dès la première semaine :

- `POST /v1/recommendations` renvoie `count` articles tirés au hasard en base, hors `exclude`, avec `score: 0.5`, `reason: "explore"` et `modelVersion: "mock"`.
- Il consomme la file `events` et se contente de les logger.

Le vrai service remplace le mock sans changement côté backend.

### Ordre de travail suggéré

- [ ] Valider ce contrat en groupe
- [ ] Mock de reco + endpoint `/v1/feed` branché dessus
- [ ] `POST /v1/events` et publication sur la file
- [ ] Reco v1 basée contenu (embeddings + pgvector)
- [ ] Feed de repli et invalidation sur événement fort

## Base de données

Le schéma complet (tables, DDL, droits) est dans [database.md](database.md), seul document qui fait foi sur ce sujet. La reco a un rôle en lecture seule sur `app`, avec une exception : elle écrit les colonnes `embedding`, `embedding_model` et `embedded_at` de `app.articles`. Elle lit et écrit `reco`.
