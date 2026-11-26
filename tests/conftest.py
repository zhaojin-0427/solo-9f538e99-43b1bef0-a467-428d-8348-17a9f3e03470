"""pytest 公共夹具：每个用例使用独立临时 SQLite 库并载入样例。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import create_app
from app.db import connect, init_schema
from app.loader import load_file, load_payload

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "data" / "sample_data.json"
INFEASIBLE = ROOT / "examples" / "infeasible_demo.json"


@pytest.fixture()
def app(tmp_path):
    db_path = tmp_path / "test.db"
    application = create_app(db_path)
    with application.app_context():
        conn = connect(str(db_path))
        init_schema(conn)
        load_file(conn, SAMPLE, replace=True)
        conn.close()
    yield application


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def db(app):
    conn = connect(app.config["DATABASE"])
    yield conn
    conn.close()


def load_named(app, path: Path):
    with app.app_context():
        conn = connect(app.config["DATABASE"])
        try:
            load_file(conn, path, replace=True)
        finally:
            conn.close()


def load_obj(app, payload: dict):
    with app.app_context():
        conn = connect(app.config["DATABASE"])
        try:
            load_payload(conn, payload, replace=True)
        finally:
            conn.close()


@pytest.fixture()
def infeasible_app(app):
    load_named(app, INFEASIBLE)
    return app


def jpost(client, url, body=None):
    return client.post(url, data=json.dumps(body or {}),
                       content_type="application/json")
