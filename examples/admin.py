"""Setup commands. Each secret is printed once; only a hash or ciphertext is stored.

python -m examples.admin create-key quicktest
python -m examples.admin rotate-webhook-secret quicktest
python -m examples.admin new-webhook-key        # value for WEBHOOK_KEY
"""

import os
import sys

from cryptography.fernet import Fernet

from . import db
from .webhooks import rotate_secret


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if argv == ["new-webhook-key"]:
        print(Fernet.generate_key().decode())
        return 0
    if len(argv) != 2 or argv[0] not in ("create-key", "rotate-webhook-secret"):
        print(__doc__)
        return 2
    command, project = argv
    engine = db.make_engine(os.environ.get("DATABASE_URL", "sqlite:///var/quiz_service.db"))
    if command == "create-key":
        print(db.add_api_key(engine, project))
    else:
        print(rotate_secret(engine, Fernet(os.environ["WEBHOOK_KEY"].encode()), project))
    return 0


if __name__ == "__main__":
    sys.exit(main())
