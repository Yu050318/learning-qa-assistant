"""Explicit transaction scope. Services own commit boundaries, DAOs own SQL."""
from contextlib import contextmanager

from app.dao.postgres import transactions


class Transaction(transactions.PersistenceHandle):
    def __init__(self, session):
        self._persistence_session = session

    def load(self, model, key, **options):
        return transactions.load(self, model, key, **options)

    def save(self, record):
        transactions.save(self, record)

    def save_all(self, records):
        transactions.save_all(self, records)

    def remove(self, record):
        transactions.remove(self, record)

    def commit(self):
        self._persistence_session.commit()

    def rollback(self):
        self._persistence_session.rollback()

    def flush(self):
        self._persistence_session.flush()

    @property
    def dialect_name(self):
        return transactions.dialect_name(self)

    def begin(self):
        return transactions.begin(self)


def as_transaction(value):
    return value if isinstance(value, Transaction) else Transaction(value)


@contextmanager
def transaction_scope(database):
    with database.sessions() as session:
        yield Transaction(session)
