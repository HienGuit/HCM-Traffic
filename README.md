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

Use Python 3.11 and a dedicated virtual environment:

```powershell
py -3.11 -m venv ..\hcm_traffic_env
& ..\hcm_traffic_env\Scripts\python.exe -m pip install -r requirements.txt
& ..\hcm_traffic_env\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
New-Item -ItemType Directory -Force config/local
Copy-Item config/study.example.yaml config/local/study.yaml
Copy-Item config/budget.example.yaml config/local/budget.yaml
```

Edit the local study configuration with the input paths, selected registry, capture plan and schedule. Relative configuration paths resolve from the YAML file; relative PBF paths resolve from `data_root`.

Set the TomTom key in the current PowerShell process:

```powershell
$secret = Read-Host "TomTom API key" -AsSecureString
$env:TOMTOM_API_KEY = [System.Net.NetworkCredential]::new("", $secret).Password
```

The default application budget is **200 traffic attempts and 200 weather attempts per day**, accounted in `Asia/Ho_Chi_Minh`. Each resource permits three total attempts, with bounded retries. Provider quotas, rate limits and account restrictions still apply.

## Usage

Replace `YYYY-MM-DD` with the study date. One command runs scheduled collection, offline processing and interim publication:

```powershell
& ..\hcm_traffic_env\Scripts\python.exe main.py run --date YYYY-MM-DD --allow-online
```

Use another configuration when needed:

```powershell
& ..\hcm_traffic_env\Scripts\python.exe main.py run --date YYYY-MM-DD --config config/local/study.yaml --allow-online
```

Recover processing from finalized raw snapshots without HTTP requests:

```powershell
& ..\hcm_traffic_env\Scripts\python.exe main.py transform --date YYYY-MM-DD
```

Daily usage is derived from the persistent attempt ledger. Restarting does not reset usage or repeat completed capture slots. Run `main.py --help` for the CLI, or use `scripts/operate.ps1` as a PowerShell helper.

## Project Structure and Outputs

- `src/`: collection, reference data, matching, processing and storage modules.
- `main.py`: the command-line entrypoint.
- `config/`: shared policies and configuration examples; `config/local/` holds local settings.
- `scripts/`: development and operational helpers.
- `data/external/` and `data/reference/`: configured source inputs and versioned reference artifacts.
- `data/raw/`: original responses and capture metadata.
- `data/interim/observations.csv`: the main 15-column analysis table, including `day_of_week`.
- `data/interim/observations_full.csv`: the full table with matching observation keys.
- `data/interim/audit/` and `manifest.json`: diagnostics and provenance.
- `logs/`: operational progress and error logs.

The tables accumulate study dates and replace reprocessed observations by their natural keys. Local data, credentials, logs, environments and state are excluded from Git.

## Attribution

Traffic, road-network and weather data come from TomTom, OpenStreetMap contributors and Open-Meteo. See [NOTICE](NOTICE) for source acknowledgements and inherited mapping attribution.
