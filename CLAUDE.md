# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Automated pipeline to run the WRF (Weather Research and Forecasting) model using ERA5 reanalysis data as boundary/initial conditions. Runs inside a Docker container (`mullenkamp/wrf-era5-runs:1.1`) with WRF 4.6.1-ARW and WPS 4.6.0 pre-installed. All configuration is driven by a single `parameters.toml` file (gitignored; see `parameters_example.toml`).

## Commands

```bash
# Run full pipeline (Docker)
docker-compose up

# Run locally (requires [no_docker] section in parameters.toml)
uv run python main.py

# Linting/formatting (line length: 120)
uv run lint:style        # ruff + black --check
uv run lint:fmt          # black + ruff --fix
uv run lint:typing       # mypy

# Tests (no test suite exists yet)
uv run pytest              # pytest tests/
```

## Pipeline Execution Order (main.py)

1. `check_ndown_params()` — Determine if ndown (one-way nesting) mode is active
2. `check_nml_params()` — Validate namelist files and resolve domain list
3. `set_nml_params(domains_init)` — First pass: configure namelists for initial domain set
4. `run_geogrid()` — Execute `geogrid.exe` (static geography); returns domain bounding box
5. `set_nml_params(domains_init)` — Second pass: set time/date/history params, generate output file list
6. `upload_namelists()` — Upload namelists to remote storage via rclone
7. `dl_ndown_input()` — (ndown only) Download prior wrfout files
8. `dl_era5()` — Download ERA5 NetCDF files via rclone
9. `run_era5_to_int()` — Convert ERA5 NetCDF to WPS intermediate format
10. `run_metgrid()` — Execute `metgrid.exe`
11. `run_real()` — Execute `real.exe` via `mpirun -n 4`
12. `run_ndown()` — (ndown only) Execute `ndown.exe` via `mpirun -n 4`
13. `monitor_wrf()` — Launch `wrf.exe` via `mpirun`, poll for output, upload files in real-time

`main_alt.py` runs steps 1–11 only (preprocessing, no WRF execution).

## Key Architecture

- **`params.py`** — Central config loader. Reads `parameters.toml`, detects Docker vs local mode (`[no_docker]` section), supports env var overrides (`start_date`, `end_date`, `domains`, `n_cores`, `duration_hours`). All other scripts import `params` for paths and settings.
- **`set_params.py`** — Namelist management. Reads/writes Fortran namelists (`namelist.wps`, `namelist.input`) using `f90nml`. Handles domain subsetting/renumbering, time parameter injection, output stream configuration, and computes `time_step = dx * 0.001 * 6`.
- **`utils.py`** — Shared utilities: rclone config creation, output file querying/renaming/uploading, variable filtering via `ncks`, domain projection recalculation (`pyproj`).
- **`monitor_wrf.py`** — Runs `wrf.exe` and polls every 60s for completed output files, uploads them via rclone, and deletes local copies. On failure, uploads `rsl.*` log files.

## Data Flow

- **Namelists**: Source templates at `/data/namelists/` → modified programmatically → written to `/data/`
- **ERA5 data**: Downloaded from S3 → converted to WPS intermediate format → consumed by metgrid → deleted
- **WRF output**: `wrfout` (history), `wrfxtrm` (daily diagnostics), `wrfzlevels` (height-interpolated) → uploaded to remote during run → deleted locally

## Domain Subsetting

The pipeline can run any subset of domains defined in the namelists (e.g., `domains = [3, 4]`). When a subset doesn't start at domain 1, `utils.recalc_geogrid()` recomputes the map projection center and grid parameters. Domains are renumbered sequentially (e.g., domain 3 becomes d01 internally, renamed back on output).

## ndown Mode

One-way nesting from a prior WRF run. Activated by the `[ndown]` section in `parameters.toml`. Requires a single non-domain-1 domain (e.g., `domains = [3]`). Downloads prior wrfout files for the parent domain, runs real+ndown, then runs WRF on the child domain only.

## Key Dependencies

- **Python**: `f90nml` (Fortran namelists), `pendulum` (dates), `era5_to_int` (ERA5→WPS conversion CLI), `pyproj` (projections), `h5netcdf` (NetCDF reading), `sentry-sdk` (error tracking)
- **System**: `mpirun` (OpenMPI), `rclone` (data transfer), `ncks` (NetCDF variable filtering), `uv` (package management)

## Style

- Python >=3.11, line length 120, black formatting with `skip-string-normalization`
- All remote data transfer uses `rclone` with dynamically created config files (see `utils.create_rclone_config()`)
- `parameters.toml` contains credentials — never commit it (only `parameters_example.toml` is tracked)
