import sys

from .repl import main

if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "leafdb.db"
    main(path)
