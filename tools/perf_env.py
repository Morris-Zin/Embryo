"""Small dotenv data parser used when a shell script needs one non-secret-shaped value."""
import sys
from pathlib import Path


def parse_dotenv_key(text, key):
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() != key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return True, value
    return False, None


def read_dotenv_key(path, key):
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError:
        return False, None
    return parse_dotenv_key(text, key)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        return 2
    found, value = read_dotenv_key(args[0], args[1])
    if not found:
        return 1
    sys.stdout.write(value)
    return 0


if __name__ == "__main__":
    sys.exit(main())
