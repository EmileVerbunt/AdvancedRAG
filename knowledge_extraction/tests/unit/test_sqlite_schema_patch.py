from __future__ import annotations

import sqlite3

from sqlalchemy import text

from knowledge_extraction.infrastructure.persistence.sqlite.repositories import make_engine


def test_make_engine_adds_missing_claim_support_columns(tmp_path) -> None:
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE claims (
                id VARCHAR(64) PRIMARY KEY,
                text TEXT NOT NULL,
                chunk_id VARCHAR(64),
                confidence FLOAT
            )
            """
        )
        conn.commit()

    engine = make_engine(db_path)
    with engine.connect() as conn:
        columns = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(claims)")).fetchall()
        }

    assert "supporting_figure_id" in columns
    assert "supporting_table_id" in columns
