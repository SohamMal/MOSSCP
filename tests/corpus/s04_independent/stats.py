import statistics
import sys
from pathlib import Path


def parse(text):
    tokens = text.replace("\n", ",").split(",")
    return [float(t) for t in tokens if t.strip()]


def describe(sample):
    return {
        "count": len(sample),
        "mean": statistics.fmean(sample),
        "median": statistics.median(sample),
        "var": statistics.variance(sample) if len(sample) > 1 else 0.0,
    }


def main(argv):
    if len(argv) < 2:
        sys.stderr.write("usage: stats FILE\n")
        return 2
    sample = parse(Path(argv[1]).read_text())
    if not sample:
        sys.stderr.write("no data\n")
        return 2
    for key, value in describe(sample).items():
        print(f"{key}\t{value:.6g}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
