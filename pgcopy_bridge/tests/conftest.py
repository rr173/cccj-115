"""Shared pytest fixtures and helpers.

The package root is placed on ``sys.path``; tests never depend on an
installed distribution.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "materials"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MATERIALS = os.path.join(ROOT, "materials", "generated")


@pytest.fixture(scope="session")
def materials_dir():
    return MATERIALS


@pytest.fixture()
def catalogue_path(materials_dir):
    return os.path.join(materials_dir, "catalogue.json")


@pytest.fixture()
def arrays_catalogue_path(materials_dir):
    return os.path.join(materials_dir, "arrays_catalogue.json")


def read_jsonl(path):
    with open(path, "rb") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_jsonl(path, records):
    with open(path, "wb") as fh:
        for record in records:
            fh.write(
                json.dumps(record, ensure_ascii=False,
                           separators=(",", ":")).encode("utf-8") + b"\n")
