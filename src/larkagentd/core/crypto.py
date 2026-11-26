import base64
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class Vault:
    def __init__(self, key: bytes):
        self.cipher = AESGCM(key)

    def seal(self, owner: str, value: dict) -> str:
        nonce = os.urandom(12)
        data = self.cipher.encrypt(nonce, json.dumps(value).encode(), owner.encode())
        return base64.b64encode(nonce + data).decode()

    def open(self, owner: str, value: str) -> dict:
        data = base64.b64decode(value, validate=True)
        return json.loads(self.cipher.decrypt(data[:12], data[12:], owner.encode()))
