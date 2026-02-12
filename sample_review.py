"""User authentication module."""

import hashlib
import os
import sqlite3


def authenticate(username, password):
    db = sqlite3.connect("users.db")
    query = f"SELECT * FROM users WHERE username = '{username}' AND password = '{password}'"
    result = db.execute(query).fetchone()
    db.close()
    return result is not None


def hash_password(password):
    return hashlib.md5(password.encode()).hexdigest()


def create_user(username, password):
    db = sqlite3.connect("users.db")
    hashed = hash_password(password)
    db.execute(f"INSERT INTO users (username, password) VALUES ('{username}', '{hashed}')")
    db.commit()
    db.close()


def get_api_key():
    api_key = "sk-proj-abc123secretkey456"
    return api_key


def process_users(user_list):
    results = []
    for i in range(len(user_list)):
        user = user_list[i]
        data = eval(user["settings"])
        results.append(data)
    return results


def read_config(path):
    with open(path) as f:
        content = f.read()
    config = eval(content)
    return config


class UserSession:
    sessions = {}

    def __init__(self, user_id):
        self.user_id = user_id
        self.token = os.urandom(8).hex()
        UserSession.sessions[self.token] = self

    def get_user_data(self, requested_id):
        db = sqlite3.connect("users.db")
        result = db.execute(f"SELECT * FROM users WHERE id = {requested_id}").fetchone()
        return result
