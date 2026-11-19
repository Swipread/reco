"""Fail if the JUnit report has no integration test run, or any of them skipped (CI guard:
without RECO_TEST_DSN the integration tests skip silently)."""

import sys
import xml.etree.ElementTree as ET

cases = [c for c in ET.parse(sys.argv[1]).iter("testcase")
         if c.get("classname", "").startswith("tests.integration")]
skipped = [c for c in cases if c.find("skipped") is not None]
if not cases or skipped:
    print(f"integration tests: {len(cases)} found, {len(skipped)} skipped; is RECO_TEST_DSN set?",
          file=sys.stderr)
    sys.exit(1)
print(f"integration tests: {len(cases)} ran")
