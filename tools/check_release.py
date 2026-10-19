"""Проверяет индекс Git перед отправкой. Значения возможных секретов не печатает."""

from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = (
    r"\bAIza[A-Za-z0-9_-]{30,}",
    r"\bsk-[A-Za-z0-9_-]{32,}",
    r"\bsb_secret_[A-Za-z0-9_-]{20,}",
    r"(?<![0-9])[0-9]{6,}:[A-Za-z0-9_-]{30,}",
)


def main():
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode("utf-8").split("\0")
    failures = []
    count = 0
    for name in filter(None, paths):
        path = Path(name)
        if (any(part in {".venv", "venv", "__pycache__", ".pytest_cache", ".git"} for part in path.parts)
                or (path.name.startswith(".env") and path.name != ".env.example")
                or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".log"}):
            failures.append(name + ": forbidden release path")
            continue
        # Читаем именно staged-содержимое, а не иную рабочую версию.
        data = subprocess.check_output(["git", "show", ":" + name], cwd=ROOT)
        count += 1
        if len(data) > 5_000_000:
            failures.append(name + ": unexpectedly large file")
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(content.splitlines(), 1):
            if any(re.search(pattern, line) for pattern in PATTERNS):
                failures.append(f"{name}:{number}: possible credential; inspect locally")
    if not count:
        raise SystemExit("No staged files to check")
    if failures:
        raise SystemExit("\n".join(failures))
    print(f"PASS: {count} staged files checked; no forbidden paths or obvious credentials")


if __name__ == "__main__":
    main()
