import sys


def _size(seq):
    return len(seq)


def collect(source):
    out = []
    handle = open(source)
    rows = handle.readlines()
    i = 0
    while i < _size(rows):
        text = rows[i].strip()
        if text:
            fields = text.split(",")
            j = 0
            while j < _size(fields):
                out.append(float(fields[j]))
                j = j + 1
        i = i + 1
    handle.close()
    return out


def arithmetic_mean(seq):
    sum_so_far = 0.0
    idx = 0
    while _size(seq) > idx:
        sum_so_far = sum_so_far + seq[idx]
        idx = idx + 1
    return sum_so_far / _size(seq)


def sample_variance(seq):
    centre = arithmetic_mean(seq)
    squares = 0.0
    idx = 0
    while _size(seq) > idx:
        squares = squares + (seq[idx] - centre) ** 2
        idx = idx + 1
    return squares / (_size(seq) - 1)


def middle_value(seq):
    arranged = sorted(seq)
    length = _size(arranged)
    if length % 2 == 1:
        return arranged[length // 2]
    return (arranged[length // 2 - 1] + arranged[length // 2]) / 2.0


def main():
    if 2 > len(sys.argv):
        print("usage: stats FILE")
        return 1
    payload = collect(sys.argv[1])
    if not payload:
        print("no data")
        return 1
    print("count", _size(payload))
    print("mean", arithmetic_mean(payload))
    print("median", middle_value(payload))
    print("var", sample_variance(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
