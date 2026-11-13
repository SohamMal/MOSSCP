import sys
from functools import reduce


def numbers_from(path):
    with open(path) as fh:
        blob = fh.read()
    pieces = [p.strip() for p in blob.replace(";", ",").split(",")]
    return tuple(float(p) for p in pieces if p)


def centre(xs):
    return reduce(lambda a, b: a + b, xs, 0.0) / len(xs)


def dispersion(xs):
    mu = centre(xs)
    return sum((x - mu) ** 2 for x in xs) / (len(xs) - 1)


def midpoint(xs):
    ys = tuple(sorted(xs))
    half, rem = divmod(len(ys), 2)
    return ys[half] if rem else (ys[half - 1] + ys[half]) / 2


def report(xs):
    labels = ("count", "mean", "median", "var")
    produced = (len(xs), centre(xs), midpoint(xs), dispersion(xs))
    for label, value in zip(labels, produced):
        print(label, value)


def main():
    try:
        xs = numbers_from(sys.argv[1])
    except (IndexError, OSError):
        print("usage: compute FILE")
        return 1
    if not xs:
        print("no data")
        return 1
    report(xs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
