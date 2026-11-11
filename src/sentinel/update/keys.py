"""Release keys this program trusts for update files: key id -> Ed25519 public key (hex).

The matching private key never leaves the build computer (``packaging/release.py keygen`` creates
it outside the repository). To replace a key: add the new public key here, ship that in an update
signed with the *old* key, then sign later updates with the new key and remove the old one.
"""

TRUSTED_KEYS: dict[str, str] = {
    "ff5f219ae91aa82a": "a476f8c9f65e68f9d1b0efef1a420e7f242afc4d8c5def221d9e1edfceaa988e",   # added 2026-11-10
}
