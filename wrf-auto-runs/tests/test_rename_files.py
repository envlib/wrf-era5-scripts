"""Coverage for utils.rename_files and the download-side spelling dedupe.

rename_files is the single point where archived output filenames are decided: domain
renumbering and the ':' -> '_' rewrite both land here (the colon rule is injected once in
monitor_wrf). It had no tests at all until this file, which is why the two failure modes
below -- both silent, both destructive -- went unnoticed in production code.
"""

import os

import pytest

import utils


def _touch(tmp_path, *names):
    paths = []
    for n in names:
        p = tmp_path / n
        p.write_text(n)  # content == original name, so we can prove which file survived
        paths.append(str(p))
    return paths


def _names(tmp_path):
    return sorted(p.name for p in tmp_path.iterdir())


# ---------------------------------------------------------------- the colon rule


def test_colon_rule_rewrites_time_field(tmp_path):
    files = _touch(tmp_path, 'wrfout_d01_2023-01-02_00:00:00.nc')
    out = utils.rename_files(files, {'_d01_': '_d01_', ':': '_'})

    assert _names(tmp_path) == ['wrfout_d01_2023-01-02_00_00_00.nc']
    assert [os.path.basename(p) for p in out] == ['wrfout_d01_2023-01-02_00_00_00.nc']


def test_returned_paths_all_exist(tmp_path):
    """The pre-fix implementation returned the pre-rename path alongside the real one.

    That phantom went into ul_output_files' --files-from-raw list, so rclone exited non-zero,
    local files were never deleted, and the post-loop upload reported success anyway.
    """
    files = _touch(tmp_path, 'wrfout_d01_2023-01-02_00:00:00.nc', 'wrfxtrm_d01_2023-01-02_00:00:00.nc')
    out = utils.rename_files(files, {'_d01_': '_d01_', ':': '_'})

    assert len(out) == 2
    for p in out:
        assert os.path.exists(p), f'returned a path that does not exist: {p}'


# ------------------------------------------------- rules must match the ORIGINAL name


def test_chained_domain_rules_do_not_collide(tmp_path):
    """Regression: the nested-ndown map chains ({'_d01_': '_d02_', '_d02_': '_d03_'}).

    Testing each rule against the partially-rewritten name sends the d01 file through the d02
    rule as well, landing it on d03 on top of the real d02 file -- os.rename overwrites
    silently on POSIX, so a domain's entire output is destroyed with no error.
    """
    files = _touch(
        tmp_path,
        'wrfout_d01_2023-01-02_00:00:00.nc',
        'wrfout_d02_2023-01-02_00:00:00.nc',
    )
    rename_dict = {'_d01_': '_d02_', '_d02_': '_d03_', ':': '_'}

    out = utils.rename_files(files, rename_dict)

    assert _names(tmp_path) == [
        'wrfout_d02_2023-01-02_00_00_00.nc',
        'wrfout_d03_2023-01-02_00_00_00.nc',
    ]
    assert len(out) == 2, 'a file was lost'
    # Prove the CONTENTS moved to the right slot, not merely that two files exist.
    assert (tmp_path / 'wrfout_d02_2023-01-02_00_00_00.nc').read_text() == 'wrfout_d01_2023-01-02_00:00:00.nc'
    assert (tmp_path / 'wrfout_d03_2023-01-02_00_00_00.nc').read_text() == 'wrfout_d02_2023-01-02_00:00:00.nc'


def test_descending_sort_protects_slot_reuse(tmp_path):
    """Same chain without the colon rule -- the ordering guarantee on its own."""
    files = _touch(
        tmp_path,
        'wrfout_d01_2023-01-02_00_00_00.nc',
        'wrfout_d02_2023-01-02_00_00_00.nc',
    )
    out = utils.rename_files(files, {'_d01_': '_d02_', '_d02_': '_d03_'})

    assert len(out) == 2
    assert (tmp_path / 'wrfout_d02_2023-01-02_00_00_00.nc').read_text() == 'wrfout_d01_2023-01-02_00_00_00.nc'
    assert (tmp_path / 'wrfout_d03_2023-01-02_00_00_00.nc').read_text() == 'wrfout_d02_2023-01-02_00_00_00.nc'


# ------------------------------------------------------- unmatched files are returned


def test_already_renamed_file_is_returned_on_retry(tmp_path):
    """Regression: an upload failure leaves files renamed on disk (deletion needs rclone exit 0).

    The next poll re-selects them, by which point no rule matches. Dropping them from the return
    value strands them locally forever -- never uploaded, never deleted, never reported.
    """
    files = _touch(tmp_path, 'wrfout_d02_2023-01-02_00_00_00.nc')
    out = utils.rename_files(files, {'_d01_': '_d02_', ':': '_'})

    assert [os.path.basename(p) for p in out] == ['wrfout_d02_2023-01-02_00_00_00.nc']
    assert os.path.exists(out[0])


def test_empty_rename_dict_passes_files_through(tmp_path):
    files = _touch(tmp_path, 'wrfout_d01_2023-01-02_00:00:00.nc')
    assert utils.rename_files(files, {}) == files


# ------------------------------------------------------------- wrfrst must not be touched


def test_wrfrst_is_not_renamed(tmp_path):
    """Pins the design decision, so a later refactor cannot quietly route restarts through here.

    A wrfrst must return to run_path under exactly the name wrf.exe reconstructs for itself.
    In production it cannot reach rename_files at all (query_out_files prefix-filters to
    wrfout_d/wrfxtrm_d/wrfzlevels_d), but the colon rule WOULD match its name if it did.
    """
    files = _touch(tmp_path, 'wrfrst_d01_2023-01-02_00:00:00')
    out = utils.rename_files(files, {'_d01_': '_d01_'})

    assert _names(tmp_path) == ['wrfrst_d01_2023-01-02_00:00:00']
    assert [os.path.basename(p) for p in out] == ['wrfrst_d01_2023-01-02_00:00:00']


def test_query_out_files_excludes_wrfrst(tmp_path):
    """The structural guarantee behind the test above."""
    _touch(
        tmp_path,
        'wrfout_d01_2023-01-02_00:00:00.nc',
        'wrfrst_d01_2023-01-02_00:00:00',
    )
    found = utils.query_out_files(tmp_path)
    assert set(found) == {('wrfout', 'd01')}


def test_query_out_files_handles_both_spellings(tmp_path):
    _touch(
        tmp_path,
        'wrfout_d01_2023-01-02_00:00:00.nc',
        'wrfout_d01_2023-01-03_00_00_00.nc',
    )
    found = utils.query_out_files(tmp_path)
    assert len(found[('wrfout', 'd01')]) == 2


# ------------------------------------------------- the composition monitor_wrf performs


def test_poll_cycle_composition(tmp_path):
    """query_out_files -> select_files_to_ul -> rename_files, with the colon rule injected.

    This is everything monitor_wrf does between polling and handing paths to rclone, minus the
    subprocess. The colon rule is injected in monitor_wrf itself (one choke point covering all
    four rename_dict construction sites), so it is spelled out here rather than imported.
    """
    _touch(
        tmp_path,
        'wrfout_d01_2023-01-02_00:00:00.nc',
        'wrfout_d01_2023-01-03_00:00:00.nc',
        'wrfzlevels_d01_2023-01-02_00:00:00.nc',
        'wrfzlevels_d01_2023-01-03_00:00:00.nc',
        'wrfrst_d01_2023-01-02_00:00:00',
        'namelist.input',
    )

    rename_dict = {**{'_d01_': '_d01_'}, ':': '_'}  # mirrors monitor_wrf's injection

    found = utils.query_out_files(tmp_path, include_xtrm=True)
    # min_files=1 skips the newest of EACH group -- the file WRF is still writing.
    selected = utils.select_files_to_ul(found, 1)
    uploaded = sorted(os.path.basename(p) for p in utils.rename_files(selected, rename_dict))

    # The archive-bound names are colon-free...
    assert uploaded == ['wrfout_d01_2023-01-02_00_00_00.nc', 'wrfzlevels_d01_2023-01-02_00_00_00.nc']
    # ...the in-progress files are untouched on disk, still colon-named...
    assert (tmp_path / 'wrfout_d01_2023-01-03_00:00:00.nc').exists()
    assert (tmp_path / 'wrfzlevels_d01_2023-01-03_00:00:00.nc').exists()
    # ...and the restart file never entered the pipeline at all.
    assert (tmp_path / 'wrfrst_d01_2023-01-02_00:00:00').exists()
    assert 'wrfrst' not in ' '.join(uploaded)


# ------------------------------------------------------------------ download-side dedupe


def test_dl_include_names_emits_both_spellings():
    import pendulum

    dts = [pendulum.datetime(2023, 1, 2)]
    names = utils.dl_include_names('wrfout_d01_{date}.nc', dts)
    assert names == [
        'wrfout_d01_2023-01-02_00:00:00.nc',
        'wrfout_d01_2023-01-02_00_00_00.nc',
    ]


def test_dedupe_dl_listing_prefers_underscore(capsys):
    listing = [
        'wrfout_d01_2023-01-02_00:00:00.nc',
        'wrfout_d01_2023-01-02_00_00_00.nc',
        'wrfout_d01_2023-01-03_00:00:00.nc',
    ]
    out = utils.dedupe_dl_listing(listing)

    assert out == [
        'wrfout_d01_2023-01-02_00_00_00.nc',
        'wrfout_d01_2023-01-03_00:00:00.nc',
    ]
    assert 'WARNING' in capsys.readouterr().out


def test_dedupe_dl_listing_is_order_independent():
    a = ['wrfout_d01_2023-01-02_00:00:00.nc', 'wrfout_d01_2023-01-02_00_00_00.nc']
    assert utils.dedupe_dl_listing(a) == utils.dedupe_dl_listing(list(reversed(a)))


@pytest.mark.parametrize('listing', [[], ['wrfout_d01_2023-01-02_00_00_00.nc']])
def test_dedupe_dl_listing_passthrough(listing):
    assert utils.dedupe_dl_listing(listing) == listing
