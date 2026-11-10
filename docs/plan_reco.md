# Service de reco Swipread — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Construire le service Python qui répond à `POST /v1/recommendations` selon l'algorithme à poids par thème de la spec, avec ses routes d'explication, son worker et son rapport d'évaluation.

**Architecture:** Un cœur en fonctions pures (profil, score, répartition, assemblage) testé sur un catalogue synthétique, sans base ni Redis. Autour, une couche d'accès PostgreSQL/Redis derrière un protocole `Store`, une API FastAPI qui garde le catalogue en mémoire, et un worker (encodage, listes de repli, purge).

**Tech Stack:** Python 3.12, uv, FastAPI, uvicorn, psycopg 3 + pgvector, redis-py, numpy, sentence-transformers, pytest, fakeredis, ruff.

**Spec:** [algo_reco.md](algo_reco.md) (algorithme, paramètres, tests d'acceptation), [database.md](database.md) (schéma), [Contrat backend reco.md](Contrat%20backend%20reco.md) (formats d'échange). Les numéros de section cités dans ce plan (« spec 4.2 ») renvoient à `algo_reco.md`.

## Global Constraints

- Le code vit dans un nouveau dépôt `Swipread/reco`, à créer avec l'accord d'Enzo avant la tâche 1. Paquet `swipread_reco`, disposition `src/`.
- Python 3.12. Dépendances gérées par uv ; `uv run pytest` lance les tests.
- Les modules `profile`, `scoring`, `allocation`, `assemble`, `recommend` n'importent ni psycopg, ni redis, ni fastapi, et ne lisent jamais l'horloge : `now` est toujours un argument.
- Toutes les valeurs chiffrées viennent de `Params` (tâche 1), jamais d'un littéral dans le code. Les valeurs par défaut sont celles de la spec, section 10.
- Les dates sont des `datetime` avec fuseau UTC.
- Les embeddings sont des `numpy.float32` de norme 1 et de dimension quelconque : le code ne suppose pas 384, les tests utilisent de petites dimensions.
- `modelVersion` vaut `topic-weights-v1`. JSON en camelCase vers l'extérieur, snake_case en Python.
- Déterminisme : mêmes entrées, même graine, même `now` → même sortie, à l'octet près.
- Tests d'intégration (PostgreSQL, Redis réels) marqués `@pytest.mark.integration` et ignorés si `RECO_TEST_DSN` n'est pas défini. Tous les autres tests tournent sans infrastructure.
- Un commit par tâche au minimum, message au format `feat: …` / `test: …`.

## Review Focus

Cas que la spec implique sans les tester, du plus probable au moins probable. Chacun a son test dans la tâche indiquée.

1. **Événement au payload incomplet** (`view` sans `dwellMs`, `read` sans `readMs`, valeur non numérique) : l'événement est ignoré, le profil se construit quand même. → Tâche 2.
2. **Article aimé ou rejeté absent de `app.articles` ou sans embedding** : il est retiré des listes, le lot sort quand même. → Tâche 8.
3. **Aucun candidat ou trop peu** (langue sans article, `count` supérieur au catalogue, table des thèmes vide) : lot plus court ou vide, réponse 200, pas de boucle infinie. → Tâches 7 et 10.
4. **Base injoignable pendant un rechargement du catalogue** : le service garde l'ancien catalogue et `/healthz` indique son âge. → Tâche 10.
5. **Redis injoignable à la lecture de `seen:{userId}`** : le lot est calculé avec les autres exclusions, un avertissement est journalisé. → Tâche 10.

## Structure des fichiers

```
pyproject.toml
compose.yaml                     PostgreSQL (pgvector) + Redis pour l'intégration
sql/app_schema.sql               DDL de `app` copié de database.md (tests uniquement)
src/swipread_reco/
  params.py                      Params : tous les paramètres de la spec, section 10
  models.py                      Event, Topic, Catalog, Profile, Lot… (données seules)
  profile.py                     spec 4 : valeur d'un article, poids, aimés/rejetés, écho
  scoring.py                     spec 5 : MU, ressemblance, proximité, rejet, fraîcheur
  allocation.py                  spec 6 : plafond, graine, décalages, ordre des thèmes
  assemble.py                    spec 7 : candidats placés, doublons, sources, reason
  recommend.py                   enchaîne les quatre modules ; fonction pure
  store.py                       protocole Store + PgStore (psycopg)
  catalog_cache.py               catalogue en mémoire, rechargement périodique
  api.py                         FastAPI : /v1/recommendations, /healthz
  explain.py                     routes /v1/explain/…
  worker/encode.py               encodage des articles
  worker/fallback.py             listes fallback:{slug}
  worker/purge.py                purge de reco.feed_requests
  __main__.py                    `python -m swipread_reco api|worker`
  sim/catalog.py                 catalogue synthétique (spec 11.1)
  sim/backend.py                 imitation du stock backend (pages de 10, règle n+10)
  sim/readers.py                 4 lecteurs types
  sim/report.py                  rapport Markdown + CSV (spec 11.3)
tests/
  unit/                          un fichier par module pur
  acceptance/                    A1 à A13 (spec 11.2)
  api/                           API avec un Store factice
  integration/                   PgStore, worker, sur vraie base
```

## Ordre et parallélisme

La tâche 1 d'abord. Les tâches 2, 3, 4, 5 et 6 ne dépendent que de la tâche 1 et peuvent être menées en parallèle (3 dépend de 2). Ensuite 7, puis 8. Les tâches 9, 12 et 13 peuvent démarrer dès que 8 est finie ; 10 dépend de 9, 11 de 10.

---

### Task 1: Squelette, paramètres et modèles de données

**Files:**
- Create: `pyproject.toml`, `src/swipread_reco/__init__.py`, `src/swipread_reco/params.py`, `src/swipread_reco/models.py`
- Test: `tests/unit/test_params.py`

**Interfaces:**
- Produces:

```python
# params.py
@dataclass(frozen=True)
class Params:
    epsilon: float = 0.10
    w_floor: float = 0.01
    p_max: float = 0.60
    t_skip_ms: int = 1500
    t_long_ms: int = 6000
    v_skip: float = -0.02
    v_long_view: float = 0.02
    v_open: float = 0.03
    v_read_bonus: float = 0.04
    v_like: float = 0.08
    v_hide: float = -0.15
    s_skip: float = 0.15
    half_life_profile_days: float = 30.0
    profile_max_items: int = 100
    events_max: int = 5000
    w_aff: float = 0.6
    w_fresh: float = 0.4
    w_neg: float = 0.5
    half_life_fresh_hours: float = 48.0
    max_age_days: int = 14
    dup_sim: float = 0.80
    source_max: int = 2
    aff_reason_min: float = 0.20
    catalog_refresh_s: int = 60
    encode_interval_s: int = 15

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Params": ...
    def as_dict(self) -> dict[str, float | int]: ...

MODEL_VERSION = "topic-weights-v1"
EMBEDDING_MODEL = "multilingual-e5-small"
```

```python
# models.py — dataclasses gelées, aucune logique
@dataclass(frozen=True)
class Topic:
    id: int
    slug: str
    label: str

@dataclass(frozen=True)
class Event:
    event_id: str
    type: str                    # view, open, read, like, unlike, save, share, hide
    article_id: str
    occurred_at: datetime
    received_at: datetime
    payload: Mapping[str, Any]   # tel qu'envoyé par le front (api.md)
    topic_id: int | None         # thème principal de l'article, None si inconnu
    word_count: int | None       # app.articles.word_count, None si article inconnu

@dataclass(frozen=True)
class ProfileItem:
    article_id: str
    strength: float              # force après facteur d'ancienneté
    last_event_at: datetime

@dataclass(frozen=True)
class WeightChange:
    event_id: str
    type: str
    article_id: str
    topic_id: int
    delta: float                 # δ appliqué avant renormalisation
    weight_after: float          # poids du thème après renormalisation
    occurred_at: datetime

@dataclass(frozen=True)
class Profile:
    weights: dict[int, float]            # topic_id -> poids, somme 1
    liked: list[ProfileItem]             # v > 0, plus récent d'abord
    rejected: list[ProfileItem]          # v < 0, plus récent d'abord
    echo_topic: int | None
    touched_article_ids: frozenset[str]  # tout article présent dans les événements
    history: list[WeightChange]          # ordre chronologique

@dataclass(frozen=True)
class Catalog:
    ids: list[str]
    index: dict[str, int]                # id -> ligne
    topic_ids: np.ndarray                # (n,) int
    source_ids: np.ndarray               # (n,) int
    languages: list[str]                 # code à deux lettres
    cluster_ids: list[str | None]
    published_at: list[datetime]
    embeddings: np.ndarray               # (n, d) float32, norme 1
    mu: float
    loaded_at: datetime

@dataclass(frozen=True)
class LotItem:
    article_id: str
    topic_id: int
    reason: str                  # similar, onboarding, explore
    score: float                 # borné [0, 1]
    explanation: dict[str, Any]  # clés de la spec 9.1

@dataclass(frozen=True)
class Lot:
    request_id: str
    seed: int
    items: list[LotItem]
    profile: Profile
    shares: dict[int, float]     # parts après plafond
    mu: float
```

- [ ] **Step 1: Écrire le test qui échoue**

```python
def test_defaults_match_spec_section_10():
    p = Params()
    assert (p.epsilon, p.w_floor, p.p_max) == (0.10, 0.01, 0.60)
    assert (p.v_skip, p.v_long_view, p.v_open, p.v_read_bonus, p.v_like, p.v_hide) == (
        -0.02, 0.02, 0.03, 0.04, 0.08, -0.15)
    assert (p.w_aff, p.w_fresh, p.w_neg) == (0.6, 0.4, 0.5)
    assert (p.dup_sim, p.source_max, p.aff_reason_min) == (0.80, 2, 0.20)

def test_from_env_overrides_and_keeps_types():
    p = Params.from_env({"RECO_P_MAX": "0.5", "RECO_SOURCE_MAX": "3"})
    assert p.p_max == 0.5 and p.source_max == 3 and isinstance(p.source_max, int)

def test_from_env_rejects_unparseable_value():
    with pytest.raises(ValueError, match="RECO_P_MAX"):
        Params.from_env({"RECO_P_MAX": "abc"})
```

- [ ] **Step 2: Lancer** `uv run pytest tests/unit/test_params.py -v` → FAIL (module absent).
- [ ] **Step 3: Créer `pyproject.toml`** (dépendances de la Tech Stack ; `fakeredis`, `pytest`, `httpx`, `ruff` en groupe dev ; marqueurs pytest `integration` et `perf` déclarés), puis `params.py` et `models.py` selon le bloc Interfaces. `from_env` lit `RECO_<NOM_EN_MAJUSCULES>` et convertit vers le type du champ.
- [ ] **Step 4: Relancer** → PASS. `uv run ruff check` → aucun avertissement.
- [ ] **Step 5: Commit** `feat: project skeleton, params and data models`

---

### Task 2: Valeur d'un article (spec 4.1)

**Files:**
- Create: `src/swipread_reco/profile.py`
- Test: `tests/unit/test_article_value.py`

**Interfaces:**
- Consumes: `Event`, `Params`.
- Produces:

```python
@dataclass(frozen=True)
class ArticleState:
    skipped: bool = False
    long_view: bool = False
    opened: bool = False
    read: float | None = None    # None = jamais lu ; sinon max des c
    liked: bool = False
    saved: bool = False
    shared: bool = False
    hidden: bool = False

def apply_event(state: ArticleState, event: Event, params: Params) -> ArticleState
def article_value(state: ArticleState, params: Params) -> float
```

- [ ] **Step 1: Écrire les tests qui échouent.** Un helper local `ev(type, payload=None, word_count=230)` construit un `Event` ; `value(*events)` replie `apply_event` puis appelle `article_value`.

```python
@pytest.mark.parametrize("events, expected", [
    ([ev("view", {"dwellMs": 1000})], -0.02),
    ([ev("view", {"dwellMs": 1500})], 0.0),          # borne : pas zappé à 1500
    ([ev("view", {"dwellMs": 3000})], 0.0),
    ([ev("view", {"dwellMs": 6000})], 0.02),         # borne : vue longue à 6000
    ([ev("open")], 0.03),
    ([ev("read", {"readMs": 30000, "scrollDepth": 1.0})], 0.05),   # c = 0,5
    ([ev("read", {"readMs": 600000, "scrollDepth": 0.1})], 0.07),  # c borné à 1
    ([ev("read", {"readMs": 0, "scrollDepth": 0})], 0.03),
    ([ev("like")], 0.08), ([ev("save")], 0.08), ([ev("share", {"channel": "x"})], 0.08),
    ([ev("hide")], -0.15),
    ([ev("view", {"dwellMs": 500}), ev("like")], 0.08),            # le positif l'emporte
    ([ev("like"), ev("unlike")], 0.0),                              # A8
    ([ev("open"), ev("like"), ev("unlike")], 0.03),
    ([ev("like"), ev("hide")], -0.15),
    ([ev("hide"), ev("like")], -0.15),                              # masquage définitif
    ([ev("impression")], 0.0),
])
def test_value(events, expected):
    assert value(*events) == pytest.approx(expected)

def test_read_without_word_count_uses_60s():
    assert value(ev("read", {"readMs": 60000, "scrollDepth": 0}, word_count=None)) == pytest.approx(0.07)
    assert value(ev("read", {"readMs": 30000, "scrollDepth": 0}, word_count=0)) == pytest.approx(0.05)

@pytest.mark.parametrize("bad", [
    ev("view", {}), ev("view", {"dwellMs": "abc"}), ev("view", {"dwellMs": None}),
    ev("read", {"scrollDepth": 0.9}), ev("read", {"readMs": -5}),
])
def test_malformed_payload_is_ignored(bad):      # Review Focus 1
    assert apply_event(ArticleState(), bad, Params()) == ArticleState()
```

- [ ] **Step 2: Lancer** `uv run pytest tests/unit/test_article_value.py -v` → FAIL.
- [ ] **Step 3: Implémenter `apply_event` et `article_value`** d'après la table et le pseudo-code de la spec 4.1. `durée_attendue = word_count / 230 × 60 000` ms.
- [ ] **Step 4: Relancer** → PASS.
- [ ] **Step 5: Commit** `feat: article value from events`

---

### Task 3: Profil du lecteur (spec 4.2 à 4.4)

**Files:**
- Modify: `src/swipread_reco/profile.py`
- Test: `tests/unit/test_profile.py`

**Interfaces:**
- Consumes: `apply_event`, `article_value`, modèles de la tâche 1.
- Produces:

```python
def initial_weights(all_topics: Sequence[int], chosen: Set[int], params: Params) -> dict[int, float]

def build_profile(
    events: Sequence[Event],            # ordre quelconque ; triés ici par (occurred_at, event_id)
    all_topics: Sequence[int],
    chosen: Set[int],
    now: datetime,
    last_request_at: datetime | None,   # None = aucune requête précédente
    params: Params,
) -> Profile
```

Décisions que la spec laisse ouvertes :
- `last_request_at is None` : tous les événements sont éligibles à l'écho.
- Un événement dont `topic_id` est `None` ne change aucun poids mais compte pour les listes aimés/rejetés et pour `touched_article_ids`.
- La troncature à `events_max` se fait dans le `Store` (tâche 9), pas ici.
- Un `event_id` présent deux fois n'est appliqué qu'une fois.

- [ ] **Step 1: Écrire les tests qui échouent.** Constantes : `TOPICS = list(range(1, 11))`, `CHOSEN = {1, 2, 3}`, `NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)`. Helper `ev(type, article_id, topic_id, minutes_ago, payload=None, received_minutes_ago=None)`.

```python
def test_initial_weights():
    w = initial_weights(TOPICS, CHOSEN, Params())
    assert w[1] == pytest.approx(0.30) and w[4] == pytest.approx(0.1 / 7)
    assert sum(w.values()) == pytest.approx(1.0)
    assert initial_weights(TOPICS, set(), Params())[5] == pytest.approx(0.1)
    assert initial_weights(TOPICS, set(TOPICS), Params())[5] == pytest.approx(0.1)

def test_worked_example_of_spec_4_2():
    events = [ev("like", "s1", 4, 30), ev("like", "s2", 4, 20), ev("hide", "p1", 5, 10)]
    w = build_profile(events, TOPICS, CHOSEN, NOW, None, Params()).weights
    assert w[1] == pytest.approx(0.26039, abs=1e-4)
    assert w[4] == pytest.approx(0.15683, abs=1e-4)
    assert w[5] == 0.0
    assert w[6] == pytest.approx(0.01240, abs=1e-4)

def test_five_likes_on_unchosen_topic_reach_30_percent():      # A3
    events = [ev("like", f"s{i}", 4, 50 - i) for i in range(5)]
    assert build_profile(events, TOPICS, CHOSEN, NOW, None, Params()).weights[4] >= 0.30

def test_one_article_counts_once():                             # A7
    events = [ev("view", "a", 1, 9, {"dwellMs": 7000}), ev("open", "a", 1, 8),
              ev("read", "a", 1, 7, {"readMs": 10**7, "scrollDepth": 1}), ev("like", "a", 1, 6)]
    p = build_profile(events, TOPICS, CHOSEN, NOW, None, Params())
    assert sum(h.delta for h in p.history) == pytest.approx(0.08)

def test_chosen_topic_never_below_floor():
    events = [ev("hide", f"a{i}", 1, 50 - i) for i in range(6)]
    assert build_profile(events, TOPICS, CHOSEN, NOW, None, Params()).weights[1] >= 0.01

def test_skip_does_not_extinguish_unchosen_topic():
    w = build_profile([ev("view", "a", 4, 5, {"dwellMs": 300})], TOPICS, CHOSEN, NOW, None, Params()).weights
    assert w[4] > 0.0

def test_liked_unchosen_topic_survives_first_hide_then_dies():
    likes = [ev("like", f"s{i}", 4, 60 - i) for i in range(3)]          # ≈ 21,8 %
    one = build_profile(likes + [ev("hide", "h1", 4, 5)], TOPICS, CHOSEN, NOW, None, Params()).weights[4]
    two = build_profile(likes + [ev("hide", "h1", 4, 5), ev("hide", "h2", 4, 4)], TOPICS, CHOSEN, NOW, None, Params()).weights[4]
    assert one > 0.0 and two == 0.0

def test_order_and_duplicates():
    a, b = ev("like", "x", 1, 10), ev("unlike", "x", 1, 5)
    p1 = build_profile([a, b], TOPICS, CHOSEN, NOW, None, Params())
    p2 = build_profile([b, a, a], TOPICS, CHOSEN, NOW, None, Params())
    assert p1.weights == p2.weights

def test_liked_and_rejected_lists():
    old = NOW - timedelta(days=30)
    events = [ev("like", "l", 1, 0), ev("open", "o", 1, 0), ev("view", "s", 2, 0, {"dwellMs": 100}),
              ev("hide", "h", 3, 0), dataclasses.replace(ev("like", "old", 1, 0), occurred_at=old)]
    p = build_profile(events, TOPICS, CHOSEN, NOW, None, Params())
    liked = {i.article_id: i.strength for i in p.liked}
    rejected = {i.article_id: i.strength for i in p.rejected}
    assert liked == pytest.approx({"l": 1.0, "o": 0.375, "old": 0.5})
    assert rejected == pytest.approx({"s": 0.15, "h": 1.0})
    assert p.touched_article_ids == {"l", "o", "s", "h", "old"}

def test_lists_keep_most_recent_items():
    events = [ev("like", f"a{i}", 1, i) for i in range(150)]
    p = build_profile(events, TOPICS, CHOSEN, NOW, None, Params())
    assert len(p.liked) == 100 and p.liked[0].article_id == "a0"

def test_unknown_topic_changes_no_weight_but_is_listed():
    p = build_profile([ev("like", "x", None, 5)], TOPICS, CHOSEN, NOW, None, Params())
    assert p.weights == initial_weights(TOPICS, CHOSEN, Params())
    assert [i.article_id for i in p.liked] == ["x"]

def test_echo_topic():
    last = NOW - timedelta(minutes=10)
    mk = lambda e: build_profile(e, TOPICS, CHOSEN, NOW, last, Params()).echo_topic
    assert mk([ev("like", "a", 4, 5, received_minutes_ago=5)]) == 4
    assert mk([ev("like", "a", 4, 20, received_minutes_ago=20)]) is None       # déjà pris en compte
    assert mk([ev("read", "a", 2, 5, {"readMs": 10, "scrollDepth": 0.9}, 5)]) == 2
    assert mk([ev("read", "a", 2, 5, {"readMs": 10**6, "scrollDepth": 0.8}, 5)]) is None  # strictement > 0,8
    assert mk([ev("hide", "a", 4, 5, received_minutes_ago=5)]) is None
    assert mk([ev("like", "a", 4, 6, received_minutes_ago=6), ev("save", "b", 2, 5, received_minutes_ago=5)]) == 2
    assert mk([ev("hide", "h", 5, 6, received_minutes_ago=6), ev("like", "a", 5, 5, received_minutes_ago=5)]) == 5

def test_echo_ignored_when_topic_weight_is_zero():
    events = [ev("like", "a", 5, 9, received_minutes_ago=9), ev("hide", "a", 5, 8, received_minutes_ago=8)]
    assert build_profile(events, TOPICS, CHOSEN, NOW, NOW - timedelta(minutes=10), Params()).echo_topic is None
```

- [ ] **Step 2: Lancer** `uv run pytest tests/unit/test_profile.py -v` → FAIL.
- [ ] **Step 3: Implémenter `initial_weights` et `build_profile`.** La mise à jour d'un poids est exactement le pseudo-code de la spec 4.2 ; « plus récent » pour l'écho se juge sur `occurred_at`.
- [ ] **Step 4: Relancer** → PASS.
- [ ] **Step 5: Commit** `feat: reader profile replayed from events`

---

### Task 4: Score d'un article (spec 5)

**Files:**
- Create: `src/swipread_reco/scoring.py`
- Test: `tests/unit/test_scoring.py`

**Interfaces:**
- Consumes: `Params`.
- Produces:

```python
def baseline_mu(embeddings: np.ndarray, n_pairs: int = 2000, seed: int = 0) -> float
def sim(a: np.ndarray, b: np.ndarray, mu: float) -> np.ndarray   # (n,d),(m,d) -> (n,m) dans [0,1]

@dataclass(frozen=True)
class Scores:
    affinity: np.ndarray        # (n,)
    penalty: np.ndarray         # (n,)
    freshness: np.ndarray       # (n,)
    raw: np.ndarray             # (n,) non borné, sert au classement
    score: np.ndarray           # (n,) borné [0,1]
    liked_sim: np.ndarray       # (n, L) ressemblance non pondérée
    rejected_sim: np.ndarray    # (n, R)

def score_candidates(
    embeddings: np.ndarray, published_at: Sequence[datetime],
    liked_emb: np.ndarray, liked_strength: np.ndarray,          # (L,d), (L,)
    rejected_emb: np.ndarray, rejected_strength: np.ndarray,    # (R,d), (R,)
    mu: float, now: datetime, params: Params,
) -> Scores
```

`baseline_mu` : 0.0 si moins de 50 lignes ; sinon, avec `rng = np.random.default_rng(seed)`, `i = rng.integers(0, n, n_pairs)`, `j = (i + 1 + rng.integers(0, n - 1, n_pairs)) % n` (jamais `i == j`), moyenne des cosinus. Les tableaux `liked_*` et `rejected_*` peuvent avoir 0 ligne.

- [ ] **Step 1: Écrire les tests qui échouent.** Helper `u(c)` = vecteur unité de dimension 2 de cosinus `c` avec `(1, 0)` : `np.array([c, sqrt(1 - c*c)], dtype=np.float32)`. `A = u(1.0)[None, :]`, `EMPTY = np.zeros((0, 2), np.float32)`.

```python
def test_sim_recenters_and_clips():
    assert sim(A, np.stack([u(0.9), u(0.5), u(1.0)]), 0.8)[0] == pytest.approx([0.5, 0.0, 1.0], abs=1e-6)

def test_mu_is_zero_under_50_rows_and_deterministic_above():
    rng = np.random.default_rng(1); e = rng.normal(size=(200, 16)).astype(np.float32)
    e /= np.linalg.norm(e, axis=1, keepdims=True)
    assert baseline_mu(e[:49]) == 0.0
    assert baseline_mu(e) == baseline_mu(e) and abs(baseline_mu(e)) < 0.1

def test_affinity_is_mean_of_top_three():
    liked = np.stack([u(0.9), u(0.8), u(0.7), u(0.1)])
    s = score_candidates(A, [NOW], liked, np.ones(4), EMPTY, np.zeros(0), 0.0, NOW, Params())
    assert s.affinity[0] == pytest.approx(0.8, abs=1e-6)
    s = score_candidates(A, [NOW], liked, np.array([0.5, 1, 1, 1]), EMPTY, np.zeros(0), 0.0, NOW, Params())
    assert s.affinity[0] == pytest.approx(0.65, abs=1e-6)

def test_affinity_with_one_or_zero_liked():
    one = score_candidates(A, [NOW], u(0.6)[None, :], np.ones(1), EMPTY, np.zeros(0), 0.0, NOW, Params())
    none = score_candidates(A, [NOW], EMPTY, np.zeros(0), EMPTY, np.zeros(0), 0.0, NOW, Params())
    assert one.affinity[0] == pytest.approx(0.6, abs=1e-6) and none.affinity[0] == 0.0

def test_freshness_halves_every_48h_and_caps_future_dates():
    dates = [NOW, NOW - timedelta(hours=48), NOW - timedelta(hours=96), NOW + timedelta(hours=5)]
    s = score_candidates(np.repeat(A, 4, 0), dates, EMPTY, np.zeros(0), EMPTY, np.zeros(0), 0.0, NOW, Params())
    assert s.freshness == pytest.approx([1.0, 0.5, 0.25, 1.0])

def test_raw_and_clipped_score():
    two_days = [NOW - timedelta(hours=48)]
    s = score_candidates(A, two_days, u(0.6)[None, :], np.ones(1), EMPTY, np.zeros(0), 0.0, NOW, Params())
    assert s.raw[0] == pytest.approx(0.56, abs=1e-6)                     # 0,6×0,6 + 0,4×0,5
    s = score_candidates(A, two_days, u(0.6)[None, :], np.ones(1), A, np.ones(1), 0.0, NOW, Params())
    assert s.penalty[0] == pytest.approx(1.0) and s.raw[0] == pytest.approx(0.06, abs=1e-6)
    four_days = [NOW - timedelta(hours=96)]
    s = score_candidates(A, four_days, EMPTY, np.zeros(0), A, np.ones(1), 0.0, NOW, Params())
    assert s.raw[0] == pytest.approx(-0.4, abs=1e-6) and s.score[0] == 0.0  # classé sur raw, renvoyé borné

def test_penalty_is_max_of_weighted_sims():
    rej = np.stack([u(0.9), u(0.5)])
    s = score_candidates(A, [NOW], EMPTY, np.zeros(0), rej, np.array([0.15, 1.0]), 0.0, NOW, Params())
    assert s.penalty[0] == pytest.approx(0.5, abs=1e-6)
```

- [ ] **Step 2: Lancer** `uv run pytest tests/unit/test_scoring.py -v` → FAIL.
- [ ] **Step 3: Implémenter** les trois fonctions, vectorisées numpy (aucune boucle Python sur les candidats).
- [ ] **Step 4: Relancer** → PASS.
- [ ] **Step 5: Commit** `feat: article scoring`

---

### Task 5: Répartition des places entre thèmes (spec 6)

**Files:**
- Create: `src/swipread_reco/allocation.py`
- Test: `tests/unit/test_allocation.py`

**Interfaces:**
- Produces:

```python
def cap_shares(weights: Mapping[int, float], p_max: float) -> dict[int, float]
def seed_from_request_id(request_id: str) -> int          # 0 <= seed < 2**63
def topic_offsets(shares: Mapping[int, float], echo_topic: int | None, seed: int) -> dict[int, float]

class TopicSequence:
    def __init__(self, shares: Mapping[int, float], offsets: Mapping[int, float]) -> None
    def pop(self) -> int | None        # thème de la prochaine place ; None si plus aucun thème
    def advance(self, topic: int) -> None   # à appeler après avoir rempli une place de `topic`
```

- `seed_from_request_id` : `int.from_bytes(sha256(request_id.encode()).digest()[:8], "big") >> 1` (le décalage fait tenir la graine dans un `bigint`).
- `topic_offsets` : `rng = np.random.default_rng(seed)` ; un tirage `rng.random()` par thème de part > 0, dans l'ordre croissant des `topic_id`, thème en écho compris ; puis la valeur du thème en écho est remplacée par 0. Les thèmes de part nulle sont absents du résultat.
- `TopicSequence` : tas ordonné par `(τ, topic_id)` avec `τ = (k + u) / p`. `pop` retire le thème du tas ; `advance` l'y remet avec `k + 1`. Un thème retiré et jamais avancé est épuisé. Les thèmes de part nulle ou absents de `offsets` sont ignorés.

- [ ] **Step 1: Écrire les tests qui échouent**

```python
def test_cap_redistributes_pro_rata():
    assert cap_shares({1: 0.79, 2: 0.14, 3: 0.07}, 0.60) == pytest.approx({1: 0.60, 2: 0.26667, 3: 0.13333}, abs=1e-4)

def test_cap_iterates_until_none_exceeds():
    assert cap_shares({1: 0.50, 2: 0.45, 3: 0.05}, 0.40) == pytest.approx({1: 0.40, 2: 0.40, 3: 0.20})

def test_cap_not_applied_when_others_are_zero_and_noop_below_cap():
    assert cap_shares({1: 1.0, 2: 0.0}, 0.60) == {1: 1.0, 2: 0.0}
    assert cap_shares({1: 0.5, 2: 0.5}, 0.60) == {1: 0.5, 2: 0.5}

def test_seed_is_stable_and_fits_bigint():
    s = seed_from_request_id("r_abc")
    assert s == seed_from_request_id("r_abc") != seed_from_request_id("r_abd")
    assert 0 <= s < 2**63

def test_offsets():
    o = topic_offsets({1: 0.5, 2: 0.5, 3: 0.0}, echo_topic=2, seed=7)
    assert set(o) == {1, 2} and o[2] == 0.0 and 0.0 <= o[1] < 1.0
    assert o[1] == topic_offsets({1: 0.5, 2: 0.5, 3: 0.0}, echo_topic=None, seed=7)[1]

def drain(seq, n):
    out = []
    while len(out) < n and (t := seq.pop()) is not None:
        out.append(t); seq.advance(t)
    return out

def test_sequence_interleaves():
    assert drain(TopicSequence({1: 0.5, 2: 0.5}, {1: 0.0, 2: 0.5}), 6) == [1, 2, 1, 2, 1, 2]

def test_sequence_is_proportional_on_50_slots():
    seq = TopicSequence({1: 0.3, 2: 0.3, 3: 0.3, 4: 0.1}, {t: 0.5 for t in (1, 2, 3, 4)})
    assert Counter(drain(seq, 50)) == {1: 15, 2: 15, 3: 15, 4: 5}

def test_ties_go_to_smallest_topic_id():
    assert drain(TopicSequence({2: 0.5, 1: 0.5}, {1: 0.0, 2: 0.0}), 2) == [1, 2]

def test_exhausted_topic_gives_its_slots_to_others():
    seq = TopicSequence({1: 0.5, 2: 0.5}, {1: 0.0, 2: 0.5})
    assert seq.pop() == 1; seq.advance(1)
    assert seq.pop() == 2                       # jamais avancé : épuisé
    assert drain(seq, 3) == [1, 1, 1]
    seq2 = TopicSequence({1: 1.0}, {1: 0.0}); seq2.pop()
    assert seq2.pop() is None
```

- [ ] **Step 2: Lancer** `uv run pytest tests/unit/test_allocation.py -v` → FAIL.
- [ ] **Step 3: Implémenter** les quatre éléments (`heapq` pour la séquence).
- [ ] **Step 4: Relancer** → PASS.
- [ ] **Step 5: Commit** `feat: topic share cap and slot sequence`

---

### Task 6: Catalogue synthétique (spec 11.1)

**Files:**
- Create: `src/swipread_reco/sim/__init__.py`, `src/swipread_reco/sim/catalog.py`
- Modify: `src/swipread_reco/models.py` (ajout de `make_catalog`)
- Test: `tests/unit/test_sim_catalog.py`

**Interfaces:**
- Consumes: `Catalog`, `baseline_mu`, `sim`.
- Produces:

```python
# models.py
@dataclass(frozen=True)
class CatalogRow:
    id: str; topic_id: int; source_id: int; language: str
    cluster_id: str | None; published_at: datetime; embedding: np.ndarray

def make_catalog(rows: Sequence[CatalogRow], now: datetime) -> Catalog   # calcule mu, trie par id

# sim/catalog.py
@dataclass(frozen=True)
class SimCatalog:
    catalog: Catalog
    subtopic: dict[str, tuple[int, int]]     # article_id -> (topic_id, n° de sous-thème 0..2)
    def ids(self, topic: int, sub: int | None = None) -> list[str]
    def embeddings(self, ids: Sequence[str]) -> dict[str, np.ndarray]

def make_sim_catalog(
    now: datetime, seed: int = 0, n_topics: int = 10, n_subtopics: int = 3,
    per_subtopic: int = 60, n_sources: int = 8, dim: int = 384,
    age_hours: float | Sequence[float] = 24.0, language: str = "fr",
) -> SimCatalog
```

Thèmes numérotés de 1 à `n_topics`, sources de 1 à `n_sources` attribuées à tour de rôle dans l'ordre de création, `cluster_id = None`, identifiants `a_{thème}_{sous-thème}_{i:02d}`. Géométrie : celle de la spec 11.1 (coefficients 0,6 et 0,7). Si `age_hours` est une séquence, elle est parcourue cycliquement.

- [ ] **Step 1: Écrire les tests qui échouent**

```python
def test_shape_and_determinism():
    a, b = make_sim_catalog(NOW), make_sim_catalog(NOW)
    assert len(a.catalog.ids) == 1800 and a.catalog.embeddings.shape == (1800, 384)
    assert np.array_equal(a.catalog.embeddings, b.catalog.embeddings)
    assert np.allclose(np.linalg.norm(a.catalog.embeddings, axis=1), 1.0, atol=1e-5)
    assert set(a.catalog.source_ids.tolist()) == set(range(1, 9))
    assert len(a.ids(3)) == 180 and len(a.ids(3, 1)) == 60

def test_geometry_separates_subtopic_topic_and_rest():
    s = make_sim_catalog(NOW); c = s.catalog
    e = lambda ids: np.stack([c.embeddings[c.index[i]] for i in ids])
    same_sub = sim(e(s.ids(1, 0)[:20]), e(s.ids(1, 0)[20:40]), c.mu).mean()
    same_topic = sim(e(s.ids(1, 0)[:20]), e(s.ids(1, 1)[:20]), c.mu).mean()
    other = sim(e(s.ids(1, 0)[:20]), e(s.ids(2, 0)[:20]), c.mu).mean()
    assert 0.55 < same_sub < 0.75 and 0.35 < same_topic < 0.55 and other < 0.1
    assert sim(e(s.ids(1, 0)), e(s.ids(1, 0)), c.mu)[~np.eye(60, dtype=bool)].max() < 0.80   # pas de fausse « même actualité »
```

- [ ] **Step 2: Lancer** `uv run pytest tests/unit/test_sim_catalog.py -v` → FAIL.
- [ ] **Step 3: Implémenter** `CatalogRow`, `make_catalog`, `make_sim_catalog`.
- [ ] **Step 4: Relancer** → PASS.
- [ ] **Step 5: Commit** `feat: synthetic catalog for tests`

---

### Task 7: Assemblage du lot (spec 7)

**Files:**
- Create: `src/swipread_reco/assemble.py`
- Test: `tests/unit/test_assemble.py`

**Interfaces:**
- Consumes: `Catalog`, `Scores`, `TopicSequence`, `sim`, `LotItem`, `Params`.
- Produces:

```python
def candidate_mask(catalog: Catalog, language: str, excluded: Set[str]) -> np.ndarray   # (n,) bool

def build_lot(
    catalog: Catalog,
    mask: np.ndarray,                    # candidats
    scores: Scores,                      # calculés sur toutes les lignes du catalogue
    sequence: TopicSequence,
    weights: Mapping[int, float],        # pour explanation.topicWeight
    chosen: Set[int],
    liked_ids: Sequence[str],            # même ordre que les colonnes de scores.liked_sim
    rejected_ids: Sequence[str],
    count: int,
    params: Params,
) -> list[LotItem]
```

Ordre dans un thème : `raw` décroissant, puis `published_at` décroissant, puis `id` croissant. `explanation` contient exactement les clés de la spec 9.1 : `affinity`, `freshness`, `penalty`, `topicWeight`, `nearestLiked` (au plus 3 `{articleId, sim}`, `sim` décroissant, uniquement ceux de `sim > 0`), `nearestRejected` (`{articleId, sim}` du plus ressemblant, ou `None` si aucun ou si `sim = 0`), `relaxedSource`.

- [ ] **Step 1: Écrire les tests qui échouent.** Helper `lot(sim_catalog, shares, *, count=50, excluded=frozenset(), language="fr", chosen={1,2,3}, scores=None, offsets=None, liked_ids=(), rejected_ids=())` : construit masque, scores sans aimés ni rejetés par défaut, séquence avec décalages 0,5 par défaut, et appelle `build_lot`.

```python
S = make_sim_catalog(NOW)
EVEN = {t: 0.1 for t in range(1, 11)}

def test_fills_count_in_sequence_order_with_no_duplicate():
    items = lot(S, EVEN)
    assert len(items) == 50 and len({i.article_id for i in items}) == 50
    assert Counter(i.topic_id for i in items) == {t: 5 for t in range(1, 11)}

def test_source_window():                                        # A10, seconde moitié
    src = [S.catalog.source_ids[S.catalog.index[i.article_id]] for i in lot(S, EVEN)]
    assert all(max(Counter(src[k:k + 10]).values()) <= 2 for k in range(41))

def test_same_story_is_never_repeated():                         # A10, première moitié
    rows = [dataclasses.replace(r, cluster_id="c1") for r in rows_of(S, S.ids(1))] + rows_of(S, S.ids(2))
    items = lot(sim_from(rows), {1: 0.5, 2: 0.5}, count=10)
    assert sum(i.topic_id == 1 for i in items) == 1              # un seul article du cluster

def test_near_duplicate_embeddings_count_as_same_story():
    rows = rows_of(S, S.ids(1)[:5]); twin = dataclasses.replace(rows[0], id="twin")
    items = lot(sim_from(rows + [twin]), {1: 1.0}, count=6)
    assert len(items) == 5 and not {"twin", rows[0].id} <= {i.article_id for i in items}

def test_source_rule_is_relaxed_when_no_other_choice():
    rows = [dataclasses.replace(r, source_id=1) for r in rows_of(S, S.ids(1))]
    items = lot(sim_from(rows), {1: 1.0}, count=10)
    assert len(items) == 10
    assert [i.explanation["relaxedSource"] for i in items] == [False, False] + [True] * 8

def test_exhausted_topic_and_short_lot():                        # Review Focus 3
    rows = rows_of(S, S.ids(1)[:3]) + rows_of(S, S.ids(2))
    items = lot(sim_from(rows), {1: 0.5, 2: 0.5}, count=20)
    assert len(items) == 20 and sum(i.topic_id == 1 for i in items) == 3
    assert len(lot(sim_from(rows_of(S, S.ids(1)[:3])), {1: 1.0}, count=20)) == 3
    assert lot(S, EVEN, language="de") == []
    assert lot(S, {t: 0.0 for t in EVEN}) == []

def test_excluded_and_language():
    ex = frozenset(S.ids(1))
    assert all(i.topic_id != 1 for i in lot(S, EVEN, excluded=ex))

def test_reason_and_explanation_keys():
    liked = S.ids(1, 0)[:3]
    items = lot(S, EVEN, scores=scores_with(S, liked=liked), liked_ids=liked, excluded=frozenset(liked))
    by_topic = {i.topic_id: i for i in reversed(items)}          # première carte de chaque thème
    assert by_topic[1].reason == "similar" and len(by_topic[1].explanation["nearestLiked"]) == 3
    assert by_topic[2].reason == "onboarding" and by_topic[7].reason == "explore"
    assert set(by_topic[1].explanation) == {"affinity", "freshness", "penalty", "topicWeight",
                                            "nearestLiked", "nearestRejected", "relaxedSource"}
    assert 0.0 <= by_topic[1].score <= 1.0

def test_newest_first_without_likes():                           # A12
    s = make_sim_catalog(NOW, age_hours=[1, 30, 60, 90, 120])
    for t in (1, 2):
        dates = [s.catalog.published_at[s.catalog.index[i.article_id]] for i in lot(s, EVEN) if i.topic_id == t]
        assert dates == sorted(dates, reverse=True)
```

Les helpers `rows_of(sim_catalog, ids) -> list[CatalogRow]`, `sim_from(rows) -> SimCatalog` et `scores_with(sim_catalog, liked=(), rejected=())` vont dans `tests/unit/conftest.py`.

- [ ] **Step 2: Lancer** `uv run pytest tests/unit/test_assemble.py -v` → FAIL.
- [ ] **Step 3: Implémenter `candidate_mask` et `build_lot`.** Boucle : `t = sequence.pop()` ; meilleur candidat de `t` respectant les règles 1 et 2 de la spec 7.2, sinon la règle 1 seule ; s'il y en a un, l'ajouter et `sequence.advance(t)` ; sinon ne pas avancer. Arrêt à `count` ou quand `pop()` renvoie `None`.
- [ ] **Step 4: Relancer** → PASS.
- [ ] **Step 5: Commit** `feat: lot assembly with dedup and source variety`

---

### Task 8: Fonction `recommend` et tests d'acceptation (spec 11.2)

**Files:**
- Create: `src/swipread_reco/recommend.py`
- Test: `tests/acceptance/conftest.py`, `tests/acceptance/test_acceptance.py`, `tests/acceptance/test_perf.py`

**Interfaces:**
- Consumes: tout ce qui précède.
- Produces:

```python
@dataclass(frozen=True)
class RecoInput:
    request_id: str
    language: str                    # deux lettres, minuscules
    count: int
    excluded: frozenset[str]         # exclude ∪ seen:{userId} ∪ app.user_articles
    events: Sequence[Event]
    all_topics: Sequence[int]
    chosen: frozenset[int]
    last_request_at: datetime | None
    now: datetime

def recommend(
    inp: RecoInput,
    catalog: Catalog,
    fetch_embeddings: Callable[[Sequence[str]], Mapping[str, np.ndarray]],
    params: Params,
    seed: int | None = None,         # None -> seed_from_request_id(inp.request_id)
) -> Lot
```

Enchaînement : `build_profile` → `fetch_embeddings` sur les aimés et rejetés (ceux qui manquent dans la réponse sont retirés des listes) → `score_candidates` sur tout le catalogue → `cap_shares` → `topic_offsets` → `candidate_mask` avec `inp.excluded ∪ profile.touched_article_ids` → `build_lot`. `Lot.profile` porte les listes après retrait.

- [ ] **Step 1: Écrire `tests/acceptance/conftest.py`**

```python
NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)
CHOSEN = frozenset({1, 2, 3})
X, Y = 4, 5                                    # thèmes non choisis

def ev(type, article_id, sim, minutes_ago, payload=None) -> Event:
    """topic_id tiré de sim.subtopic, word_count 230, received_at = occurred_at."""

def run(sim, events=(), *, count=50, seed=1, excluded=frozenset(), chosen=CHOSEN,
        last_request_at=NOW - timedelta(hours=1), now=NOW, params=Params()) -> Lot:
    """Appelle recommend avec request_id="r_test", language="fr", all_topics=1..10."""

def topics(lot) -> list[int]
def subs(lot, sim, topic) -> list[int]         # sous-thème de chaque carte du thème
```

- [ ] **Step 2: Écrire les tests d'acceptation qui échouent.** Un test par ligne de la spec 11.2, nommé `test_a01_…` à `test_a12_…`. `S = make_sim_catalog(NOW)`.

```python
def test_a01_new_reader():
    c = Counter(topics(run(S)))
    assert 40 <= sum(c[t] for t in CHOSEN) <= 48 and all(13 <= c[t] <= 17 for t in CHOSEN)

def test_a02_two_likes_then_hide():
    e = [ev("like", S.ids(X)[0], S, 30), ev("like", S.ids(X)[1], S, 20), ev("hide", S.ids(Y)[0], S, 10)]
    lot = run(S, e); t = topics(lot); w = lot.profile.weights
    assert w[1] == pytest.approx(0.26039, abs=1e-3) and w[X] == pytest.approx(0.15683, abs=1e-3) and w[Y] == 0.0
    assert t[0] == X and Y not in t and sum(x in CHOSEN for x in t[:10]) >= 6

def test_a03_five_likes():
    e = [ev("like", S.ids(X)[i], S, 50 - i) for i in range(5)]
    assert run(S, e).profile.weights[X] >= 0.30

def test_a04_cap():
    e = [ev("like", S.ids(1)[i], S, 50 - i) for i in range(20)]
    lot = run(S, e); c = Counter(topics(lot))
    assert lot.shares[1] == pytest.approx(0.60) and c[1] <= 31 and len(c) >= 3

def test_a05_liked_subtopic_dominates_its_topic():
    e = [ev("like", S.ids(1, 0)[i], S, 30 - i) for i in range(3)]
    s = subs(run(S, e), S, 1)
    assert s.count(0) / len(s) >= 0.70

def test_a06_hidden_subtopic_recedes():
    lot = run(S, [ev("hide", S.ids(1, 0)[0], S, 10)]); s = subs(lot, S, 1)
    assert 0.01 <= lot.profile.weights[1] < 0.30 and s.count(0) / len(s) <= 0.15

def test_a07_one_article_counts_once():
    a = S.ids(1)[0]
    e = [ev("view", a, S, 9, {"dwellMs": 7000}), ev("open", a, S, 8),
         ev("read", a, S, 7, {"readMs": 10**7, "scrollDepth": 1}), ev("like", a, S, 6)]
    assert sum(h.delta for h in run(S, e).profile.history) == pytest.approx(0.08)

def test_a08_like_then_unlike():
    a = S.ids(1)[0]
    p = run(S, [ev("like", a, S, 9), ev("unlike", a, S, 8)]).profile
    assert p.liked == [] and p.rejected == []
    assert p.weights[1] == pytest.approx(0.2955, abs=1e-3)   # pas 0,30 : la renormalisation n'est pas réversible

def test_a09_no_repeat_over_twenty_lots():
    seen, banned = set(), frozenset(S.ids(2)[:10])
    for k in range(20):
        ids = [i.article_id for i in run(S, excluded=frozenset(seen) | banned, seed=k).items]
        assert not set(ids) & (seen | banned)
        seen |= set(ids)

def test_a10_no_same_story_and_source_window():
    lot = run(S); c = S.catalog
    rows = [c.index[i.article_id] for i in lot.items]
    pair = sim(c.embeddings[rows], c.embeddings[rows], c.mu)
    assert pair[~np.eye(len(rows), dtype=bool)].max() < 0.80
    src = c.source_ids[rows].tolist()
    assert all(max(Counter(src[k:k + 10]).values()) <= 2 for k in range(len(src) - 9))

def test_a11_deterministic():
    e = [ev("like", S.ids(X)[0], S, 30)]
    assert run(S, e) == run(S, e)
    assert [i.article_id for i in run(S, e, seed=2).items] != [i.article_id for i in run(S, e, seed=3).items]

def test_a12_newest_first_without_interaction():
    s = make_sim_catalog(NOW, age_hours=[1, 30, 60, 90, 120]); lot = run(s)
    for t in CHOSEN:
        d = [s.catalog.published_at[s.catalog.index[i.article_id]] for i in lot.items if i.topic_id == t]
        assert d == sorted(d, reverse=True)

def test_liked_article_without_embedding_is_dropped():           # Review Focus 2
    gone = Event("e1", "like", "a_missing", NOW, NOW, {}, 1, 500)
    lot = run(S, [gone, ev("like", S.ids(1, 0)[0], S, 5)])
    assert [i.article_id for i in lot.profile.liked] == [S.ids(1, 0)[0]] and len(lot.items) == 50
```

A1 et A2 dépendent de la graine pour leurs bornes ; si l'un échoue avec `seed=1`, vérifier la répartition avant de toucher à la graine ou au seuil.

- [ ] **Step 3: Écrire `tests/acceptance/test_perf.py`** (A13), marqué `@pytest.mark.perf` :

```python
def test_a13_p95_under_80ms():
    s = make_sim_catalog(NOW, per_subtopic=167)                 # 5 010 articles
    ids = s.catalog.ids
    events = [ev("view", ids[i * 7], s, 400 - i, {"dwellMs": 300 + 40 * i}) for i in range(260)] + \
             [ev("like", ids[i * 11 + 3], s, 100 - i) for i in range(40)]
    times = []
    for k in range(200):
        t0 = time.perf_counter(); run(s, events, seed=k); times.append(time.perf_counter() - t0)
    assert np.percentile(times, 95) < 0.080
```

- [ ] **Step 4: Lancer** `uv run pytest tests/acceptance -v -m "not perf"` → FAIL (`recommend` absent).
- [ ] **Step 5: Implémenter `recommend`.**
- [ ] **Step 6: Relancer** `uv run pytest tests/acceptance -v -m "not perf"` → 13 tests PASS. Puis `uv run pytest tests/acceptance -m perf` → PASS.
- [ ] **Step 7: Commit** `feat: recommend pipeline with acceptance tests A1-A13`

---

### Task 9: Accès PostgreSQL (`Store`)

**Files:**
- Create: `src/swipread_reco/store.py`, `compose.yaml`, `sql/app_schema.sql`
- Test: `tests/integration/conftest.py`, `tests/integration/test_store.py`

**Interfaces:**
- Consumes: modèles, `Params`, `MODEL_VERSION`.
- Produces:

```python
class Store(Protocol):
    def ensure_schema(self) -> None                                  # tables reco, database.md
    def topics(self) -> list[Topic]
    def user_locale(self, user_id: str) -> str | None                # None = utilisateur absent
    def chosen_topics(self, user_id: str) -> frozenset[int]
    def events(self, user_id: str, limit: int) -> list[Event]        # les `limit` plus récents
    def user_article_ids(self, user_id: str) -> frozenset[str]       # app.user_articles
    def embeddings(self, ids: Sequence[str]) -> dict[str, np.ndarray]
    def last_request_at(self, user_id: str) -> datetime | None
    def catalog_rows(self, now: datetime, max_age_days: int) -> list[CatalogRow]
    def save_lot(self, lot: Lot, *, user_id: str, session_id: str | None,
                 requested_count: int, generated_at: datetime, params: Params) -> None
    def request(self, request_id: str) -> dict[str, Any] | None      # ligne + items + titres
    def article_titles(self, ids: Sequence[str]) -> dict[str, str]
    # worker
    def articles_to_embed(self, model: str, limit: int) -> list[tuple[str, str, str]]   # id, title, summary
    def write_embeddings(self, rows: Sequence[tuple[str, np.ndarray]], model: str, now: datetime) -> None
    def fallback_ids(self, topic_id: int, now: datetime, max_age_days: int, limit: int) -> list[str]
    def purge(self, now: datetime, retention_days: int) -> int       # nombre de requêtes supprimées

class PgStore:                        # implémente Store
    def __init__(self, dsn: str) -> None   # pool psycopg, pgvector enregistré
```

Règles SQL fixées par la spec :
- `events` : types `view, open, read, like, unlike, save, share, hide` ; `topic_id` = thème principal (confidence max, puis plus petit `topic_id`) ; `word_count` de `app.articles` ; jointures externes pour qu'un article supprimé donne `topic_id = None`.
- `catalog_rows` : `status = 'active'`, `embedding IS NOT NULL`, `published_at >= now − max_age_days`, source `active`, thème principal présent ; `language` ramenée à ses deux premières lettres en minuscules.
- `articles_to_embed` : `embedding IS NULL OR embedding_model <> model`, `ingested_at` décroissant.
- `fallback_ids` : mêmes filtres que `catalog_rows` sauf l'embedding, thème principal = `topic_id`, `published_at` décroissant.
- `save_lot` : une transaction ; `profile` et `explanation` au format de la spec 9.1, les thèmes désignés par leur `slug` dans `profile.weights` et `profile.shares`.
- `purge` : requêtes de plus de `retention_days` jours, et celles dont `user_id` n'est plus dans `app.users`.

- [ ] **Step 1: Écrire `compose.yaml`** (`pgvector/pgvector:pg16` sur 5433, `redis:7` sur 6380) et `sql/app_schema.sql` (copie du DDL `app` de `database.md`, plus une partition `app.events_default DEFAULT`).
- [ ] **Step 2: Écrire `tests/integration/conftest.py`** : fixture `store` qui recrée les schémas `app` et `reco` à partir de `sql/app_schema.sql` puis `ensure_schema()`, et fixture `seed` qui insère 3 thèmes, 2 sources (dont une inactive), 1 utilisateur `u_1` avec le thème 1, et 6 articles couvrant chaque filtre (actif récent ; expiré ; sans embedding ; trop vieux ; source inactive ; sans thème).
- [ ] **Step 3: Écrire les tests qui échouent** (tous `@pytest.mark.integration`)

```python
def test_ensure_schema_is_idempotent(store): store.ensure_schema(); store.ensure_schema()
def test_user_locale_and_topics(store, seed):
    assert store.user_locale("u_1") == "fr-FR" and store.user_locale("u_nope") is None
    assert store.chosen_topics("u_1") == {1} and [t.slug for t in store.topics()] == ["tech", "eco", "sport"]
def test_catalog_rows_apply_every_filter(store, seed):
    assert [r.id for r in store.catalog_rows(NOW, 14)] == ["a_ok"]
def test_primary_topic_is_highest_confidence_then_lowest_id(store, seed): ...
    # a_ok a (thème 2, 0.9), (thème 1, 0.9), (thème 3, 0.5) -> topic_id == 1
def test_events_are_limited_to_most_recent_and_carry_topic(store, seed): ...
    # 5 événements insérés, limit=3 -> les 3 plus récents ; article supprimé -> topic_id None
def test_impressions_are_not_returned(store, seed): ...
def test_embeddings_roundtrip_and_skip_missing(store, seed):
    assert set(store.embeddings(["a_ok", "a_noemb", "a_nope"])) == {"a_ok"}
def test_save_lot_then_request_and_last_request_at(store, seed): ...
    # profile["weights"] indexé par slug ; items dans l'ordre ; last_request_at == generated_at
def test_articles_to_embed_and_write(store, seed): ...
    # a_noemb listé ; après write_embeddings, plus listé, embedding_model renseigné
def test_fallback_ids_newest_first(store, seed): ...
def test_purge_removes_old_and_orphans(store, seed): ...
    # une requête de 31 jours, une d'un utilisateur supprimé, une récente -> purge() == 2
```

- [ ] **Step 4: Lancer** `docker compose up -d && RECO_TEST_DSN=postgresql://postgres:postgres@localhost:5433/postgres uv run pytest tests/integration/test_store.py -v` → FAIL.
- [ ] **Step 5: Implémenter `Store` et `PgStore`.**
- [ ] **Step 6: Relancer** → PASS. Sans `RECO_TEST_DSN`, `uv run pytest` → tests d'intégration ignorés, le reste PASS.
- [ ] **Step 7: Commit** `feat: postgres store`

---

### Task 10: API `/v1/recommendations`, catalogue en mémoire, `/healthz`

**Files:**
- Create: `src/swipread_reco/catalog_cache.py`, `src/swipread_reco/api.py`, `src/swipread_reco/__main__.py`
- Test: `tests/api/conftest.py`, `tests/api/test_recommendations.py`, `tests/api/test_catalog_cache.py`

**Interfaces:**
- Consumes: `Store`, `recommend`, `RecoInput`, `make_catalog`, `Params`.
- Produces:

```python
# catalog_cache.py
class CatalogCache:
    def __init__(self, store: Store, params: Params, clock: Callable[[], datetime]) -> None
    def refresh(self) -> bool            # True si rechargé ; False si le Store a levé (ancien catalogue gardé)
    def get(self) -> Catalog | None
    def start(self) -> None              # fil d'arrière-plan : refresh toutes les catalog_refresh_s
    def stop(self) -> None

# api.py
class SeenReader(Protocol):
    def seen(self, user_id: str) -> frozenset[str]      # SMEMBERS seen:{userId} ; lève si Redis est injoignable

def create_app(store: Store, seen: SeenReader, cache: CatalogCache, params: Params,
               clock: Callable[[], datetime] = utcnow) -> FastAPI
```

Comportement de `POST /v1/recommendations` (formats : contrat, section « Endpoint ») :
- 400 : corps invalide, `count` hors de 1..100 (FastAPI renvoie 422 par défaut : le convertir en 400).
- 404 : `store.user_locale(userId)` renvoie `None`.
- 503 : exception du `Store` pendant le traitement.
- Langue : deux premières lettres de `context.locale`, sinon de la locale de l'utilisateur, en minuscules.
- `requestId = "r_" + uuid4().hex`. `generatedAt = clock()`.
- Exclusions : `exclude ∪ seen.seen(userId) ∪ store.user_article_ids(userId)`. Si `seen` lève : avertissement dans les journaux, on continue sans.
- Catalogue absent ou vide : 200 avec `items: []`, rien n'est enregistré.
- Sinon : `recommend(...)`, `store.save_lot(...)`, réponse avec `items` dans l'ordre du lot, `score` arrondi à 4 décimales.
- `GET /healthz` : `{"status": "ok", "catalogSize": n, "catalogAgeSeconds": x}` ; `catalogSize: 0` et `catalogAgeSeconds: null` s'il n'y a pas de catalogue.
- `python -m swipread_reco api` lit `RECO_DATABASE_URL`, `RECO_REDIS_URL`, `RECO_PORT` (défaut 8000), appelle `ensure_schema()`, un premier `refresh()`, `cache.start()`, puis uvicorn.

- [ ] **Step 1: Écrire `tests/api/conftest.py`** : `FakeStore` en mémoire implémentant `Store` au-dessus d'un `SimCatalog` (utilisateurs, thèmes choisis, événements et lots enregistrés dans des dictionnaires ; attribut `fail: bool` qui fait lever chaque méthode), `FakeSeen` (ensemble par utilisateur, attribut `fail`), et fixture `client` (`TestClient` sur `create_app`, horloge fixe `NOW`, cache déjà rechargé).
- [ ] **Step 2: Écrire les tests qui échouent**

```python
def post(client, **body): return client.post("/v1/recommendations", json={"userId": "u_1", "count": 50, **body})

def test_contract_shape(client):
    r = post(client, context={"sessionId": "s_1", "locale": "fr-FR", "device": "mobile"}); b = r.json()
    assert r.status_code == 200 and set(b) == {"requestId", "modelVersion", "generatedAt", "items"}
    assert b["modelVersion"] == "topic-weights-v1" and re.fullmatch(r"r_[0-9a-f]{32}", b["requestId"])
    assert len(b["items"]) == 50 and set(b["items"][0]) == {"articleId", "score", "reason"}
    assert all(0 <= i["score"] <= 1 and i["reason"] in {"similar", "onboarding", "explore"} for i in b["items"])

def test_count_is_respected(client): assert len(post(client, count=7).json()["items"]) == 7
@pytest.mark.parametrize("body", [{"count": 0}, {"count": 101}, {"count": "x"}, {"userId": None}])
def test_invalid_request_is_400(client, body): assert post(client, **body).status_code == 400
def test_unknown_user_is_404(client): assert post(client, userId="u_nope").status_code == 404
def test_store_failure_is_503(client, store): store.fail = True; assert post(client).status_code == 503

def test_new_user_with_topics_gets_a_lot(client): assert len(post(client, userId="u_new").json()["items"]) == 50

def test_exclusions_come_from_request_redis_and_user_articles(client, store, seen):
    a, b, c = store.sim.ids(1)[:3]
    seen.add("u_1", b); store.user_articles["u_1"] = {c}
    ids = {i["articleId"] for i in post(client, exclude=[a]).json()["items"]}
    assert not ids & {a, b, c}

def test_redis_failure_is_tolerated(client, seen, caplog):       # Review Focus 5
    seen.fail = True
    assert len(post(client).json()["items"]) == 50 and "seen" in caplog.text

def test_locale_falls_back_to_user_then_filters(client, store):
    store.locales["u_1"] = "de-DE"
    assert post(client).json()["items"] == []                    # Review Focus 3
    assert len(post(client, context={"locale": "fr-FR"}).json()["items"]) == 50

def test_lot_is_saved_with_request_fields(client, store):
    b = post(client, context={"sessionId": "s_9"}).json(); saved = store.lots[b["requestId"]]
    assert saved["user_id"] == "u_1" and saved["session_id"] == "s_9" and saved["requested_count"] == 50
    assert [i.article_id for i in saved["lot"].items] == [i["articleId"] for i in b["items"]]

def test_echo_uses_previous_request_time(client, store):
    post(client)                                                  # fixe last_request_at
    store.add_event("u_1", "like", store.sim.ids(4)[0], received_at=NOW + timedelta(seconds=5))
    first = post(client).json()["items"][0]["articleId"]
    assert store.sim.subtopic[first][0] == 4

def test_empty_catalog_returns_empty_items_and_saves_nothing(empty_client, store):
    assert post(empty_client).json()["items"] == [] and store.lots == {}

def test_no_topics_at_all_returns_empty_items(client, store):    # Review Focus 3
    store.topic_list = []
    assert post(client).json()["items"] == []

def test_healthz(client, empty_client):
    assert client.get("/healthz").json() == {"status": "ok", "catalogSize": 1800, "catalogAgeSeconds": 0.0}
    assert empty_client.get("/healthz").json()["catalogSize"] == 0
```

```python
# test_catalog_cache.py
def test_refresh_failure_keeps_previous_catalog(store):          # Review Focus 4
    cache = CatalogCache(store, Params(), lambda: NOW); assert cache.refresh() is True
    old = cache.get(); store.fail = True
    assert cache.refresh() is False and cache.get() is old

def test_refresh_swaps_atomically_and_recomputes_mu(store):
    cache = CatalogCache(store, Params(), lambda: NOW); cache.refresh(); first = cache.get()
    store.drop_articles(store.sim.ids(1)); cache.refresh()
    assert len(cache.get().ids) == 1620 and len(first.ids) == 1800
```

- [ ] **Step 3: Lancer** `uv run pytest tests/api -v` → FAIL.
- [ ] **Step 4: Implémenter** `CatalogCache`, `create_app`, un `RedisSeen` (classe concrète de `SeenReader`) et `__main__`.
- [ ] **Step 5: Relancer** → PASS.
- [ ] **Step 6: Vérification de bout en bout** : `docker compose up -d`, charger `sql/app_schema.sql` et le jeu de la fixture `seed`, `uv run python -m swipread_reco api`, puis `curl -s -X POST localhost:8000/v1/recommendations -H 'content-type: application/json' -d '{"userId":"u_1","count":5}'` → JSON avec `modelVersion: "topic-weights-v1"` et un item (`a_ok`).
- [ ] **Step 7: Commit** `feat: recommendations endpoint with in-memory catalog`

---

### Task 11: Routes d'explication (spec 9.2)

**Files:**
- Create: `src/swipread_reco/explain.py`
- Modify: `src/swipread_reco/api.py` (inclure le routeur)
- Test: `tests/api/test_explain.py`

**Interfaces:**
- Consumes: `Store`, `build_profile`, `cap_shares`.
- Produces: `def explain_router(store: Store, params: Params, clock: Callable[[], datetime]) -> APIRouter`

Formats de réponse :

```
GET /v1/explain/users/{userId}
{ "userId", "topics": [{ "id", "slug", "label", "chosen", "weight", "share" }],      # tri : weight décroissant, id croissant
  "liked": [{ "articleId", "title", "strength" }], "rejected": [idem],
  "history": [{ "eventId", "type", "articleId", "topicSlug", "delta", "weightAfter", "occurredAt" }] }   # 50 derniers, plus récent d'abord

GET /v1/explain/requests/{requestId}
{ "requestId", "userId", "sessionId", "modelVersion", "requestedCount", "seed", "generatedAt", "profile", "params",
  "items": [{ "position", "articleId", "title", "topicSlug", "reason", "score", "explanation" }] }
```

404 si l'utilisateur ou la requête n'existe pas. Le profil est recalculé à la demande avec `last_request_at` réel ; `title` vaut `null` si l'article a disparu.

- [ ] **Step 1: Écrire les tests qui échouent**

```python
def test_user_profile_matches_spec_example(client, store):
    for i, (t, a) in enumerate([("like", store.sim.ids(4)[0]), ("like", store.sim.ids(4)[1]), ("hide", store.sim.ids(5)[0])]):
        store.add_event("u_1", t, a, occurred_at=NOW - timedelta(minutes=30 - 10 * i))
    b = client.get("/v1/explain/users/u_1").json(); by = {t["id"]: t for t in b["topics"]}
    assert by[4]["weight"] == pytest.approx(0.15683, abs=1e-3) and by[5]["weight"] == 0.0 and by[1]["chosen"] is True
    assert sum(t["share"] for t in b["topics"]) == pytest.approx(1.0)
    assert [h["type"] for h in b["history"]] == ["hide", "like", "like"] and b["history"][0]["delta"] < 0
    assert len(b["liked"]) == 2 and b["liked"][0]["title"] and len(b["rejected"]) == 1

def test_history_is_capped_at_50(client, store): ...
def test_request_explanation(client):
    rid = post(client, count=5).json()["requestId"]; b = client.get(f"/v1/explain/requests/{rid}").json()
    assert [i["position"] for i in b["items"]] == [0, 1, 2, 3, 4] and b["requestedCount"] == 5
    assert {"affinity", "freshness", "penalty", "topicWeight"} <= set(b["items"][0]["explanation"])
    assert b["items"][0]["title"] and b["items"][0]["topicSlug"]
def test_unknown_user_and_request_are_404(client):
    assert client.get("/v1/explain/users/u_nope").status_code == 404
    assert client.get("/v1/explain/requests/r_nope").status_code == 404
```

- [ ] **Step 2: Lancer** `uv run pytest tests/api/test_explain.py -v` → FAIL.
- [ ] **Step 3: Implémenter `explain_router`** et l'inclure dans `create_app`.
- [ ] **Step 4: Relancer** → PASS.
- [ ] **Step 5: Commit** `feat: explanation routes`

---

### Task 12: Worker — encodage, listes de repli, purge (spec 8.4, 8.5, 8.7)

**Files:**
- Create: `src/swipread_reco/worker/__init__.py`, `worker/encode.py`, `worker/fallback.py`, `worker/purge.py`
- Modify: `src/swipread_reco/__main__.py` (commande `worker`)
- Test: `tests/unit/test_worker.py`

**Interfaces:**
- Consumes: `Store`, `Params`, `EMBEDDING_MODEL`.
- Produces:

```python
# encode.py
class Encoder(Protocol):
    def encode(self, texts: Sequence[str]) -> np.ndarray          # (n, d) float32, norme 1
class E5Encoder:                                                   # sentence-transformers, chargé au premier appel
    def __init__(self, model_name: str = "intfloat/multilingual-e5-small") -> None
def article_text(title: str, summary: str) -> str
def encode_pending(store: Store, encoder: Encoder, now: datetime, batch: int = 64) -> int   # nombre encodé

# fallback.py
def write_fallback(store: Store, redis: Redis, now: datetime, params: Params,
                   limit: int = 50, ttl_s: int = 3600) -> None

# purge.py
def run_purge(store: Store, now: datetime, retention_days: int = 30) -> int

# worker/__init__.py
def run_worker(store, redis, encoder, params, clock, sleep) -> None   # boucle : encodage toutes les encode_interval_s, repli toutes les 600 s, purge toutes les 3600 s
```

`article_text` : `"query: {titre}. {résumé}"`, balises HTML retirées du résumé, entités décodées, espaces multiples réduits à un seul ; si le titre finit déjà par `.`, `!` ou `?`, pas de point ajouté. `write_fallback` : pour chaque thème, écrit `fallback:{slug}:tmp` puis `RENAME` vers `fallback:{slug}` et `EXPIRE` ; si la liste est vide, supprime `fallback:{slug}`. Une exception dans une des trois activités est journalisée et n'arrête pas la boucle.

- [ ] **Step 1: Écrire les tests qui échouent** (`FakeStore` de `tests/api/conftest.py` déplacé dans `tests/fakes.py` ; `fakeredis.FakeRedis(decode_responses=True)` ; `FakeEncoder` renvoyant des vecteurs unité déterministes)

```python
@pytest.mark.parametrize("title, summary, expected", [
    ("Titre", "Un <b>résumé</b>  propre.", "query: Titre. Un résumé propre."),
    ("Vraiment ?", "Oui &amp; non", "query: Vraiment ? Oui & non"),
    ("Titre", "", "query: Titre."),
])
def test_article_text(title, summary, expected): assert article_text(title, summary) == expected

def test_encode_pending_writes_model_and_stops_when_done(store):
    store.pending = [("a1", "T1", "S1"), ("a2", "T2", "S2")]
    assert encode_pending(store, FakeEncoder(), NOW) == 2
    assert store.written_model == "multilingual-e5-small" and set(store.written) == {"a1", "a2"}
    assert encode_pending(store, FakeEncoder(), NOW) == 0

def test_fallback_lists_by_slug_with_ttl(store, redis):
    write_fallback(store, redis, NOW, Params())
    slug = store.topics()[0].slug
    assert redis.lrange(f"fallback:{slug}", 0, -1) == store.fallback_ids(store.topics()[0].id, NOW, 14, 50)
    assert 0 < redis.ttl(f"fallback:{slug}") <= 3600 and not redis.exists(f"fallback:{slug}:tmp")

def test_fallback_replaces_previous_list_and_deletes_empty_ones(store, redis):
    redis.rpush("fallback:tech", "old"); store.drop_articles(store.sim.ids(1)); write_fallback(store, redis, NOW, Params())
    assert not redis.exists("fallback:tech")

def test_worker_loop_survives_a_failing_step(store, redis, caplog):
    store.fail_purge = True
    run_worker(store, redis, FakeEncoder(), Params(), FakeClock(NOW), sleep=StopAfter(seconds=3700))
    assert "purge" in caplog.text and store.encode_calls > 200 and redis.exists("fallback:tech")
```

- [ ] **Step 2: Lancer** `uv run pytest tests/unit/test_worker.py -v` → FAIL.
- [ ] **Step 3: Implémenter** les quatre fonctions, `E5Encoder` et la commande `python -m swipread_reco worker`.
- [ ] **Step 4: Relancer** → PASS.
- [ ] **Step 5: Vérifier le vrai modèle une fois** : `uv run python -c "from swipread_reco.worker.encode import E5Encoder; import numpy as np; v = E5Encoder().encode(['query: Bonjour. Monde']); print(v.shape, round(float(np.linalg.norm(v[0])), 3))"` → `(1, 384) 1.0`.
- [ ] **Step 6: Commit** `feat: worker for encoding, fallback lists and purge`

---

### Task 13: Lecteurs simulés et rapport pour le jury (spec 11.3)

**Files:**
- Create: `src/swipread_reco/sim/backend.py`, `sim/readers.py`, `sim/report.py`
- Test: `tests/unit/test_sim_report.py`

**Interfaces:**
- Consumes: `recommend`, `RecoInput`, `SimCatalog`, `Params`.
- Produces:

```python
# readers.py
@dataclass(frozen=True)
class Reader:
    name: str
    chosen: frozenset[int]
    loves: frozenset[tuple[int, int]]     # (thème, sous-thème) -> vue de 8 s puis like
    likes_topics: frozenset[int]          # vue de 8 s, sans like
    hates_topics: frozenset[int]          # hide
    # tout le reste : vue de 800 ms (zappé)
    def react(self, article_id: str, sim: SimCatalog) -> list[tuple[str, dict]]   # (type, payload)
    def relevant(self, article_id: str, sim: SimCatalog) -> bool                  # loves ou likes_topics

READERS: list[Reader]     # dans cet ordre : "fidele", "passionne", "allergique", "zappeur"

# backend.py
@dataclass(frozen=True)
class Served:
    rank: int                 # 1-based, sur toute la session
    article_id: str
    topic_id: int
    reason: str
    lot_index: int            # n° du lot de reco dont vient la carte
    computed_after_rank: int  # rang de la dernière carte vue quand ce lot a été calculé

def simulate(reader: Reader, sim: SimCatalog, n_cards: int, start: datetime, params: Params,
             seed: int = 0) -> list[Served]

# report.py
def page_rows(served: Sequence[Served], reader: Reader, sim: SimCatalog) -> list[dict]   # une ligne par page de 10
def write_report(out_dir: Path, n_cards: int = 100, seed: int = 0) -> None               # report.md, report.csv
```

Lecteurs : `fidele` choisit {1, 2, 3}, aime les thèmes {1, 2, 3}, adore (1, 0) ; `passionne` choisit {1, 2, 3}, adore (4, 0), (4, 1), (4, 2), aime {1, 2, 3} ; `allergique` choisit {1, 2, 3}, déteste {3}, aime {1, 2} ; `zappeur` choisit {1, 2, 3}, n'aime rien.

`simulate` imite le backend : stock vide au départ ; remplissage (`count=50`, `exclude` = contenu du stock) quand le stock passe sous 20 ; page de 10 retirée du stock quand le lecteur a vu toutes ses cartes ; une carte toutes les 10 secondes d'horloge ; après chaque carte, les événements du lecteur sont ajoutés à l'historique. Événement fort (`like`, `save`, `share`, `hide`) sur la carte de position `p` dans la page (à partir de 0) : le stock est réduit à ses `p` premiers éléments puis rempli. Chaque appel utilise `request_id = f"r_{reader.name}_{lot_index}"` et l'horloge simulée ; `last_request_at` est l'heure du lot précédent.

Colonnes de `page_rows` et du CSV : `reader, page, relevant_share, top_topic_share, explore_share, n_topics, n_sources`. `top_topic_share` : part du thème préféré (`fidele` 1, `passionne` 4, `allergique` 1, `zappeur` 1). `report.md` : un tableau par lecteur, précédé d'une phrase décrivant sa règle.

- [ ] **Step 1: Écrire les tests qui échouent**

```python
S = make_sim_catalog(NOW)
run = lambda name, n=100, seed=0: simulate(next(r for r in READERS if r.name == name), S, n, NOW, Params(), seed)

def test_no_repeat_and_exact_length():
    s = run("fidele"); assert len(s) == 100 and len({c.article_id for c in s}) == 100

def test_n_plus_10_rule():
    reader = next(r for r in READERS if r.name == "allergique"); s = run("allergique")
    strong = [c.rank for c in s if c.topic_id in reader.hates_topics]      # chaque carte détestée est masquée
    n = strong[0]
    assert s[n + 9].computed_after_rank >= n                                # la carte n+10 connaît l'événement
    assert all(c.computed_after_rank < n for c in s[n:n + 9])               # les 9 d'avant, non

def test_allergic_reader_stops_seeing_hated_topic():
    s = run("allergique"); share = lambda cards: sum(c.topic_id == 3 for c in cards) / len(cards)
    assert share(s[:20]) > 0.10 and share(s[50:]) < 0.05

def test_faithful_reader_gets_more_of_loved_subtopic():
    s = run("fidele"); loved = lambda cards: sum(S.subtopic[c.article_id] == (1, 0) for c in cards) / len(cards)
    assert loved(s[50:]) > loved(s[:10])

def test_skipper_always_gets_cards_and_keeps_floors():
    assert len(run("zappeur", 200)) == 200

def test_simulation_is_deterministic():
    assert run("passionne") == run("passionne")

def test_report_files(tmp_path):
    write_report(tmp_path, n_cards=100)
    rows = list(csv.DictReader((tmp_path / "report.csv").open()))
    assert len(rows) == 40 and set(rows[0]) == {"reader", "page", "relevant_share", "top_topic_share",
                                                 "explore_share", "n_topics", "n_sources"}
    md = (tmp_path / "report.md").read_text()
    assert all(f"## {r.name}" in md for r in READERS)
```

- [ ] **Step 2: Lancer** `uv run pytest tests/unit/test_sim_report.py -v` → FAIL.
- [ ] **Step 3: Implémenter** `Reader`, `READERS`, `simulate`, `page_rows`, `write_report`, et le point d'entrée `python -m swipread_reco.sim.report --out report/`.
- [ ] **Step 4: Relancer** → PASS. Puis `uv run python -m swipread_reco.sim.report --out report/` et relire `report/report.md` : pour `allergique`, `top_topic_share` monte et le thème 3 disparaît après les premières pages.
- [ ] **Step 5: Lancer toute la suite** : `uv run pytest -m "not perf"` → tout PASS (intégration ignorée sans `RECO_TEST_DSN`) ; `uv run ruff check` → propre.
- [ ] **Step 6: Commit** `feat: simulated readers and jury report`

---

## Hors plan

- Le remplacement du mock de reco côté backend et la règle du stock (contrat, règle 4) : travail du backend.
- L'écran qui affiche les explications au jury : non décidé.
- La mesure de la latence complète (objectif 150 ms, spec 8.3) : à faire sur l'environnement de démo, avec la vraie base.
- La vérification de `DUP_SIM` et `AFF_REASON_MIN` sur de vrais articles : à faire à la main quand l'ingestion tourne (spec, section 10).
