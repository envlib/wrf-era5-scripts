#!/bin/bash -e
# This should be run in the data path

uv run main.py

n_cores=16
# wrf_exe_path=$(toml get --toml-path parameters.toml executables.wrf_path)/main/wrf.exe

cd run

# echo $n_cores $wrf_exe_path
mpirun -np $n_cores ./wrf.exe

uv run upload_wrfout.py
