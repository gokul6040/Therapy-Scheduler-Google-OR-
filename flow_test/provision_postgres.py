"""Provision a restricted test namespace inside an existing PostgreSQL database.

Read the administrator URL from FLOW_TEST_ADMIN_DATABASE_URL. Save only the
restricted test login in flow_test/.env. Never modify public tables or solver
files, and never send a WhatsApp message.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets

from psycopg import sql
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import make_url

from database.models import Appointment, Patient, Therapist, TreatmentRequirement
from flow_test.service import FlowTest, ROOT
from flow_test.storage import ALLOWED_SCHEMAS


def read_settings(path: Path) -> dict[str, str]:
    """Read local settings without printing or changing the process environment."""
    if not path.exists():
        return {}
    return {
        key.strip(): value.strip().strip('\"\'')
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
        for key, value in [line.split("=", 1)]
    }


def save_settings(path: Path, updates: dict[str, str]) -> None:
    """Preserve all unrelated settings, including the existing WhatsApp values."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    result = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in updates and not line.lstrip().startswith("#"):
            result.append(f"{key}={updates[key]}")
            remaining.pop(key, None)
        else:
            result.append(line)
    result.extend(f"{key}={value}" for key, value in remaining.items())
    temporary = path.with_name(".env.storage-writing")
    temporary.write_text("\n".join(result) + "\n", encoding="utf-8")
    temporary.replace(path)


def public_structure(connection) -> str:
    """Fingerprint public table/column metadata without reading patient records."""
    rows = connection.execute(text("""
        SELECT c.oid, c.relname, a.attnum, a.attname,
               pg_catalog.format_type(a.atttypid, a.atttypmod), a.attnotnull
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid
        WHERE n.nspname='public' AND c.relkind IN ('r','p')
              AND a.attnum>0 AND NOT a.attisdropped
        ORDER BY c.oid, a.attnum
    """)).all()
    return hashlib.sha256(repr(rows).encode()).hexdigest()


def outside_access(connection, role: str, schema: str) -> int:
    """Count non-system tables the test role could read or modify outside its schema."""
    return connection.execute(text("""
        SELECT count(*) FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname<>:schema AND n.nspname NOT IN ('pg_catalog','information_schema')
          AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%'
          AND c.relkind IN ('r','p','v','m','f')
          AND has_table_privilege(:role,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE')
    """), {"role": role, "schema": schema}).scalar_one()


def provision(admin_url: str, schema: str = "flow_test") -> dict:
    """Create one schema and its restricted login; seed only fictional test rows."""
    if schema not in ALLOWED_SCHEMAS:
        raise ValueError("Only the designated test schemas can be provisioned")
    admin = make_url(admin_url)
    if admin.get_backend_name() != "postgresql":
        raise ValueError("Provisioning requires PostgreSQL")
    role = f"{schema}_app"
    prefix = "FLOW_TEST" if schema == "flow_test" else "FLOW_TEST_PREVIEW"
    env_path = ROOT / ".env"
    local = read_settings(env_path)
    role_url = make_url(local[f"{prefix}_DATABASE_URL"]) if local.get(f"{prefix}_DATABASE_URL") else None
    admin_engine = create_engine(admin, connect_args={"connect_timeout": 15}, hide_parameters=True)
    protected_files = [*sorted((ROOT.parent / "scheduling").glob("*.py")), ROOT.parent / "config.py"]
    protected_hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected_files}
    try:
        with admin_engine.begin() as conn:
            # Serializes repeat provisioning without locking any scheduler tables.
            conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"therapy:{schema}:provision"})
            before = public_structure(conn)
            public_count = conn.execute(text("SELECT count(*) FROM pg_tables WHERE schemaname='public'")).scalar_one()
            role_exists = conn.execute(text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=:role)"), {"role": role}).scalar_one()
            if role_exists:
                if role_url is None or (role_url.host, role_url.database, role_url.username) != (admin.host, admin.database, role):
                    raise ValueError("Existing test role requires its saved test connection; no password was reset")
            else:
                password = secrets.token_urlsafe(40)
                with conn.connection.driver_connection.cursor() as cursor:
                    cursor.execute(sql.SQL(
                        "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS"
                    ).format(sql.Identifier(role), sql.Literal(password)))
                role_url = admin.set(username=role, password=password)
            schema_exists = inspect(conn).has_schema(schema)
            if schema_exists:
                tables = set(inspect(conn).get_table_names(schema=schema))
                if tables and "flow_test_settings" not in tables:
                    raise ValueError("Existing schema is not marked as flow-test storage")
            with conn.connection.driver_connection.cursor() as cursor:
                if not schema_exists:
                    cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
                cursor.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(schema)))
                cursor.execute(sql.SQL("GRANT USAGE, CREATE ON SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
                cursor.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(admin.database), sql.Identifier(role)))
                cursor.execute(sql.SQL("ALTER ROLE {} IN DATABASE {} SET search_path TO {}, pg_catalog").format(
                    sql.Identifier(role), sql.Identifier(admin.database), sql.Identifier(schema),
                ))
            if outside_access(conn, role, schema):
                raise ValueError("Test role has access outside its schema; existing permissions were not changed")
            if conn.execute(text("SELECT has_schema_privilege(:role,'public','CREATE')"), {"role": role}).scalar_one():
                raise ValueError("Public schema grants CREATE to the test role; no existing public permissions were changed")
        # Save only the limited login so interrupted initialization can be retried.
        save_settings(env_path, {
            f"{prefix}_DATABASE_URL": role_url.render_as_string(hide_password=False),
            f"{prefix}_DATABASE_SCHEMA": schema,
        })

        def no_messages(*_args):
            raise RuntimeError("Provisioning cannot send messages")

        flow = FlowTest(
            role_url.render_as_string(hide_password=False), database_schema=schema,
            live=schema == "flow_test", send=no_messages,
        )
        try:
            with flow.sessions() as session:
                counts = {model.__tablename__: session.scalar(select(func.count()).select_from(model))
                          for model in (Patient, Therapist, TreatmentRequirement, Appointment)}
            with admin_engine.connect() as conn:
                unchanged = before == public_structure(conn)
                outside = outside_access(conn, role, schema)
                after_public_count = conn.execute(text("SELECT count(*) FROM pg_tables WHERE schemaname='public'")).scalar_one()
            files_unchanged = all(hashlib.sha256(p.read_bytes()).hexdigest() == protected_hashes[str(p)] for p in protected_files)
            report = {
                "database": admin.database, "schema": schema, "test_role": role,
                "test_table_count": len(inspect(flow.engine).get_table_names(schema=schema)),
                "fictional_records": counts, "public_table_count_before": public_count,
                "public_table_count_after": after_public_count,
                "public_structure_unchanged": unchanged,
                "tables_accessible_outside_test_schema": outside,
                "scheduler_files_unchanged": files_unchanged,
                "whatsapp_messages_sent": 0,
            }
            if not unchanged or outside or not files_unchanged:
                raise ValueError("Post-provision isolation verification failed")
            output = ROOT.parent / "outputs" / "flow_test_storage_report.json"
            output.parent.mkdir(exist_ok=True)
            output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            return report
        finally:
            flow.engine.dispose()
    finally:
        admin_engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", choices=ALLOWED_SCHEMAS, default="flow_test")
    args = parser.parse_args()
    admin_url = os.getenv("FLOW_TEST_ADMIN_DATABASE_URL", "")
    if not admin_url:
        parser.error("Set FLOW_TEST_ADMIN_DATABASE_URL for this provisioning process only")
    try:
        print(json.dumps(provision(admin_url, args.schema), indent=2))
    except Exception as exc:
        # Driver exceptions can embed connection details or DDL parameters.
        print(f"Provisioning stopped: {type(exc).__name__}")
        if isinstance(exc, ValueError):
            print(str(exc))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
