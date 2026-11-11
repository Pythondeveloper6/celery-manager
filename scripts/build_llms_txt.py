"""Build docs/llms-full.txt: the documentation as one Markdown file for agents.

    python scripts/build_llms_txt.py          # write the file
    python scripts/build_llms_txt.py --check  # exit 1 if it is out of date
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
OUT = DOCS / "llms-full.txt"
# Left out: the changelog is an include of CHANGELOG.md and is large; llms.txt links it.
SKIP = {"changelog.md"}
HEADER = (
    "# celery-manager: full documentation\n\n"
    "Generated from docs/*.md by scripts/build_llms_txt.py. Index: llms.txt.\n"
)


def page_order() -> list[str]:
    """Pages in mkdocs.yml nav order, so the file reads like the site."""
    names = re.findall(r":\s*([\w.-]+\.md)\s*$", (ROOT / "mkdocs.yml").read_text(encoding="utf-8"), re.MULTILINE)
    seen: list[str] = []
    for name in names:
        if name not in seen and name not in SKIP and (DOCS / name).is_file():
            seen.append(name)
    return seen


def build() -> str:
    parts = [HEADER]
    for name in page_order():
        text = (DOCS / name).read_text(encoding="utf-8").strip()
        text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.DOTALL).strip()
        text = re.sub(r"!\[[^\]]*\]\([^)]*\)(\{[^}]*\})?\n?", "", text)  # screenshots mean nothing to an agent
        parts.append(f"\n\n<!-- docs/{name} -->\n\n{text}\n")
    return "".join(parts)


def main(argv: list[str]) -> int:
    text = build()
    if "--check" in argv:
        if not OUT.is_file() or OUT.read_text(encoding="utf-8") != text:
            print("docs/llms-full.txt is out of date: run python scripts/build_llms_txt.py")
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(text):,} characters, {len(page_order())} pages)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
