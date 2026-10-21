#!/usr/bin/env python3
"""Offline, read-only audit. Python 3.12+, standard library only."""
import argparse
import json
import math
import re
import stat
import sys
from decimal import Context, Decimal, InvalidOperation
from pathlib import Path

MAX_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 50000
MAX_FINDINGS = 100

class InputError(ValueError):
    """A safe, non-payload-bearing input error code."""

class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise InputError("invalid_arguments")

def read_text(path):
    p = Path(path)
    if not stat.S_ISREG(p.stat().st_mode):
        raise InputError("regular_file_required")
    with p.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise InputError("file_size_limit")
    return data.decode("utf-8")

def reject_constant(value):
    raise InputError("non_finite_json_number")

def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError("duplicate_json_member")
        result[key] = value
    return result

def parse_decimal(value):
    # Decimal construction must not inherit a host application's disabled traps.
    return Decimal(value, context=Context(traps=[InvalidOperation]))

def parse_json(text):
    try:
        result = json.loads(text, object_pairs_hook=unique_pairs,
                            parse_float=parse_decimal, parse_constant=reject_constant)
    except InputError:
        raise
    except (ValueError, RecursionError, InvalidOperation):
        raise InputError("invalid_json") from None
    stack, nodes = [(result, 0)], 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if depth > 128 or nodes > 200000:
            raise InputError("json_structure_limit")
        if isinstance(value, dict):
            stack.extend((v, depth + 1) for v in value.values())
        elif isinstance(value, list):
            stack.extend((v, depth + 1) for v in value)
    return result

class Report:
    def __init__(self, tool):
        self.tool, self.findings, self.count = tool, [], 0
    def add(self, code, **location):
        self.count += 1
        if len(self.findings) < MAX_FINDINGS:
            self.findings.append(dict(code=code, **location))
    def finish(self, **summary):
        return {"tool": self.tool, "status": "fail" if self.count else "pass",
                "summary": dict(summary, finding_count=self.count),
                "findings": self.findings,
                "findings_truncated": self.count > len(self.findings)}

def emit_main(run):
    try:
        result = run()
        code = 1 if result["status"] == "fail" else 0
    except InputError as exc:
        result, code = {"tool": TOOL, "status": "error", "error": str(exc)}, 2
    except (OSError, UnicodeError):
        result, code = {"tool": TOOL, "status": "error", "error": "unreadable_utf8_input"}, 2
    except (ValueError, RecursionError, InvalidOperation):
        result, code = {"tool": TOOL, "status": "error", "error": "invalid_input"}, 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False))
    return code

TOOL = "record-key-collision-audit"

import unicodedata

def component(value, policy):
    if value is None:
        return ("null", None)
    if type(value) is bool:
        return ("boolean", value)
    if isinstance(value, (dict, list)):
        raise InputError("scalar_key_component_required")
    if isinstance(value, str):
        if len(value) > 65536:
            raise InputError("key_size_limit")
        if policy == "text":
            return ("text", unicodedata.normalize("NFC", value).casefold().strip())
        if policy == "number" and re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value):
            if len(value) > 128:
                raise InputError("numeric_key_size_limit")
            try:
                value = parse_decimal(value)
            except InvalidOperation:
                raise InputError("numeric_key_magnitude_limit") from None
        else:
            return ("string", value)
    if type(value) is int or isinstance(value, Decimal):
        number = Decimal(value)
        if not number.is_finite() or len(number.as_tuple().digits) > 100 or abs(number.as_tuple().exponent) > 100:
            raise InputError("numeric_key_magnitude_limit")
        if policy == "text":
            # Canonical decimal text without context rounding; integers and 1.0
            # become "1". String "1.0" is intentionally not numeric-normalized.
            string = format(number, "f")
            if "." in string:
                string = string.rstrip("0").rstrip(".")
            if number == 0:
                string = "0"
            return ("text", string)
        if policy == "number":
            return ("number", number)
        return ("integer" if type(value) is int else "decimal", number)
    raise InputError("unsupported_key_component")

def audit(records, *, keys, normalization="typed", nulls="reject"):
    if normalization not in ("typed", "text", "number") or nulls not in ("reject", "match", "skip"):
        raise InputError("invalid_key_policy")
    if not isinstance(keys, list) or not 1 <= len(keys) <= 20 or any(not isinstance(k, str) or not k or len(k) > 1024 for k in keys) or len(set(keys)) != len(keys):
        raise InputError("invalid_key_fields")
    if not isinstance(records, list) or not 1 <= len(records) <= MAX_RECORDS:
        raise InputError("nonempty_record_array_required")
    report = Report(TOOL)
    seen, frequencies = {}, {}
    eligible = skipped = duplicate_rows = pairs = 0
    for number, record in enumerate(records, 1):
        if not isinstance(record, dict):
            raise InputError("object_record_required")
        missing = [i for i, key in enumerate(keys, 1) if key not in record]
        if missing:
            report.add("missing_key_field", record=number, key_positions=missing)
            skipped += 1
            continue
        values = [record[key] for key in keys]
        empty = [i for i, value in enumerate(values, 1) if value is None]
        if empty and nulls != "match":
            skipped += 1
            if nulls == "reject":
                report.add("null_key_component", record=number, key_positions=empty)
            continue
        signature = tuple(component(value, normalization) for value in values)
        eligible += 1
        previous_count = frequencies.get(signature, 0)
        pairs += previous_count  # Incremental n*(n-1)//2 without enumerating pairs.
        frequencies[signature] = previous_count + 1
        if signature in seen:
            duplicate_rows += 1
            report.add("key_collision", record=number, first_record=seen[signature])
        else:
            seen[signature] = number
    return report.finish(records=len(records), eligible_records=eligible, skipped_records=skipped,
                         unique_keys=len(seen), duplicate_rows=duplicate_rows, collision_pairs=pairs,
                         collision_groups=sum(n > 1 for n in frequencies.values()),
                         normalization=normalization, nulls=nulls, key_components=len(keys))

def run():
    parser = Parser(description=__doc__)
    parser.add_argument("input", help="JSON array of records")
    parser.add_argument("--key", action="append", required=True, help="Repeat for a composite key")
    parser.add_argument("--normalization", choices=("typed", "text", "number"), default="typed")
    parser.add_argument("--nulls", choices=("reject", "match", "skip"), default="reject")
    args = parser.parse_args()
    return audit(parse_json(read_text(args.input)), keys=args.key,
                 normalization=args.normalization, nulls=args.nulls)

if __name__ == "__main__":
    sys.exit(emit_main(run))
