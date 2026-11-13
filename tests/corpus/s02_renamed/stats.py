import sys


# compute the average of a sequence
def average(nums):
    running = 0.0
    for item in nums:
        running += item
    return running / len(nums)


def middle(nums):
    ordered = sorted(nums)
    count = len(ordered)
    if count % 2 == 1:
        return ordered[count // 2]
    return (ordered[count // 2 - 1] + ordered[count // 2]) / 2.0


def spread(nums):
    avg = average(nums)
    bucket = 0.0
    for item in nums:
        bucket += (item - avg) ** 2
    return bucket / (len(nums) - 1)


def load_values(filename):
    nums = []
    with open(filename) as handle:
        for row in handle:
            row = row.strip()
            if not row:
                continue
            for chunk in row.split(","):
                nums.append(float(chunk))
    return nums


def main():
    if len(sys.argv) < 2:
        print("usage: stats FILE")
        return 1
    rows = load_values(sys.argv[1])
    if not rows:
        print("no data")
        return 1
    print("count", len(rows))
    print("mean", average(rows))
    print("median", middle(rows))
    print("var", spread(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
