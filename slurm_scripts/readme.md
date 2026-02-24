# Slurm Scripts for WRF-ERA5 Pipeline

These scripts run the WRF-ERA5 pipeline inside an Apptainer container on HPC clusters managed by Slurm.

## Prerequisites

### 1. SIF Image

The pipeline runs inside an Apptainer (SIF) image converted from the Docker image `mullenkamp/wrf-era5-runs:2.0`.

**Option A: Download the pre-built image (recommended)**

```bash
wget -N https://b2.envlib.xyz/file/envlib/sif/wrf-era5-runs_2.0.sif
```

**Option B: Build from Docker Hub**

If your HPC allows it, you can convert directly from Docker Hub:

```bash
module load Apptainer
apptainer pull docker://mullenkamp/wrf-era5-runs:2.0
```

Note: This may fail on some HPC systems due to memory or permission constraints during the squashfs build. If so, build locally and transfer:

```bash
# On your local machine
apptainer pull docker://mullenkamp/wrf-era5-runs:2.0

# Copy to HPC
scp wrf-era5-runs_2.0.sif user@hpc:/path/to/scratch/
```

If you only have Docker locally (no Apptainer):

```bash
docker pull mullenkamp/wrf-era5-runs:2.0
docker save mullenkamp/wrf-era5-runs:2.0 -o wrf-era5-runs_2.0.tar
apptainer build wrf-era5-runs_2.0.sif docker-archive://wrf-era5-runs_2.0.tar
```

### 2. WPS_GEOG Static Data

**Option A: NZ-specific dataset (recommended for New Zealand domains)**

A curated WPS_GEOG dataset for New Zealand simulations:

```bash
wget -N https://b2.envlib.xyz/file/envlib/wrf/static_data/nz_wps_geog.tar.zst -O nz_wps_geog.tar.zst
tar --zstd -xf nz_wps_geog.tar.zst
rm nz_wps_geog.tar.zst
```

**Option B: Full WPS_GEOG from NCAR**

The complete global dataset from NCAR:
https://www2.mmm.ucar.edu/wrf/users/download/get_sources_wps_geog.html

### 3. parameters.toml

Copy `parameters_example.toml` to `parameters.toml` and configure it with your simulation settings and remote storage credentials. Do **not** include a `[no_docker]` section -- the container uses its built-in Docker-mode paths (`/data`, `/WPS_GEOG`, `/WRF`, `/WPS`).

### 4. Network Access

The pipeline uses `rclone` to download ERA5 data and upload WRF output. Compute nodes may have restricted network access depending on your cluster. Check with your HPC support if rclone transfers fail.

## Scripts

### run_wrf_nesi.sl -- Single Run

Runs one WRF simulation. Edit the configuration section at the top of the script to set paths for your environment (`PROJECT_CODE`, `SCRATCH`, `SIF_PATH`, `WPS_GEOG_PATH`).

**Submit:**

```bash
sbatch slurm_scripts/run_wrf_nesi.sl
```

**Override parameters via environment variables:**

```bash
sbatch --export=ALL,START_DATE="2020-01-01 00:00:00",DURATION_HOURS=48 slurm_scripts/run_wrf_nesi.sl
```

Available overrides: `START_DATE`, `END_DATE`, `DOMAINS`, `DURATION_HOURS`.

### run_wrf_nesi_array.sl -- Multiple Runs (Job Array)

Submits multiple WRF runs as a Slurm job array, each with a different start date. The start date for each task is computed from a base date and step size:

```
start_date = BASE_DATE + (SLURM_ARRAY_TASK_ID * STEP_HOURS)
```

Edit `BASE_DATE` and `STEP_HOURS` in the script, then set the array range to match the number of runs you need. Duration is controlled globally via `duration_hours` in `parameters.toml`.

**Example:** 12 runs, 48 hours apart, starting 2020-01-01:

```bash
# BASE_DATE="2020-01-01 00:00:00", STEP_HOURS=48 (set in script)
sbatch --array=0-11 slurm_scripts/run_wrf_nesi_array.sl
```

This produces:
| Task ID | Start Date |
|---------|------------|
| 0 | 2020-01-01 00:00:00 |
| 1 | 2020-01-03 00:00:00 |
| 2 | 2020-01-05 00:00:00 |
| ... | ... |
| 11 | 2020-01-23 00:00:00 |

### run_wrf_uc.sl -- University of Canterbury HPC

A variant configured for a different HPC environment. Same structure as `run_wrf_nesi.sl` with different default paths and resource allocations.

## How It Works

The scripts use Apptainer bind mounts to inject host files into the container, replicating the same setup as `docker-compose.yml`:

| Host Path | Container Path | Purpose |
|-----------|---------------|---------|
| `parameters.toml` | `/app/parameters.toml` | Pipeline configuration |
| WPS_GEOG directory | `/WPS_GEOG` (read-only) | Static geography data |
| Per-job scratch directory | `/data` | Working directory for namelists, metgrid, WRF output |

WRF, WPS, Python, and all dependencies are baked into the SIF image -- no bind mounts needed for those.

Key flags:
- **`--writable-tmpfs`** -- The pipeline creates a symlink inside `/WPS/geogrid/` for Noah-MP. Without this, the read-only SIF filesystem would block it.
- **`HYDRA_LAUNCHER=fork`** -- MPICH inside the container detects Slurm environment variables and tries to use `srun`, which doesn't exist in the container. This forces MPICH's Hydra process manager to use `fork` instead.
- **`n_cores=$SLURM_NTASKS`** -- Syncs the pipeline's MPI process count with the Slurm allocation.

Each job gets an isolated data directory (using `$SLURM_JOB_ID` or `$SLURM_ARRAY_JOB_ID_$SLURM_ARRAY_TASK_ID`), so multiple runs don't interfere with each other.

## Monitoring Jobs

```bash
squeue -u $USER          # List your running/queued jobs
scancel <job_id>          # Cancel a job
scancel <array_job_id>    # Cancel all tasks in an array
```

Log files are written to the submission directory:
- Single run: `wrf-era5_<job_id>.log`
- Array: `wrf-era5_<array_job_id>_<task_id>.log`
