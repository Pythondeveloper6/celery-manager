"""Fail a configured release gate if any selected check silently skipped."""
import sys
from xml.etree import ElementTree


def check_report(path):
    root = ElementTree.parse(path).getroot()
    cases = root.findall(".//testcase")
    if not cases:
        raise ValueError("Release gate collected no tests")
    skipped = [case.get("name", "unknown") for case in cases if case.find("skipped") is not None]
    if skipped:
        raise ValueError("Release gate skipped required checks: " + ", ".join(skipped))
    return len(cases)


if __name__ == "__main__":
    try:
        count = check_report(sys.argv[1])
    except (ValueError, IndexError) as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Release gate executed {count} checks without skips")
