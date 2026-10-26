# FIO Analysis Tools

Unified Python tool for analyzing FIO (Flexible I/O Tester) JSON results: IOPS, bandwidth, graphs, perf-report tables, and cross-tier comparisons.

## Contents

- [Overview](#overview)
- [Installation](#installation)
- [analyze_bw_mean_with_graphs.py](#analyze_bw_mean_with_graphspy)
  - [Metric flags](#metric-flags)
  - [Options](#options)
  - [Examples](#examples)
- [Input layout](#input-layout)
- [Output files](#output-files)
- [Tips](#tips)
- [Troubleshooting](#troubleshooting)

## Overview

`analyze_bw_mean_with_graphs.py` can:

- Extract IOPS and/or bandwidth from FIO JSON under any machine subdirectories
- Choose **overall rate** (`iops` / `bw`) or **sample mean** (`iops_mean` / `bw_mean`)
- Generate CSV + PNG (bar, line, or both)
- Build operation summaries across block sizes
- Compare multiple result directories (cross-tier / multi-run)
- Build perf-report style throughput/latency tables and BW graphs
- Extract harness start/complete times into `testtimes*.txt`
- Optionally patch a `perf-report.md` Cross-Tier section

Supported operations: `read`, `write`, `randread`, `randwrite`, `rw`, `randrw`.

## Installation

- Python 3.6+
- FIO results in JSON format

```bash
pip install pandas matplotlib
```

## analyze_bw_mean_with_graphs.py

```bash
python3 analyze_bw_mean_with_graphs.py --help
```

You must pass at least one of: `--iops`, `--iops-mean`, `--bw`, `--bw-mean`, `--perf-report-table`, `--perf-report-bw-graph`, `--cross-tier-latency`, `--latency-bs`.

### Metric flags

| Flag | FIO field | Meaning |
|------|-----------|---------|
| `--iops` | `iops` | Overall IOPS = I/Os / runtime |
| `--iops-mean` | `iops_mean` | Mean of status samples (older default) |
| `--bw` | `bw` | Overall BW = bytes / runtime (closer to Grafana/RBD, especially with fsync) |
| `--bw-mean` | `bw_mean` | Mean of status samples (older default) |

`--iops` and `--iops-mean` are mutually exclusive; same for `--bw` / `--bw-mean`. You can combine one IOPS flag with one BW flag (e.g. `--iops --bw`).

### Options

| Option | Description |
|--------|-------------|
| `--input-dir DIR [DIR ...]` | One or more FIO result trees. Multiple dirs → compare/cross-tier mode. A single parent with 2+ child result dirs is auto-expanded. Default: `.` |
| `--input-dirs DIR [DIR ...]` | Alias for multi-dir `--input-dir` (`--input-dirs` wins if both are set) |
| `--run-labels LABELS` | Comma-separated labels paired by position with input dirs (count must match). Used as-is on cross-tier axes/tables. Default: directory basenames |
| `--combined` | Emit `combined_*` run-comparison graphs (requires 2+ input dirs; off by default) |
| `--output-dir DIR` | Where to write CSV/PNG/Markdown (default: `.`) |
| `--graph-type {bar,line,both}` | Graph style (default: `bar`) |
| `--block-sizes SIZES` | Comma-separated block sizes, e.g. `4k,8k,128k` |
| `--operation-summary` | Per-operation summary across all block sizes |
| `--summary-only` | Summary graphs only (skip per-machine graphs) |
| `--show-values` | Annotate numeric values on bars/points |
| `--perf-report-table` | Throughput + latency tables (CSV, Markdown, PNG) |
| `--perf-report-bw-graph` | Total BW (MB/s) by operation from throughput summary |
| `--cross-tier-latency` | Per-operation latency multipanel graphs under `latency_crosstier/` |
| `--latency-bs SIZES` | Cross-tier only: per op×bs latency detail tables under `latency-bs/` |
| `--latency-bs-ops OPS` | With `--latency-bs`: limit ops, e.g. `randread,randwrite` |
| `--latency-bs-baseline LABEL` | With `--latency-bs`: baseline tier for ratio column (must match a `--run-labels` entry) |
| `--update-perf-report PATH` | After compare mode, patch Cross-Tier section in the given `perf-report.md` |

### Examples

```bash
# Single run — overall IOPS + BW, line graphs + operation summary
python3 analyze_bw_mean_with_graphs.py \
  --iops --bw \
  --input-dir fio-results \
  --output-dir iops-bw-output \
  --graph-type line \
  --operation-summary \
  --summary-only

# Sample-mean metrics (legacy-style fields)
python3 analyze_bw_mean_with_graphs.py --iops-mean --bw-mean --input-dir fio-results/ --output-dir out/

# Specific block sizes
python3 analyze_bw_mean_with_graphs.py --iops --bw-mean --graph-type both --block-sizes 4k,8k,128k

# Compare two runs (cross-tier)
python3 analyze_bw_mean_with_graphs.py --iops --bw \
  --input-dir /path/run100 /path/run1000 \
  --run-labels "Large,Medium" \
  --output-dir compare/

# Same via --input-dirs + optional combined_* graphs
python3 analyze_bw_mean_with_graphs.py --bw-mean --combined \
  --input-dirs /path/run100 /path/run1000 \
  --run-labels "100 VMs,1000 VMs" \
  --output-dir compare/

# Parent dir with multiple tier children + patch perf-report.md
python3 analyze_bw_mean_with_graphs.py --iops --bw \
  --input-dir /path/tiers/ \
  --run-labels "Large,Medium,Standard,Small,Tiny" \
  --output-dir compare/ \
  --update-perf-report containers-bench/high-scale/perf-report.md

# Perf-report tables + Total BW graph
python3 analyze_bw_mean_with_graphs.py \
  --perf-report-table --iops --bw --perf-report-bw-graph \
  --input-dir fio-results/ --output-dir out/

# Cross-tier latency detail for selected block sizes
python3 analyze_bw_mean_with_graphs.py --iops --bw \
  --input-dirs tier-250/ tier-500/ tier-1000/ \
  --run-labels "250vm,500vm,1000vm" \
  --latency-bs 4k,8k \
  --latency-bs-ops randread,randwrite \
  --latency-bs-baseline 250vm \
  --output-dir compare/
```

## Input layout

JSON files live in **subdirectories** under each result root (any subdirectory name; not limited to `vm-*`):

```
fio-results/                 # --input-dir
├── vm-1/   (or machine-1/, host-a/, …)
│   ├── fio-test-read-bs-4k.json
│   ├── fio-test-write-bs-4k.json
│   └── ...
├── vm-2/
│   └── ...
└── fio-test-*.txt           # optional harness log → testtimes.txt
```

JSON filenames must include the FIO **operation** and **block size** (e.g. `fio-read-4k.json`, `write-fio-1024k.json`).

For compare mode, pass multiple roots (or one parent that contains several such trees).

## Output files

Outputs go under `--output-dir`. CSVs for many paths land in `csv_files/`.

### Single-run analysis

- Per op/block-size summaries and graphs (`summary-*`, machine/job summary CSVs)
- With `--operation-summary`: `summary-{operation}-all-blocks.csv` and comparison PNGs
- `testtimes.txt` when a harness `fio-test-*.txt` log is found

### Multi-run / cross-tier

- Per-operation cross-tier graphs: `{operation}_cross_tier_comparison.png`, `{operation}_bw_cross_tier_comparison.png`, `{operation}_iops_cross_tier_comparison.png`
- Key workloads: `key_cross_tier_comparison.png` (+ tables)
- Aggregate tables: `perf_report_cross_tier_comparison.md`, `perf_report_cross_tier_{slug}.png`, `perf_report_cross_tier_{operation}_{iops|bw}_table.png`
- With `--combined`: `combined_block_size_*_operation_*_*.{csv,png}`
- With `--cross-tier-latency`: `latency_crosstier/{operation}_latency_cross_tier_comparison.png`
- With `--latency-bs`: `latency-bs/{operation}_{bs}_latency_detail.png` (+ CSV)
- Multi-input test times: `testtimes-<label>.txt`

### Perf-report helpers

- `csv_files/perf_report_throughput_summary.csv`, latency summary CSV
- Markdown/PNG table renders from `--perf-report-table`
- Total BW by operation graph from `--perf-report-bw-graph`

Exact names vary with graph type and metric suffix; see script docstring/`--help` for the full list.

## Tips

1. Prefer `--iops --bw` when matching Grafana/RBD sustained rates; use `--iops-mean --bw-mean` only if you need the old sample-mean behavior.
2. Keep `--run-labels` order aligned with `--input-dir` / `--input-dirs` or tiers will be mislabeled.
3. Use `--summary-only` for quick smoke runs; drop it when you need per-machine graphs.
4. For compare graphs of totals across runs, add `--combined`.

## Troubleshooting

1. **Must specify a metric** — pass `--iops`/`--iops-mean` and/or `--bw`/`--bw-mean` (or a report/latency flag).
2. **No subdirs with JSON** — put FIO JSON under machine subdirectories, not only at the result root.
3. **`--run-labels` count mismatch** — label count must equal resolved input-dir count (after auto-expand).
4. **`--combined` / `--latency-bs` need 2+ dirs** — single-run mode does not produce those artifacts.
5. **Missing deps** — `pip install pandas matplotlib`.

## Contributing

1. Keep CLI help and this README aligned when flags or outputs change.
2. Prefer the script docstring/`--help` as the detailed source of truth for new examples.
3. Test with single-run and multi-dir (cross-tier) layouts.
