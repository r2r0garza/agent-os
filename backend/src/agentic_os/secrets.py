from __future__ import annotations

import logging
from functools import lru_cache

from cryptography.fernet import Fernet

from agentic_os.config import resolve_master_key

logger = logging.getLogger("agentic_os.secrets")


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = resolve_master_key()
    if key is None:
        logger.warning(
            "no master key is configured (AGENTIC_OS_MASTER_KEY or a key file); "
            "generating an ephemeral in-process key. Restarting this process will "
            "make previously encrypted secrets unrecoverable. Run "
            "'agentic-os config generate-master-key' for a durable key."
        )
        key = Fernet.generate_key()
    return Fernet(key)


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode()).decode()
