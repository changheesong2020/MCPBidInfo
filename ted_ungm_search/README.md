# TED × UNGM Search Toolkit

This project provides a reusable Python 3.10 toolkit for querying the [TED (Tenders Electronic Daily) Search API v3](https://ted.europa.eu/TED/main/HomePage.do) and synchronising open helper code lists from [UNGM](https://www.ungm.org/).  It offers:

* A resilient HTTP client with retry, timeout and logging support.
* Reusable Pydantic models to describe search configuration and results.
* Utilities to cache UNGM helper catalogues locally for offline use.
* A command line interface for both paged and scroll (iteration token) harvesting.
* Unit and smoke tests that keep the integration touch points verifiable.

## Installation

The project uses a standard `pyproject.toml`.  Create a virtual environment and install the dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ./ted_ungm_search
```

For development and tests add the optional extras:

```bash
pip install -e ./ted_ungm_search[dev]
```

> **Note**: Offline environments ship with lightweight fallbacks for `pydantic` and `tenacity`.
> Install the official dependencies via `pip` when preparing a production deployment.


## Command Line Usage

The CLI exposes three sub-commands.

### 1. TED Search (paged)

```bash
python ted_ungm_search/cli.py ted \
  --date-from 2025-06-18 --date-to 2025-09-16 \
  --countries DE FR \
  --cpv 33* \
  --keywords PCR reagent diagnostic \
  --fields publication-number title buyer-name publication-date classification-cpv place-of-performance.country \
  --mode page \
  --page 1 \
  --limit 50 \
  --out results_page.json \
  --pretty
```

The command emits a JSON document with metadata and notice hits.  Use `--pretty` to format the output and `--out` to write to a file.

### 2. TED Search (iteration token scroll)

```bash
python ted_ungm_search/cli.py ted \
  --date-from 2025-06-18 --date-to 2025-09-16 \
  --countries DE FR \
  --cpv 33* \
  --keywords PCR reagent diagnostic \
  --mode iteration \
  --limit 250 \
  --out results_stream.jsonl
```

Iteration mode streams notices as JSON Lines (one notice per line) and keeps requesting the TED API until the iteration token indicates completion.  The command is optimised for daily harvesting jobs where append-only JSONL files are preferred.

### 3. UNGM helper synchronisation

```bash
python ted_ungm_search/cli.py ungm-sync --dataset country
python ted_ungm_search/cli.py ungm-sync --dataset unspsc
```

The helper caches are stored under `~/.cache/ted-ungm-search/` by default.

To build quick UNGM deep links:

```bash
python ted_ungm_search/cli.py build-ungm-url --countries DE --unspsc 33 --keywords "PCR reagent"
```

## Query building recipes

The search query combines several facets.  Each component is optional except the publication window.

* **Period filter** – `publication-date:[DATE_FROM TO DATE_TO]`
* **Territory filter** – countries are included as `(place-of-performance.country:XX OR buyer-country:XX)` clauses joined with `OR`.
* **Subject filters** – CPV prefixes map to `classification-cpv:PREFIX`, while keywords are injected into `title:(...)` clauses.  Both rely on `OR` logic.
* **Form type filter** – `form-type:(...)` is appended when present.

Leverage the filters together to narrow the stream: for example a daily batch could focus on `(PCR OR reagent)` keywords, CPV segment `33*` and buyers in Germany and France.

## Operational tips

* **Keep the field list short.**  The default field projection includes publication number, title, buyer, publication date, CPV and place of performance country.  Expanding the list increases payload size.
* **Sort by the latest publication date.**  The client defaults to a `publication-date` descending sort to ease incremental harvesting.
* **Retrieval robustness.**  Each request has a 20 second timeout and retries up to five times with exponential back-off for `429` and `5xx` responses.
* **Iteration harvesting.**  The CLI emits structured logging for each iteration token making it easy to resume or monitor batch runs.

## Tests

Run the unit tests with:

```bash
pytest ted_ungm_search/tests/test_query_building.py
```

The integration smoke test calls the live TED API and is skipped by default.  Enable it explicitly once network access is available:

```bash
pytest ted_ungm_search/tests/test_integration_smoke.py --runlive
```

The smoke test only validates status codes and the presence of mandatory fields to stay lightweight.

## JSONL loading example

Use `jq` or Python to ingest the streamed JSON lines:

```bash
python - <<'PY'
import json
from pathlib import Path

with Path('results_stream.jsonl').open() as fh:
    for line in fh:
        record = json.loads(line)
        print(record.get('publication-number'), record.get('title'))
PY
```

