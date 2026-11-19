"""Test data of the end-to-end tests, written by scripts/e2e.sh once the backend has migrated.

Run as `python -m tests.e2e.seed` with E2E_ADMIN_DSN (superuser of the test database) and
E2E_RECO_DSN (login role of the reco, created here as a member of `swipread_reco`).

The backend's 300 sample articles have near-identical titles and a single source: encoded with
the real model they would all be near-duplicates. So this script
- spreads them over SOURCE_COUNT sources and gives them recent, varied publication times;
- writes synthetic embeddings for them, built per topic with the geometry of
  swipread_reco.sim.catalog (topic centre, 3 subtopic centres, article = subtopic + noise);
- adds DUP_ID, a near-duplicate of DUP_OF (same topic, embedding almost identical);
- adds the EXTRA_PAIRS articles, real French text and no embedding, for the worker to encode.
"""

import os
from datetime import UTC, datetime, timedelta

import numpy as np
import psycopg
from pgvector.psycopg import register_vector
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from swipread_reco.params import EMBEDDING_MODEL
from swipread_reco.sim.catalog import ARTICLE_NOISE, SUBTOPIC_NOISE, _noise, _unit
from swipread_reco.store import PgStore

SEED = 0
DIM = 384
SOURCE_COUNT = 6
SUBTOPICS = 3
MAX_AGE_HOURS = 240          # sample articles published over the last 10 days
DUP_OF = "a_dev0001"         # article liked in E7
DUP_ID = "a_e2e_dup"         # its near-duplicate, the freshest article of its topic
DUP_NOISE = 0.05             # cos(DUP_ID, DUP_OF) ~ 0.999

# (id, topic slug, title, summary), in pairs about the same subject.
EXTRA_PAIRS = [
    [
        ("a_e2e_foot1", "sport", "Ligue 1 : le PSG s'impose face à l'OM au Parc des Princes",
         ("Dans un classique tendu, les Parisiens ont battu Marseille deux buts à un grâce à un"
         " doublé de leur attaquant, et prennent cinq points d'avance en tête du championnat.")),
        ("a_e2e_foot2", "sport", "Football : Marseille s'incline à Paris malgré une belle réaction",
         ("L'OM a longtemps tenu tête au PSG avant de céder en seconde période ; l'entraîneur"
         " marseillais regrette un penalty refusé et vise la deuxième place de Ligue 1.")),
    ],
    [
        ("a_e2e_bce1", "economy", "La BCE maintient ses taux directeurs inchangés",
         ("La Banque centrale européenne laisse son taux de dépôt à 2 % et juge l'inflation"
         " maîtrisée dans la zone euro, tout en restant prudente sur la croissance.")),
        ("a_e2e_bce2", "economy", "Politique monétaire : Francfort temporise avant de baisser ses taux",
         ("Les gouverneurs de la Banque centrale européenne attendent de nouveaux chiffres de"
         " l'inflation avant tout assouplissement du crédit, selon la présidente de l'institution.")),
    ],
    [
        ("a_e2e_film1", "culture", "Festival de Cannes : la Palme d'or pour un drame iranien",
         ("Le jury du Festival de Cannes a récompensé un film tourné clandestinement à Téhéran,"
         " salué par la critique pour sa mise en scène et l'interprétation de son actrice.")),
        ("a_e2e_film2", "culture", "Cannes : le palmarès du festival consacre le cinéma d'auteur",
         ("Sur la Croisette, la cérémonie de clôture a distingué réalisateurs et comédiens ;"
         " la Palme d'or revient à un long-métrage iranien très applaudi en sélection officielle.")),
    ],
]
EXTRA_IDS = [article[0] for pair in EXTRA_PAIRS for article in pair]


def _create_reco_role(admin: psycopg.Connection, reco_dsn: str) -> None:
    info = conninfo_to_dict(reco_dsn)
    role, password = info["user"], info["password"]
    exists = admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
    if not exists:
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} IN ROLE swipread_reco").format(
            sql.Identifier(role), sql.Literal(password)))


def _create_sources(admin: psycopg.Connection) -> list[int]:
    ids = []
    for n in range(1, SOURCE_COUNT + 1):
        row = admin.execute(
            "INSERT INTO app.sources (name, site_url, feed_url, language, license)"
            " VALUES (%s, %s, %s, 'fr', 'e2e only')"
            " ON CONFLICT (feed_url) DO UPDATE SET name = EXCLUDED.name RETURNING id",
            (f"E2E source {n}", f"https://e2e-{n}.example", f"https://e2e-{n}.example/rss"),
        ).fetchone()
        ids.append(row[0])
    return ids


def _spread_sample_articles(
    admin: psycopg.Connection, sources: list[int], now: datetime, rng: np.random.Generator
) -> list[tuple[str, int]]:
    """Reassign sources and publication times; (article id, topic id) of the sample articles."""
    rows = admin.execute(
        "SELECT a.id, t.topic_id FROM app.articles a"
        " JOIN app.article_topics t ON t.article_id = a.id"
        " WHERE a.id LIKE 'a_dev%' ORDER BY a.id"
    ).fetchall()
    hours = rng.uniform(1, MAX_AGE_HOURS, size=len(rows))
    with admin.cursor() as cur:
        cur.executemany(
            "UPDATE app.articles SET source_id = %s, published_at = %s WHERE id = %s",
            [
                (sources[k % len(sources)], now - timedelta(hours=float(hours[k])), article_id)
                for k, (article_id, _) in enumerate(rows)
            ],
        )
    return [(article_id, topic_id) for article_id, topic_id in rows]


def _synthetic_embeddings(
    articles: list[tuple[str, int]], rng: np.random.Generator
) -> dict[str, np.ndarray]:
    """Per topic: centre, SUBTOPICS subtopic centres, article = subtopic centre + noise."""
    embeddings: dict[str, np.ndarray] = {}
    for topic in sorted({t for _, t in articles}):
        centre = _noise(rng, DIM)
        subs = [_unit(centre + SUBTOPIC_NOISE * _noise(rng, DIM)) for _ in range(SUBTOPICS)]
        members = [a for a, t in articles if t == topic]
        for k, article_id in enumerate(members):
            vector = _unit(subs[k % SUBTOPICS] + ARTICLE_NOISE * _noise(rng, DIM))
            embeddings[article_id] = vector.astype(np.float32)
    return embeddings


def _insert_article(
    admin: psycopg.Connection, article_id: str, source: int, topic: int, title: str,
    summary: str, published_at: datetime,
) -> None:
    admin.execute(
        "INSERT INTO app.articles (id, source_id, url, title, summary, language, word_count,"
        " published_at) VALUES (%s, %s, %s, %s, %s, 'fr', %s, %s) ON CONFLICT (id) DO NOTHING",
        (article_id, source, f"https://e2e.example/{article_id}", title, summary,
         max(len(f"{title} {summary}".split()), 1), published_at),
    )
    admin.execute(
        "INSERT INTO app.article_topics (article_id, topic_id, confidence)"
        " VALUES (%s, %s, 0.9) ON CONFLICT DO NOTHING",
        (article_id, topic),
    )


def seed(admin_dsn: str, reco_dsn: str, now: datetime) -> None:
    rng = np.random.default_rng(SEED)
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        _create_reco_role(admin, reco_dsn)
        # The reco's tables are created by the reco's role, which therefore owns them.
        store = PgStore(reco_dsn)
        try:
            store.ensure_schema()
        finally:
            store.close()
        register_vector(admin)

        sources = _create_sources(admin)
        articles = _spread_sample_articles(admin, sources, now, rng)
        embeddings = _synthetic_embeddings(articles, rng)

        topic_of = dict(articles)
        title = admin.execute("SELECT title FROM app.articles WHERE id = %s", (DUP_OF,)).fetchone()[0]
        _insert_article(admin, DUP_ID, sources[1], topic_of[DUP_OF], f"Reprise : {title}",
                        f"Le même sujet que {DUP_OF}, repris par une autre rédaction.",
                        now - timedelta(minutes=30))
        embeddings[DUP_ID] = _unit(embeddings[DUP_OF] + DUP_NOISE * _noise(rng, DIM)).astype(
            np.float32)

        with admin.cursor() as cur:
            cur.executemany(
                "INSERT INTO reco.article_embeddings (article_id, embedding, model, embedded_at)"
                " VALUES (%s, %s, %s, %s) ON CONFLICT (article_id) DO UPDATE"
                " SET embedding = EXCLUDED.embedding, model = EXCLUDED.model,"
                " embedded_at = EXCLUDED.embedded_at",
                [(a, v, EMBEDDING_MODEL, now) for a, v in embeddings.items()],
            )

        topic_ids = dict(admin.execute("SELECT slug, id FROM app.topics").fetchall())
        for k, (article_id, slug, title, summary) in enumerate(
            article for pair in EXTRA_PAIRS for article in pair
        ):
            _insert_article(admin, article_id, sources[k % len(sources)], topic_ids[slug], title,
                            summary, now - timedelta(hours=2 + k))


def main() -> None:
    seed(os.environ["E2E_ADMIN_DSN"], os.environ["E2E_RECO_DSN"], datetime.now(UTC))
    print("e2e seed written")


if __name__ == "__main__":
    main()
