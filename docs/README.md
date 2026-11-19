# Swipread recommender documentation

Developer documentation for the recommendation service. New here? Read [Getting started](getting-started.md), then [Architecture](architecture.md) and the [algorithm](algorithm.md).

## Guides

| Page | Read it to… |
| --- | --- |
| [Getting started](getting-started.md) | Install, run the tests, run the api and the worker next to the real API |
| [Architecture](architecture.md) | Understand the two processes, the modules, the request path, errors and timeouts |
| [Flows](flows.md) | Follow the main sequences (a batch, a strong signal, a new article, a deleted account) |
| [Database](database.md) | See what the recommender reads in `app`, its own tables in `reco` and its rights |
| [Configuration](configuration.md) | Environment variables, parameters of the algorithm, production checklist |
| [Testing](testing.md) | Unit, integration, performance and end-to-end tests, continuous integration, the report |
| [Decisions](decisions.md) | Architecture decision records, gaps with the backend, what real traffic would change, open questions |
| [FAQ](faq.md) | Fix common problems and understand a batch |
| [Glossary](glossary.md) | Look up a term |

## Reference

| Page | Content |
| --- | --- |
| [Algorithm](algorithm.md) | The algorithm, its parameters and its acceptance tests; the reference for any change |
| [API reference of Swipread/api](https://github.com/Swipread/api/blob/main/docs/api.md) | The request and response formats of `POST /v1/recommendations`, owned by the API |
