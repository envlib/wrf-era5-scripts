#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Tue Sep 23 15:09:38 2025

@author: mike
"""
import uuid
from time import sleep

import pendulum
import sentry_sdk

# from download_nml_domain import dl_nml_domain
from set_params import check_set_params, set_ndown_params
from download_era5 import dl_era5
from run_era5_to_int import run_era5_to_int
from run_metgrid import run_metgrid
from run_real import run_real
from monitor_wrf import monitor_wrf
from upload_namelists import upload_namelists
from check_ndown import check_ndown_params
from run_geogrid import run_geogrid
from run_ndown import run_ndown
from download_ndown_input import dl_ndown_input

import params

run_uuid = uuid.uuid4().hex[-13:]

########################################
## Sentry
sentry = params.file['sentry']

if sentry['dsn'] != '':
    sentry_sdk.init(
        dsn=sentry['dsn'],
        # Add data like request headers and IP for users,
        # see https://docs.sentry.io/platforms/python/data-management/data-collected/ for more info
        send_default_pii=True,
    )

if sentry['tags']:
    sentry_sdk.set_tags(sentry['tags'])

sentry_sdk.set_tags({'run_uuid': run_uuid})

########################################
### Run sequence

start_time = pendulum.now()

print(f'--  run uuid: {run_uuid}')

print(f"-- start time: {start_time.format('YYYY-MM-DD HH:mm:ss')}")

# print('-- Downloading namelists...')
# dl_check = dl_nml_domain()

if 'domains' in params.file:
    domains = params.file['domains']
    if isinstance(domains, int):
        domains = [domains]
    elif isinstance(domains, list):
        for domain in domains:
            if not isinstance(domain, int):
                raise ValueError('domains must be a list of int.')
    else:
        raise ValueError('domains must be a list of int.')
else:
    domains = None

ndown_check, domains_init = check_ndown_params(domains)

start_date, end_date, hour_interval, outputs = check_set_params(domains_init)

print(f'start date: {start_date}, end date: {end_date}, input hour interval: {hour_interval}')

print('-- Uploading updated namelists')
ul_nml_check = upload_namelists(run_uuid)

if ndown_check:
    print('-- ndown has been selected and the prior wrfout files will be downloaded...')
    dl_ndown_input(domains_init[0], start_date, end_date)
else:
    print('-- A full nested domain model will be run')

print('-- Run geogrid.exe...')
min_lon, min_lat, max_lon, max_lat = run_geogrid()

print('-- Top domain bounds:')
print(min_lon, min_lat, max_lon, max_lat, sep=', ')

print('-- Downloading ERA5 data...')
era5_check = dl_era5(start_date, end_date)

print('-- Processing ERA5 to WPS Int...')
run_era5_to_int(start_date, end_date, hour_interval, False)

print('-- Running metgrid.exe...')
run_metgrid(False)

print('-- Running real.exe...')
run_real(run_uuid, False)

if ndown_check:

    print('-- Running ndown.exe...')
    run_ndown(run_uuid, False)

    start_date, end_date, hour_interval, outputs = check_set_params(domains)
    set_ndown_params()

    rename_dict = {'_d01_': f'_d{domains[-1]:02d}_'}

else:
    rename_dict = {}

# print('-- Running WRF...')
# monitor_wrf(outputs, end_date, run_uuid, rename_dict)

# end_time = pendulum.now()

# print(f"-- end time: {end_time.format('YYYY-MM-DD HH:mm:ss')}")

# diff = end_time - start_time

# mins = round(diff.total_minutes())

# print(f"-- Total run minutes: {mins}")



























