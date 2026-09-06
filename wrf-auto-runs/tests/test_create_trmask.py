import numpy as np
import h5netcdf
import pendulum
import pytest
import scipy.io.netcdf as nc3

from create_trmask import (BOUNDARY_FACES, _build_boundary_mask, create_trmask,
                           fill_enclosed_water, normalize_boundary_faces,
                           normalize_wvt_regions, num_wvt_regions)


# Deterministic small grid: 10x10, left half land (x < 5), right half ocean.
# Lats span -45 to -36, lons span 170 to 179 (inclusive endpoints).
SN, WE = 10, 10
LAT_1D = np.linspace(-45.0, -36.0, SN, dtype=np.float32)
LON_1D = np.linspace(170.0, 179.0, WE, dtype=np.float32)
LAT_2D, LON_2D = np.meshgrid(LAT_1D, LON_1D, indexing='ij')
LANDMASK_2D = np.zeros((SN, WE), dtype=np.float32)
LANDMASK_2D[:, :5] = 1.0  # left half is land

# Dateline-crossing longitude grid: ascending eastward from +150 across +/-180
# to -166 (i.e. 150..178E, then 182..194E expressed as -178..-166 in -180..180).
LON_1D_DL = np.array([150, 156, 162, 168, 174, 178, -178, -174, -170, -166], dtype=np.float32)
_, LON_2D_DL = np.meshgrid(LAT_1D, LON_1D_DL, indexing='ij')


def _write_fake_geo_em(path, e_vert=10, lon=None):
    """Write a minimal geo_em.d01.nc that create_trmask can read.

    `lon` overrides the default LON_2D grid (e.g. a dateline-crossing grid).
    """
    if lon is None:
        lon = LON_2D
    with h5netcdf.File(path, 'w') as f:
        f.dimensions['Time'] = 1
        f.dimensions['south_north'] = SN
        f.dimensions['west_east'] = WE

        lat_var = f.create_variable('XLAT_M', ('Time', 'south_north', 'west_east'), dtype='f4')
        lat_var[0, :, :] = LAT_2D
        lon_var = f.create_variable('XLONG_M', ('Time', 'south_north', 'west_east'), dtype='f4')
        lon_var[0, :, :] = lon
        lm_var = f.create_variable('LANDMASK', ('Time', 'south_north', 'west_east'), dtype='f4')
        lm_var[0, :, :] = LANDMASK_2D

        f.attrs['MMINLU'] = 'MODIFIED_IGBP_MODIS_NOAH'
        f.attrs['NUM_LAND_CAT'] = np.int32(21)


def _read_trmask(path, region=0):
    """Read one region's TRMASK (2D) from a generated trmask file.

    TRMASK is region-dimensioned (Time, wvt_regions, sn, we); default region 0
    is WRF region 1 (the single region in the flat single-region tests).
    """
    with nc3.netcdf_file(str(path), 'r', mmap=False) as f:
        return np.array(f.variables['TRMASK'][0, region, :, :])


def _read_trmask_nreg(path):
    """Return the size of the wvt_regions dimension of a trmask file."""
    with nc3.netcdf_file(str(path), 'r', mmap=False) as f:
        return f.variables['TRMASK'].shape[1]


def _read_trmask3d(path):
    with nc3.netcdf_file(str(path), 'r', mmap=False) as f:
        return np.array(f.variables['TRMASK3D'][0, :, :, :])


def _configure(mock_params, wvt, dynamics_extra=None):
    """Set [wvt] and [dynamics] on the in-memory TOML dict used by params."""
    mock_params['wvt'] = wvt
    dynamics = {'tracer_opt': 4, 'tracer2dsource': 1, 'tracer3dsource': 0}
    if dynamics_extra:
        dynamics.update(dynamics_extra)
    mock_params['dynamics'] = dynamics


START = pendulum.datetime(2020, 1, 1, 0, 0, 0)


class TestCreateTrmaskMaskTypes:
    def test_land_no_bbox_no_relax(self, mock_params, tmp_path):
        _write_fake_geo_em(tmp_path / 'geo_em.d01.nc')
        _configure(mock_params, {'mask_type': 'land', 'relax_width': 0})

        create_trmask([1], START)

        mask = _read_trmask(tmp_path / 'trmask_d01')
        np.testing.assert_array_equal(mask, LANDMASK_2D)

    def test_ocean_no_bbox_no_relax(self, mock_params, tmp_path):
        _write_fake_geo_em(tmp_path / 'geo_em.d01.nc')
        _configure(mock_params, {'mask_type': 'ocean', 'relax_width': 0})

        create_trmask([1], START)

        mask = _read_trmask(tmp_path / 'trmask_d01')
        np.testing.assert_array_equal(mask, 1.0 - LANDMASK_2D)

    def test_all_no_bbox_no_relax(self, mock_params, tmp_path):
        _write_fake_geo_em(tmp_path / 'geo_em.d01.nc')
        _configure(mock_params, {'mask_type': 'all', 'relax_width': 0})

        create_trmask([1], START)

        mask = _read_trmask(tmp_path / 'trmask_d01')
        np.testing.assert_array_equal(mask, np.ones((SN, WE), dtype=np.float32))


class TestCreateTrmaskBbox:
    def test_ocean_plus_bbox_intersects(self, mock_params, tmp_path):
        """Bbox covers rows 2..6 across the full x range -- ocean cells
        inside that stripe should be 1, everything else 0."""
        _write_fake_geo_em(tmp_path / 'geo_em.d01.nc')
        # rows 2..6 correspond to lat indices 2..6 (LAT_1D is ascending)
        _configure(
            mock_params,
            {
                'mask_type': 'ocean',
                'relax_width': 0,
                'bbox_deg': [float(LAT_1D[2]), float(LAT_1D[6]), float(LON_1D[0]), float(LON_1D[-1])],
            },
        )

        create_trmask([1], START)

        mask = _read_trmask(tmp_path / 'trmask_d01')

        expected = np.zeros((SN, WE), dtype=np.float32)
        expected[2:7, 5:] = 1.0  # rows 2..6 AND ocean half (cols 5..9)
        np.testing.assert_array_equal(mask, expected)

    def test_all_plus_bbox_reproduces_old_bbox(self, mock_params, tmp_path):
        """mask_type='all' + bbox should equal the old bbox-only behavior."""
        _write_fake_geo_em(tmp_path / 'geo_em.d01.nc')
        _configure(
            mock_params,
            {
                'mask_type': 'all',
                'relax_width': 0,
                'bbox_deg': [float(LAT_1D[3]), float(LAT_1D[8]), float(LON_1D[1]), float(LON_1D[7])],
            },
        )

        create_trmask([1], START)

        mask = _read_trmask(tmp_path / 'trmask_d01')

        expected = np.zeros((SN, WE), dtype=np.float32)
        expected[3:9, 1:8] = 1.0
        np.testing.assert_array_equal(mask, expected)


class TestCreateTrmaskDateline:
    """Bbox restrictions on a domain crossing the antimeridian (XLONG in
    -180..180). A bbox with min_lon > max_lon wraps the dateline (OR semantics)."""

    def test_ocean_plus_bbox_wraps_dateline(self, mock_params, tmp_path):
        """min_lon=162, max_lon=-170 keeps the eastward arc 162E..-170 across
        +/-180. Intersected with ocean (cols 5..9) -> cols 5..8."""
        _write_fake_geo_em(tmp_path / 'geo_em.d01.nc', lon=LON_2D_DL)
        _configure(
            mock_params,
            {
                'mask_type': 'ocean',
                'relax_width': 0,
                'bbox_deg': [-90.0, 90.0, 162.0, -170.0],
            },
        )

        create_trmask([1], START)

        mask = _read_trmask(tmp_path / 'trmask_d01')

        expected = np.zeros((SN, WE), dtype=np.float32)
        expected[:, 5:9] = 1.0  # lon in {178, -178, -174, -170} AND ocean
        np.testing.assert_array_equal(mask, expected)

    def test_all_plus_bbox_wraps_dateline(self, mock_params, tmp_path):
        """Same wrap with mask_type='all': keep every col on the eastward arc
        162E..-170 -> cols 2..8 (lon >= 162 OR lon <= -170)."""
        _write_fake_geo_em(tmp_path / 'geo_em.d01.nc', lon=LON_2D_DL)
        _configure(
            mock_params,
            {
                'mask_type': 'all',
                'relax_width': 0,
                'bbox_deg': [-90.0, 90.0, 162.0, -170.0],
            },
        )

        create_trmask([1], START)

        mask = _read_trmask(tmp_path / 'trmask_d01')

        expected = np.zeros((SN, WE), dtype=np.float32)
        expected[:, 2:9] = 1.0  # lon 162,168,174,178,-178,-174,-170
        np.testing.assert_array_equal(mask, expected)

    def test_nonwrap_bbox_on_dateline_grid(self, mock_params, tmp_path):
        """Regression: a normal box (min_lon <= max_lon) on the same grid keeps
        AND semantics and excludes the negative-lon (east-of-dateline) columns.
        min_lon=162, max_lon=178 -> cols 2..5 only."""
        _write_fake_geo_em(tmp_path / 'geo_em.d01.nc', lon=LON_2D_DL)
        _configure(
            mock_params,
            {
                'mask_type': 'all',
                'relax_width': 0,
                'bbox_deg': [-90.0, 90.0, 162.0, 178.0],
            },
        )

        create_trmask([1], START)

        mask = _read_trmask(tmp_path / 'trmask_d01')

        expected = np.zeros((SN, WE), dtype=np.float32)
        expected[:, 2:6] = 1.0  # lon 162,168,174,178
        np.testing.assert_array_equal(mask, expected)


class TestCreateTrmaskBboxIJ:
    """Grid-index bbox: [i_min, i_max, j_min, j_max], 0-based inclusive,
    i = west-east, j = south-north. Intersected with the mask_type selection."""

    def test_bbox_ij_drops_west_columns(self, mock_params, tmp_path):
        """The real fetch-test use case: keep cols 5..9 -> drops the 5 westmost
        columns. With mask_type='all' the kept box is exactly those columns."""
        _write_fake_geo_em(tmp_path / 'geo_em.d01.nc')
        _configure(mock_params, {'mask_type': 'all', 'relax_width': 0, 'bbox_ij': [5, 9, 0, 9]})

        create_trmask([1], START)

        mask = _read_trmask(tmp_path / 'trmask_d01')
        expected = np.zeros((SN, WE), dtype=np.float32)
        expected[:, 5:] = 1.0  # cols 5..9 inclusive
        np.testing.assert_array_equal(mask, expected)

    def test_bbox_ij_intersects_ocean(self, mock_params, tmp_path):
        """i 4..8, j 2..6 (inclusive) intersected with ocean (cols 5..9) ->
        rows 2..6, cols 5..8 (col 4 is land, col 9 is outside the box)."""
        _write_fake_geo_em(tmp_path / 'geo_em.d01.nc')
        _configure(mock_params, {'mask_type': 'ocean', 'relax_width': 0, 'bbox_ij': [4, 8, 2, 6]})

        create_trmask([1], START)

        mask = _read_trmask(tmp_path / 'trmask_d01')
        expected = np.zeros((SN, WE), dtype=np.float32)
        expected[2:7, 5:9] = 1.0
        np.testing.assert_array_equal(mask, expected)

    def test_bbox_ij_single_cell_inclusive(self, mock_params, tmp_path):
        """Inclusive bounds: i_min==i_max, j_min==j_max selects exactly one cell."""
        _write_fake_geo_em(tmp_path / 'geo_em.d01.nc')
        _configure(mock_params, {'mask_type': 'all', 'relax_width': 0, 'bbox_ij': [3, 3, 7, 7]})

        create_trmask([1], START)

        mask = _read_trmask(tmp_path / 'trmask_d01')
        expected = np.zeros((SN, WE), dtype=np.float32)
        expected[7, 3] = 1.0
        np.testing.assert_array_equal(mask, expected)


class TestCreateTrmaskRelaxZone:
    def test_relax_width_applied_last(self, mock_params, tmp_path):
        _write_fake_geo_em(tmp_path / 'geo_em.d01.nc')
        _configure(mock_params, {'mask_type': 'all', 'relax_width': 2})

        create_trmask([1], START)

        mask = _read_trmask(tmp_path / 'trmask_d01')

        expected = np.zeros((SN, WE), dtype=np.float32)
        expected[2:-2, 2:-2] = 1.0
        np.testing.assert_array_equal(mask, expected)


class TestCreateTrmask3D:
    def test_3d_mask_replicates_2d(self, mock_params, tmp_path):
        _write_fake_geo_em(tmp_path / 'geo_em.d01.nc')
        mock_params['domains']['e_vert'] = 10
        _configure(
            mock_params,
            {'mask_type': 'land', 'relax_width': 0},
            dynamics_extra={'tracer3dsource': 1, 'tracer2dsource': 0},
        )

        create_trmask([1], START)

        mask3d = _read_trmask3d(tmp_path / 'trmask_d01')
        assert mask3d.shape == (9, SN, WE)  # e_vert - 1
        for k in range(mask3d.shape[0]):
            np.testing.assert_array_equal(mask3d[k], LANDMASK_2D)


class TestCreateTrmaskErrors:
    def test_bbox_mask_type_raises_migration_error(self, mock_params, tmp_path):
        _write_fake_geo_em(tmp_path / 'geo_em.d01.nc')
        _configure(mock_params, {'mask_type': 'bbox'})

        with pytest.raises(ValueError, match='no longer supported'):
            create_trmask([1], START)

    def test_legacy_scalar_bbox_keys_raise(self, mock_params, tmp_path):
        _write_fake_geo_em(tmp_path / 'geo_em.d01.nc')
        _configure(mock_params, {'mask_type': 'ocean', 'min_lat': -42.0, 'max_lat': -38.0})

        with pytest.raises(ValueError, match='no longer supported'):
            create_trmask([1], START)

    def test_both_bbox_forms_raise(self, mock_params, tmp_path):
        _write_fake_geo_em(tmp_path / 'geo_em.d01.nc')
        _configure(
            mock_params,
            {'mask_type': 'ocean', 'bbox_deg': [-45.0, -36.0, 170.0, 179.0], 'bbox_ij': [0, 4, 0, 9]},
        )

        with pytest.raises(ValueError, match='only one of bbox_deg or bbox_ij'):
            create_trmask([1], START)

    def test_bbox_wrong_length_raises(self, mock_params, tmp_path):
        _write_fake_geo_em(tmp_path / 'geo_em.d01.nc')
        _configure(mock_params, {'mask_type': 'ocean', 'bbox_ij': [5, 9, 0]})

        with pytest.raises(ValueError, match='must be a list of 4'):
            create_trmask([1], START)

    def test_bbox_ij_reversed_bounds_raise(self, mock_params, tmp_path):
        _write_fake_geo_em(tmp_path / 'geo_em.d01.nc')
        _configure(mock_params, {'mask_type': 'ocean', 'bbox_ij': [8, 4, 0, 9]})

        with pytest.raises(ValueError, match='i_min <= i_max'):
            create_trmask([1], START)

    def test_bbox_ij_out_of_range_raises(self, mock_params, tmp_path):
        _write_fake_geo_em(tmp_path / 'geo_em.d01.nc')
        _configure(mock_params, {'mask_type': 'all', 'relax_width': 0, 'bbox_ij': [0, 10, 0, 9]})

        with pytest.raises(ValueError, match='exceeds domain'):
            create_trmask([1], START)

    def test_unknown_mask_type_raises(self, mock_params, tmp_path):
        _write_fake_geo_em(tmp_path / 'geo_em.d01.nc')
        _configure(mock_params, {'mask_type': 'desert'})

        with pytest.raises(ValueError, match='Unknown mask_type'):
            create_trmask([1], START)


class TestCreateTrmaskMultiRegion:
    """v2.0 multi-region: [[wvt.regions]] -> region-dimensioned TRMASK(Time, wvt_regions, sn, we)."""

    def test_single_region_is_region_dimensioned(self, mock_params, tmp_path):
        """Even the flat single-region form writes a size-1 wvt_regions axis (v2.0 registry)."""
        _write_fake_geo_em(tmp_path / 'geo_em.d01.nc')
        _configure(mock_params, {'mask_type': 'ocean', 'relax_width': 0})
        create_trmask([1], START)
        assert _read_trmask_nreg(tmp_path / 'trmask_d01') == 1
        np.testing.assert_array_equal(_read_trmask(tmp_path / 'trmask_d01', 0), 1.0 - LANDMASK_2D)

    def test_two_disjoint_regions(self, mock_params, tmp_path):
        """Two ocean bands (west/east column splits) -> 2 disjoint regions; union = all ocean."""
        _write_fake_geo_em(tmp_path / 'geo_em.d01.nc')
        _configure(mock_params, {
            'mask_type': 'ocean',
            'relax_width': 0,
            'regions': [
                {'name': 'west', 'bbox_ij': [0, 6, 0, 9]},
                {'name': 'east', 'bbox_ij': [7, 9, 0, 9]},
            ],
        })
        create_trmask([1], START)
        path = tmp_path / 'trmask_d01'
        assert _read_trmask_nreg(path) == 2
        r1 = _read_trmask(path, 0)
        r2 = _read_trmask(path, 1)
        # ocean = cols 5..9; west bbox keeps cols 0..6 -> ocean cols 5..6; east keeps 7..9.
        exp1 = np.zeros((SN, WE), np.float32); exp1[:, 5:7] = 1.0
        exp2 = np.zeros((SN, WE), np.float32); exp2[:, 7:] = 1.0
        np.testing.assert_array_equal(r1, exp1)
        np.testing.assert_array_equal(r2, exp2)
        np.testing.assert_array_equal(np.minimum(r1, r2), np.zeros((SN, WE), np.float32))   # disjoint
        np.testing.assert_array_equal(np.maximum(r1, r2), 1.0 - LANDMASK_2D)                # union = all ocean

    def test_per_region_mask_type_override(self, mock_params, tmp_path):
        """A region overrides the inherited mask_type (ocean default + a land region)."""
        _write_fake_geo_em(tmp_path / 'geo_em.d01.nc')
        _configure(mock_params, {
            'mask_type': 'ocean',
            'relax_width': 0,
            'regions': [
                {'name': 'sea'},                        # inherits ocean
                {'name': 'land', 'mask_type': 'land'},  # override
            ],
        })
        create_trmask([1], START)
        path = tmp_path / 'trmask_d01'
        np.testing.assert_array_equal(_read_trmask(path, 0), 1.0 - LANDMASK_2D)
        np.testing.assert_array_equal(_read_trmask(path, 1), LANDMASK_2D)

    def test_overlapping_regions_raise(self, mock_params, tmp_path):
        _write_fake_geo_em(tmp_path / 'geo_em.d01.nc')
        _configure(mock_params, {
            'mask_type': 'all',
            'relax_width': 0,
            'regions': [
                {'name': 'a', 'bbox_ij': [0, 5, 0, 9]},
                {'name': 'b', 'bbox_ij': [4, 9, 0, 9]},  # cols 4..5 overlap a
            ],
        })
        with pytest.raises(ValueError, match='overlap'):
            create_trmask([1], START)

    def test_empty_region_raises(self, mock_params, tmp_path):
        """A region whose mask selects no cells is a config error."""
        _write_fake_geo_em(tmp_path / 'geo_em.d01.nc')
        _configure(mock_params, {
            'mask_type': 'ocean',
            'relax_width': 0,
            'regions': [
                {'name': 'good', 'bbox_ij': [5, 9, 0, 9]},
                {'name': 'empty', 'mask_type': 'land', 'bbox_ij': [5, 9, 0, 9]},  # land ∩ ocean-cols = empty
            ],
        })
        with pytest.raises(ValueError, match='empty mask'):
            create_trmask([1], START)


class TestCreateTrmask2DMode:
    """WVT_TRMASK_2D=1 -> flat TRMASK(Time, sn, we) for the single-region image (registry TRMASK = ij)."""

    def test_2d_mode_writes_flat_trmask(self, mock_params, tmp_path, monkeypatch):
        monkeypatch.setenv('WVT_TRMASK_2D', '1')
        _write_fake_geo_em(tmp_path / 'geo_em.d01.nc')
        _configure(mock_params, {'mask_type': 'ocean', 'relax_width': 0})

        create_trmask([1], START)

        with nc3.netcdf_file(str(tmp_path / 'trmask_d01'), 'r', mmap=False) as f:
            assert 'wvt_regions' not in f.dimensions                       # no region axis
            v = f.variables['TRMASK']
            assert v.dimensions == ('Time', 'south_north', 'west_east')    # flat 2-D layout
            np.testing.assert_array_equal(np.array(v[0, :, :]), 1.0 - LANDMASK_2D)

    def test_2d_mode_rejects_multiple_regions(self, mock_params, tmp_path, monkeypatch):
        """The flat 2-D layout has no region axis, so >1 region under WVT_TRMASK_2D is a config error."""
        monkeypatch.setenv('WVT_TRMASK_2D', '1')
        _write_fake_geo_em(tmp_path / 'geo_em.d01.nc')
        _configure(mock_params, {
            'mask_type': 'ocean',
            'relax_width': 0,
            'regions': [
                {'name': 'west', 'bbox_ij': [5, 6, 0, 9]},
                {'name': 'east', 'bbox_ij': [7, 9, 0, 9]},
            ],
        })

        with pytest.raises(ValueError, match='WVT_TRMASK_2D'):
            create_trmask([1], START)


class TestNormalizeWvtRegions:
    def test_flat_single_region(self):
        cfg = {'mask_type': 'ocean', 'bbox_deg': [-48, -34, 162, 172]}
        regs = normalize_wvt_regions(cfg)
        assert num_wvt_regions(cfg) == 1
        assert len(regs) == 1
        assert regs[0]['mask_type'] == 'ocean'
        assert regs[0]['bbox_deg'] == [-48, -34, 162, 172]

    def test_array_inheritance_and_override(self):
        cfg = {'mask_type': 'ocean', 'regions': [
            {'name': 'w', 'bbox_deg': [-48, -34, 162, 172]},
            {'name': 'l', 'mask_type': 'land'},
        ]}
        regs = normalize_wvt_regions(cfg)
        assert num_wvt_regions(cfg) == 2
        assert regs[0]['name'] == 'w' and regs[0]['mask_type'] == 'ocean'  # inherited default
        assert regs[1]['name'] == 'l' and regs[1]['mask_type'] == 'land'   # per-region override

    def test_empty_regions_array_raises(self):
        with pytest.raises(ValueError, match='non-empty array'):
            normalize_wvt_regions({'regions': []})


class TestBoundaryFaceShells:
    """Lateral-boundary face tags: the shells must tile the margin with no gap or overlap."""

    @pytest.mark.parametrize('we,sn', [(99, 111), (10, 10), (200, 150), (317, 537)])
    def test_shells_exactly_tile_the_margin(self, we, sn):
        rw = 5 if min(we, sn) > 12 else 2
        masks = {f: _build_boundary_mask(f, rw, we, sn, 'x') for f in BOUNDARY_FACES}
        total = sum(masks.values())
        j = np.arange(sn)[:, None]
        i = np.arange(we)[None, :]
        margin = np.minimum(np.minimum(i, we - 1 - i), np.minimum(j, sn - 1 - j)) < rw
        # A gap is the dangerous case: a margin cell in no shell and no source region is a
        # permanent untagged source that the remainder floor absorbs silently.
        assert (total <= 1).all(), 'shells overlap'
        assert ((total > 0) == margin).all(), 'shells do not exactly tile the margin'

    def test_corner_convention_is_pinned(self):
        # Ties go to the meridional faces. This is the convention the tiling check depends on;
        # if it changes, the numbers below change with it -- deliberately hard to alter silently.
        counts = [int(_build_boundary_mask(f, 5, 99, 111, 'x').sum()) for f in BOUNDARY_FACES]
        assert counts == [535, 535, 465, 465]

    def test_boundary_region_rejects_a_bbox(self):
        from create_trmask import _validate_region
        reg = {'name': 'west_face', 'mask_type': 'boundary', 'face': 'west',
               'bbox_deg': [-40, -30, 170, 175], 'bbox_ij': None}
        with pytest.raises(ValueError, match='takes no bbox'):
            _validate_region(reg)

    def test_face_rejected_on_non_boundary_region(self):
        from create_trmask import _validate_region
        reg = {'name': 'r', 'mask_type': 'ocean', 'face': 'west', 'bbox_deg': None, 'bbox_ij': None}
        with pytest.raises(ValueError, match='only meaningful'):
            _validate_region(reg)


class TestBoundaryFacesKey:
    """[wvt] boundary_faces -- the single switch, and the ways it could silently do nothing."""

    def test_absent_means_no_boundary_regions(self):
        regions = normalize_wvt_regions({'mask_type': 'ocean', 'regions': [{'name': 'a'}]})
        assert [r['mask_type'] for r in regions] == ['ocean']

    def test_faces_are_appended_after_the_sources(self):
        regions = normalize_wvt_regions({
            'mask_type': 'ocean',
            'regions': [{'name': 'a'}, {'name': 'b'}],
            'boundary_faces': ['west', 'east'],
        })
        assert [r['name'] for r in regions] == ['a', 'b', 'west_face', 'east_face']
        # "boundary regions are last" must hold BY CONSTRUCTION -- nothing validates it.
        assert [r['mask_type'] for r in regions] == ['ocean', 'ocean', 'boundary', 'boundary']

    def test_faces_apply_to_the_legacy_flat_form_too(self):
        # The flat single-region form returns early; appending inside the [[wvt.regions]]
        # branch alone would make the key a silent no-op here.
        regions = normalize_wvt_regions({'mask_type': 'ocean', 'boundary_faces': ['north']})
        assert [r['name'] for r in regions] == ['region_01', 'north_face']

    def test_hand_declared_boundary_region_is_refused(self):
        with pytest.raises(ValueError, match='boundary_faces'):
            normalize_wvt_regions({'regions': [{'name': 'x', 'mask_type': 'boundary'}]})

    def test_unknown_or_duplicate_face_is_refused(self):
        with pytest.raises(ValueError, match='unknown face'):
            normalize_boundary_faces({'boundary_faces': ['up']})
        with pytest.raises(ValueError, match='more than once'):
            normalize_boundary_faces({'boundary_faces': ['west', 'west']})

    def test_string_instead_of_list_is_refused(self):
        # 'west' would otherwise iterate as characters and produce four bogus faces.
        with pytest.raises(ValueError, match='must be an array'):
            normalize_boundary_faces({'boundary_faces': 'west'})


class TestEnclosedWaterFill:
    """Tier-1 fill: enclosed water becomes land in the tool's own derived landmask."""

    def test_interior_lake_is_filled(self):
        lm = np.zeros((12, 12))
        lm[3:9, 3:9] = 1.0
        lm[5:7, 5:7] = 0.0
        out, n = fill_enclosed_water(lm)
        assert n == 4 and out[3:9, 3:9].all()
        assert out[0, 0] == 0.0, 'open ocean must stay ocean'

    def test_water_open_to_the_sea_is_not_filled(self):
        lm = np.zeros((12, 12))
        lm[3:9, 3:9] = 1.0
        lm[5:7, 0:6] = 0.0          # channel out to the west edge
        assert fill_enclosed_water(lm)[1] == 0

    def test_connectivity_is_four_not_eight(self):
        # The interior water cell (2,2) reaches the open ocean ONLY through the diagonal gap at
        # (1,1). Under 4-connectivity that is not a path, so it stays enclosed and is filled.
        # Under 8-connectivity it leaks and nothing is filled.
        #
        #   . . . . .      . = open ocean
        #   . o L L .      o = the diagonal gap at (1,1)
        #   . L w L .      w = interior water at (2,2)
        #   . L L L .      L = land
        #   . . . . .
        #
        # This case is the whole point of pinning the convention: an earlier version of this
        # test used two diagonally-adjacent interior cells, which are enclosed under BOTH
        # conventions, so it passed with 8-connectivity substituted and tested nothing.
        lm = np.zeros((5, 5))
        lm[1:4, 1:4] = 1.0
        lm[2, 2] = 0.0      # interior water
        lm[1, 1] = 0.0      # diagonal gap to the open ocean
        assert fill_enclosed_water(lm)[1] == 1

    def test_input_is_not_mutated(self):
        lm = np.zeros((12, 12))
        lm[3:9, 3:9] = 1.0
        lm[5:7, 5:7] = 0.0
        before = lm.copy()
        fill_enclosed_water(lm)
        assert np.array_equal(lm, before), 'must never mutate the caller\'s landmask'

    def test_degenerate_grids(self):
        assert fill_enclosed_water(np.ones((5, 5)))[1] == 0
        assert fill_enclosed_water(np.zeros((5, 5)))[1] == 0


class TestBoundaryFacesEndToEnd:
    """create_trmask() driven all the way through with boundary_faces set.

    A dual-blind review showed the unit tests above pass with the tiling check DISABLED and
    with the boundary early-return REMOVED, because nothing exercised the integration wiring.
    Testing `_build_boundary_mask` in isolation does not test create_trmask using it.
    """

    RW = 2  # the fixture grid is 10x10, so a 2-cell margin leaves a 6x6 interior

    def _cfg(self, mock_params, faces, **extra):
        _configure(mock_params, {'mask_type': 'ocean', 'relax_width': self.RW,
                                 'regions': [{'name': 'sea'}],
                                 'boundary_faces': faces, **extra})

    def test_twelve_regions_written_and_shells_tile_the_margin(self, mock_params, tmp_path):
        _write_fake_geo_em(tmp_path / 'geo_em.d01.nc')
        self._cfg(mock_params, list(BOUNDARY_FACES))
        create_trmask([1], START)

        masks = np.stack([_read_trmask(tmp_path / 'trmask_d01', region=r) for r in range(5)])
        margin = np.zeros((SN, WE), dtype=bool)
        margin[:self.RW, :] = margin[-self.RW:, :] = True
        margin[:, :self.RW] = margin[:, -self.RW:] = True

        shells = masks[1:]
        # The early return is what lets the shells occupy the margin at all: without it the
        # generic path zeroes exactly these cells and the region comes out empty.
        assert (shells.sum(axis=0) > 0).sum() == margin.sum()
        np.testing.assert_array_equal(shells.sum(axis=0) > 0, margin)
        assert (shells.sum(axis=0) <= 1).all(), 'shells overlap'
        # and the source region still avoids the margin entirely
        assert not (masks[0] > 0)[margin].any()

    def test_a_partial_face_list_is_allowed_but_announced(self, mock_params, tmp_path, capsys):
        # Fewer faces is a legitimate cost choice (~0.4 node-days per face per sim-year), so
        # it must not be an error -- but the untagged margin has to be reported, or the larger
        # untagged remainder downstream looks like a physics result rather than a config choice.
        _write_fake_geo_em(tmp_path / 'geo_em.d01.nc')
        self._cfg(mock_params, ['west', 'east', 'south'])
        create_trmask([1], START)
        err = capsys.readouterr().err
        assert 'in NO shell' in err and '3 of 4 faces' in err

    def test_shells_may_not_escape_the_margin(self, mock_params, tmp_path):
        # A shell wider than the margin must be refused. Which check catches it depends on the
        # source regions: with a full-domain source it overlaps one, so the pre-existing
        # disjointness check fires first; if the sources had a bbox leaving those cells free,
        # nothing but the margin check would notice. Assert the property, not the messenger.
        _write_fake_geo_em(tmp_path / 'geo_em.d01.nc')
        self._cfg(mock_params, list(BOUNDARY_FACES))
        import create_trmask as ct
        orig = ct._build_boundary_mask
        try:
            ct._build_boundary_mask = lambda f, rw, we, sn, n: orig(f, rw + 1, we, sn, n)
            with pytest.raises(ValueError, match='overlap|OUTSIDE the margin'):
                create_trmask([1], START)
        finally:
            ct._build_boundary_mask = orig

    def test_margin_check_catches_an_escape_the_overlap_check_cannot(self, mock_params, tmp_path):
        # The case that isolates the new check: the source region is bbox-restricted to the
        # interior, so an over-wide shell overlaps nothing and the disjointness check is blind.
        _write_fake_geo_em(tmp_path / 'geo_em.d01.nc')
        _configure(mock_params, {'mask_type': 'ocean', 'relax_width': self.RW,
                                 'regions': [{'name': 'sea', 'bbox_ij': [4, 5, 4, 5]}],
                                 'boundary_faces': list(BOUNDARY_FACES)})
        import create_trmask as ct
        orig = ct._build_boundary_mask
        try:
            ct._build_boundary_mask = lambda f, rw, we, sn, n: orig(f, rw + 1, we, sn, n)
            with pytest.raises(ValueError, match='OUTSIDE the margin'):
                create_trmask([1], START)
        finally:
            ct._build_boundary_mask = orig


class TestEnclosedWaterFillEndToEnd:
    def test_open_ocean_is_the_largest_component_not_the_first(self, tmp_path):
        # The fill's whole safety is "the largest water component is the sea". On a grid whose
        # first raster row is land, a lake gets label 1 and the sea gets label 2, so an
        # implementation that assumed label 1 would reclassify the ENTIRE OCEAN as land and
        # every existing test would still pass.
        lm = np.zeros((10, 10))
        lm[0:4, :] = 1.0        # land band across the top -- so the lake is found first
        lm[1:3, 1:3] = 0.0      # 4-cell lake inside that band
        out, n = fill_enclosed_water(lm)
        assert n == 4, 'the lake should be filled'
        assert out[0:4, :].all(), 'the land band stays land'
        assert not out[5:, :].any(), 'the open ocean must NOT be reclassified as land'


class TestMarginGeometry:
    """margin_geometry is the ONE margin definition; these pin it against independent witnesses."""

    @pytest.mark.parametrize('we,sn,rw', [(99, 111, 5), (10, 10, 2), (200, 150, 5), (317, 537, 5)])
    def test_dist_matches_the_literal_margin(self, we, sn, rw):
        # The witness is written out in full ON PURPOSE. Do not "simplify" it to call the
        # helper -- a test that derives its expectation from the thing under test cannot fail.
        from create_trmask import margin_geometry
        j = np.arange(sn)[:, None]
        i = np.arange(we)[None, :]
        literal = np.minimum(np.minimum(i, we - 1 - i), np.minimum(j, sn - 1 - j)) < rw
        assert np.array_equal(margin_geometry(we, sn)['dist'] < rw, literal)

    def test_shell_union_equals_dist_margin(self):
        from create_trmask import margin_geometry
        we, sn, rw = 99, 111, 5
        union = sum(_build_boundary_mask(f, rw, we, sn, 'x') for f in BOUNDARY_FACES) > 0
        assert np.array_equal(union, margin_geometry(we, sn)['dist'] < rw)


class TestResolveRelaxWidth:
    """The margin width has ONE default, shared with the namelist writer."""

    def test_default_is_the_defaults_module_constant(self, monkeypatch):
        # ⚠ Comparing against the constant is NOT enough: the constant is 5 and so was the old
        # literal, so `resolve_relax_width({}, {}) == defaults[...]` passed with the literal
        # restored (mutation-tested 2026-09-07 -- 141 passed with `default = 5` in place). The
        # test must MOVE the constant and require the resolver to follow it.
        import defaults
        from create_trmask import resolve_relax_width
        assert resolve_relax_width({}, {}) == defaults.WRF_BDY_CONTROL_DEFAULTS['spec_bdy_width']
        monkeypatch.setitem(defaults.WRF_BDY_CONTROL_DEFAULTS, 'spec_bdy_width', 7)
        assert resolve_relax_width({}, {}) == 7, 'the fallback is not read from defaults.py'

    def test_precedence_wvt_over_bdy_control_over_default(self):
        from create_trmask import resolve_relax_width
        assert resolve_relax_width({}, {'spec_bdy_width': 10}) == 10
        assert resolve_relax_width({'relax_width': 3}, {'spec_bdy_width': 10}) == 3

    def test_per_domain_list_is_read_at_d01(self):
        # set_params._first de-lists the namelist side; the mask side used to take the raw list
        # and mask[:relax_width] raised TypeError while the width guard passed.
        from create_trmask import resolve_relax_width
        assert resolve_relax_width({}, {'spec_bdy_width': [10, 10]}) == 10
        assert resolve_relax_width({'relax_width': [4, 4]}, {}) == 4
        assert isinstance(resolve_relax_width({}, {'spec_bdy_width': [10, 10]}), int)


class TestFaceSidesArePinned:
    """Which SIDE a named face lands on. Every other witness is symmetric under a west/east or
    south/north swap (counts 535/535/465/465, the union, the distance grid), so a swap inside
    margin_geometry passed 141 tests (review round redund-code-1). These are not."""

    def test_west_is_column_zero_and_south_is_row_zero(self):
        we, sn, rw = 99, 111, 5
        w = _build_boundary_mask('west', rw, we, sn, 'x') > 0
        e = _build_boundary_mask('east', rw, we, sn, 'x') > 0
        s = _build_boundary_mask('south', rw, we, sn, 'x') > 0
        n = _build_boundary_mask('north', rw, we, sn, 'x') > 0
        assert w[sn // 2, 0] and not e[sn // 2, 0] and e[sn // 2, we - 1] and not w[sn // 2, we - 1]
        assert s[0, we // 2] and not n[0, we // 2] and n[sn - 1, we // 2] and not s[sn - 1, we // 2]

    def test_boundary_faces_are_normalised_to_canonical_order(self):
        assert normalize_boundary_faces({'boundary_faces': ['north', 'west']}) == ['west', 'north']
        assert normalize_boundary_faces({'boundary_faces': ['south', 'east', 'north', 'west']}) \
            == list(BOUNDARY_FACES)
