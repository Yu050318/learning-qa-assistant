"""Opt-in: provision a disposable database/role on an explicitly supplied test server."""

import os
import secrets
import unittest
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.core.errors import AppError
from app.infrastructure.sql import ReadOnlySQL


@unittest.skipUnless(os.getenv("RUN_SQL_INTEGRATION") == "1" and os.getenv("SQL_TEST_ADMIN_URL"),
                     "requires RUN_SQL_INTEGRATION=1 and isolated SQL_TEST_ADMIN_URL")
class SQLReadOnlyIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        admin_url = make_url(os.environ["SQL_TEST_ADMIN_URL"]).set(drivername="postgresql")
        cls.admin = psycopg.connect(admin_url.render_as_string(hide_password=False), autocommit=True)
        cls.database, cls.role = "agent_test_" + uuid4().hex, "reader_" + uuid4().hex
        password = secrets.token_hex(20)
        cls.admin.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(cls.role), sql.Literal(password)))
        cls.admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.database)))
        cls.owner = psycopg.connect(admin_url.set(database=cls.database).render_as_string(hide_password=False), autocommit=True)
        cls.owner.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        cls.owner.execute("CREATE SCHEMA sales")
        cls.owner.execute("CREATE TABLE sales.orders (id integer, amount integer, note text)")
        cls.owner.execute("INSERT INTO sales.orders SELECT i, i, 'sale%' FROM generate_series(1,1000) i")
        cls.owner.execute(sql.SQL("GRANT USAGE ON SCHEMA sales TO {}").format(sql.Identifier(cls.role)))
        cls.owner.execute(sql.SQL("GRANT SELECT ON sales.orders TO {}").format(sql.Identifier(cls.role)))
        reader_url = admin_url.set(database=cls.database, username=cls.role, password=password)
        cls.adapter = ReadOnlySQL(Settings(_env_file=None, database_url="", sql_allowed_tables=["sales.orders"],
                                          sql_database_url=reader_url.render_as_string(hide_password=False), sql_max_rows=2))

    @classmethod
    def tearDownClass(cls):
        cls.adapter.close()
        cls.owner.close()
        cls.admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(cls.database)))
        cls.admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(cls.role)))
        cls.admin.close()

    def test_schema_aggregate_truncation_and_percent_literals(self):
        self.assertIn("sales.orders", self.adapter.schema(5)["tables"])
        self.assertEqual([[500500]], self.adapter.query("SELECT SUM(amount) FROM sales.orders", 5)["rows"])
        result = self.adapter.query("SELECT id FROM sales.orders WHERE note LIKE '%sale%' ORDER BY id", 5)
        self.assertEqual([[1], [2]], result["rows"])
        self.assertTrue(result["truncated"])

    def test_writes_and_outside_relations_rejected(self):
        for query in ("DELETE FROM sales.orders", "SELECT * FROM pg_catalog.pg_roles"):
            with self.assertRaises(AppError):
                self.adapter.query(query, 5)
        self.assertEqual([[1000]], self.adapter.query("SELECT COUNT(*) FROM sales.orders", 5)["rows"])

    def test_timeout(self):
        with self.assertRaises(AppError) as raised:
            self.adapter.query("SELECT COUNT(*) FROM sales.orders a CROSS JOIN sales.orders b CROSS JOIN sales.orders c", .02)
        self.assertEqual("SQL_TIMEOUT", raised.exception.code)

    def test_write_privilege_is_refused(self):
        self.owner.execute(sql.SQL("GRANT UPDATE ON sales.orders TO {}").format(sql.Identifier(self.role)))
        try:
            with self.assertRaises(AppError) as raised:
                self.adapter.query("SELECT COUNT(*) FROM sales.orders", 5)
            self.assertEqual("SQL_ROLE_NOT_READ_ONLY", raised.exception.code)
        finally:
            self.owner.execute(sql.SQL("REVOKE UPDATE ON sales.orders FROM {}").format(sql.Identifier(self.role)))
