"""PostgreSQL LISTEN setup for the manager's notification lifecycle."""
def listen(connection):
    connection.autocommit = True
    connection.execute('LISTEN rag_run_events')
