import csv
import sys


class Summary:
    def __init__(self):
        self.n = 0
        self.total = 0.0
        self.sq = 0.0
        self.items = []

    def push(self, x):
        self.n += 1
        self.total += x
        self.sq += x * x
        self.items.append(x)

    def mean(self):
        return self.total / self.n if self.n else 0.0

    def var(self):
        if self.n < 2:
            return 0.0
        mu = self.mean()
        return (self.sq - self.n * mu * mu) / (self.n - 1)

    def median(self):
        self.items.sort()
        mid = self.n // 2
        if self.n % 2:
            return self.items[mid]
        return 0.5 * (self.items[mid - 1] + self.items[mid])


def main():
    if len(sys.argv) < 2:
        print("usage: summary FILE")
        return 1
    acc = Summary()
    with open(sys.argv[1], newline="") as fh:
        for row in csv.reader(fh):
            for cell in row:
                if cell.strip():
                    acc.push(float(cell))
    if acc.n == 0:
        print("no data")
        return 1
    print("count", acc.n)
    print("mean", acc.mean())
    print("median", acc.median())
    print("var", acc.var())
    return 0


if __name__ == "__main__":
    sys.exit(main())
