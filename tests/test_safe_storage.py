"""Cursor safe-storage decryption (AES-256-GCM, Chromium v10 layout) and its use by read_token."""
import base64
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collector import safe_storage
from collector.adapters import cursor

KEY = bytes.fromhex("feffe9928665731c6d6a8f9467308308feffe9928665731c6d6a8f9467308308")
IV = bytes.fromhex("cafebabefacedbaddecaf888")
# NIST GCM test case 15 (AES-256, no extra data)
NIST_C = bytes.fromhex(
    "522dc1f099567d07f47f37a32a84427d643a8cdcbfe5c0c97598a2bd2555d1aa"
    "8cb08e48590dbb3da7b08b1056828838c5f61e6393ba7a0abcc9f662898015ad")
NIST_T = bytes.fromhex("b094dac5d93471bdec1a502270e3cc6c")
NIST_P = bytes.fromhex(
    "d9313225f88406e5a55909c5aff5269a86a7a9531534f7da2e4c303d8a318a72"
    "1c3c0c95956809532fcf0e2449a6b525b16aedf5aa0de657ba637b391aafd255")


def encrypt(key, nonce, plain):
    """Test-only AES-GCM encrypt, built from the module's own primitives."""
    rk = safe_storage._expand_key(key)
    h = int.from_bytes(safe_storage._encrypt_block(rk, b"\0" * 16), "big")
    out = bytearray()
    counter = 1
    for i in range(0, len(plain), 16):
        counter += 1
        stream = safe_storage._encrypt_block(rk, nonce + counter.to_bytes(4, "big"))
        out += bytes(a ^ b for a, b in zip(plain[i:i + 16], stream))
    padded = bytes(out).ljust((len(out) + 15) // 16 * 16, b"\0")
    lengths = (0).to_bytes(8, "big") + (len(out) * 8).to_bytes(8, "big")
    s = safe_storage._ghash(h, padded + lengths)
    tag = (int.from_bytes(safe_storage._encrypt_block(rk, nonce + b"\0\0\0\1"), "big") ^ s).to_bytes(16, "big")
    return bytes(out), tag


def blob(key, plain_text):
    c, t = encrypt(key, IV, plain_text.encode("utf-8"))
    return safe_storage.PREFIX + base64.b64encode(b"v10" + IV + c + t).decode("ascii")


class GcmTests(unittest.TestCase):
    def test_nist_vector(self):
        self.assertEqual(safe_storage.gcm_decrypt(KEY, IV, NIST_C, NIST_T), NIST_P)

    def test_wrong_tag_or_key_rejected(self):
        self.assertIsNone(safe_storage.gcm_decrypt(KEY, IV, NIST_C, b"\0" * 16))
        self.assertIsNone(safe_storage.gcm_decrypt(b"\1" * 32, IV, NIST_C, NIST_T))

    def test_round_trip_and_value_handling(self):
        text = "header.payload.signature-" + "x" * 40
        self.assertEqual(safe_storage.decrypt_value(blob(KEY, text), "unused", key=KEY), text)
        self.assertEqual(safe_storage.decrypt_value("plain-token", "unused"), "plain-token")
        self.assertIsNone(safe_storage.decrypt_value(blob(KEY, text), "unused", key=b"\2" * 32))
        self.assertIsNone(safe_storage.decrypt_value(safe_storage.PREFIX + "!!notbase64", "unused", key=KEY))
        self.assertIsNone(safe_storage.decrypt_value(
            safe_storage.PREFIX + base64.b64encode(b"v11" + b"\0" * 40).decode(), "unused", key=KEY))


class ReadTokenTests(unittest.TestCase):
    def make_db(self, value):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        path = os.path.join(d, "state.vscdb")
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
        con.execute("INSERT INTO ItemTable VALUES ('cursorAuth/accessToken', ?)", (value,))
        con.commit()
        con.close()
        return path

    def test_plain_token_still_read(self):
        self.assertEqual(cursor.read_token(self.make_db("plain-token")), "plain-token")

    def test_encrypted_token_is_decrypted(self):
        path = self.make_db(safe_storage.PREFIX + "abc")
        with mock.patch.object(safe_storage, "decrypt_value", return_value="real-token"):
            self.assertEqual(cursor.read_token(path), "real-token")

    def test_undecryptable_token_reports_why(self):
        path = self.make_db(safe_storage.PREFIX + "abc")
        with mock.patch.object(safe_storage, "decrypt_value", return_value=None):
            self.assertIsNone(cursor.read_token(path))
        self.assertEqual(cursor.token_problem(), cursor.TOKEN_ENCRYPTED)
        self.assertIsNone(cursor.read_token(os.path.join(os.path.dirname(path), "missing.vscdb")))
        self.assertEqual(cursor.token_problem(), cursor.TOKEN_NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
