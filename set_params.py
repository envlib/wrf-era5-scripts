#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Mon Sep 22 17:21:47 2025

@author: mike
"""
import pathlib
import f90nml
from datetime import datetime, timedelta

import params, utils


################################################
### function


def check_nml_params(domains):
    """

    """
    ##############################################
    ### Assign and check executables

    if not params.wrf_path.exists():
        raise ValueError(f'wrf path does not exist: {params.wrf_path}')

    if not params.wrf_exe.exists():
        raise ValueError(f'wrf.exe does not exist: {params.wrf_exe}')

    if not params.real_exe.exists():
        raise ValueError(f'real.exe does not exist: {params.real_exe}')

    if not params.wps_path.exists():
        raise ValueError(f'wps path does not exist: {params.wps_path}')

    if not params.geogrid_exe.exists():
        raise ValueError(f'geogrid.exe does not exist: {params.geogrid_exe}')

    if not params.metgrid_exe.exists():
        raise ValueError(f'metgrid.exe does not exist: {params.metgrid_exe}')


    ##############################################
    ### Namelist checks

    wps_nml = f90nml.read(params.src_wps_nml_path)
    # wrf_nml = f90nml.read(params.src_wrf_nml_path)

    ## domains
    wps_domains = wps_nml['geogrid']
    # wrf_domains = wrf_nml['domains']

    parent_ids = wps_domains['parent_id']

    src_n_domains = len(parent_ids)

    for f in params.geogrid_array_fields:
        if f not in wps_domains:
            raise ValueError(f'The geogrid field {f} does not exist in the namelist.wps.')

        v = wps_domains[f]

        if len(v) != src_n_domains:
            raise ValueError(f'The field {f} must be an array with {src_n_domains} values.')

        if f in ('e_we', 'e_sn'):
            for i in v:
                if i < 100:
                    raise ValueError('The number of grid points in the domain must be greater than or equal to 100.')

    for f in params.geogrid_single_fields:
        if f not in wps_domains:
            raise ValueError(f'The geogrid field {f} does not exist in the namelist.wps.')

        v = wps_domains[f]

        if isinstance(v, list):
            raise ValueError(f'The field {f} must be an single value.')

    if domains:

        domains.sort()

        # Check if assigned domains are properly nested
        for domain in domains[1:]:
            parent_id = parent_ids[domain - 1]
            if parent_id not in domains:
                raise ValueError(f'The parent_id {parent_id} does not exist in the assigned domains. The parent/child domains must match.')
    else:
        domains = list(range(1, src_n_domains + 1))

    # with open(params.wps_nml_path, 'w') as nml_file:
    #    wps_nml.write(nml_file)

    return src_n_domains, domains


def set_nml_params(domains=None):
    """

    """
    #########################################
    ### Read in nml

    wps_nml = f90nml.read(params.src_wps_nml_path)
    wrf_nml = f90nml.read(params.src_wrf_nml_path)

    ## domains
    wps_domains = wps_nml['geogrid']
    wrf_domains = wrf_nml['domains']

    parent_ids = wps_domains['parent_id']

    old_n_domains = len(parent_ids)

    #########################################
    ### WPS geogrid processing

    if domains:

        domains.sort()

        # Update the geogrid if needed
        _ = utils.update_geogrid(wps_domains, domains)

        n_domains = len(domains)

    else:
        domains = list(range(1, old_n_domains + 1))
        n_domains = old_n_domains

    #########################################
    ### Assign values in namelists

    data_path = params.data_path

    ## Time control (WRF) / Share (WPS)
    wps_nml['share']['opt_output_from_geogrid_path'] = str(data_path)
    wps_nml['share']['wrf_core'] = 'ARW'
    wps_nml['share']['io_form_geogrid'] = 2
    wps_nml['share']['max_dom'] = n_domains

    wrf_nml['time_control']['io_form_history'] = 2
    wrf_nml['time_control']['io_form_restart'] = 2
    wrf_nml['time_control']['io_form_input'] = 2
    wrf_nml['time_control']['io_form_boundary'] = 2
    wrf_nml['time_control']['adjust_output_times'] = True

    start_date = datetime.fromisoformat(params.file['time_control']['start_date'])
    end_date = datetime.fromisoformat(params.file['time_control']['end_date'])

    if start_date > end_date:
        raise ValueError(f'start_date ({start_date}) is greater than end_date ({end_date}).')

    interval_secs = params.file['time_control']['interval_seconds']
    
    if interval_secs % (60*60) != 0:
        raise ValueError('interval_seconds must be an interval of an hour.')
    
    wps_nml['share']['interval_seconds'] = interval_secs
    wrf_nml['time_control']['interval_seconds'] = interval_secs
    
    history_intervals = params.file['time_control']['history_file']['history_interval']
    history_intervals = {int(domain): hi for domain, hi in history_intervals.items() if int(domain) in domains}
    history_interval_nml = [history_intervals[i] for i in domains]

    for hi in history_interval_nml:
        if hi % 60 != 0:
            raise ValueError('history interval must be an interval of an hour.')
    
    wrf_nml['time_control']['history_interval'] = history_interval_nml
    
    n_hours_per_file = 24 # It's now hard coded due to issues of running a day without an extra single timestamp file at the end

    frames_per_outfile = []
    for hi in history_interval_nml:
        hours = int(hi/60)
        frames_per_outfile.append(int(n_hours_per_file/hours))

    history_begin = params.file['time_control']['history_file']['history_begin']

    wrf_nml['time_control']['frames_per_outfile'] = frames_per_outfile
    wrf_nml['time_control']['history_begin'] = [history_begin] * n_domains
    wrf_nml['time_control']['history_outname'] = params.history_outname

    ## Other output files
    summ_file = params.file['time_control']['summary_file']
    output_diagnostics = summ_file['output_diagnostics']
    
    wrf_nml['time_control']['output_diagnostics'] = output_diagnostics
    
    if output_diagnostics == 1:
        diag_interval = summ_file['auxhist3_interval']

        if diag_interval % (60*24) != 0:
            raise ValueError('auxhist3_interval must be an interval of a day.')

        wrf_nml['time_control']['auxhist3_interval'] = [diag_interval] * n_domains
    
        n_days_per_file = summ_file['n_days_per_file']
    
        days = int(diag_interval/60/24)
    
        wrf_nml['time_control']['frames_per_auxhist3'] = [int(n_days_per_file/days)] * n_domains
    
        wrf_nml['time_control']['auxhist3_outname'] = params.summ_outname
        wrf_nml['time_control']['io_form_auxhist3'] = 2
        wrf_nml['time_control']['auxhist3_begin'] = [params.file['time_control']['history_file']['history_begin']] * n_domains

    z_level_file = params.file['time_control']['z_level_file']
    z_level_flag = z_level_file['z_lev_diags']
    
    wrf_nml['diags']['z_lev_diags'] = z_level_flag

    if z_level_flag == 1:
        wrf_nml['diags']['z_levels'] = [-z for z in z_level_file['z_levels']]
        wrf_nml['diags']['num_z_levels'] = len(z_level_file['z_levels'])

        wrf_nml['time_control']['auxhist22_outname'] = params.zlevel_outname
        wrf_nml['time_control']['io_form_auxhist22'] = 2
        wrf_nml['time_control']['auxhist22_interval'] = history_interval_nml
        wrf_nml['time_control']['frames_per_auxhist22'] = frames_per_outfile
        wrf_nml['time_control']['auxhist22_begin'] = [params.file['time_control']['history_file']['history_begin']] * n_domains

    history_begin = wrf_nml['time_control']['history_begin'][0]

    start_date = start_date - timedelta(minutes=history_begin)

    wps_nml['share']['start_date'] = [start_date.strftime(params.wps_date_format)] * n_domains
    wps_nml['share']['end_date'] = [end_date.strftime(params.wps_date_format)] * n_domains

    wrf_nml['time_control']['start_year'] = [start_date.year] * n_domains
    wrf_nml['time_control']['start_month'] = [start_date.month] * n_domains
    wrf_nml['time_control']['start_day'] = [start_date.day] * n_domains
    wrf_nml['time_control']['start_hour'] = [start_date.hour] * n_domains
    wrf_nml['time_control']['end_year'] = [end_date.year] * n_domains
    wrf_nml['time_control']['end_month'] = [end_date.month] * n_domains
    wrf_nml['time_control']['end_day'] = [end_date.day] * n_domains
    wrf_nml['time_control']['end_hour'] = [end_date.hour] * n_domains
    wrf_nml['time_control']['input_from_file'] = [True] * n_domains

    outputs = ['wrfout']
    if wrf_nml['time_control']['output_diagnostics'] == 1:
        outputs.append('summ')
    if wrf_nml['diags']['z_lev_diags'] == 1:
        outputs.append('zlevel')

    ### Domains - namelist.wps copied to namelist.input
    wps_nml['geogrid']['geog_data_path'] = str(params.geog_data_path)
    wps_nml['geogrid']['opt_geogrid_tbl_path'] = str(params.geogrid_exe.parent.joinpath('geogrid'))

    for k, v in wps_domains.items():
        if k in wrf_domains:
            wrf_domains[k] = v

    wrf_domains['max_dom'] = n_domains

    if 'max_step_increase_pct' in wrf_domains:
        wrf_domains['max_step_increase_pct'] = [wrf_domains['max_step_increase_pct'][domain - 1] for domain in domains]
        # if n_domains > 1:
        wrf_domains['max_step_increase_pct'][0] = 5

    # wrf_domains['parent_id'] = [wrf_domains['parent_id'][domain - 1] for domain in domains]
    # wrf_domains['parent_id'][0] = 1

    wrf_domains['grid_id'] = list(range(1, n_domains + 1))
    wrf_domains['parent_time_step_ratio'] = [wrf_domains['parent_time_step_ratio'][domain - 1] for domain in domains]
    wrf_domains['parent_time_step_ratio'][0] = 1

    dx = wrf_domains['dx']
    wrf_domains['time_step'] = int(dx * 0.001 * 6)

    ### Physics - Most should be done by the user
    ## Output precip rate to history file
    wrf_nml['physics']['prec_acc_dt'] = history_interval_nml

    ### ungrib
    wps_nml['ungrib']['out_format'] = 'WPS'
    wps_nml['ungrib']['prefix'] = 'ERA5'

    ### metgrid
    wps_nml['metgrid']['fg_name'] = str(data_path.joinpath('ERA5'))
    wps_nml['metgrid']['io_form_metgrid'] = 2
    wps_nml['metgrid']['opt_metgrid_tbl_path'] = str(params.metgrid_exe.parent.joinpath('metgrid'))
    wps_nml['metgrid']['opt_output_from_metgrid_path'] = str(data_path)

    ### Update all fields that are "max_domain" to the new n_domains in WRF nml
    for grp_name, grp in wrf_nml.items():
        if grp_name not in ('diags', ):
            for k, v in grp.items():
                if isinstance(v, list):
                    if len(v) == old_n_domains:
                        wrf_nml[grp_name][k] = [wrf_nml[grp_name][k][domain - 1] for domain in domains]
                    elif len(v) != n_domains:
                        raise ValueError(f'The field {k} in group {grp_name} has {len(v)} values, but it should either have {n_domains} or be a single value.')


    #############################################
    ### Write namelists

    with open(params.wps_nml_path, 'w') as nml_file:
       wps_nml.write(nml_file)

    with open(params.wrf_nml_path, 'w') as nml_file:
       wrf_nml.write(nml_file)

    return start_date, end_date, int(wrf_nml['time_control']['interval_seconds']/(60*60)), outputs



def set_ndown_params():
    """
    Should be set after ndown is run.
    """
    wrf_nml = f90nml.read(params.wrf_nml_path)

    wrf_nml['bdy_control']['have_bcs_moist'] = True
    wrf_nml['bdy_control']['have_bcs_scalar'] = True
    wrf_nml['time_control']['io_form_auxinput2'] = 2

    with open(params.wrf_nml_path, 'w') as nml_file:
       wrf_nml.write(nml_file)
















































