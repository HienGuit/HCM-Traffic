# HCM-Traffic

## Project Overview

HCM-Traffic is a Python baseline for collecting and processing urban traffic congestion data for exploratory analysis and dashboards. It produces reproducible observations for configured, directed road segments.

## Data Sources

- **TomTom Orbis v2 Vector Flow Tiles:** live traffic geometry, OpenLR references, relative speed and current speed.
- **OpenStreetMap:** a local road network and static attributes, including road type, lanes, junctions and traffic signals.
- **Open-Meteo:** precipitation and weather codes.

## Methodology

The study configuration selects directed road segments and collection times. The collector saves traffic and weather responses with timestamps and request metadata. Offline processing decodes OpenLR references, associates traffic geometry with directed road edges, and partitions each segment into unique, non-overlapping intervals.

Congestion intensity is the length-weighted mean of `1 - relative_speed`. Current speed uses a harmonic mean over its own valid interval support. Each measure must meet the configured coverage threshold. Weather and static road features are joined to the resulting observations before tabular export.

## Setup

1. Open PowerShell in the repository root and create a Python 3.11 environment:

   ```powershell
   New-Item -ItemType Directory -Force .local/tmp, .local/pip-cache | Out-Null
   $env:TMP = Join-Path $PWD ".local/tmp"
   $env:TEMP = $env:TMP
   $env:TMPDIR = $env:TMP
   $env:PIP_CACHE_DIR = Join-Path $PWD ".local/pip-cache"
   py -3.11 -m venv .venv
   ```

2. Install dependencies and the package:

   ```powershell
   & .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   & .\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
   ```

3. Create local configuration files (first setup only):

   ```powershell
   New-Item -ItemType Directory -Force config/local
   Copy-Item config/study.example.yaml config/local/study.yaml
   Copy-Item config/budget.example.yaml config/local/budget.yaml
   Copy-Item config/runtime.example.json config/local/runtime.json
   ```

4. Add the local inputs:

   - PBF: `data/external/<filename>.osm.pbf`.
   - Candidate bundle: `candidates.geojson`, `paths.json`, `candidate_assessment.csv`, `shortlist.json`, `static.json`, `manifest.json`, `compatibility.json` under `data/reference/candidates/<candidate_version>/`.
   - Source evidence and referenced artifacts, only if configured.

5. Edit `study.yaml`, `budget.yaml` and `runtime.json` in `config/local/`:

   - Set input/output paths, candidate version, schedule and budget.
   - Set `osm_pbf` relative to `data_root`, for example `external/<filename>.osm.pbf`.
   - Set other relative paths from the declaring config file's directory.
   - Set runtime `python` to the environment from step 1, `config` to `study.yaml`, and `plan` to the configured capture plan.
   - Set `capture_plan_file` to `../../data/reference/candidates/<candidate_version>/capture_plan_local.json`.

6. Build the graph from the configured PBF offline; valid cached artifacts are reused:

   ```powershell
   $python = ".\.venv\Scripts\python.exe"
   $result = & $python -B main.py --config config/local/study.yaml reference setup
   if ($LASTEXITCODE -ne 0) { throw "Reference setup failed" }
   $reference = $result | ConvertFrom-Json
   $graph = $reference.graph
   $reference
   ```

   Check `reference_status` and `candidate_compatibility` separately. If the latter is `requires_rebind`, run:

   ```powershell
   $version = "<candidate_version>"
   $result = & $python -B main.py --config config/local/study.yaml reference rebind-paths --graph $graph --candidates "data/reference/candidates/$version" --compatibility "data/reference/candidates/$version/compatibility.json"
   if ($LASTEXITCODE -ne 0) { $result; throw "Candidate rebinding failed; keep the original bundle" }
   $migration = $result | ConvertFrom-Json
   $version = $migration.candidate_version
   ```

   Set `graph_cache` in `study.yaml` to `../../data/reference/graph/<graph_version>` using the returned graph version. Set `selected_paths_file`, `operational_selection_file`, `capture_plan_file` and runtime `plan` to the selected candidate version. A graph/config mismatch is resolved by these path settings; a failed compatibility check must not be bypassed. No config or candidate approval is changed automatically.

7. Generate the local capture plan offline after finishing configuration:

   ```powershell
   $version = "<selected_candidate_version>"
   $result = & $python -B main.py --config config/local/study.yaml plan --graph $graph --paths "data/reference/candidates/$version/paths.json"
   if ($LASTEXITCODE -ne 0) { $result; throw "Plan generation failed" }
   $plan = $result | ConvertFrom-Json
   if (-not $plan.plan) { $result; throw "Plan output is missing its path" }
   Copy-Item -LiteralPath $plan.plan -Destination "data/reference/candidates/$version/capture_plan_local.json"
   ```

   Regenerate the plan after changing capture scope, schedule, source contract or budget. Moving storage directories alone does not invalidate a new portable plan; resolved paths remain recorded separately as provenance. Historical plans retain their original hash rules.

8. Run offline checks; set the key in this PowerShell process to avoid `KEY_MISSING` in dry-run:

   ```powershell
   & .\.venv\Scripts\python.exe -B main.py --config config/local/study.yaml doctor
   $secret = Read-Host "TomTom API key" -AsSecureString
   $env:TOMTOM_API_KEY = [System.Net.NetworkCredential]::new("", $secret).Password
   .\scripts\operate.ps1 -Action dry-run -Date YYYY-MM-DD
   ```

   Before transferring an older candidate bundle without `compatibility.json`, export its companion once on the source machine using the matching validated graph:

   ```powershell
   & $python -B main.py --config config/local/study.yaml reference compatibility-export --graph "<source_graph_folder>" --candidates "data/reference/candidates/<source_candidate_version>" --output ".local/project/compatibility.json"
   ```

   Include that file as `compatibility.json` in the transferred bundle. It binds the source manifest, ordered paths, offsets and complete graph semantics; no source graph copy is required on the new machine. Rebinding fails if the rebuilt network differs.

## Usage

`collect`: collect the specified day's scheduled slots, automatically transform and update CSVs; enter the key when prompted and keep the window open until completion.

```powershell
.\scripts\operate.ps1 -Action collect -Date YYYY-MM-DD
```

`transform`: reprocess existing raw data for recovery without calling APIs.

```powershell
.\scripts\operate.ps1 -Action transform -Date YYYY-MM-DD
```

## Project Structure and Outputs

- `src/`: collection, reference data, matching, processing and storage modules.
- `main.py`: the command-line entrypoint.
- `config/`: shared policies and configuration examples; `config/local/` holds local settings.
- `scripts/`: development and operational helpers.
- `data/external/` and `data/reference/`: configured source inputs and versioned reference artifacts.
- `data/reference/graph/<graph_version>/`: graph generated during setup; excluded from Git with all of `data/`.
- `.venv/`: project Python environment.
- `.local/qa/`: optional QA bundle and results.
- `.local/project/`: reports, decisions and migration evidence.
- `.local/tmp/` and `.local/pip-cache/`: temporary files and installation cache.
- `.local/python/`: optional portable Python installation (used on the current local setup).
- `data/interim/`: transform runs, input inventories, staging, intervals and association diagnostics.
- `.local/cache/`: disposable decode cache.
- `.local/state/`: persistent attempt ledger and scheduler/job state; retain when restarting or upgrading.
- `.local/archive/`: earlier output generations and migration backups.
- `.local/pilots/`: pilot inputs and outputs.
- `.local/dev/`: development runs, build staging and packages.
- `data/raw/`: original responses and capture metadata.
- `data/processed/observations.csv`: the main 15-column analysis table.
- `data/processed/observations_full.csv`: the full table with matching observation keys.
- `data/interim/publication/audit/`: published-table diagnostics, linked by `data/processed/manifest.json`.
- `data/processed/manifest.json`: final table hashes, partitions and provenance.
- `logs/`: operational progress and error logs.

Final tables are published only after transform invariants pass; unavailable measurements retain nulls and reason codes. Historical outputs remain intact; a verified legacy final generation in `interim/` is copied into `processed/` on first publication/read. The tables accumulate study dates and replace reprocessed observations by their natural keys. All default generated folders stay inside this project and are excluded from Git. Python 3.11 must already be available for setup; a portable installation may live in `.local/python/`. Recreate `.venv/` after moving the project. Existing historical backups are not removed. External roots remain supported when explicitly configured.

## Attribution

Traffic, road-network and weather data come from TomTom, OpenStreetMap contributors and Open-Meteo. See [NOTICE](NOTICE) for source acknowledgements and inherited mapping attribution.
