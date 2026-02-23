#!/bin/bash -e
# This should be run in the data path
#SBATCH --job-name=wrf1
#SBATCH --time=12:00:00
#SBATCH --ntasks=24
#SBATCH --hint=nomultithread

# python main_alt.py

# n_cores=16
# wrf_exe_path=$(toml get --toml-path parameters.toml executables.wrf_path)/main/wrf.exe

# cd run

# module purge 2> /dev/null
# module load netCDF-Fortran/4.6.1-gompi-2024a
# module list

# echo $n_cores $wrf_exe_path
srun --kill-on-bad-exit --output=wrf.log ./wrf.exe

# python upload_wrfout.py
