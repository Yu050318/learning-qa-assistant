"""SQLAlchemy operations used by the transaction manager, never by controllers."""
class PersistenceHandle:
    """Marker for the manager's transaction handle, distinct from driver doubles."""


def unwrap(value):
    return value._persistence_session if isinstance(value, PersistenceHandle) else value


def dialect_name(transaction):
    return unwrap(transaction).get_bind().dialect.name


def begin(transaction):
    return unwrap(transaction).begin()


def load(transaction, model, key, **options):
    return unwrap(transaction).get(model, key, **options)


def save(transaction, record):
    unwrap(transaction).add(record)


def save_all(transaction, records):
    unwrap(transaction).add_all(records)


def remove(transaction, record):
    unwrap(transaction).delete(record)


def database_now(transaction):
    from sqlalchemy import func, select
    from app.entity.time import aware
    session = unwrap(transaction)
    clock = func.clock_timestamp() if session.get_bind().dialect.name == 'postgresql' else func.current_timestamp()
    return aware(session.scalar(select(clock)))
