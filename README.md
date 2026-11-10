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
   py -3.11 -m venv ..\hcm_traffic_env
   ```

2. Install dependencies and the package:

   ```powershell
   & ..\hcm_traffic_env\Scripts\python.exe -m pip install -r requirements.txt
   & ..\hcm_traffic_env\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
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
   - Candidates: `candidates.geojson`, `paths.json`, `candidate_assessment.csv`, `shortlist.json`, `static.json`, `manifest.json` under `data/reference/candidates/<candidate_version>/`.
   - Source evidence and referenced artifacts, only if configured.

5. Edit `study.yaml`, `budget.yaml` and `runtime.json` in `config/local/`:

   - Set input/output paths, candidate version, schedule and budget.
   - Set `osm_pbf` relative to `data_root`, for example `external/<filename>.osm.pbf`.
   - Set other relative paths from the declaring config file's directory.
   - Set runtime `python` to the environment from step 1, `config` to `study.yaml`, and `plan` to the configured capture plan.
   - Set `capture_plan_file` to `../../data/reference/candidates/<candidate_version>/capture_plan_local.json`.

6. Build the graph from the configured PBF offline; valid cached artifacts are reused:

   ```powershell
   $python = "..\hcm_traffic_env\Scripts\python.exe"
   $result = & $python -B main.py --config config/local/study.yaml reference setup
   if ($LASTEXITCODE -ne 0) { throw "Reference setup failed" }
   $reference = $result | ConvertFrom-Json
   $graph = $reference.graph
   $reference
   ```

   Set `graph_cache` in `study.yaml` to `../../data/reference/graph/<graph_version>` using the returned `graph_version`. Use candidate paths matching this graph version; if they differ, stop and prepare compatible candidates. Setup does not rewrite candidates or config.

7. Generate the local capture plan offline after finishing configuration:

   ```powershell
   $version = "<candidate_version>"
   $result = & $python -B main.py --config config/local/study.yaml plan --graph $graph --paths "data/reference/candidates/$version/paths.json"
   if ($LASTEXITCODE -ne 0) { throw "Plan generation failed" }
   $plan = $result | ConvertFrom-Json
   Copy-Item -LiteralPath $plan.plan -Destination "data/reference/candidates/$version/capture_plan_local.json"
   ```

   Repeat this step after changing the study or budget configuration.

8. Run offline checks; set the key in this PowerShell process to avoid `KEY_MISSING` in dry-run:

   ```powershell
   & ..\hcm_traffic_env\Scripts\python.exe -B main.py --config config/local/study.yaml doctor
   $secret = Read-Host "TomTom API key" -AsSecureString
   $env:TOMTOM_API_KEY = [System.Net.NetworkCredential]::new("", $secret).Password
   .\scripts\operate.ps1 -Action dry-run -Date YYYY-MM-DD
   ```

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
- `data/raw/`: original responses and capture metadata.
- `data/interim/observations.csv`: the main 15-column analysis table.
- `data/interim/observations_full.csv`: the full table with matching observation keys.
- `data/interim/audit/` and `manifest.json`: diagnostics and provenance.
- `logs/`: operational progress and error logs.

The tables accumulate study dates and replace reprocessed observations by their natural keys. Local data, credentials, logs, environments and state are excluded from Git.

## Attribution

Traffic, road-network and weather data come from TomTom, OpenStreetMap contributors and Open-Meteo. See [NOTICE](NOTICE) for source acknowledgements and inherited mapping attribution.
