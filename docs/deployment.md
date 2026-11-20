# Deployment

How the recommender is packaged, released and run in production. The settings themselves are in [Configuration](configuration.md).

## Container image

The [`Dockerfile`](../Dockerfile) builds one image for both processes; the command chooses which: `api` (the default) or `worker`.

```bash
docker build -t swipread-reco .                        # add --platform linux/amd64 for the server
docker run --rm -p 8081:8081 -e RECO_DATABASE_URL=… -e RECO_REDIS_URL=… swipread-reco
docker run --rm -e RECO_DATABASE_URL=… -e RECO_REDIS_URL=… swipread-reco worker
```

| Layer | Content |
| --- | --- |
| Base | `python:3.12-slim` (Debian trixie), uv copied from its official image, both pinned |
| Dependencies | `uv sync --locked --no-dev` in `/opt/venv`. On Linux, `pyproject.toml` takes torch from PyTorch's CPU index (`torch==2.15.0+cpu`): the PyPI wheels would add about 3 GB of CUDA libraries |
| Model | `intfloat/multilingual-e5-small` at the revision the encoder loads (`HF_REVISION` in `worker/encode.py`), only the files listed in `HF_FILES`, in `/opt/hf` |
| Runtime | Non-root user `swipread`; `HF_HUB_OFFLINE=1`, so a container never contacts Hugging Face; `RECO_HOST=0.0.0.0`, `RECO_PORT=8081` |

The image weighs about 2 GB (torch about 800 MB, the model 470 MB). The tests do not run in it: CI runs them before an image is built. Both processes log `swipread-reco <version>, <api|worker>` first, the version coming from `pyproject.toml`.

On SIGTERM (`docker stop`) both processes exit at once: uvicorn for the api, a handler for the worker, which would otherwise be killed after the grace period. Work cut short is safe: an article being encoded is taken again at the next start, and a stream message not yet acknowledged is read again.

## Production

The recommender runs on the Swipread server with Docker Compose, next to the API, PostgreSQL and Redis, on the internal network only (its routes are not authenticated). The configuration lives in the organisation's private `infra` repository:

| Service | Command | Memory limit | Notes |
| --- | --- | --- | --- |
| `reco` | `api` | 256 MB | Answers `http://reco:8081`, the API's `SWIPREAD_RECO_URL`. About 75 MB with 200 articles |
| `reco-worker` | `worker` | 1.25 GB | About 670 to 770 MB once the model is loaded, plus the model file in page cache. `RECO_STREAM_CONSUMER=worker`, `OMP_NUM_THREADS=1` |

Both get `RECO_DATABASE_URL` (login role `reco_service`), `RECO_REDIS_URL` (the API's Redis) and `RECO_DB_TIMEOUT_S=0.25`, under the API's 300 ms timeout.

- **One torch thread.** The server has 2 vCPUs. With one thread, an encoding pass (about 9.5 seconds per 64 articles on the server) leaves the other core to the API and to `reco`, which must answer within 300 ms.
- **Database role.** The deployment creates `reco_service`, member of `swipread_reco`, or sets its password from the server's secrets, before every start: the API must have run its migrations first.
- **First start.** Both processes create the `reco` tables if they are missing, one at a time (an advisory lock). The worker then encodes the whole catalogue (about 50 seconds for 200 articles), writes the fallback lists and creates the consumer group `reco` on the `events` stream.
- **When it is down**, the API serves its fallback feed: a deployment or a crash of the recommender never breaks the reader's feed.
- **Success of each reason** (algorithm 11.4), from the deployment directory: `docker compose run --rm --no-deps reco reasons`. A throwaway container of the same image and settings, so the running `reco` keeps its memory.
- **Order with the API.** The recommender reads `app.articles.word_count_from_summary`, added by the API's migration V10 (API 1.2.0, in production since 2026-11-20): deploy that API version first.

## Releasing

A release is a version tag on a commit of `main` whose `pyproject.toml` has that version. It deploys to production:

```bash
# in a commit on main: version = "1.2.0" in pyproject.toml, then uv lock
git switch main && git pull
git tag -a v1.2.0 -m "v1.2.0"
git push origin v1.2.0
```

Only admins can create, move or delete `v*` tags (a tag ruleset). A tag with a suffix, such as `v1.2.0-rc.1`, makes a pre-release, deployed the same way. The recommender, the API and the web app are versioned independently; the interface between the API and the recommender is versioned by the backend contract (v1.7).

The [`Release`](../.github/workflows/release.yml) workflow then runs:

| Job | Does |
| --- | --- |
| Check the tag | The tag is a version (`v1.2.0`), equal to the version of `pyproject.toml`, on a commit of `main` |
| Checks | The [CI](testing.md#continuous-integration) workflow, reused as is |
| Build and publish the image | Builds the [container image](#container-image) for `linux/amd64` and pushes `ghcr.io/swipread/reco:<version>` (public, like the code); about 4 minutes |
| Deploy to production | Connects to the server over SSH. The key can only ask for `deploy reco <version>`: the server prepares the database role, pulls the image, starts `reco` and `reco-worker` and waits for `/healthz` and for a worker running 20 seconds without a restart; if they are not healthy within 2 minutes, it restores the previous version, and on a first deployment it stops them. About 2.5 minutes, most of it pulling the image |
| Create the GitHub release | With notes generated by GitHub, only once the deployment is healthy |

One release runs at a time. When a job fails, there is no GitHub release and the previous version keeps running (or, before any version, the API's fallback feed): fix the cause, then *Re-run failed jobs* or push a new tag with a new version. Rolling back to an earlier version is done on the server (see the `infra` repository).

### GitHub settings

| Where | Name | Value |
| --- | --- | --- |
| Repository variable | `APP_ORIGIN` | Public origin of the app, the environment's link |
| Environment `production` (deployments from `v*` tags only), variable | `DEPLOY_HOST` | The server's hostname |
| Environment `production`, secret | `DEPLOY_SSH_KEY` | Private half of the recommender's release key pair, restricted on the server to `deploy reco <version>`; it is not the key of the API and the web app |
| Environment `production`, secret | `DEPLOY_KNOWN_HOSTS` | The server's host key, checked against the fingerprint read on the server |

Because the environment only accepts `v*` tags, a workflow started from a branch or a pull request cannot read the deploy key.

### Learnt from the first release

- **1.0.0 never ran.** The api and the worker start together; on a database without the `reco` tables, both ran `CREATE TABLE IF NOT EXISTS` at once, PostgreSQL refused the second (`duplicate key value violates unique constraint "pg_type_typname_nsp_index"`) and the worker exited. The deployment stopped both and the API kept its fallback feed. 1.0.1 creates the tables under an advisory lock, with a test that runs six of them at once. Locally the race had been missed: the first local deployment used a deliberately broken worker, so the api alone created the tables.
- **The model download can hang.** Hugging Face's default transfer (Xet) failed during the image build: the build downloads over plain HTTP (`HF_HUB_DISABLE_XET=1`).
- **A small catalogue makes short batches.** Below 50 articles `MU = 0` and the embeddings are all close (cosine above 0.8), so most articles pass for duplicates; and once a reader has seen the few articles of their topics, batches fill with other topics (`explore`, `similar`). Both fade as sources are added.
- **Restoring a dump keeps the owners.** Restored with `pg_restore --no-owner`, the `reco` tables belong to the restoring role without any right for `reco_service`, and the recommender refuses to start (see the [FAQ](faq.md#running-locally)).

## Release checklist

1. CI is green on the commit to tag, and `pyproject.toml` has the new version (`uv lock` run).
2. An algorithm change is reported in [the algorithm](algorithm.md); a change to the interface with the API is agreed in the backend contract first.
3. A new `EMBEDDING_MODEL` or `HF_REVISION` re-encodes every article: check the time it takes against the catalogue size.
4. Tag, push the tag and follow the `Release` workflow until the GitHub release exists.
5. After deployment: `/healthz` gives the expected catalogue size, the worker log shows `encoded …` and `fallback lists written`, the consumer group `reco` has no lag, a reader's feed carries a `requestId` other than `fallback` from its second page, and the API's log has no `Recommendation service unavailable`.
