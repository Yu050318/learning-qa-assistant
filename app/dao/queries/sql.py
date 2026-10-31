"""Persistence queries for app/manager/sql.py."""
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from app.dao.postgres.transactions import unwrap


def business_url(self):
    return (make_url(self.settings.sql_database_url.get_secret_value()))


def platform_url(self):
    return (make_url(self.settings.database_url.get_secret_value()) if self.settings.database_url.get_secret_value() else None)


def create_readonly_engine(self, url):
    return (create_engine(url.set(drivername="postgresql+psycopg"), pool_pre_ping=True,
                                            connect_args={"connect_timeout": max(1, int(self.settings.sql_timeout))},
                                            pool_timeout=self.settings.sql_timeout))


def set_readonly(connection):
    connection = unwrap(connection)
    return (connection.execute(text("SET TRANSACTION READ ONLY")))


def set_statement_timeout(connection, timeout):
    connection = unwrap(connection)
    return (connection.execute(text("SELECT set_config('statement_timeout', :ms, true)"), {"ms": str(max(1, int(timeout * 1000)))}))


def set_lock_timeout(connection, timeout):
    connection = unwrap(connection)
    return (connection.execute(text("SELECT set_config('lock_timeout', :ms, true)"), {"ms": str(max(1, int(timeout * 1000)))}))


def set_catalog_path(connection):
    connection = unwrap(connection)
    return (connection.execute(text("SET LOCAL search_path = pg_catalog")))


def has_write_privileges(connection):
    connection = unwrap(connection)
    return (connection.execute(text("""
            SELECT r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication
              OR EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'
                AND c.relkind IN ('r','p','v','m','f') AND
                (pg_has_role(current_user, c.relowner, 'USAGE') OR
                 has_table_privilege(current_user,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER')))
              OR EXISTS (SELECT 1 FROM pg_namespace n WHERE n.nspname NOT LIKE 'pg_%'
                AND n.nspname <> 'information_schema' AND has_schema_privilege(current_user,n.oid,'CREATE'))
              OR has_database_privilege(current_user,current_database(),'CREATE')
            FROM pg_roles r WHERE r.rolname=current_user
        """)).scalar_one())


def relation_kind(connection, name, schema):
    connection = unwrap(connection)
    return (connection.execute(text("""SELECT c.relkind FROM pg_class c
                JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=:schema AND c.relname=:name"""),
                {"schema": schema, "name": name}).scalar_one_or_none())


def schema_inspector(connection):
    connection = unwrap(connection)
    return (inspect(connection))


def set_query_timeout(connection, timeout):
    connection = unwrap(connection)
    return (connection.execute(text("SELECT set_config('statement_timeout', :ms, true)"), {"ms": str(max(1, int(timeout * 1000)))}))
