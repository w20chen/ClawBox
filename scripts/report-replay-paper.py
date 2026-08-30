#!/usr/bin/env python3
"""Generate fail-closed JSON and Markdown from registered replay suites."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from clawbox.replay.paper_report import build_report, markdown_report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--main", required=True, type=Path)
    parser.add_argument("--density", required=True, type=Path)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    args = parser.parse_args()
    report = build_report(args.main, args.density)
    json_text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    markdown = markdown_report(report)
    if args.json_out:
        args.json_out.write_text(json_text, encoding="utf-8")
    if args.markdown_out:
        args.markdown_out.write_text(markdown, encoding="utf-8")
    if not args.json_out and not args.markdown_out:
        print(markdown, end="")


if __name__ == "__main__":
    main()
