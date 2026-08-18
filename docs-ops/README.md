# Operator documentation

These are the technical docs: architecture, deployment, configuration, and the
dashboard API. They describe the backend and the individual services.

**They are deliberately not part of the published docs site.** That site
(`docs/`, served at port 9121) is for people *using* the assistant — chat,
automations, channels, knowledge. Mixing deployment instructions into it was the
problem this split solves.

These files are still accurate and still worth keeping. Read them here, or on
GitHub, where Markdown renders fine without a site around it.

| File | Covers |
|---|---|
| `quickstart.md` | Clone to running stack |
| `architecture/overview.md` | The eight services and why the networking looks like that |
| `architecture/profiles.md` | The three agent profiles, and GSD Core |
| `architecture/wiki-memory.md` | Markdown + FTS5 store, write paths, sharp edges |
| `architecture/workflows.md` | ADK agents and the eval suite that gates them |
| `deploy/prerequisites.md` | Host requirements, and why Ollama is not containerised |
| `deploy/install.md` | Installing on a new host, backups |
| `deploy/upgrade.md` | `hermes-update`, migrations, snapshots, and why not the installer |
| `deploy/configuration.md` | Environment variables and **network exposure** |
| `deploy/cloud.md` | Running on a cloud VM: sizing, Terraform, why not managed containers |
| `reference/dashboard-api.md` | The FastAPI surface behind the console |

`_old-index.md` is the previous site landing page, kept only so nothing in these
pages links into a gap.

## If you want these published too

They are ordinary MkDocs Markdown and were building cleanly before the move. A
second `mkdocs.yml` pointed at `docs_dir: docs-ops` would build them as their own
site on another port, without putting deployment instructions in front of
end users.

## Note on accuracy

Two things in here were corrected against `docker-compose.yml` rather than
inherited from the old README, and both still matter:

- The operator console binds `0.0.0.0:9120` with **no authentication**.
- `API_SERVER_KEY` is absent from `.env.example` and defaults to a placeholder
  string published in this repository, on an API server bound to `0.0.0.0`.

See `deploy/configuration.md#network-exposure`.
