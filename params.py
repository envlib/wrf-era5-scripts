#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Tue Sep 23 15:03:38 2025

@author: mike
"""
import tomllib
import os
import pathlib

############################################
### Read params file

base_path = pathlib.Path(os.path.realpath(os.path.dirname(__file__)))

with open(base_path.joinpath("parameters.toml"), "rb") as f:
    file = tomllib.load(f)

if 'no_docker' in file:
    no_docker = file['no_docker']
    data_path = pathlib.Path(no_docker['data_path'])
    geog_data_path = pathlib.Path(no_docker['geog_data_path'])
    wrf_path = pathlib.Path(no_docker['wrf_path'])
    wps_path = pathlib.Path(no_docker['wps_path'])
else:
    no_docker = None
    data_path = pathlib.Path('/data')
    geog_data_path = pathlib.Path('/WPS_GEOG')
    wrf_path = pathlib.Path('/WRF')
    wps_path = pathlib.Path('/WPS')

if 'start_date' in os.environ:
    file['time_control']['start_date'] = os.environ['start_date']

if 'end_date' in os.environ:
    file['time_control']['end_date'] = os.environ['end_date']

if 'domains' in os.environ:
    file['domains'] = os.environ['domains']

run_path = data_path.joinpath('run')

is_sentry = 'sentry' in file


##############################################
### Assign executables

wrf_exe = wrf_path.joinpath('main/wrf.exe')
real_exe = wrf_path.joinpath('main/real.exe')
ndown_exe = wrf_path.joinpath('main/ndown.exe')
geogrid_exe = wps_path.joinpath('geogrid.exe')
metgrid_exe = wps_path.joinpath('metgrid.exe')


###########################################
### WPS

src_wps_nml_path = data_path.joinpath('namelists/namelist.wps')
wps_nml_path = data_path.joinpath('namelist.wps')

wps_date_format = '%Y-%m-%d_%H:%M:%S'

outfile_format = '{prefix}_d{domain:02}_{date}.nc'

geogrid_array_fields = ('parent_id', 'parent_grid_ratio', 'i_parent_start', 'j_parent_start', 'e_we', 'e_sn', 'geog_data_res')

geogrid_single_fields = ('dx', 'dy', 'map_proj', 'ref_lat', 'ref_lon', 'stand_lon')

domain_array_fields = ('parent_id', 'parent_grid_ratio', 'i_parent_start', 'j_parent_start', 'e_we', 'e_sn', 'geog_data_res', 'e_vert', 'parent_time_step_ratio')

########################################
### WRF

src_wrf_nml_path = data_path.joinpath('namelists/namelist.input')
wrf_nml_path = data_path.joinpath('namelist.input')

history_outname = "wrfout_d<domain>_<date>.nc"
summ_outname = "wrfxtrm_d<domain>_<date>.nc"
zlevel_outname = 'wrfzlevels_d<domain>_<date>.nc'

# wrf_nml_one_first_fields = ('parent_time_step_ratio',)

###########################################
### Others

config_path = data_path.joinpath('rclone.config')

wrf_sphere_radius = 6370000

