"""Streaming / memory-boundary tests and CLI smoke tests."""

from __future__ import annotations

import os
import resource
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dwca import ArchiveDataError, DwcaLibrary, import_archive  # noqa: E402

from . import materials as M

MEAS_ID = "ext:" + M.MEAS


def _build_scaled_archive(path: str, n_cores: int, ext_per_core: int) -> None:
    """Wide-text archive whose total record text grows with n_cores while
    the parser may only ever hold one batch."""
    core_header = ["id", M.OCC_TERM, M.CAT_TERM]
    core_rows = [core_header] + [
        [f"K{i:08d}", f"OCC-{i}", "x" * 200] for i in range(n_cores)
    ]
    core_data = M.csv_bytes(core_rows)

    meas_header = ["coreid", M.MEAS_TERM, M.MEAS_VAL]
    meas_rows = [meas_header]
    for i in range(n_cores):
        for j in range(ext_per_core):
            meas_rows.append([
                f"K{i:08d}", f"type-{j}", "y" * 200,
            ])
    meas_data = M.csv_bytes(meas_rows)

    meta = M.META.format(
        core=M.core_xml(
            "core.csv",
            [M.field_xml(M.OCC_TERM, 1), M.field_xml(M.CAT_TERM, 2)], 0),
        extensions=M.ext_xml(
            M.MEAS, "meas.csv",
            [M.field_xml(M.MEAS_TERM, 1), M.field_xml(M.MEAS_VAL, 2)], 0),
    )
    M.write_zip(
        path, [("core.csv", core_data), ("meas.csv", meas_data)], meta
    )


def _rss_mb() -> float:
    # ru_maxrss is KB on Linux.
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def test_memory_does_not_grow_with_record_text(tmp_path):
    small_zip = tmp_path / "small.zip"
    small_db = tmp_path / "small.db"
    big_zip = tmp_path / "big.zip"
    big_db = tmp_path / "big.db"

    _build_scaled_archive(str(small_zip), n_cores=500, ext_per_core=4)
    _build_scaled_archive(str(big_zip), n_cores=20_000, ext_per_core=4)

    import_archive(str(small_zip), str(small_db))
    rss_after_small = _rss_mb()

    import_archive(str(big_zip), str(big_db))
    rss_after_big = _rss_mb()

    # The big archive holds roughly 40x more record text; a non-streaming
    # implementation would balloon RSS by hundreds of MB.  A 64 MB allowance
    # covers interpreter/cache noise while proving text is not accumulated.
    assert rss_after_big - rss_after_small < 64.0

    # And counts still reconcile at full scale.
    with DwcaLibrary.open(str(big_db)) as lib:
        counts = {t.table_id: t for t in lib.table_counts()}
        assert counts["core"].input_count == 20_000
        assert counts["core"].valid_count == 20_000
        assert counts[MEAS_ID].input_count == 80_000
        assert counts[MEAS_ID].valid_count == 80_000

        # Streaming read of the whole base stays bounded as well.
        seen = 0
        for rec in lib.iter_core():
            seen += 1
            assert len(rec.extensions[MEAS_ID]) == 4
        assert seen == 20_000


def test_iter_core_is_lazy(tmp_path):
    z = tmp_path / "lazy.zip"
    d = tmp_path / "lazy.db"
    _build_scaled_archive(str(z), n_cores=2_000, ext_per_core=2)
    import_archive(str(z), str(d))
    with DwcaLibrary.open(str(d)) as lib:
        gen = lib.iter_core()
        first = next(gen)
        assert first.core_id == "K00000000"
        # Consuming just one record must work without draining the iterator.
        second = next(gen)
        assert second.core_id == "K00000001"


def test_undecodable_member_fails_cleanly(tmp_path):
    # Declared Latin-1 would decode anything; instead declare UTF-8 and feed
    # a lone continuation byte.
    data = b"id,x\nK1,\xff\xfe\n"
    meta = M.META.format(
        core=M.core_xml(
            "core.csv", [M.field_xml(M.OCC_TERM, 1)], 0,
            encoding="UTF-8"),
        extensions="",
    )
    z = tmp_path / "enc.zip"
    d = tmp_path / "enc.db"
    M.write_zip(str(z), [("core.csv", data)], meta)
    with pytest.raises(ArchiveDataError):
        import_archive(str(z), str(d))
    assert not os.path.exists(str(d))


def test_cli_import_inspect_and_overwrite_guard(tmp_path):
    z = tmp_path / "cli.zip"
    d = tmp_path / "cli.db"
    M.build_feature_archive(str(z))
    pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # -s keeps user site disabled while still honoring PYTHONPATH (unlike
    # -I, which ignores it); PYTHONPATH points only at this package root.
    run = lambda *args: subprocess.run(
        [sys.executable, "-s", "-m", "dwca.cli", *args],
        cwd=pkg_root,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": pkg_root},
    )

    proc = run("import", str(z), str(d))
    assert proc.returncode == 0, proc.stderr
    assert "input" in proc.stdout and "quarantined" in proc.stdout
    assert "core" in proc.stdout

    # Refuse to overwrite.
    proc = run("import", str(z), str(d))
    assert proc.returncode == 2
    assert "refusing to overwrite" in proc.stderr

    # Inspect works offline (ZIP removed).
    os.remove(str(z))
    proc = run("inspect", str(d))
    assert proc.returncode == 0, proc.stderr
    assert "quarantine: core" in proc.stdout
    assert "duplicate_key" in proc.stdout
