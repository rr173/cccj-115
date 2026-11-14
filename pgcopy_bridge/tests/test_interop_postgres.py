"""Real PostgreSQL interoperability -- opt-in, never silently skipped.

Set ``PGCOPY_BRIDGE_PSQL`` to a psql command (or leave PATH containing psql)
and the tests will:

1. create a scratch schema, load the hand-written material with the
   server's own binary COPY reader and compare values in SQL;
2. load our encoder's output the same way;
3. COPY TO a server-side file, decode it with our decoder, and compare with
   the independently written expectations;
4. decode a server-produced file with our codec and compare.

If no server is reachable, every test is skipped and the reason is printed,
so no run can claim interoperability it did not perform.  The same steps
are available as ``materials/generated/interop.sql`` for a DBA to run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

import pytest

from pgcopy_bridge import load_catalogue
from pgcopy_bridge.library import decode_file, encode_file

PSQL = os.environ.get("PGCOPY_BRIDGE_PSQL") or shutil.which("psql")
HOST = os.environ.get("PGCOPY_BRIDGE_PGHOST", "")
PORT = os.environ.get("PGCOPY_BRIDGE_PGPORT", "5432")
USER = os.environ.get("PGCOPY_BRIDGE_PGUSER", "")
SERVER_FILE_DIR = os.environ.get("PGCOPY_BRIDGE_SERVER_DIR")

pytestmark = pytest.mark.skipif(
    PSQL is None, reason="no psql on PATH; set PGCOPY_BRIDGE_PSQL to run")


def _env():
    return {**os.environ, "PGPASSWORD": os.environ.get("PGPASSWORD", "")}


def _psql(sql: str, *args: str):
    cmd = [PSQL, "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1"]
    if HOST:
        cmd += ["-h", HOST]
    if PORT:
        cmd += ["-p", PORT]
    if USER:
        cmd += ["-U", USER]
    cmd += list(args) + ["-c", sql]
    return subprocess.run(cmd, capture_output=True, text=True, env=_env(),
                          check=False)


def _server_available():
    if not PSQL:
        return False
    r = _psql("SELECT 1")
    return r.returncode == 0


@pytest.fixture(scope="module")
def pg(materials_dir):
    if not _server_available():
        pytest.skip(f"psql at {PSQL} cannot reach a server; interoperability "
                    f"not verified in this environment")
    _psql("DROP SCHEMA IF EXISTS pgcopy_bridge_it CASCADE")
    _psql("CREATE SCHEMA pgcopy_bridge_it")
    yield materials_dir
    _psql("DROP SCHEMA IF EXISTS pgcopy_bridge_it CASCADE")


def _server_path(directory, name):
    if not SERVER_FILE_DIR:
        pytest.skip("COPY ... TO/FROM file requires a server-visible "
                    "directory; set PGCOPY_BRIDGE_SERVER_DIR")
    return os.path.join(SERVER_FILE_DIR, name)


def test_handwritten_material_loads_in_postgresql(pg):
    material = os.path.join(pg, "sample.copy")
    target = _server_path(pg, "sample.copy")
    shutil.copy(material, target)
    table = ("id int4, neg int8, label text, raw bytea, uid uuid, "
             "amount numeric, zero4 numeric, d date, ts timestamp, "
             "tz timestamptz, grid int4[], notes text[], ranks numeric[]")
    script = f"""
    SET search_path = pgcopy_bridge_it, public;
    CREATE TABLE s ({table});
    COPY s FROM '{target}' WITH (FORMAT binary);
    SELECT count(*) FROM s;
    SELECT bool_and(id IS NULL AND neg = -9007199254740993 AND label = ''
      AND raw = '\\x00ff1080'::bytea
      AND uid = '123e4567-e89b-12d3-a456-426614174000'::uuid
      AND amount = 123456789012345678901234567890.1234567890000
      AND zero4 = 0.0000 AND d = '-infinity'::date
      AND ts = 'infinity'::timestamp AND tz = '-infinity'::timestamptz
      AND grid IS NULL AND notes IS NULL AND ranks IS NULL)
    FROM s WHERE id IS NULL;
    SELECT bool_and(neg = 0 AND label = 'plain' AND raw = '\\x'::bytea
      AND amount = -1.2300 AND zero4 = 'NaN'::numeric
      AND d = '2000-01-01' AND ts = '2000-01-01 00:00:00'
      AND tz = '2000-01-01 01:00:00+00'
      AND grid::text = '[0:1][-3:-2]={{{{11,NULL}},{{13,14}}}}'
      AND notes::text = '[0:1][-3:-2]={{{{"",NULL}},{{ok,""}}}}')
    FROM s WHERE id = 0;
    DROP TABLE s;
    """
    r = _psql(script)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.split()
    assert lines[-3:] == ["2", "t", "t"], r.stdout


def test_our_encoder_output_loads_in_postgresql(pg, tmp_path):
    catalogue = os.path.join(pg, "catalogue.json")
    out = tmp_path / "ours.copy"
    encode_file(os.path.join(pg, "sample.jsonl"), catalogue, str(out))
    target = _server_path(pg, "ours.copy")
    shutil.copy(str(out), target)
    script = f"""
    SET search_path = pgcopy_bridge_it, public;
    CREATE TABLE o (id int4, neg int8, label text, raw bytea, uid uuid,
      amount numeric, zero4 numeric, d date, ts timestamp, tz timestamptz,
      grid int4[], notes text[], ranks numeric[]);
    COPY o FROM '{target}' WITH (FORMAT binary);
    SELECT count(*) FROM o;
    SELECT (SELECT count(*) FROM (SELECT amount, zero4, d, ts, tz, grid,
      notes, ranks FROM o EXCEPT SELECT amount, zero4, d, ts, tz, grid,
      notes, ranks FROM s2) x);
    DROP TABLE o;
    """
    # s2 is created by the server round-trip test; create it here too so
    # this test stands alone.
    prepared = f"""
    SET search_path = pgcopy_bridge_it, public;
    DROP TABLE IF EXISTS pgcopy_bridge_it.s2;
    CREATE TABLE pgcopy_bridge_it.s2 (id int4, neg int8, label text,
      raw bytea, uid uuid, amount numeric, zero4 numeric, d date,
      ts timestamp, tz timestamptz, grid int4[], notes text[],
      ranks numeric[]);
    COPY pgcopy_bridge_it.s2 FROM
      '{_server_path(pg, "sample.copy")}' WITH (FORMAT binary);
    """
    r = _psql(prepared)
    assert r.returncode == 0, r.stderr
    r = _psql(script)
    assert r.returncode == 0, r.stderr
    assert "2" in r.stdout.split()


def test_server_written_copy_decodes_with_our_reader(pg, tmp_path):
    server_file = _server_path(pg, "roundtrip.copy")
    script = f"""
    SET search_path = pgcopy_bridge_it, public;
    DROP TABLE IF EXISTS rt;
    CREATE TABLE rt (id int4, neg int8, label text, raw bytea, uid uuid,
      amount numeric, zero4 numeric, d date, ts timestamp, tz timestamptz,
      grid int4[], notes text[], ranks numeric[]);
    COPY rt FROM '{_server_path(pg, "sample.copy")}' WITH (FORMAT binary);
    COPY rt TO '{server_file}' WITH (FORMAT binary);
    """
    r = _psql(script)
    assert r.returncode == 0, r.stderr
    local = tmp_path / "from_server.copy"
    shutil.copy(server_file, str(local))
    out = tmp_path / "decoded.jsonl"
    decode_file(str(local), os.path.join(pg, "catalogue.json"), str(out),
                chunk_size=1)
    expected = open(os.path.join(pg, "sample.jsonl"), "rb").read()
    assert out.read_bytes() == expected


def test_server_normalises_our_negative_zero_scale(pg, tmp_path):
    """0.0000 survives: PostgreSQL prints four decimals after loading."""
    server_file = _server_path(pg, "zeroscale.copy")
    catalogue = os.path.join(pg, "catalogue.json")
    out = tmp_path / "ours.copy"
    encode_file(os.path.join(pg, "sample.jsonl"), catalogue, str(out))
    shutil.copy(str(out), _server_path(pg, "sample.copy"))
    script = f"""
    SET search_path = pgcopy_bridge_it;
    DROP TABLE IF EXISTS z;
    CREATE TABLE z (zero4 numeric, amount numeric);
    COPY z FROM '{_server_path(pg, "sample.copy")}' WITH (FORMAT binary);
    SELECT zero4::text, scale(zero4) FROM z WHERE id IS NULL;
    """
    # id is not in z; just read the first tuple's zero4 by ordering rows
    # via a CTE table instead.
    script = f"""
    SET search_path = pgcopy_bridge_it;
    DROP TABLE IF EXISTS zz;
    CREATE TABLE zz (id int4, neg int8, label text, raw bytea, uid uuid,
      amount numeric, zero4 numeric, d date, ts timestamp, tz timestamptz,
      grid int4[], notes text[], ranks numeric[]);
    COPY zz FROM '{_server_path(pg, "sample.copy")}' WITH (FORMAT binary);
    SELECT zero4::text || '|' || scale(zero4)::text FROM zz
      WHERE id IS NULL;
    DROP TABLE zz;
    """
    r = _psql(script)
    assert r.returncode == 0, r.stderr
    assert "0.0000|4" in r.stdout


def test_server_reports_environment(pg):
    r = _psql("SELECT version()")
    print("connected to:", r.stdout.strip())
    assert r.stdout.startswith("PostgreSQL ")
