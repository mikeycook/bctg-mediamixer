import psycopg2

class PostgresInterpreter:
    """
    A utility class to manage PostgreSQL database connections.
    """

    def __init__(self, host, database, user, password, port=5432):
        """
        Initializes the connection manager with database parameters.
        """
        self.host = host
        self.database = database
        self.user = user
        self.password = password
        self.port = port
        self.connection = None
        self.cursor = None

    def connect(self):
        """
        Establishes a connection to the PostgreSQL database.
        """
        try:
            self.connection = psycopg2.connect(
                host=self.host,
                database=self.database,
                user=self.user,
                password=self.password,
                port=self.port
            )
            self.cursor = self.connection.cursor()
            print("Successfully connected to the database.")
        except psycopg2.Error as e:
            print(f"Error connecting to the database: {e}")
            self.connection = None
            self.cursor = None

    def execute_query(self, query, params=None):
        """
        Executes a SQL query and returns raw tuples.
        """
        if not self.connection or not self.cursor:
            print("Not connected to the database. Call connect() first.")
            return None

        try:
            self.cursor.execute(query, params)
            if self.cursor.description:  # SELECT
                return self.cursor.fetchall()
            else:
                self.connection.commit()
                return True
        except psycopg2.Error as e:
            print(f"Error executing query: {e}")
            self.connection.rollback()
            return False

    def execute_query_as_dict(self, query, params=None):
        """
        Executes a SQL query and returns results as a list of dictionaries.
        Column names are used as keys.
        """
        if not self.connection or not self.cursor:
            print("Not connected to the database. Call connect() first.")
            return None

        try:
            self.cursor.execute(query, params)

            if not self.cursor.description:
                # Non-SELECT query
                self.connection.commit()
                return True

            rows = self.cursor.fetchall()
            columns = [desc[0] for desc in self.cursor.description]

            return [dict(zip(columns, row)) for row in rows]

        except psycopg2.Error as e:
            print(f"Error executing query: {e}")
            self.connection.rollback()
            return False

    def close(self):
        """
        Closes the database connection and cursor.
        """
        if self.cursor:
            self.cursor.close()
            print("Cursor closed.")
        if self.connection:
            self.connection.close()
            print("Connection closed.")

    def __enter__(self):
        """
        Context manager entry point for 'with' statement.
        """
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """
        Context manager exit point for 'with' statement.
        """
        self.close()
