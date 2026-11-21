# Osmund Energi for Home Assistant

Custom integration (Home Assistant Marketplace / HACS) for the [Osmund Energi API](https://osmundenergi.dk/docs/osmund-api/openapi.yaml).

> [!NOTE]
> The Osmund Energi API is currently in beta and requires **Osmund Plus** on the location. Endpoints and schemas may change without notice.

## Keeping your API key out of this repository

Your personal Osmund Energi API key (starts with `osm_`) must **never** be committed to git:

- **In Home Assistant:** You enter your API key in the Home Assistant UI when adding the integration (**Settings → Devices & services → Add integration → Osmund Energi**). Home Assistant stores it locally in `.storage/core.config_entries` — outside this repository.
- **In local development:** Copy [`.env.example`](.env.example) to `.env` (which is gitignored) or export `OSMUND_API_KEY` in your shell:
  ```sh
  cp .env.example .env
  # Edit .env and replace the placeholder with your key from https://osmundenergi.dk/min-side#api
  ```
- **Automatic guardrails:**
  - `.gitignore` excludes `.env`, `.env.*` (except `.env.example`), `secrets.yaml`, and `config/`.
  - Enable the repository git hook to block accidental commits of `.env` files or `osm_...` keys:
    ```sh
    git config core.hooksPath .githooks
    ```
  - GitHub Actions CI also runs `python3 scripts/check_secrets.py` on every push and pull request.

## Local Development

Install dependencies with [`uv`](https://docs.astral.sh/uv/):

```sh
uv sync
```

Run the unit tests and linter (no API key needed):

```sh
uv run ruff check .
uv run pytest
```

Verify your own API key against the live Osmund Energi API (reads `OSMUND_API_KEY` from `.env` or the environment without ever printing it):

```sh
uv run python scripts/smoke_test.py
```

## Installation (Home Assistant Marketplace / HACS)

Once entities are implemented, you can add this repository as a custom integration repository in the Home Assistant Marketplace (or HACS on Home Assistant < 2026.11):

1. Open **Marketplace** (or **HACS**) in Home Assistant.
2. Select **Add from link** (or **Custom repositories**) and enter `alrekr/OsmundEnergyHomeAssistantPlugin` with category **Integration**.
3. Download **Osmund Energi** and restart Home Assistant if prompted.
4. Go to **Settings → Devices & services → Add integration**, search for **Osmund Energi**, and enter your API key.
