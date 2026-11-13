import sys


def read_numbers(path):
    values = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            for part in line.split(","):
                values.append(float(part))
    return values


def mean(values):
    total = 0.0
    for v in values:
        total += v
    return total / len(values)


def variance(values):
    m = mean(values)
    acc = 0.0
    for v in values:
        acc += (v - m) ** 2
    return acc / (len(values) - 1)


def median(values):
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def main():
    if len(sys.argv) < 2:
        print("usage: stats FILE")
        return 1
    data = read_numbers(sys.argv[1])
    if not data:
        print("no data")
        return 1
    print("count", len(data))
    print("mean", mean(data))
    print("median", median(data))
    print("var", variance(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
