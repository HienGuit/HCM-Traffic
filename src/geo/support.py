"""One-dimensional metre support: union, intersection and atomic partitions."""


def union(pieces: list[tuple[float, float]]) -> list[tuple[float, float]]:
    result = []
    for start, end in sorted(pieces):
        if end <= start:
            continue
        if result and start <= result[-1][1] + 1e-8:
            result[-1] = (result[-1][0], max(end, result[-1][1]))
        else:
            result.append((start, end))
    return result


def length(pieces) -> float:
    return sum(end - start for start, end in union(pieces))


def intersection(first, second):
    return union(
        [(max(a, c), min(b, d)) for a, b in first for c, d in second if min(b, d) > max(a, c)]
    )


def contains(pieces, start, end) -> bool:
    return any(a <= start + 1e-7 and b >= end - 1e-7 for a, b in pieces)


def atomic(pieces, length_m):
    points = sorted(
        {0.0, length_m}
        | {max(0, min(length_m, v)) for group in pieces for pair in group for v in pair}
    )
    return [(a, b) for a, b in zip(points, points[1:]) if b > a + 1e-9]
