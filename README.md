# mosaic

A plagiarism checker for source code that runs on your own machine. It is built to catch what MOSS misses.

MOSS does one thing: winnowing fingerprints over a stream of normalized tokens. That catches retyped and renamed code. It loses track as soon as a copier rewrites `for` as `while`, turns a `switch` into an `if/else` chain, splits statements or reorders functions. MOSS also gives you only a percentage, and it needs a base file before it will ignore skeleton code.

mosaic keeps MOSS's algorithm as one signal among five. It also judges every score against the rest of the cohort.

| Signal | What it catches |
|---|---|
| **Winnowing fingerprints** (MOSS's algorithm) | retyped or renamed copies |
| **Normalized structure trees** (subtree hashing) | `for`↔`while`, `switch`↔`if/else`, `x++`↔`x = x + 1`, `a > b`↔`b < a`, reordered functions and statements, dropped types and docstrings |
| **Identifier correspondence** | systematic renaming. The report shows the actual map (`tmp → hold`, `n → count`, …) |
| **Compression distance (NCD)** | heavy but shallow obfuscation |
| **Construct profile** | weak supporting evidence only |

**Two scores, side by side.** Every pair shows its **mosaic score** (all five signals combined) and its **MOSS score** (winnowing-fingerprint overlap alone: the share of each file covered by shared passages, the number MOSS itself reports, with provided code excluded). The room dashboard and the report have a **Rank by: mosaic score / MOSS score** toggle; in the terminal use `--rank-by moss`. A pair is listed when either score reaches the threshold. A big gap between the two (high mosaic, low MOSS) is the signature of a rewritten copy.

On top of the signals, at the cohort level:

- **Automatic boilerplate suppression.** Material shared by more than 30% of submissions is treated as provided code. You don't need a base file (`--base` still works).
- **z-scores.** Each pair is scored against the cohort's own distribution, using the median and MAD. In a cohort where everyone writes similar code, 40% can be normal. In a diverse cohort it is unusual.
- **Clusters.** Copy rings are grouped transitively, so six students sharing one solution show up as a single group instead of 15 separate pairs.
- **Explanations.** Each flagged pair comes with reasons, for example *"structure matches although the text does not: rewritten, not retyped"*.

Python and C/C++ get full structural analysis. Java, JavaScript, Go, Rust, C# and others get the token, rename and compression signals, plus a structure tree from the tolerant C-family parser.

## Usage

Requires Python 3.10 or later. It has no dependencies.

```bash
python -m mosaic submissions/ -o report.html
```

Put each submission in its own top-level folder (`submissions/alice/…`, `submissions/bob/…`). Files from the same submission are never compared with each other. Use `--by-file` to compare every file with every other file.

```bash
python -m mosaic submissions/ --base skeleton/ --lang cpp --json results.json --open
```

| Option | Default | |
|---|---|---|
| `-t, --threshold` | 0.25 | report pairs whose mosaic or MOSS score reaches this |
| `--rank-by` | mosaic | order pairs by `mosaic` or `moss` score (the HTML report also has a toggle) |
| `--base PATH` | | provided code to ignore (repeatable) |
| `--max-df` | 0.30 | auto-suppress material shared by more than this share of submissions |
| `-k`, `-w` | 14, 12 | k-gram length and winnowing window |
| `--min-subtree` | 6 | smallest structural match counted, in nodes |
| `--lang` | auto | restrict to one language |
| `--detail` | 60 | pairs expanded with side-by-side code in the report |
| `--json PATH` | | machine-readable results |

## Rooms: collect submissions from everyone's own computer

```bash
python -m mosaic serve
```

Then open `http://127.0.0.1:8765/create`.

1. **The admin creates a room.** They give it a name, choose **Hidden** or **Visible** (see below), and set how long it stays open. The room gets a code like `K7QM-29TX` and a join link.
2. **Peers join from their own PC.** They open the site, type the code (or use the link), enter their name or roll number, and drop in their folder or a `.zip`. They can replace or withdraw their own submission. They never see anyone else's files.
3. **The admin runs the check** from the room page and gets the results table, clusters and full report.
4. **The admin closes the room.** Every file and report in it is deleted and the code stops working. Rooms also close automatically when their time runs out, and stopping the server closes every room.

One server can hold many rooms at once. Closing one room doesn't affect the others.

### Hidden vs visible

This is chosen when the room is created and **cannot be changed afterwards**. Peers see which mode the room uses before they upload.

| | Visible | Hidden |
|---|---|---|
| Scores, reasons, clusters | ✓ | ✓ |
| Which lines match, colour-paired across files | ✓ | ✓ (on blurred line shapes) |
| Reading the code side by side | ✓ | — |
| Identifier rename map (`tmp → hold`) | ✓ | count only |

In hidden mode the code is not just blurred with CSS. **No source text is ever sent to a browser**, the admin's included. The report, the JSON export and the dashboard are built without it, and a test checks every identifier from the submissions against all of them. File names are still shown.

### Predictor rooms: class MPKI, no individual results

For the branch-predictor assignment (`bpsim/`), choose **Predictor MPKI** when creating a room. Peers upload their `mine.cpp`; the server builds it against the simulator, runs it on the traces, and publishes only the **class average and median MPKI** (plus the same two numbers for each trace).

| Who | Sees |
|---|---|
| Everyone in the room (admin and peers) | how many submissions were evaluated, failed to run, or did not build; average and median MPKI; per-trace average and median |
| The admin | the names of those who submitted and when; counts of builds ready / in progress / not accepted |
| Each submitter, about their own upload only | whether it built, and if not, why (rule violations from `tools/check_rules.py`, compiler errors, storage over budget); the storage it uses |
| Nobody | anyone's code, or anyone's MPKI, including their own |

How that is enforced:

- **The source is dropped once compiled.** Only the compiled program stays in memory, and nobody can download it. The admin cannot upload code into these rooms.
- **No individual result is ever stored.** The evaluator is handed the programs without names, in random order. Each program's MPKIs are added to running lists inside one function, reduced to averages and medians, and the lists are cleared. Failures are only counted. A test checks that no non-median individual value appears in any page or API response, or in the room's state.
- **Program output is never shown.** Submitters see only messages the server writes itself, plus compiler errors about their own file. Includes are checked before compiling, so an `#include` of any file outside the simulator and the standard library (a trace, for example) is refused.
- **Builds and runs are sandboxed**, one at a time across the server, in a private temporary folder that is deleted as soon as the job ends. Each job has CPU-time, memory and output-size limits, a minimal environment, and no network where the OS allows unprivileged namespaces (`mosaic serve` says which). A predictor that hangs is killed after `--bp-run-seconds` and counted as failed. The server marks itself non-dumpable, so a submitted program cannot read the server's memory through `/proc`.

Each submission's MPKI is the mean over the traces (what `bpsim` prints as "average MPKI"); the room reports the average and median of those across submissions.

Things to know:

- **Small groups reveal individuals.** With one evaluated submission, the average *is* that person's MPKI; with two, each person can work out the other's from their own. With an odd number of submissions, the median is one person's actual score, though not whose. Comparing results before and after one person withdraws reveals that person's score. Evaluate once, after closing submissions, with a reasonable number of people.
- **Traces.** Rooms run on the public traces in `bpsim/traces/`, the same ones students have. `--bp-traces DIR` (`MOSAIC_BP_TRACES`) points at a different folder if that is ever needed.
- **The rules checker and the include check are strong but not proof.** A determined student could still find C++ that reaches around them, for example to read files while running. The traces are public anyway, and they would have no way to see anyone's result.

| Predictor options | Default | |
|---|---|---|
| `--bp-harness` | `bpsim/` | simulator folder with `src/` and `tools/check_rules.py` (`MOSAIC_BP_HARNESS`) |
| `--bp-traces` | `<harness>/traces` | traces to run on (`MOSAIC_BP_TRACES`) |
| `--bp-run-seconds` | 600 | time limit for one submission on all traces (`MOSAIC_BP_RUN_SECONDS`) |
| `--no-predictor-rooms` | | turn predictor rooms off |

The included traces take about a quarter of a second per submission on a laptop. On Render's free tier, allow roughly ten times that.

### Nothing is stored

There is no database and no upload folder. Uploads, unpacked archives and reports exist only in the server's memory, and closing a room drops them. The one exception is predictor rooms: a compiler and a program need real files, so each build or run gets a private temporary folder that is deleted the moment it finishes, and only one exists at a time. A test runs two complete rooms (upload, unpack, check, report, close) and fails if anything is opened for writing. In hidden mode the admin never receives the code. In visible mode, code only reaches the admin's disk if they click *Download report*.

A database would mean the files *are* stored somewhere, which is why there isn't one. The central piece is the memory-only server itself.

The limits of that guarantee: whoever runs the server process could in principle inspect its memory. Python cannot securely erase memory, and the OS may swap it to disk under pressure. "Deleted" means dropped from the program and freed, not forensically wiped. If that matters, run the server yourself, or on a machine you trust.

### Where to run it

**On your laptop, same Wi-Fi** (classroom or lab). Run `python -m mosaic serve`; peers use the `http://192.168.x.x:8765` address it prints. Windows will ask once whether to let Python through the firewall; allow it on private networks.

**On your laptop, peers anywhere.** Expose it with a tunnel and tell mosaic its public address:

```bash
cloudflared tunnel --url http://localhost:8765
```

```bash
python -m mosaic serve --public-url https://<name>.trycloudflare.com --create-key <pick-a-key>
```

**On Render, always available (free).** The repo includes a `render.yaml` blueprint, so deploying is three clicks:

1. Push this folder to a GitHub repository (private is fine).
2. On [render.com](https://render.com), sign in with GitHub, then choose **New → Blueprint**, pick the repository, and click **Apply**.
3. When the deploy finishes, open the service's **Environment** tab and copy `MOSAIC_CREATE_KEY`. Anyone creating a room enters this key.

The site lives at `https://<service-name>.onrender.com`, and every push to the repository redeploys it. mosaic reads Render's `RENDER_EXTERNAL_URL` and `PORT` itself, so join links come out right without any extra settings.

About Render's free plan:

- **Rooms last as long as you set.** Render's free plan puts a service to sleep after 15 minutes without visitors, and sleeping would restart the server and close every room. To prevent that, while at least one room is open mosaic requests its own public address every 10 minutes, so a room set to 4 hours stays open for 4 hours even if nobody visits. When no rooms are open the pings stop and Render may let it sleep; the first visit afterwards takes 30–60 seconds. This is on by default on Render (`--keep-awake` / `MOSAIC_KEEP_AWAKE=1` elsewhere, `--no-keep-awake` to turn it off).
- **A restart still closes every room.** Rooms live only in memory, so a redeploy (every push, with `autoDeploy`) or a rare platform restart drops them. Avoid pushing while a room is in use.
- **Free hours.** The free plan gives 750 instance hours a month, enough for one service running around the clock.
- **512 MB of memory.** The blueprint caps uploads at 250 MB in total (120 MB per room, 15 rooms), which is plenty for source code.
- **Run exactly one instance.** Rooms live in that instance's memory.

**Other cloud hosts.** The `Dockerfile` runs on Railway, Fly.io, Google Cloud Run or any Docker host. It has no volume and no database, and can run with a read-only filesystem. Set `MOSAIC_CREATE_KEY` (a secret) and `MOSAIC_PUBLIC_URL` (the site's https address); the host sets `PORT`.

| `mosaic serve` option | Default | |
|---|---|---|
| `--create-key` | none | require this key to create rooms (`MOSAIC_CREATE_KEY`) |
| `--public-url` | | the address peers use behind a tunnel or cloud host (`MOSAIC_PUBLIC_URL`) |
| `--host` | 0.0.0.0 | `127.0.0.1` keeps it to this computer |
| `--port` | `$PORT` or 8765 | |
| `--max-rooms` | 50 | rooms open at once |
| `--max-room-hours` | 24 | longest a room may stay open |
| `--max-upload-mb` | 25 | per peer upload |
| `--max-room-mb` / `--max-total-mb` | 512 / 2048 | memory budgets per room and overall (`MOSAIC_MAX_ROOM_MB`, `MOSAIC_MAX_TOTAL_MB`) |
| `--keep-awake` | on on Render | while rooms are open, ping the public URL every 10 minutes so free hosts don't sleep (`MOSAIC_KEEP_AWAKE`) |
| `--behind-proxy` | off (on in Docker) | trust `X-Forwarded-For` for rate limiting |

Safeguards:

- **Access.**
  - Room codes are random 8-character codes, and wrong guesses are rate-limited per IP.
  - Each room has its own admin token, which is never shown to peers.
  - Peers need a receipt kept in their browser to replace or withdraw their upload.
- **Uploads.**
  - Archive paths are cleaned, so nothing can escape its submission.
  - Zip bombs and oversized uploads are refused.
  - Only source files are kept.
- **Browser.**
  - Requests from other websites are blocked.
  - Pages are sent with `no-store` and a strict Content-Security-Policy.

## The report

The report is a single self-contained HTML file that works offline and in light or dark mode. It contains:

- a histogram of pair scores against the threshold
- a ranked list of submission pairs with five-signal bars
- clusters
- an expandable view for each pair, with:
  - a signal breakdown
  - the reasons the pair was flagged
  - the identifier rename map
  - side-by-side code: grey borders mark shared token passages, purple borders mark identical normalized structure, and matching background colours pair a passage with its counterpart (hover to highlight)

## Results on the included tests

On `tests/corpus/`, the C pair rewritten with `for`→`while`, `switch`→`if` and renamed variables reaches only **40% token overlap**, which is roughly what MOSS would see. Its structural match is **95%**. The highest score for an independent pair is 26%.

On a synthetic cohort of 305 submissions sharing a skeleton, with 5 planted copies (verbatim, renamed, and renamed with `for`→`while`), all 5 copies rank in the top 5 at 94–97% (z ≈ 7). The next pair has z = 3.4. The run takes about 8 seconds, and the inverted index cuts the 46k possible pairs down to the candidates that need comparing.

```bash
python -m unittest discover -s tests -v
```

## Limitations

- Similarity is evidence, not a verdict. Read every flagged pair yourself.
- Short, tightly specified assignments converge naturally. Check the z-score as well as the raw percentage.
- Hidden rooms protect code from people, not from whoever runs the server; see *Nothing is stored*.
- Comparisons stay within one language; a solution translated into another language is not detected.
- Code written by an LLM from the same prompt can look like collusion. mosaic does not try to detect AI authorship.
