import pytest

from tests.release_gate import check_report


@pytest.mark.parametrize("body", ["", '<testcase name="required"><skipped /></testcase>'])
def test_release_gate_refuses_missing_or_skipped_checks(tmp_path, body):
    report = tmp_path / "report.xml"
    report.write_text(f"<testsuites><testsuite>{body}</testsuite></testsuites>")
    with pytest.raises(ValueError):
        check_report(report)


def test_release_gate_accepts_executed_checks(tmp_path):
    report = tmp_path / "report.xml"
    report.write_text('<testsuites><testsuite><testcase name="required" /></testsuite></testsuites>')
    assert check_report(report) == 1
