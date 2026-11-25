"""Fixed PostgreSQL projections for the assistant's read-only tools."""

from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from app.modules.assistant.lab_corpus import LAB_SOURCES
from app.modules.assistant.mcp_contract import (
    BalanceResult, MenuResults, OrderResults, OwnOrder, PublicItem, Source,
)


class ReadUnavailable(Exception):
    """A safe failure sent over the tool protocol."""


READ_COLUMNS = {
    "menu_items": {"id", "name", "price", "is_available", "category_id"},
    "categories": {"id", "name"},
    "users": {"id", "balance"},
    "orders": {"id", "user_id", "status", "total_cost"},
}

ROLE_CHECK = text("""
    SELECT r.rolsuper OR r.rolinherit OR r.rolcreatedb OR r.rolcreaterole
      OR r.rolreplication OR r.rolbypassrls
      OR d.datdba = r.oid OR has_database_privilege(d.oid, 'CREATE')
      OR EXISTS (SELECT FROM pg_auth_members WHERE member = r.oid)
      OR EXISTS (
          SELECT FROM pg_namespace
          WHERE left(nspname, 3) <> 'pg_' AND nspname <> 'information_schema'
            AND has_schema_privilege(oid, 'CREATE')
      )
      OR EXISTS (
          SELECT FROM pg_proc JOIN pg_namespace ON pg_namespace.oid = pronamespace
          WHERE prosecdef AND left(nspname, 3) <> 'pg_' AND nspname <> 'information_schema'
            AND has_function_privilege(pg_proc.oid, 'EXECUTE')
      )
    FROM pg_roles r CROSS JOIN pg_database d
    WHERE r.rolname = current_user AND d.datname = current_database()
""")

RELATION_CHECK = text("""
    SELECT n.nspname, c.relname,
      pg_get_userbyid(c.relowner) = current_user
        OR has_table_privilege(c.oid, 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
        OR has_any_column_privilege(c.oid, 'INSERT,UPDATE,REFERENCES') AS writable,
      ARRAY(
          SELECT a.attname FROM pg_attribute a
          WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
            AND has_column_privilege(c.oid, a.attnum, 'SELECT')
      ) AS readable
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r','p','v','m','f')
      AND left(n.nspname, 3) <> 'pg_' AND n.nspname <> 'information_schema'
    LIMIT 129
""")


@contextmanager
def read_transaction(dsn: str):
    url = make_url(dsn)
    if (
        url.drivername not in ("postgresql", "postgresql+psycopg2")
        or url.host not in ("db", "localhost", "127.0.0.1", "::1")
        or not (url.username and url.password and url.database)
        or url.query
    ):
        raise ReadUnavailable("reader_config_invalid")

    engine = create_engine(url, poolclass=NullPool, connect_args={"connect_timeout": 2})
    try:
        with engine.begin() as connection:
            # PostgreSQL разрешает этой транзакции только чтение.
            connection.execute(text("SET TRANSACTION READ ONLY"))
            connection.execute(text("SET LOCAL statement_timeout = '1500ms'"))
            connection.execute(text("SET LOCAL lock_timeout = '500ms'"))
            connection.execute(text("SET LOCAL search_path = pg_catalog, public"))
            if connection.execute(ROLE_CHECK).scalar_one():
                raise ReadUnavailable("reader_privileges_invalid")
            relations = connection.execute(RELATION_CHECK).all()
            if len(relations) > 128 or any(row.writable for row in relations):
                raise ReadUnavailable("reader_privileges_invalid")
            actual = {(schema, table): set(columns) for schema, table, _, columns in relations if columns}
            if actual != {("public", table): columns for table, columns in READ_COLUMNS.items()}:
                raise ReadUnavailable("reader_privileges_invalid")
            yield connection
    finally:
        engine.dispose()


def read_menu(connection, arguments):
    limit = 8
    name = "replace(lower(m.name), 'ё', 'е')"
    category = "replace(lower(c.name), 'ё', 'е')"
    filters = []
    parameters = {"limit": limit + 1}
    if arguments.query:
        parameters["pattern"] = f"%{arguments.query}%"
        filters.append(f"({name} LIKE :pattern OR {category} LIKE :pattern)")
    if arguments.max_price is not None:
        operator = "<" if arguments.max_exclusive else "<="
        filters.append(f"m.price {operator} :max_price")
        parameters["max_price"] = arguments.max_price
    if arguments.available is not None:
        filters.append("coalesce(m.is_available, false) = :available")
        parameters["available"] = arguments.available
    where = "WHERE " + " AND ".join(filters) if filters else ""
    rows = connection.execute(text(f"""
        SELECT m.id, left(m.name, 200) AS name, m.price,
          coalesce(m.is_available, false) AS available
        FROM public.menu_items m LEFT JOIN public.categories c ON c.id = m.category_id
        {where} ORDER BY m.price, m.id LIMIT :limit
    """), parameters).mappings().all()
    return MenuResults(items=[PublicItem.model_validate(dict(row)) for row in rows[:limit]], has_more=len(rows) > limit)


def read_orders(connection, arguments, student_id):
    # ID владельца приходит из проверенной сессии. Даже при поиске по номеру заказ фильтруется по владельцу.
    parameters = {"student_id": student_id}
    lookup = ""
    if arguments.order_id is not None:
        lookup = "AND id = :order_id"
        parameters["order_id"] = arguments.order_id
    rows = connection.execute(text(f"""
        SELECT id, status, total_cost FROM public.orders
        WHERE user_id = :student_id {lookup}
        ORDER BY id DESC LIMIT 5
    """), parameters).mappings().all()
    return OrderResults(orders=[OwnOrder.model_validate(dict(row)) for row in rows])


def execute_tool(name, arguments, dsn, student_id):
    if name == "project.read":
        article = LAB_SOURCES.get(f"project-{arguments.topic}") or LAB_SOURCES[f"rules-{arguments.topic}"]
        return Source(**article)
    if name.startswith("student.") and student_id is None:
        raise ReadUnavailable("authentication_required")
    with read_transaction(dsn) as connection:
        if name == "menu.read":
            return read_menu(connection, arguments)
        if name == "student.balance":
            amount = connection.execute(
                text("SELECT coalesce(balance, 0) FROM public.users WHERE id = :student_id"),
                {"student_id": student_id},
            ).scalar_one()
            return BalanceResult(amount=amount)
        return read_orders(connection, arguments, student_id)
