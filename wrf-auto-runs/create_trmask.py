#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generate WRF-WVT tracer mask files (trmask_d<domain>) from geo_em files.

Called automatically by main.py when tracer_opt=4 is set in the [dynamics]
section of parameters.toml. Configuration comes from the [wvt] section.

Multi-region (v2.0): when [[wvt.regions]] is defined, one disjoint mask is
written per region into a region-dimensioned TRMASK(Time, wvt_regions, sn, we);
region order = WRF region index (region 1 = the unsuffixed tracer fields). The
flat single-region form ([wvt] mask_type at the top level, no regions array)
remains supported and produces a single region (wvt_regions = 1). The v2.0
multi-region registry declares TRMASK as i{wvtreg}j, so the multi-region image
carries the region axis even for a one-region run.

TRMASK layout (region axis vs flat 2D) is selected by the WVT_TRMASK_2D env var:
the frozen single-region image (registry TRMASK = 2D ij) sets WVT_TRMASK_2D=1 in
its pipeline Dockerfile, which writes a flat TRMASK(Time, sn, we) it can read;
otherwise (default) TRMASK is region-dimensioned. WVT_TRMASK_2D=1 requires exactly
one region.

Generates TRMASK (2D source) when tracer2dsource=1 and/or TRMASK3D (3D source,
single-region only) when tracer3dsource=1.
"""
import os
import sys

import numpy as np
import scipy.io.netcdf as nc3
from scipy import ndimage
import h5netcdf

import defaults
import params


def normalize_wvt_regions(wvt_config):
    """Return an ordered list of per-region config dicts from the [wvt] section.

    Supports two equivalent forms:
      * flat single-region: ``mask_type``/``bbox_deg``/``bbox_ij`` at the [wvt] top
        level (backward compatible) -> one region.
      * multi-region: an array of tables ``[[wvt.regions]]``; each region may set its
        own ``name``/``mask_type``/``bbox_deg``/``bbox_ij`` and inherits the top-level
        ``mask_type`` as its default. List order = WRF region index (1 = first).

    Returns a list of dicts with keys: name, mask_type, bbox_deg, bbox_ij.
    """
    default_mask_type = wvt_config.get('mask_type', 'land')
    regions_raw = wvt_config.get('regions')

    if regions_raw is None:
        # Flat single-region form.
        regions = [{
            'name': wvt_config.get('name', 'region_01'),
            'mask_type': default_mask_type,
            'bbox_deg': wvt_config.get('bbox_deg'),
            'bbox_ij': wvt_config.get('bbox_ij'),
            'face': None,
        }]
    else:
        if not isinstance(regions_raw, list) or not regions_raw:
            raise ValueError(
                '[wvt] regions must be a non-empty array of tables, e.g. [[wvt.regions]]'
            )

        regions = []
        for i, r in enumerate(regions_raw):
            if not isinstance(r, dict):
                raise ValueError(f'[wvt] regions[{i}] must be a table, got {r!r}')
            if r.get('mask_type') == 'boundary':
                raise ValueError(
                    f"[wvt] regions[{i}]: mask_type = \"boundary\" cannot be declared in "
                    '[[wvt.regions]]. Lateral-boundary face tags come from the [wvt] '
                    'boundary_faces key, which appends them after the source regions so that '
                    'their region indices are always last. Declaring one by hand would break '
                    'that ordering silently.'
                )
            regions.append({
                'name': r.get('name', f'region_{i + 1:02d}'),
                'mask_type': r.get('mask_type', default_mask_type),
                'bbox_deg': r.get('bbox_deg'),
                'bbox_ij': r.get('bbox_ij'),
                'face': None,
            })

    # Lateral-boundary face tags, appended AFTER the source regions in both input forms.
    # Appending here (rather than letting the user place them) is what makes "boundary
    # regions are last" true by construction, so no ordering check is needed anywhere --
    # and doing it after the two forms converge is what stops the key being a silent no-op
    # on the legacy flat config.
    for face in normalize_boundary_faces(wvt_config):
        regions.append({
            'name': f'{face}_face',
            'mask_type': 'boundary',
            'bbox_deg': None,
            'bbox_ij': None,
            'face': face,
        })
    return regions


#: The four lateral faces, in the order their region indices are assigned.
BOUNDARY_FACES = ('west', 'east', 'south', 'north')


def normalize_boundary_faces(wvt_config):
    """Return the ordered list of lateral faces to tag, from ``[wvt] boundary_faces``.

    Absent or ``[]`` means no boundary tags, which reproduces the pre-existing behaviour
    exactly: every existing parameters.toml is unaffected and emits no new namelist key.
    """
    faces = wvt_config.get('boundary_faces', [])
    if faces is None:
        faces = []
    if isinstance(faces, str):
        raise ValueError(
            f'[wvt] boundary_faces must be an array, e.g. ["west", "east"]; got a string {faces!r}'
        )
    faces = list(faces)
    seen = set()
    for f in faces:
        if f not in BOUNDARY_FACES:
            raise ValueError(
                f'[wvt] boundary_faces: unknown face {f!r}. Valid faces: {", ".join(BOUNDARY_FACES)}.'
            )
        if f in seen:
            raise ValueError(f'[wvt] boundary_faces: face {f!r} listed more than once.')
        seen.add(f)
    # CANONICAL order, not the user's: region indices 9.. are assigned in BOUNDARY_FACES order
    # everywhere downstream (the gate's labels, wvt_regions.BOUNDARY_REGIONS), so a TOML listing
    # ["north","west"] must not silently make region 9 the north shell (review round
    # redund-code-1: the gate accepted such a mask and labelled every face wrongly).
    return [f for f in BOUNDARY_FACES if f in seen]


def num_wvt_regions(wvt_config):
    """Number of WVT source regions defined in the [wvt] section (>= 1)."""
    return len(normalize_wvt_regions(wvt_config))


def resolve_relax_width(wvt_config, bdy_control):
    """The margin width the masks use, as an int.

    Precedence: ``[wvt] relax_width`` -> ``[bdy_control] spec_bdy_width`` -> the pipeline default.
    The default is ``defaults.WRF_BDY_CONTROL_DEFAULTS['spec_bdy_width']`` -- the SAME constant
    set_params writes into the namelist -- and not a literal, so the masks and the namelist
    cannot drift apart through two copies of one number (they agreed by coincidence of defaults
    until 2026-09-07). Per-domain list values are read at d01, exactly as set_params._first does;
    before this the mask side took the raw list and ``mask[:relax_width]`` raised TypeError while
    the width guard in set_params passed, because it compared against an already-normalised value.
    """
    def _d01(v):
        return v[0] if isinstance(v, list) else v

    default = defaults.WRF_BDY_CONTROL_DEFAULTS['spec_bdy_width']
    nml_w = _d01(bdy_control.get('spec_bdy_width', default))
    return int(_d01(wvt_config.get('relax_width', nml_w)))


def _check4(name, region_name, v):
    if not isinstance(v, (list, tuple)) or len(v) != 4:
        raise ValueError(f'[wvt] region {region_name!r}: {name} must be a list of 4 values, got {v!r}')


def _reject_legacy_bbox_keys(d, ctx):
    """Raise if the deprecated scalar bbox keys (min_lat/...) appear in a config table."""
    legacy = [k for k in ('min_lat', 'max_lat', 'min_lon', 'max_lon') if k in d]
    if legacy:
        raise ValueError(
            f'{ctx}: {legacy} are no longer supported. Use bbox_deg = '
            '[min_lat, max_lat, min_lon, max_lon] or bbox_ij = [i_min, i_max, j_min, j_max].'
        )


def _validate_region(reg):
    """Validate one region's mask_type + bbox config (raises ValueError)."""
    name = reg['name']
    mask_type = reg['mask_type']

    if mask_type == 'bbox':
        raise ValueError(
            f'[wvt] region {name!r}: mask_type = "bbox" is no longer supported. '
            'Use mask_type = "all" together with bbox_deg or bbox_ij.'
        )
    if mask_type not in ('land', 'ocean', 'all', 'boundary'):
        raise ValueError(
            f'[wvt] region {name!r}: Unknown mask_type {mask_type!r}. Use land, ocean, all, or boundary.'
        )

    bbox_deg = reg['bbox_deg']
    bbox_ij = reg['bbox_ij']

    if mask_type == 'boundary':
        if reg.get('face') not in BOUNDARY_FACES:
            raise ValueError(
                f'[wvt] region {name!r}: boundary regions need face in {BOUNDARY_FACES}; '
                f'got {reg.get("face")!r}'
            )
        if bbox_deg is not None or bbox_ij is not None:
            raise ValueError(
                f'[wvt] region {name!r}: a boundary region takes no bbox -- the face and the '
                'margin width ARE its geometry.'
            )
    elif reg.get('face') is not None:
        raise ValueError(
            f'[wvt] region {name!r}: face is only meaningful for mask_type = "boundary".'
        )
    if bbox_deg is not None and bbox_ij is not None:
        raise ValueError(f'[wvt] region {name!r}: set only one of bbox_deg or bbox_ij, not both.')

    if bbox_deg is not None:
        _check4('bbox_deg', name, bbox_deg)
        min_lat, max_lat, min_lon, max_lon = (float(x) for x in bbox_deg)
        if min_lat > max_lat:
            raise ValueError(f'[wvt] region {name!r}: bbox_deg needs min_lat <= max_lat; got {min_lat} > {max_lat}')
        # min_lon > max_lon is allowed (antimeridian-crossing arc).
    if bbox_ij is not None:
        _check4('bbox_ij', name, bbox_ij)
        i_min, i_max, j_min, j_max = (int(x) for x in bbox_ij)
        if i_min > i_max or j_min > j_max:
            raise ValueError(f'[wvt] region {name!r}: bbox_ij needs i_min <= i_max and j_min <= j_max; got {bbox_ij}')
        if i_min < 0 or j_min < 0:
            raise ValueError(f'[wvt] region {name!r}: bbox_ij indices must be >= 0; got {bbox_ij}')


#: 4-connectivity structuring element. The connectivity convention DECIDES which cells count
#: as enclosed -- under 8-connectivity Manukau and Kaipara touch open ocean diagonally and the
#: count drops from 9 to 7 on the C1 d01 grid -- so it is pinned here, not left to a default.
_CONN4 = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=bool)


def fill_enclosed_water(landmask):
    """Reclassify enclosed water bodies (lakes, tidal harbours) as land.

    Operates on a COPY of the landmask this tool derives from the geogrid. It never touches
    the geogrid or the LANDMASK the model physics reads -- turning Taupo into land tiles in
    the surface scheme is one careless edit away and is not what this is for.

    Rationale (wvt_region_basis_validation.md sec 4.8): the analysis landmask fills interior
    lakes, so Taupo counts toward the NZ landmass for RECEIVING precipitation while its
    EVAPORATION is tagged to whichever ocean bbox encloses it. Same water, land on one side of
    the ledger and ocean on the other.

    Returns (filled_landmask, n_filled).
    """
    landmask = np.asarray(landmask)
    water = landmask < 0.5
    lab, n = ndimage.label(water, structure=_CONN4)
    if n <= 1:
        return landmask.copy(), 0
    sizes = ndimage.sum(np.ones_like(lab), lab, index=range(1, n + 1))
    open_ocean = int(np.argmax(sizes)) + 1      # the one component that is the open sea
    enclosed = water & (lab != open_ocean)
    out = landmask.copy()
    out[enclosed] = 1.0
    return out, int(enclosed.sum())


def margin_geometry(we, sn):
    """Per-cell distances to the domain edges, for a `sn` x `we` grid (axis 0 = south-north).

    Returns a dict with ``d_west, d_east, d_south, d_north`` (cells to each edge), ``d_we`` and
    ``d_sn`` (the axis minima) and ``dist`` (the Chebyshev distance to the nearest edge). A pure
    function of the grid shape, and the ONE definition of margin geometry in this module: the
    boundary shells, the source-mask margin zeroing and the shell-tiling check all consume it.
    Before 2026-09-07 each of the three re-derived the margin with its own expression; they
    agreed, but nothing made them agree, which is the defect class the boundary-tag review
    rounds kept finding in the checking code.

    The outermost `relax_width` rings are ``dist < relax_width``.
    """
    j = np.arange(sn)[:, None]          # south-north (axis 0)
    i = np.arange(we)[None, :]          # west-east   (axis 1)
    d_west, d_east = i, we - 1 - i
    d_south, d_north = j, sn - 1 - j
    d_we = np.minimum(d_west, d_east)
    d_sn = np.minimum(d_south, d_north)
    dist = np.minimum(d_we, d_sn)
    return {
        'd_west': np.broadcast_to(d_west, (sn, we)),
        'd_east': np.broadcast_to(d_east, (sn, we)),
        'd_south': np.broadcast_to(d_south, (sn, we)),
        'd_north': np.broadcast_to(d_north, (sn, we)),
        'd_we': np.broadcast_to(d_we, (sn, we)),
        'd_sn': np.broadcast_to(d_sn, (sn, we)),
        'dist': dist,
    }


def _build_boundary_mask(face, relax_width, we, sn, name):
    """Margin cells nearest `face`: the outermost `relax_width` rings, split by nearest edge.

    Corner convention: nearest face wins; a tie goes to WEST/EAST (the meridional faces).

    Note what pins this: the tiling check does NOT -- it only asks that the union of the
    shells equals the margin, which holds under either tie-break (verified by mutation). The
    convention is pinned solely by test_corner_convention_is_pinned. It decides which face is
    credited with a corner cell's inflow, so changing it silently would shift attribution
    between faces without any check noticing.
    """
    if relax_width <= 0:
        raise ValueError(
            f'[wvt] region {name!r}: boundary regions need relax_width > 0; got {relax_width}'
        )
    if 2 * relax_width >= min(we, sn):
        raise ValueError(
            f'[wvt] region {name!r}: relax_width={relax_width} leaves no interior on a '
            f'{we}x{sn} grid.'
        )
    g = margin_geometry(we, sn)
    d_west, d_east, d_south, d_north = g['d_west'], g['d_east'], g['d_south'], g['d_north']
    in_margin = g['dist'] < relax_width

    # Ties (d_we == d_sn) go to the meridional faces, hence <= here and strict < for S/N.
    meridional = g['d_we'] <= g['d_sn']
    if face == 'west':
        sel = meridional & (d_west <= d_east)
    elif face == 'east':
        sel = meridional & (d_east < d_west)
    elif face == 'south':
        sel = (~meridional) & (d_south <= d_north)
    elif face == 'north':
        sel = (~meridional) & (d_north < d_south)
    else:
        raise ValueError(f'[wvt] region {name!r}: unknown face {face!r}')

    return (in_margin & sel).astype('f4')


def _build_region_mask(reg, lat, lon, landmask, relax_width, we, sn, domain_idx):
    """Build the 2D float32 source mask for one region on one domain grid.

    mask = (mask_type selection) intersected with the optional bbox, with the
    lateral relaxation zone zeroed (tracers are not conserved there).
    """
    mask_type = reg['mask_type']
    if mask_type == 'boundary':
        # The shell IS the margin every source mask excludes, so it deliberately bypasses the
        # zeroing at the end of this function -- returning early is what implements that.
        return _build_boundary_mask(reg['face'], relax_width, we, sn, reg['name'])
    if mask_type == 'land':
        mask = landmask.copy()
    elif mask_type == 'ocean':
        mask = 1.0 - landmask
    else:  # 'all'
        mask = np.ones_like(lat)
    mask = mask.astype('f4')

    bbox_deg = reg['bbox_deg']
    bbox_ij = reg['bbox_ij']
    if bbox_deg is not None:
        min_lat, max_lat, min_lon, max_lon = (float(x) for x in bbox_deg)
        lat_in = (lat >= min_lat) & (lat <= max_lat)
        if min_lon <= max_lon:
            lon_in = (lon >= min_lon) & (lon <= max_lon)
        else:
            # Antimeridian-crossing box: lon >= min_lon (east up to +180) OR lon <= max_lon (across -180).
            lon_in = (lon >= min_lon) | (lon <= max_lon)
        mask = mask * np.where(lat_in & lon_in, 1.0, 0.0).astype('f4')
    elif bbox_ij is not None:
        i_min, i_max, j_min, j_max = (int(x) for x in bbox_ij)
        # i = west-east (axis 1), j = south-north (axis 0); inclusive bounds.
        if i_max > we - 1 or j_max > sn - 1:
            raise ValueError(
                f"[wvt] region {reg['name']!r}: bbox_ij exceeds domain d{domain_idx:02d} grid "
                f'({we}x{sn}): i_max={i_max}, j_max={j_max} (valid max i={we - 1}, j={sn - 1})'
            )
        box = np.zeros_like(mask)
        box[j_min:j_max + 1, i_min:i_max + 1] = 1.0
        mask = mask * box

    if relax_width > 0:
        # The same margin the boundary shells occupy and the tiling check tests -- one
        # definition (margin_geometry), so a source region and a shell cannot disagree about
        # where the margin is.
        mask[margin_geometry(we, sn)['dist'] < relax_width] = 0

    return mask


def create_trmask(domains, start_date):
    """
    Generate trmask_d<domain> files for each active domain.

    Parameters
    ----------
    domains : list of int
        Domain numbers to create masks for (e.g. [1, 2]).
    start_date : str
        Simulation start date in 'YYYY-MM-DD HH:MM:SS' format.
    """
    wvt_config = params.file.get('wvt', {})
    dynamics = params.file.get('dynamics', {})

    relax_width = resolve_relax_width(wvt_config, params.file.get('bdy_control', {}))
    # Tier-1 enclosed-water fill. Explicit rather than unconditional: it MOVES cells between
    # regions, so switching it on silently would break both reproduction of earlier runs and
    # the bit-identity gate on the region-cap raise. C1 sets it true.
    fill_water = bool(wvt_config.get('fill_enclosed_water', False))

    do_2d = dynamics.get('tracer2dsource', 0) == 1
    do_3d = dynamics.get('tracer3dsource', 0) == 1

    if not do_2d and not do_3d:
        print('   WARNING: tracer_opt=4 but neither tracer2dsource nor tracer3dsource is enabled')
        return

    # Resolve + validate the region list up front, before any file I/O.
    _reject_legacy_bbox_keys(wvt_config, '[wvt]')
    for i, r in enumerate(wvt_config.get('regions') or []):
        if isinstance(r, dict):
            _reject_legacy_bbox_keys(r, f'[wvt] regions[{i}]')
    regions = normalize_wvt_regions(wvt_config)
    for reg in regions:
        _validate_region(reg)
    n_reg = len(regions)

    # TRMASK layout: region-dimensioned (Time, wvt_regions, sn, we) for the v2.0 multi-region
    # registry (TRMASK = i{wvtreg}j), or flat 2-D (Time, sn, we) for the frozen single-region image
    # whose registry declares TRMASK = ij. The single-region image opts in via WVT_TRMASK_2D=1 (set in
    # its pipeline Dockerfile); default is region-dimensioned. 2-D has no region axis -> single region only.
    region_axis = os.environ.get('WVT_TRMASK_2D') != '1'
    if not region_axis and n_reg != 1:
        raise ValueError(
            f'WVT_TRMASK_2D=1 selects the flat 2-D TRMASK layout (single-region image) but [wvt] defines '
            f'{n_reg} regions. The 2-D layout has no region axis; define exactly one region, or unset '
            'WVT_TRMASK_2D to use the region-dimensioned layout.'
        )

    if n_reg > 1 and do_3d:
        raise ValueError(
            '[wvt] multiple regions ([[wvt.regions]]) require tracer3dsource=0 '
            '(the 3D atmospheric source is single-region only).'
        )

    # Get e_vert for the 3D mask vertical dimension.
    n_vert = None
    if do_3d:
        e_vert_raw = params.file['domains'].get('e_vert', 33)
        e_vert = e_vert_raw[0] if isinstance(e_vert_raw, list) else e_vert_raw
        n_vert = e_vert - 1  # full levels = stagger points - 1

    # Format the Times string as WRF expects: "YYYY-MM-DD_HH:MM:SS".
    if hasattr(start_date, 'format'):
        times_str = start_date.format('YYYY-MM-DD_HH:mm:ss')
    else:
        times_str = str(start_date).replace(' ', '_')

    for i, domain in enumerate(domains):
        domain_idx = i + 1
        geo_em_path = params.data_path / f'geo_em.d{domain_idx:02d}.nc'
        trmask_path = params.data_path / f'trmask_d{domain_idx:02d}'

        if not geo_em_path.exists():
            raise FileNotFoundError(f'geo_em file not found: {geo_em_path}')

        # Read grid info from geo_em.
        with h5netcdf.File(geo_em_path) as geo:
            lat = np.array(geo['XLAT_M'][0, :, :])
            lon = np.array(geo['XLONG_M'][0, :, :])
            landmask = np.array(geo['LANDMASK'][0, :, :])
            if fill_water:
                landmask, n_filled = fill_enclosed_water(landmask)
                print(f'-- [wvt] d{domain_idx:02d}: filled {n_filled} enclosed-water cell(s) '
                      'into the land mask (lakes/harbours tag as land, not ocean)',
                      file=sys.stderr)
            else:
                _, n_enclosed = fill_enclosed_water(landmask)
                if n_enclosed:
                    print(f'-- [wvt] d{domain_idx:02d}: NOTE {n_enclosed} enclosed-water cell(s) '
                          '(lakes/harbours) will be tagged as OCEAN. Set [wvt] '
                          'fill_enclosed_water = true to count them as land.',
                          file=sys.stderr)
            mminlu = geo.attrs.get('MMINLU', 'MODIFIED_IGBP_MODIS_NOAH')
            num_land_cat = geo.attrs.get('NUM_LAND_CAT', 21)
            if isinstance(mminlu, bytes):
                mminlu = mminlu.decode()

        sn, we = lat.shape

        # Build one mask per region.
        masks = np.zeros((n_reg, sn, we), dtype='f4')
        for k, reg in enumerate(regions):
            masks[k] = _build_region_mask(reg, lat, lon, landmask, relax_width, we, sn, domain_idx)
            if masks[k].sum() == 0:
                raise ValueError(
                    f"[wvt] region {reg['name']!r} has an empty mask on d{domain_idx:02d} "
                    f"(mask_type={reg['mask_type']!r}, bbox_deg={reg['bbox_deg']}, bbox_ij={reg['bbox_ij']}). "
                    'Check the mask_type / bbox selects cells OUTSIDE the zeroed boundary margin.'
                )

        # Disjointness: every source cell must belong to at most one region, or the
        # per-region attribution double-counts and Sum(regions) != single-run total.
        coverage = (masks > 0).sum(axis=0)
        n_overlap = int((coverage > 1).sum())
        if n_overlap > 0:
            jj, ii = np.where(coverage > 1)
            raise ValueError(
                f'[wvt] regions overlap on {n_overlap} cell(s) of d{domain_idx:02d} '
                f'(first at j={jj[0]}, i={ii[0]}); regions must be disjoint. '
                'Adjust the bboxes/mask_types so no cell is tagged by two regions.'
            )

        # Boundary shells must EXACTLY tile the zeroed margin. A margin cell in no shell and
        # no source region is not a harmless gap: it is a permanent untagged source, and the
        # untagged-remainder floor absorbs it silently rather than reporting it. Checked on
        # the masks actually written, across every region -- the face list alone cannot see a
        # source region that failed to zero its margin.
        bdy_idx = [k for k, r in enumerate(regions) if r['mask_type'] == 'boundary']
        if bdy_idx:
            shell = (masks[bdy_idx] > 0).any(axis=0)
            margin = margin_geometry(we, sn)['dist'] < relax_width
            extra = int((shell & ~margin).sum())
            if extra:
                raise ValueError(
                    f'[wvt] {extra} boundary-shell cell(s) fall OUTSIDE the margin on '
                    f'd{domain_idx:02d} (relax_width={relax_width}); shells must stay within '
                    'the margin the source masks exclude, or they would overlap a source region.'
                )
            missing = int((margin & ~shell).sum())
            n_faces = len(bdy_idx)
            if n_faces == len(BOUNDARY_FACES):
                # All four listed: the margin must be tiled exactly. A gap here is a bug, and
                # it would surface downstream only as a slightly larger untagged remainder.
                if missing:
                    raise ValueError(
                        f'[wvt] boundary shells leave {missing} margin cell(s) in no shell on '
                        f'd{domain_idx:02d} with all four faces listed (relax_width='
                        f'{relax_width}). That is a tiling bug, not a configuration choice.'
                    )
            elif missing:
                # Fewer faces is a legitimate cost choice -- each face is roughly 0.4 node-days
                # per simulated year -- but it means inflow through the unlisted faces is NOT
                # tagged, and that shows up as a larger untagged remainder rather than as an
                # error. Say so loudly rather than letting the number drift unexplained.
                print(
                    f'-- [wvt] d{domain_idx:02d}: {n_faces} of {len(BOUNDARY_FACES)} faces '
                    f'tagged; {missing} margin cell(s) are in NO shell, so inflow there stays '
                    'untagged and the untagged remainder will be correspondingly larger.',
                    file=sys.stderr,
                )

        _write_trmask(trmask_path, lat, lon, masks, times_str, mminlu, num_land_cat,
                      do_2d=do_2d, do_3d=do_3d, n_vert=n_vert, region_axis=region_axis)

        parts = []
        if do_2d:
            parts.append(f'TRMASK ({n_reg} region{"s" if n_reg > 1 else ""})')
        if do_3d:
            parts.append(f'TRMASK3D ({n_vert} levels)')
        per_region = ', '.join(
            f"{reg['name']}={int(masks[k].sum())}" for k, reg in enumerate(regions)
        )
        print(
            f'   Created {trmask_path.name} ({we}x{sn}, relax_width={relax_width}, '
            f'vars: {", ".join(parts)}; source cells: {per_region})'
        )


def _write_trmask(path, lat, lon, masks, times_str, mminlu, num_land_cat,
                  do_2d=True, do_3d=False, n_vert=None, region_axis=True):
    """Write a trmask NetCDF3 classic file in the format WRF expects.

    masks : (n_reg, south_north, west_east) float array -- one mask per WVT region.
    region_axis True  -> TRMASK(Time, wvt_regions, sn, we) for the multi-region registry (i{wvtreg}j).
    region_axis False -> flat TRMASK(Time, sn, we) for the single-region registry (ij); n_reg must be 1.
    """
    n_reg, sn, we = masks.shape

    f = nc3.netcdf_file(str(path), 'w', version=1)

    # Dimensions
    f.createDimension('Time', None)  # unlimited
    if region_axis:
        f.createDimension('wvt_regions', n_reg)
    f.createDimension('south_north', sn)
    f.createDimension('west_east', we)
    f.createDimension('DateStrLen', 19)
    if do_3d:
        f.createDimension('bottom_top', n_vert)

    # XLAT
    v = f.createVariable('XLAT', 'f4', ('south_north', 'west_east'))
    v[:] = lat.astype(np.float32)
    v.FieldType = np.int32(104)
    v.MemoryOrder = 'XY '
    v.description = 'LATITUDE SOUTH IS NEGATIVE'
    v.units = 'degree_north'
    v.stagger = ''

    # XLONG
    v = f.createVariable('XLONG', 'f4', ('south_north', 'west_east'))
    v[:] = lon.astype(np.float32)
    v.FieldType = np.int32(104)
    v.MemoryOrder = 'XY '
    v.description = 'LONGITUDE WEST IS NEGATIVE'
    v.units = 'degree_east'
    v.stagger = ''

    # TRMASK source mask. region_axis True: i{wvtreg}j layout read by auxinput8 into grid%trmask(i, n, j).
    # region_axis False: flat ij layout (single-region image) read into grid%trmask(i, j); region 0 only.
    if do_2d:
        if region_axis:
            v = f.createVariable('TRMASK', 'f4', ('Time', 'wvt_regions', 'south_north', 'west_east'))
            v[0, :, :, :] = masks.astype(np.float32)
            v.MemoryOrder = 'XYZ'
            v.description = 'Tracer Source Mask (1 FOR SOURCE), per WVT region'
        else:
            v = f.createVariable('TRMASK', 'f4', ('Time', 'south_north', 'west_east'))
            v[0, :, :] = masks[0].astype(np.float32)
            v.MemoryOrder = 'XY'
            v.description = 'Tracer Source Mask (1 FOR SOURCE)'
        v.FieldType = np.int32(104)
        v.units = ''
        v.stagger = ''
        v.coordinates = 'XLONG XLAT'

    # TRMASK3D (3D source mask -- region 1's 2D mask extruded to all levels). The 3D
    # source is single-region only (enforced in create_trmask), so region 1 is used.
    if do_3d:
        v = f.createVariable('TRMASK3D', 'f4', ('Time', 'bottom_top', 'south_north', 'west_east'))
        mask_3d = np.tile(masks[0].astype(np.float32)[np.newaxis, :, :], (n_vert, 1, 1))
        v[0, :, :, :] = mask_3d
        v.FieldType = np.int32(104)
        v.MemoryOrder = 'XYZ'
        v.description = '3D SOURCE MASK FOR MOISTURE TRACERS'
        v.units = ''
        v.stagger = ''
        v.coordinates = 'XLONG XLAT'

    # Times -- byte-by-byte for scipy.io.netcdf NC_CHAR compatibility
    v = f.createVariable('Times', 'c', ('Time', 'DateStrLen'))
    time_str_19 = times_str[:19].ljust(19, ' ')
    for i, char in enumerate(time_str_19):
        v[0, i] = char.encode('ascii')

    # Global attributes
    f.TITLE = 'OUTPUT FROM WVT TRACER MASK GENERATOR V4.0'
    f.START_DATE = times_str
    f.MMINLU = mminlu
    f.NUM_LAND_CAT = np.int32(num_land_cat)

    f.close()
