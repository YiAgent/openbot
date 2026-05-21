# This is a test file added for E2E business testing
# Contains some intentional code smells for the review bot to catch


def process_user_data(data):
    # BAD: no input validation
    user_id = data["user_id"]  # Will throw KeyError if missing
    password = data["password"]  # BAD: logging passwords is risky
    print(f"Processing user {user_id} with password {password}")  # BAD: plaintext password in logs

    # BAD: SQL injection risk simulation
    query = f"SELECT * FROM users WHERE id = {user_id}"
    return query


class DataManager:
    def __init__(self):
        self.data = []

    def add(self, item):
        self.data.append(item)  # OK

    def delete_all(self):
        self.data = []  # RISKY: no confirmation, no backup

    def get_secret(self):
        SECRET_KEY = "hardcoded_secret_abc123"  # BAD: hardcoded secret
        return SECRET_KEY
