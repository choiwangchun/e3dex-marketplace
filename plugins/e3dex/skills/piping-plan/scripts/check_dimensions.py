"""Compare selected model-space linear dimensions with projected model anchors."""
import argparse
import json
import math
import sys
from pathlib import Path


def records(path):
    # Geometry fields, entity types and section names use ASCII even in legacy DXF.
    with path.open(encoding="latin-1") as stream:
        current = {}
        for line in stream:
            code = int(line.strip())
            value = next(stream, None)
            if value is None:
                raise ValueError("Incomplete DXF group")
            if code == 0:
                if current:
                    yield current
                current = {}
            current.setdefault(code, value.strip())
        if current:
            yield current


def dimensions(path):
    section = ""
    for row in records(path):
        kind = row.get(0)
        if kind == "SECTION":
            section = row.get(2)
        elif kind == "ENDSEC":
            section = ""
        elif kind == "DIMENSION" and section == "ENTITIES":
            if row.get(67, "0") != "0" or row.get(410, "Model").lower() != "model":
                continue
            if int(row[70]) & 7 or row.get(1, "") not in ("", "<>"):
                continue  # Rotated linear type only, with automatic dimension text.
            normal = tuple(float(row.get(k, v)) for k, v in ((210, 0), (220, 0), (230, 1)))
            if normal == (0, 0, 1):
                yield row


def point(row, key):
    return float(row[key]), float(row[key + 10])


def matches(row, start, end, axis, baseline, tolerance):
    angle = float(row.get(50, 0))
    rotation_error = abs((angle - axis * 90 + 90) % 180 - 90)
    distance = abs(end[axis] - start[axis])
    if rotation_error > 1e-5 or 42 not in row:
        return False
    if abs(float(row[42]) - distance) > tolerance:
        return False
    if abs(point(row, 10)[1 - axis] - baseline) > tolerance:
        return False
    a, b = point(row, 13), point(row, 14)
    return any(math.dist(a, p) <= tolerance and math.dist(b, q) <= tolerance
               for p, q in ((start, end), (end, start)))


def verify(rows, groups, tolerance):
    seen = set()
    errors = []
    if not groups or not isinstance(groups, list):
        raise ValueError("Expected a nonempty list of dimension groups")
    for group in groups:
        axis = {"x": 0, "y": 1}[group["axis"]]
        pts = group["points"]
        base = float(group["baseline"])
        if len(pts) < 2 or not math.isfinite(base):
            raise ValueError("Invalid chain or baseline")
        if any(len(p) != 2 or not all(math.isfinite(v) for v in p) for p in pts):
            raise ValueError("Expected finite XY coordinate pairs")
        for index, (p, q) in enumerate(zip(pts, pts[1:]), 1):
            if abs(p[axis] - q[axis]) <= tolerance:
                raise ValueError(f"Degenerate span {p} -> {q}")
            hits = [i for i, row in enumerate(rows)
                    if matches(row, p, q, axis, base, tolerance)]
            if len(hits) != 1 or hits[0] in seen:
                errors.append(f"{group['name']} #{index} {p} -> {q}: "
                              f"{len(hits)} matches, one unique entity required")
            else:
                seen.add(hits[0])
    for message in errors:
        print(message, file=sys.stderr)
    print(f"{len(seen)} matched, {len(errors)} failed")
    return 1 if errors else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dxf", type=Path)
    parser.add_argument("expected", type=Path)
    parser.add_argument("--tolerance", type=float, required=True,
                        help="positive absolute tolerance in DXF units")
    args = parser.parse_args()
    if not math.isfinite(args.tolerance) or args.tolerance <= 0:
        parser.error("tolerance must be finite and positive")
    try:
        groups = json.loads(args.expected.read_text(encoding="utf-8-sig"))["groups"]
        return verify(list(dimensions(args.dxf)), groups, args.tolerance)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(2, f"Invalid input: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
