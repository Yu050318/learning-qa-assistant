import unittest
from unittest.mock import MagicMock

from app.core.config import Settings
from app.core.errors import AppError
from app.infrastructure.sql import ReadOnlySQL, validate_query


class SQLGuardTests(unittest.TestCase):
    allowed = ["sales.orders", "sales.products"]

    def test_valid_aggregate_join_cte_and_union(self):
        queries = [
            "SELECT SUM(amount) AS total FROM sales.orders",
            "WITH totals AS (SELECT product_id, SUM(amount) AS total FROM sales.orders GROUP BY product_id) SELECT * FROM totals",
            "SELECT p.name, COUNT(*) FROM sales.products p JOIN sales.orders o ON p.id=o.product_id GROUP BY p.name",
            "SELECT id FROM sales.orders UNION ALL SELECT id FROM sales.products",
            "SELECT COUNT(*) FROM sales.orders WHERE note LIKE '%sale%'",
            "SELECT date_trunc('month', created_at), SUM(amount) FROM sales.orders GROUP BY 1",
            "SELECT CAST(amount AS numeric(10,2)), string_agg(note, ',') OVER () FROM sales.orders",
        ]
        for query in queries:
            with self.subTest(query=query):
                self.assertIn("ONLY sales.", validate_query(query, self.allowed))

    def test_reject_mutations_system_tables_functions_and_hidden_relations(self):
        queries = [
            "DELETE FROM sales.orders", "SELECT 1; DROP TABLE sales.orders",
            "WITH x AS (DELETE FROM sales.orders RETURNING *) SELECT * FROM x",
            "SELECT * INTO sales.copy FROM sales.orders", "SELECT * FROM sales.orders FOR UPDATE",
            "SELECT * FROM rag.users", "SELECT * FROM pg_catalog.pg_roles", "SELECT * FROM orders",
            "SELECT * FROM other_db.sales.orders", "SELECT * FROM sales.orders o JOIN secret.users s ON true",
            "SELECT * FROM (SELECT * FROM secret.users) nested", "SELECT * FROM sales.orders UNION SELECT * FROM secret.users",
            "SELECT pg_sleep(20)", "SELECT pg_read_file('/etc/passwd')", "SELECT set_config('x','y',true)",
            "SELECT dblink('secret', 'select 1')", "SELECT evil.sum(amount) FROM sales.orders",
            "SELECT * FROM generate_series(1,100)", "EXPLAIN SELECT * FROM sales.orders",
            "COPY sales.orders TO '/tmp/data'", "WITH RECURSIVE x AS (SELECT 1) SELECT * FROM x",
            "SELECT CAST('value' AS custom_type)", "SELECT 'pg_roles'::regclass",
        ]
        for query in queries:
            with self.subTest(query=query):
                with self.assertRaises(AppError) as raised:
                    validate_query(query, self.allowed)
                self.assertEqual("SQL_NOT_ALLOWED", raised.exception.code)

    def test_missing_connection_and_platform_database_rejected(self):
        with self.assertRaises(AppError):
            ReadOnlySQL(Settings(_env_file=None))._engine()
        settings = Settings(_env_file=None, database_url="postgresql+psycopg://platform:secret@localhost/app",
                            sql_database_url="postgresql://reader:secret@localhost/app", sql_allowed_tables=self.allowed)
        with self.assertRaises(AppError) as raised:
            ReadOnlySQL(settings)._engine()
        self.assertEqual("SQL_CONFIGURATION_ERROR", raised.exception.code)

    def test_invalid_whitelist_rejected_before_connect(self):
        settings = Settings(_env_file=None, sql_database_url="postgresql://reader:secret@localhost/business",
                            sql_allowed_tables=["pg_catalog.pg_roles"])
        with self.assertRaises(AppError):
            ReadOnlySQL(settings)._engine()

    def test_transaction_checks_read_only_role_and_table_kind(self):
        adapter = ReadOnlySQL(Settings(_env_file=None, sql_allowed_tables=self.allowed))
        connection = MagicMock()
        connection.execute.return_value.scalar_one.return_value = True
        with self.assertRaises(AppError) as raised:
            adapter._configure(connection, 5)
        self.assertEqual("SQL_ROLE_NOT_READ_ONLY", raised.exception.code)
        self.assertEqual("SET TRANSACTION READ ONLY", str(connection.execute.call_args_list[0].args[0]))
        connection.execute.return_value.scalar_one.return_value = False
        connection.execute.return_value.scalar_one_or_none.return_value = "v"
        with self.assertRaises(AppError):
            adapter._configure(connection, 5)

    def test_query_bounds_rows_and_parameterless_percent_literals(self):
        settings = Settings(_env_file=None, sql_allowed_tables=self.allowed, sql_max_rows=2)
        adapter = ReadOnlySQL(settings)
        engine, connection = MagicMock(), MagicMock()
        engine.connect.return_value.__enter__.return_value = connection
        adapter._engine = lambda: engine
        adapter._configure = lambda connection, timeout: None
        connection.exec_driver_sql.return_value.keys.return_value = ["amount"]
        connection.exec_driver_sql.return_value.fetchmany.return_value = [(1,), (2,), (3,)]
        result = adapter.query("SELECT amount FROM sales.orders WHERE note LIKE '%sale%'", 5)
        self.assertTrue(result["truncated"])
        self.assertEqual([[1], [2]], result["rows"])
        self.assertIn("LIMIT 3", connection.exec_driver_sql.call_args.args[0])
        self.assertTrue(connection.exec_driver_sql.call_args.kwargs["execution_options"]["no_parameters"])


if __name__ == "__main__":
    unittest.main()
