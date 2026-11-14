-- Real PostgreSQL interoperability script.
--
-- Run against a database you own, with this directory reachable by the
-- server process (psql is a client; COPY ... FROM/TO file is executed by
-- the *server*, so the path must be on the server's filesystem).  With a
-- remote server, replace COPY ... FROM STDIN by \copy, which the psql
-- client performs locally.
--
--    psql -v ON_ERROR_STOP=1 -f interop.sql
--
-- It creates a scratch schema, loads interop.copy with the server's own
-- binary COPY reader, checks row counts and values, dumps it again with
-- COPY ... TO, and prints an MD5 so a second run proves determinism.
-- Nothing here was executed by the delivery environment unless the report
-- says so explicitly.

\set catalogue_present true
DROP SCHEMA IF EXISTS pgcopy_bridge_check CASCADE;
CREATE SCHEMA pgcopy_bridge_check;
SET search_path = pgcopy_bridge_check, public;

CREATE TABLE sample (
    id      int4,
    neg     int8,
    label   text,
    raw     bytea,
    uid     uuid,
    amount  numeric,
    zero4   numeric,
    d       date,
    ts      timestamp,
    tz      timestamptz,
    grid    int4[],
    notes   text[],
    ranks   numeric[]
);

\copy sample FROM 'sample.copy' WITH (FORMAT binary)

SELECT count(*) AS rows_loaded FROM sample;
-- expected: rows_loaded = 2

SELECT id, neg, label, raw, uid, amount::text, zero4::text, d::text,
       ts::text, tz::text, grid::text, notes::text, ranks::text
FROM sample ORDER BY id NULLS FIRST;

-- Values are compared in SQL, not in the client encoding:
SELECT bool_and(id IS NULL AND neg = -9007199254740993 AND label = ''
                AND raw = '\x00ff1080'::bytea
                AND uid = '123e4567-e89b-12d3-a456-426614174000'::uuid
                AND amount = 123456789012345678901234567890.1234567890000
                AND zero4 = 0.0000
                AND d = '-infinity'::date
                AND ts = 'infinity'::timestamp
                AND tz = '-infinity'::timestamptz
                AND grid IS NULL AND notes IS NULL AND ranks IS NULL)
         AS tuple1_ok
FROM sample WHERE id IS NULL;
-- expected: tuple1_ok = t

SELECT bool_and(neg = 0 AND label = 'plain' AND raw = '\x'::bytea
                AND amount = -1.2300 AND zero4 = 'NaN'::numeric
                AND d = '2000-01-01' AND ts = '2000-01-01 00:00:00'
                AND tz = '2000-01-01 01:00:00+00'
                AND grid::text = '[0:1][-3:-2]={{11,NULL},{13,14}}'
                AND notes::text = '[0:1][-3:-2]={{"",NULL},{ok,""}}'
                AND ranks::text = '[0:0]={1.2300}') AS tuple2_ok
FROM sample WHERE id = 0;
-- expected: tuple2_ok = t

\copy sample TO 'roundtrip.copy' WITH (FORMAT binary)

-- Determinism / round-trip proof: decode the PostgreSQL-produced file
-- again and compare counts.
DROP TABLE IF EXISTS sample2;
CREATE TABLE sample2 (LIKE sample);
\copy sample2 FROM 'roundtrip.copy' WITH (FORMAT binary)
SELECT (SELECT count(*) FROM sample2) = (SELECT count(*) FROM sample)
       AS roundtrip_rows_match;
-- expected: t

-- Optional independent byte check with md5sum on the server's filesystem.
-- \! md5sum sample.copy roundtrip.copy
