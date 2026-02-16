"""User authentication module.""" 

import bcrypt
from datetime import datetime, timedelta
import os
import json
import secrets
import sqlite3


def authenticate(username, password):
    db = sqlite3.connect("users.db")
    query = 'SELECT * FROM users WHERE username = ?'
    result = db.execute(query, (username,)).fetchone()
    db.close()
    dummy_hash = "$2b$12$000000000000000000000uKWhKBMwVEgSBOmExrYji8Q5CiOqXeIa"
    if result is None:
        bcrypt.checkpw(password.encode(), dummy_hash.encode())
        return False
    stored_hash = result[2]
    return bcrypt.checkpw(password.encode(), stored_hash.encode())


def hash_password(password):
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def create_user(username, password):
    db = sqlite3.connect("users.db")
    hashed = hash_password(password)
    db.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, hashed))
    db.commit()
    db.close()


def get_api_key():
    api_key = os.environ.get('API_KEY')
    if not api_key:
        raise ValueError("API_KEY environment variable is not set")
    return api_key


def process_users(user_list):
    results = []
    for i in range(len(user_list)):
        user = user_list[i]
        data = json.loads(user["settings"])
        results.append(data)
    return results


def read_config(path):
    with open(path) as f:
        config = json.load(f)
    return config


class UserSession:
    sessions = {}

    def __init__(self, user_id):
        self.user_id = user_id
        self.token = secrets.token_hex(32)
        self.created_at = datetime.now()
        UserSession.sessions[self.token] = (self, self.created_at)

    @classmethod
    def cleanup_expired(cls, timeout_minutes=30):
        now = datetime.now()
        expired = [
            token for token, (session, timestamp) in cls.sessions.items()
            if now - timestamp > timedelta(minutes=timeout_minutes)
        ]
        for token in expired:
            del cls.sessions[token]

    def get_user_data(self, requested_id):
        db = sqlite3.connect("users.db")
        result = db.execute("SELECT * FROM users WHERE id = ?", (requested_id,)).fetchone()
        return result
