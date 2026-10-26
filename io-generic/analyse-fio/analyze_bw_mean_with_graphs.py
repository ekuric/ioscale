#!/usr/bin/env python3
"""
Unified FIO Analysis Tool
========================

This script analyzes both IOPS and bandwidth values from FIO JSON files and generates comprehensive reports and graphs.

Bandwidth can use either FIO field:
- ``bw_mean`` (``--bw-mean``): mean of status samples (previous default)
- ``bw`` (``--bw``): overall rate = bytes / runtime (closer to Grafana/RBD client BW, especially with fsync)

IOPS can use either FIO field (same idea as bandwidth):
- ``iops_mean`` (``--iops-mean``): mean of status samples (previous default)
- ``iops`` (``--iops``): overall rate = I/Os / runtime

Features:
- Analyzes FIO JSON files from any subdirectories (not just vm-*)
- Supports both IOPS and bandwidth analysis
- Generates CSV files with performance data
- Creates bar charts, line graphs, or both
- Supports operation summary graphs with all block sizes
- Configurable block size filtering
- Customizable output directory
- X-axis machine marking for better readability
- Supports mixed read/write operations (rw, randrw)
- Builds perf-report.md style throughput/latency summary tables (CSV, Markdown, PNG)
- Optional Total BW by Operation graph from throughput summary
- Extracts test start/complete times from fio-test-*.txt into testtimes.txt
  (under --output-dir; one testtimes-<label>.txt per --input-dirs entry)
- Cross-tier comparison: per op×bs tables/graphs plus one multipanel graph per operation
  ({operation}_cross_tier_comparison.png with block-size subgraphs)
- Per-operation BW/IOPS cross-tier graphs (latency-style layout + % change)
  ({operation}_bw_cross_tier_comparison.png, {operation}_iops_cross_tier_comparison.png)
- Key cross-tier workloads (4k randread/randwrite IOPS + 1024k read/write BW):
  tables, key_cross_tier_comparison.png, optional --update-perf-report patch
- Cross-tier Aggregate IOPS/BW tables for every tested op × block size written to
  --output-dir alongside graphs (perf_report_cross_tier_comparison.md,
  perf_report_cross_tier_{slug}.png, perf_report_cross_tier_{slug}_table.png)
- Merged per-operation table PNGs with all block sizes in one image:
  perf_report_cross_tier_{operation}_{iops|bw}_table.png
- Cross-tier latency multipanel graphs from latency summaries
  (latency_crosstier/{operation}_latency_cross_tier_comparison.png)
- Per block-size latency detail tables across tiers (--latency-bs):
  latency-bs/{operation}_{bs}_latency_detail.png (IOPS, clat p50/p99, iodepth)

Usage:
    python3 analyze_bw_mean_with_graphs.py [options]

Options:
    --iops                 Analyze IOPS using FIO overall iops (I/Os/runtime)
    --iops-mean / --iops_mean  Analyze IOPS using FIO iops_mean (sample mean)
    --bw-mean / --bw_mean  Analyze bandwidth using FIO bw_mean (sample mean)
    --bw                   Analyze bandwidth using FIO bw (bytes/runtime)
    --input-dir DIR ...    One or more FIO result directories. Multiple dirs = compare mode
                           (cross-tier key tables/graphs). A single parent dir with two or
                           more child result dirs is auto-expanded into compare mode.
    --input-dirs DIR ...   Alias for multi-dir --input-dir (same behavior)
                           (use --combined for combined_* graphs)
    --run-labels LABELS    Comma-separated labels for input dirs, paired by position
                           (1st label → 1st dir, 2nd → 2nd, …). Count must match.
                           Names need not equal directory names; order must match
                           the data or graphs will be mislabeled.
                           Cross-tier graphs/tables use these labels as-is on the X-axis.
                           Default: directory basenames
    --combined             Generate combined_* run-comparison graphs (off by default)
    --output-dir DIR       Directory to save output files (default: current directory)
    --graph-type TYPE      Type of graphs: bar, line, or both (default: bar)
    --block-sizes SIZES    Comma-separated block sizes to analyze (e.g., "4k,8k,128k")
    --operation-summary    Generate operation summary graphs (all block sizes combined)
    --summary-only         Generate only summary graphs (skip per-Machine comparison graphs)
    --show-values          Annotate numeric values on bar and line graph data points
    --perf-report-table    Build perf-report throughput/latency tables (CSV, Markdown, PNG)
    --perf-report-bw-graph Plot Total BW (MB/s) by Operation from throughput summary
    --cross-tier-latency   Build per-operation latency cross-tier multipanel graphs
                           (latency_crosstier/{operation}_latency_cross_tier_comparison.png)
    --latency-bs SIZES     Cross-tier only: per op×block-size latency detail tables (PNG/CSV)
                           under latency-bs/ (IOPS, clat p50/p99, iodepth_level/complete)
    --latency-bs-ops OPS   With --latency-bs: limit operations (e.g. "randread,randwrite")
    --latency-bs-baseline LABEL
                           With --latency-bs: add vs-baseline ratio column (3+ tiers) or
                           rename Ratio column to vs LABEL (2 tiers); LABEL must match
                           one of --run-labels
    --update-perf-report PATH
                           Patch the Cross-Tier Comparison section in the given
                           perf-report.md with the four key workload tables + graph links
    --help                 Show this help message

Examples:
    python3 analyze_bw_mean_with_graphs.py --iops-mean
    python3 analyze_bw_mean_with_graphs.py --iops
    python3 analyze_bw_mean_with_graphs.py --bw-mean
    python3 analyze_bw_mean_with_graphs.py --bw
    python3 analyze_bw_mean_with_graphs.py --iops --input-dir /path/to/data --output-dir /path/to/results
    python3 analyze_bw_mean_with_graphs.py --bw-mean --graph-type line --block-sizes 4k,8k,128k
    python3 analyze_bw_mean_with_graphs.py --iops-mean --operation-summary --graph-type both
    python3 analyze_bw_mean_with_graphs.py --iops --bw --input-dir /path/run100 /path/run1000 \\
        --run-labels "Large,Medium" --output-dir compare/
    python3 analyze_bw_mean_with_graphs.py --iops --bw --input-dir /path/tiers/ \\
        --run-labels "Large,Medium,Standard,Small,Tiny" --output-dir compare/ \\
        --update-perf-report containers-bench/high-scale/perf-report.md
    python3 analyze_bw_mean_with_graphs.py --bw --input-dirs /path/run100 /path/run1000 --run-labels "100 VMs,1000 VMs"
    python3 analyze_bw_mean_with_graphs.py --bw-mean --combined --input-dirs /path/run100 /path/run1000 --run-labels "100 VMs,1000 VMs"
    python3 analyze_bw_mean_with_graphs.py --iops-mean --bw-mean --input-dirs tier-250/ tier-500/ tier-1000/ \\
        --run-labels "250vm,500vm,1000vm" --output-dir compare/
    python3 analyze_bw_mean_with_graphs.py --bw --input-dirs run-a/ run-b/ run-c/ --output-dir compare/
    python3 analyze_bw_mean_with_graphs.py --perf-report-table --iops --bw --perf-report-bw-graph --input-dir fio-results/ --output-dir out/
    python3 analyze_bw_mean_with_graphs.py --perf-report-table --iops-mean --bw-mean --perf-report-bw-graph --input-dir fio-results/ --output-dir out/
    python3 analyze_bw_mean_with_graphs.py --perf-report-bw-graph --input-dir result_fio-results/ --output-dir result_fio-results/

Supported Operations:
    - read: Sequential read operations
    - write: Sequential write operations  
    - randread: Random read operations
    - randwrite: Random write operations
    - rw: Sequential mixed reads and writes (readwrite)
    - randrw: Random mixed reads and writes
"""

import json
import os
import matplotlib.pyplot as plt
import pandas as pd
import glob
import re
import argparse
import sys
import csv
from collections import defaultdict
from pathlib import Path

def ensure_csv_directory(output_dir):
    """Ensure the csv_files subdirectory exists within the output directory."""
    csv_dir = os.path.join(output_dir, 'csv_files')
    os.makedirs(csv_dir, exist_ok=True)
    return csv_dir


# Match harness "Running test" / "Completed test" lines (Linux + Windows logs).
# Examples:
#   [ts] INFO: Running test 1/20 (19 remaining): read with block size 4k
#   [ts] INFO: Running test 1: randread with block size 1024k
#   [ts] INFO: Completed test 1/20: write with block size 4k (19 remaining)
# Intentionally skips "Running Linux/Windows test ... on hosts: [...]" lines.
_TEST_TIME_RE = re.compile(
    r'^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+INFO:\s+'
    r'(?P<kind>Running|Completed)\s+test\s+'
    r'(?P<num>\d+)(?:/(?P<total>\d+))?'
    r'(?:\s+\([^)]*\))?'
    r':\s+(?P<op>\w+)\s+with block size\s+(?P<bs>\S+)'
)


def find_fio_test_log(input_dir):
    """Locate fio-test-*.txt log inside a results directory (prefer newest)."""
    if not input_dir or not os.path.isdir(input_dir):
        return None
    candidates = sorted(
        glob.glob(os.path.join(input_dir, 'fio-test-*.txt')),
        key=lambda p: os.path.getmtime(p),
        reverse=True,
    )
    if candidates:
        return candidates[0]
    # Fallback: any top-level *.txt that looks like a harness log
    for path in sorted(glob.glob(os.path.join(input_dir, '*.txt'))):
        base = os.path.basename(path)
        if base == 'testtimes.txt' or base.startswith('testtimes-'):
            continue
        try:
            with open(path, 'r', errors='replace') as fh:
                head = fh.read(8192)
            if 'INFO: Running test' in head or 'INFO: Completed test' in head:
                return path
        except OSError:
            continue
    return None


def parse_fio_test_times(log_path):
    """
    Parse start/complete timestamps from a fio-tests harness log.

    Returns list of dicts: ts, kind ('Running'|'Completed'), num, total, op, bs
    """
    events = []
    try:
        with open(log_path, 'r', errors='replace') as fh:
            for raw in fh:
                line = raw.strip()
                if 'INFO: Running test' not in line and 'INFO: Completed test' not in line:
                    continue
                # Skip platform-specific "Running Linux/Windows test ... on hosts"
                if (
                    ' on hosts:' in line
                    or 'Running Linux test' in line
                    or 'Running Windows test' in line
                ):
                    continue
                match = _TEST_TIME_RE.match(line)
                if not match:
                    continue
                total = match.group('total')
                bs = match.group('bs').rstrip(').,],')
                events.append({
                    'ts': match.group('ts'),
                    'kind': match.group('kind'),
                    'num': int(match.group('num')),
                    'total': int(total) if total else None,
                    'op': match.group('op'),
                    'bs': bs,
                })
    except OSError as exc:
        print(f"Warning: could not read test log {log_path}: {exc}")
        return []
    return events


def format_test_time_line(event):
    """Normalize to: [ts] INFO: Running test N/M : op with block size bs"""
    if event.get('total'):
        idx = f"{event['num']}/{event['total']}"
    else:
        idx = str(event['num'])
    return (
        f"[{event['ts']}] INFO: {event['kind']} test {idx} : "
        f"{event['op']} with block size {event['bs']}"
    )


def write_test_times_file(input_dir, output_path, run_label=None):
    """
    Extract test start/complete times from the harness log under input_dir
    and write a Grafana-friendly testtimes.txt to output_path.
    """
    log_path = find_fio_test_log(input_dir)
    if not log_path:
        print(f"Warning: no fio-test-*.txt log found under {input_dir}; skipping testtimes")
        return None

    events = parse_fio_test_times(log_path)
    if not events:
        print(f"Warning: no Running/Completed test lines in {log_path}; skipping testtimes")
        return None

    out_dir = os.path.dirname(os.path.abspath(output_path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    label = run_label or os.path.basename(input_dir.rstrip(os.sep)) or input_dir
    with open(output_path, 'w') as out:
        out.write(f"# Test times extracted for Grafana from: {os.path.basename(log_path)}\n")
        out.write(f"# Source dir: {input_dir}\n")
        if run_label:
            out.write(f"# Run label: {run_label}\n")
        out.write(
            "# Format: [timestamp] INFO: Running|Completed test N/M : "
            "<op> with block size <bs>\n"
        )
        out.write("#\n")
        for event in events:
            out.write(format_test_time_line(event) + '\n')

    n_run = sum(1 for e in events if e['kind'] == 'Running')
    n_done = sum(1 for e in events if e['kind'] == 'Completed')
    print(
        f"Wrote test times for '{label}': {n_run} start(s), {n_done} complete(s) "
        f"-> {output_path}"
    )
    return output_path


def extract_test_times_for_inputs(input_dirs, output_dir, run_labels=None):
    """
    For each input results directory, write testtimes.txt under output_dir.

    Single input  -> <output_dir>/testtimes.txt
    Multiple      -> <output_dir>/testtimes-<label>.txt  (one per input-dir)
    """
    labels = run_labels or [
        os.path.basename(path.rstrip(os.sep)) or path for path in input_dirs
    ]
    written = []
    if len(input_dirs) == 1:
        out_path = os.path.join(output_dir, 'testtimes.txt')
        path = write_test_times_file(input_dirs[0], out_path, run_label=labels[0])
        if path:
            written.append(path)
        return written

    for input_dir, label in zip(input_dirs, labels):
        safe = re.sub(r'[^\w.\-]+', '_', str(label)).strip('_') or 'run'
        out_path = os.path.join(output_dir, f'testtimes-{safe}.txt')
        path = write_test_times_file(input_dir, out_path, run_label=label)
        if path:
            written.append(path)
    return written


# Global variable to store FIO configurations for subtitles
FIO_CONFIGS = {}

# FIO reports bandwidth in KiB/s; we convert to MB/s (MiB/s, /1024) for all
# CSVs, graphs, and perf-report tables so units stay consistent.
# BW_JSON_FIELD selects which FIO JSON key to read: 'bw_mean' or 'bw'.
BW_RATE_UNIT = 'MB/s'
BW_KIB_TO_MIB = 1024.0
BW_JSON_FIELD = 'bw_mean'
IOPS_JSON_FIELD = 'iops_mean'


def set_bw_json_field(field: str) -> None:
    """Select FIO bandwidth JSON field: 'bw_mean' (samples) or 'bw' (bytes/runtime)."""
    global BW_JSON_FIELD
    if field not in ('bw_mean', 'bw'):
        raise ValueError(f"Unsupported BW JSON field: {field}")
    BW_JSON_FIELD = field


def bw_field_label() -> str:
    """Human-readable label for the active FIO bandwidth field."""
    if BW_JSON_FIELD == 'bw':
        return 'FIO bw (bytes/runtime)'
    return 'FIO bw_mean (sample mean)'


def set_iops_json_field(field: str) -> None:
    """Select FIO IOPS JSON field: 'iops_mean' (samples) or 'iops' (I/Os/runtime)."""
    global IOPS_JSON_FIELD
    if field not in ('iops_mean', 'iops'):
        raise ValueError(f"Unsupported IOPS JSON field: {field}")
    IOPS_JSON_FIELD = field


def iops_field_label() -> str:
    """Human-readable label for the active FIO IOPS field."""
    if IOPS_JSON_FIELD == 'iops':
        return 'FIO iops (I/Os/runtime)'
    return 'FIO iops_mean (sample mean)'


def bw_kib_s_to_mb_s(bw_kib_per_s):
    """Convert FIO bw_mean (KiB/s) to MB/s for display and storage."""
    return float(bw_kib_per_s) / BW_KIB_TO_MIB

def get_block_size_display_name(block_size):
    """Convert block size to display name."""
    block_size_map = {
        '4k': '4K',
        '8k': '8K', 
        '16k': '16K',
        '32k': '32K',
        '64k': '64K',
        '128k': '128K',
        '256k': '256K',
        '512k': '512K',
        '1024k': '1024K',
        '4096k': '4096K'
    }
    return block_size_map.get(block_size.lower(), block_size.upper())


# Cap on-graph labels for dense machine charts (still annotate combined/cross-tier graphs).
SHOW_VALUES_MAX_POINTS = 60


def format_graph_value(value, compact=False):
    """Format a numeric value for on-graph labels."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return ''
    if value != value:  # NaN
        return ''
    abs_v = abs(value)
    if compact:
        sign = '-' if value < 0 else ''
        abs_v = abs(value)
        if abs_v >= 1_000_000:
            scaled = abs_v / 1_000_000
            if scaled >= 100:
                return f'{sign}{scaled:.0f}M'
            if scaled >= 10:
                return f'{sign}{scaled:.1f}M'
            return f'{sign}{scaled:.2f}M'
        if abs_v >= 10_000:
            scaled = abs_v / 1_000
            if scaled >= 100:
                return f'{sign}{scaled:.0f}k'
            if scaled >= 10:
                return f'{sign}{scaled:.1f}k'
            return f'{sign}{scaled:.2f}k'
    if abs_v >= 100:
        return f'{value:.0f}'
    if abs_v >= 10:
        return f'{value:.1f}'
    return f'{value:.2f}'


def _infer_cross_tier_metric(workload=None, value_key=None):
    """Infer iops/bandwidth/latency for axis-aligned bar labels."""
    if workload and workload.get('metric'):
        return workload['metric']
    if value_key:
        key = str(value_key).lower()
        if 'bw' in key or 'bandwidth' in key:
            return 'bandwidth'
        if 'iops' in key:
            return 'iops'
        if 'latency' in key or '(us)' in key or key.startswith('p'):
            return 'latency'
    return 'default'


def format_axis_aligned_value(value, metric='default', compact_subplot=False):
    """
    Format on-bar values to match the Y-axis scale (same numeric units as ticks).

    Bandwidth labels use plain MB/s numbers (never 55.4k). IOPS uses plain counts
  unless the subplot is very dense and values are large.
    """
    try:
        value = float(value)
    except (TypeError, ValueError):
        return ''
    if value != value:
        return ''

    if metric == 'bandwidth':
        # Y-axis is MB/s — bar labels must use the same scale, not k/M shorthand.
        return format_graph_value(value, compact=False)

    if metric == 'iops':
        if compact_subplot and abs(value) >= 10_000:
            return format_graph_value(value, compact=True)
        return format_graph_value(value, compact=False)

    if metric == 'latency':
        return format_graph_value(value, compact=False)

    return format_graph_value(value, compact=compact_subplot and abs(value) >= 10_000)


def graph_value_fontsize(n_points):
    """Pick a readable label size based on how many points are on the chart."""
    if n_points <= 15:
        return 9
    if n_points <= 40:
        return 7
    return 6


def _value_label_path_effects():
    """White outline so bold value labels stay readable on colored bars/lines."""
    try:
        from matplotlib import patheffects as pe
        return [pe.withStroke(linewidth=3.0, foreground='white')]
    except Exception:
        return None


def annotate_bar_values(ax, bars, fontsize=7, rotation=90):
    """Draw bold, high-contrast value labels on top of a bar container."""
    effects = _value_label_path_effects()
    for bar in bars:
        height = bar.get_height()
        if height is None or height != height or height <= 0:
            continue
        txt = ax.text(
            bar.get_x() + bar.get_width() / 2.0,
            height,
            format_graph_value(height),
            ha='center',
            va='bottom',
            fontsize=fontsize,
            fontweight='bold',
            color='#111111',
            rotation=rotation,
            clip_on=False,
            zorder=20,
        )
        if effects is not None:
            txt.set_path_effects(effects)


def _cross_tier_value_label_rotation(n_tiers, compact_subplot=False):
    """Pick bar-value label rotation for cross-tier paired-bar subplots."""
    if compact_subplot or n_tiers >= 3:
        return 90
    if n_tiers >= 2:
        return 45
    return 0


def _cross_tier_plot_headroom(n_tiers, show_values, show_percent_change, compact_subplot=False):
    """Y-axis headroom multiplier for cross-tier value and % labels."""
    use_vertical_pct = show_percent_change and (n_tiers >= 3 or compact_subplot)
    headroom = 1.15
    if show_values and show_percent_change and n_tiers >= 2:
        if compact_subplot:
            headroom = 1.78 if use_vertical_pct else 1.62
        else:
            headroom = 1.58 if use_vertical_pct else 1.48
    elif show_values:
        headroom = 1.52 if compact_subplot else 1.38
    elif show_percent_change and n_tiers >= 2:
        headroom = 1.48 if use_vertical_pct else 1.32
    return headroom


def _cross_tier_percent_rotation(n_tiers, show_values, compact_subplot=False):
    """Rotation for % change labels on cross-tier charts."""
    if compact_subplot or n_tiers >= 3:
        return 90
    if show_values and n_tiers >= 2:
        return 45
    if n_tiers >= 2:
        return 0
    return 0


def _bboxes_overlap(bbox_a, bbox_b, pad=2.0):
    """True when two display-space bounding boxes overlap (with padding)."""
    if bbox_a is None or bbox_b is None:
        return False
    return not (
        bbox_a.x1 + pad < bbox_b.x0
        or bbox_b.x1 + pad < bbox_a.x0
        or bbox_a.y1 + pad < bbox_b.y0
        or bbox_b.y1 + pad < bbox_a.y0
    )


def _resolve_text_collisions(ax, texts, fig, ymax, y_step_frac=0.07, max_iterations=5):
    """
    Nudge or rotate labels on one axes to reduce overlap.

    Strategy per colliding pair: rotate 0→45→90, then bump y upward.
    """
    if not texts or fig is None or ymax is None or ymax <= 0:
        return

    y_step = ymax * y_step_frac
    try:
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
    except Exception:
        return

    for _ in range(max_iterations):
        changed = False
        bboxes = []
        for txt in texts:
            try:
                bboxes.append(txt.get_window_extent(renderer=renderer))
            except Exception:
                bboxes.append(None)

        for i in range(len(texts)):
            if bboxes[i] is None:
                continue
            for j in range(i + 1, len(texts)):
                if bboxes[j] is None or not _bboxes_overlap(bboxes[i], bboxes[j]):
                    continue
                txt = texts[j]
                rotation = txt.get_rotation() or 0
                if rotation < 45:
                    txt.set_rotation(45)
                    changed = True
                elif rotation < 90:
                    txt.set_rotation(90)
                    changed = True
                else:
                    x_pos, y_pos = txt.get_position()
                    txt.set_position((x_pos, y_pos + y_step))
                    changed = True

        if not changed:
            break
        try:
            fig.canvas.draw()
            renderer = fig.canvas.get_renderer()
        except Exception:
            break


def _expand_ylim_for_texts(ax, texts, fig, pad_frac=0.06):
    """Raise ylim if any label extends above the current top."""
    if not texts or fig is None:
        return
    try:
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
        ymin, ymax = ax.get_ylim()
        top_data = ymax
        for txt in texts:
            bbox = txt.get_window_extent(renderer=renderer)
            inv = ax.transData.inverted()
            _, y_top = inv.transform((bbox.x0, bbox.y1))
            top_data = max(top_data, y_top)
        if top_data > ymax:
            span = ymax - ymin if ymax > ymin else ymax or 1.0
            ax.set_ylim(ymin, top_data + span * pad_frac)
    except Exception:
        pass


def annotate_bar_values_smart(
    ax,
    bars,
    fontsize=7,
    n_tiers=1,
    compact_subplot=False,
    compact_numbers=True,
    chart_max=None,
    x_stagger_frac=0.0,
    metric='default',
):
    """
    Place bar value labels with rotation, in-bar placement, and collision handling.

    When metric is 'bandwidth', labels use MB/s numbers matching the Y-axis (not k/M).

    Returns list of matplotlib Text artists (empty when nothing drawn).
    """
    effects = _value_label_path_effects()
    rotation = _cross_tier_value_label_rotation(n_tiers, compact_subplot=compact_subplot)
    texts = []
    ymax = chart_max
    if ymax is None:
        try:
            ymax = ax.get_ylim()[1]
        except Exception:
            ymax = 0.0
    if ymax <= 0:
        heights = [bar.get_height() for bar in bars if bar.get_height() and bar.get_height() > 0]
        ymax = max(heights) if heights else 1.0

    use_compact = compact_numbers and metric not in ('bandwidth', 'latency')
    if metric == 'bandwidth':
        use_compact = False

    for bar in bars:
        height = bar.get_height()
        if height is None or height != height or height <= 0:
            continue

        bar_center = bar.get_x() + bar.get_width() / 2.0
        x_pos = bar_center + (bar.get_width() * x_stagger_frac)
        if use_compact:
            label = format_graph_value(height, compact=True)
        else:
            label = format_axis_aligned_value(
                height, metric=metric, compact_subplot=compact_subplot
            )
        inside_bar = height >= 0.28 * ymax and rotation == 0

        if inside_bar:
            y_pos = height * 0.5
            va = 'center'
            label_color = '#ffffff'
            use_effects = _value_label_path_effects()
        else:
            y_pos = height
            va = 'bottom'
            label_color = '#111111'
            use_effects = effects

        if rotation == 90:
            ha = 'center'
            va = 'bottom'
            y_pos = height
        elif rotation == 45:
            ha = 'center'
            va = 'bottom'
            y_pos = height
        else:
            ha = 'center'

        txt = ax.text(
            x_pos,
            y_pos,
            label,
            ha=ha,
            va=va,
            fontsize=fontsize,
            fontweight='bold',
            color=label_color,
            rotation=rotation,
            clip_on=False,
            zorder=20,
        )
        if use_effects is not None:
            txt.set_path_effects(use_effects)
        texts.append(txt)

    return texts


def annotate_line_values(ax, x_values, y_values, fontsize=7, color='black',
                         metric='default', compact_subplot=False):
    """Draw bold, high-contrast value labels above line/scatter points."""
    effects = _value_label_path_effects()
    # Darken pure black slightly; keep series colors (with white stroke) for multi-line charts
    label_color = '#111111' if color in (None, 'black', '#000000', 'k') else color
    for x, y in zip(x_values, y_values):
        try:
            y = float(y)
        except (TypeError, ValueError):
            continue
        if y != y or y <= 0:
            continue
        txt = ax.text(
            x,
            y,
            format_axis_aligned_value(y, metric=metric, compact_subplot=compact_subplot),
            ha='center',
            va='bottom',
            fontsize=fontsize,
            fontweight='bold',
            color=label_color,
            clip_on=False,
            zorder=20,
        )
        if effects is not None:
            txt.set_path_effects(effects)


def get_x_axis_labels_and_positions(df_sorted):
    """
    Determine X-axis labels and positions based on number of Machines.
    Show every Machine if <= 20, every 30th if <= 500, every 50th if > 500.
    """
    num_vms = len(df_sorted)
    
    if num_vms <= 20:
        # Show every machine for small datasets
        x_positions = range(num_vms)
        x_labels = [f'Machine {i+1}' for i in x_positions]
        return x_positions, x_labels
    elif num_vms <= 500:
        # Show every 30th machine for medium datasets
        x_positions = range(0, num_vms, 30)
        x_labels = [f'Machine {i+1}' for i in x_positions]
        return x_positions, x_labels
    elif num_vms <= 1000:
        # Show every 50th machine for large datasets (> 500 machines)
        x_positions = range(0, num_vms, 50)
        x_labels = [f'Machine {i+1}' for i in x_positions]
        return x_positions, x_labels
    else:
        # Show every 100th machine for large datasets (> 500 machines)
        x_positions = range(0, num_vms, 100)
        x_labels = [f'Machine {i+1}' for i in x_positions]
        return x_positions, x_labels
    

def extract_fio_config_from_json(json_file_path):
    """
    Extract FIO configuration from JSON file for subtitle display.
    Returns a dictionary with configuration parameters.
    """
    config_data = {}
    try:
        with open(json_file_path, 'r') as f:
            data = json.load(f)
        
        # Extract configuration from first job
        if 'jobs' in data and len(data['jobs']) > 0:
            job_options = data['jobs'][0].get('job options', {})
            config_data = {
                'size': job_options.get('size', 'N/A'),
                'bs': job_options.get('bs', 'N/A'),
                'runtime': job_options.get('runtime', 'N/A'),
                'direct': job_options.get('direct', 'N/A'),
                'numjobs': job_options.get('numjobs', 'N/A'),
                'iodepth': job_options.get('iodepth', 'N/A'),
                'rate_iops': job_options.get('rate_iops', 'N/A')
            }
    except Exception as e:
        print(f"Error extracting FIO config from {json_file_path}: {e}")
    
    return config_data

def format_fio_subtitle(config_data, exclude_bs=False):
    """
    Format FIO configuration data into a subtitle string.
    
    Args:
        config_data: Dictionary containing FIO configuration
        exclude_bs: If True, exclude block size from subtitle (for comparison graphs)
    """
    subtitle_parts = []
    
    if config_data.get('size') != 'N/A':
        subtitle_parts.append(f"Size: {config_data['size']}")
    if config_data.get('bs') != 'N/A' and not exclude_bs:
        subtitle_parts.append(f"BS: {config_data['bs']}")
    if config_data.get('runtime') != 'N/A':
        subtitle_parts.append(f"Runtime: {config_data['runtime']}s")
    if config_data.get('direct') != 'N/A':
        subtitle_parts.append(f"Direct: {config_data['direct']}")
    if config_data.get('numjobs') != 'N/A':
        subtitle_parts.append(f"NumJobs: {config_data['numjobs']}")
    if config_data.get('iodepth') != 'N/A':
        subtitle_parts.append(f"IODepth: {config_data['iodepth']}")
    if config_data.get('rate_iops') != 'N/A':
        subtitle_parts.append(f"Rate IOPS: {config_data['rate_iops']}")
    
    return " | ".join(subtitle_parts)

def extract_block_size_from_filename(filename):
    """
    Extract block size from filename (e.g., 'fio-test-read-bs-4k.json' -> '4k')
    """
    match = re.search(r'bs-(\d+[kmg]?)', filename, re.IGNORECASE)
    if match:
        return match.group(1).lower()
    return 'unknown'

def extract_iops_from_json(json_file_path):
    """
    Extract IOPS values from a FIO JSON file.

    Uses module ``IOPS_JSON_FIELD`` (``iops_mean`` or ``iops``). Returns a
    dictionary with operation types, block sizes, and aggregated IOPS values.
    Also stores FIO configuration data for subtitles.
    """
    global FIO_CONFIGS
    iops_field = IOPS_JSON_FIELD
    iops_data = {}
    
    try:
        with open(json_file_path, 'r') as f:
            data = json.load(f)
        
        # Check if jobs exist
        if 'jobs' not in data:
            return iops_data
        
        # Extract operation type and block size from filename
        filename = os.path.basename(json_file_path)
        block_size = extract_block_size_from_filename(filename)
        
        # Skip files with unknown block sizes
        if block_size == 'unknown':
            print(f"Skipping {filename} - unknown block size")
            return iops_data
        
        # Determine operation type from filename
        if 'randrw' in filename or 'randrw' in filename.lower():
            operation = 'randrw'
        elif 'rw' in filename or 'readwrite' in filename:
            operation = 'rw'
        elif 'randread' in filename:
            operation = 'randread'
        elif 'randwrite' in filename:
            operation = 'randwrite'
        elif 'read' in filename:
            operation = 'read'
        elif 'write' in filename:
            operation = 'write'
        else:
            return iops_data
        
        # Extract FIO configuration for subtitles
        config_data = extract_fio_config_from_json(json_file_path)
        config_key = (operation, block_size)
        FIO_CONFIGS[config_key] = config_data
        
        # Collect all IOPS values and latency data for this operation across all jobs
        iops_values = []
        latency_values = []
        
        for job in data['jobs']:
            # For mixed operations (rw, randrw), we need to handle both read and write data
            if operation in ['rw', 'randrw']:
                # For mixed operations, sum both read and write IOPS
                total_iops = 0
                total_latency = 0
                latency_count = 0
                
                # Process read data
                if 'read' in job and iops_field in job['read']:
                    read_iops = job['read'][iops_field]
                    if read_iops > 0:
                        total_iops += read_iops
                
                # Process write data  
                if 'write' in job and iops_field in job['write']:
                    write_iops = job['write'][iops_field]
                    if write_iops > 0:
                        total_iops += write_iops
                
                # Extract latency data from both read and write
                if 'read' in job and 'lat_ns' in job['read'] and 'mean' in job['read']['lat_ns']:
                    latency_ns = job['read']['lat_ns']['mean']
                    latency_ms = latency_ns / 1000000
                    total_latency += latency_ms
                    latency_count += 1
                
                if 'write' in job and 'lat_ns' in job['write'] and 'mean' in job['write']['lat_ns']:
                    latency_ns = job['write']['lat_ns']['mean']
                    latency_ms = latency_ns / 1000000
                    total_latency += latency_ms
                    latency_count += 1
                
                # Store combined IOPS and average latency
                if total_iops > 0:
                    iops_values.append(total_iops)
                
                if latency_count > 0:
                    avg_latency = total_latency / latency_count
                    latency_values.append(avg_latency)
            else:
                # For single operations (read, write, randread, randwrite)
                data_key = 'read' if operation in ['read', 'randread'] else 'write'
                
                if data_key in job:
                    # Extract IOPS data (only non-zero values)
                    if iops_field in job[data_key]:
                        iops_value = job[data_key][iops_field]
                        if iops_value > 0:  # Only include non-zero IOPS
                            iops_values.append(iops_value)
                    
                    # Extract latency data independently (regardless of IOPS value)
                    if 'lat_ns' in job[data_key] and 'mean' in job[data_key]['lat_ns']:
                        latency_ns = job[data_key]['lat_ns']['mean']
                        latency_ms = latency_ns / 1000000  # Convert nanoseconds to milliseconds
                        latency_values.append(latency_ms)
        
        # Aggregate IOPS values (sum them since they represent total jobs per machine)
        if iops_values:
            # Sum all jobs to get total IOPS per machine
            total_iops = int(sum(iops_values))
            
            # Calculate average latency across all jobs
            avg_latency_ms = sum(latency_values) / len(latency_values) if latency_values else 0
            
            # Store both IOPS and latency data
            iops_data[(operation, block_size)] = {
                'total_iops': total_iops,
                'avg_latency_ms': avg_latency_ms
            }
    
    except (json.JSONDecodeError, KeyError, FileNotFoundError) as e:
        print(f"Error processing {json_file_path}: {e}")
    
    return iops_data

def extract_bw_mean_from_json(file_path):
    """Extract bandwidth values from a JSON file, filtering out zero values.

    Uses module ``BW_JSON_FIELD`` (``bw_mean`` or ``bw``). FIO stores both in
    KiB/s; values are converted to MB/s before return so all CSVs and graphs use
    MB/s consistently. Dict entries still use key ``bw_mean`` for internal
    compatibility.
    """
    global FIO_CONFIGS
    bw_field = BW_JSON_FIELD
    try:
        with open(file_path, 'r') as f:
            data = json.load(f)
        
        bw_values = []
        latency_values = []

        # Extract FIO parameters for subtitle
        fio_params = {}
        
        # Determine operation type from filename (same logic as IOPS extraction)
        filename = os.path.basename(file_path)
        if 'randrw' in filename or 'randrw' in filename.lower():
            operation = 'randrw'
        elif 'rw' in filename or 'readwrite' in filename:
            operation = 'rw'
        elif 'randread' in filename:
            operation = 'randread'
        elif 'randwrite' in filename:
            operation = 'randwrite'
        elif 'read' in filename:
            operation = 'read'
        elif 'write' in filename:
            operation = 'write'
        else:
            return bw_values, {}
        
        # Look for the selected bandwidth field in the jobs section
        if 'jobs' in data:
            for job in data['jobs']:
                # For mixed operations (rw, randrw), we need to handle both read and write data
                if operation in ['rw', 'randrw']:
                    # For mixed operations, sum both read and write bandwidth
                    total_bw = 0
                    total_latency = 0
                    latency_count = 0
                    
                    # Process read data
                    if 'read' in job and bw_field in job['read']:
                        read_bw = job['read'][bw_field]
                        if read_bw > 0:
                            total_bw += read_bw
                    
                    # Process write data
                    if 'write' in job and bw_field in job['write']:
                        write_bw = job['write'][bw_field]
                        if write_bw > 0:
                            total_bw += write_bw
                    
                    # Extract latency data from both read and write
                    if 'read' in job and 'lat_ns' in job['read'] and 'mean' in job['read']['lat_ns']:
                        latency_ns = job['read']['lat_ns']['mean']
                        latency_ms = latency_ns / 1000000
                        total_latency += latency_ms
                        latency_count += 1
                    
                    if 'write' in job and 'lat_ns' in job['write'] and 'mean' in job['write']['lat_ns']:
                        latency_ns = job['write']['lat_ns']['mean']
                        latency_ms = latency_ns / 1000000
                        total_latency += latency_ms
                        latency_count += 1
                    
                    # Store combined bandwidth (MB/s) and average latency
                    if total_bw > 0:
                        bw_values.append({
                            'operation': operation,
                            'bw_mean': bw_kib_s_to_mb_s(total_bw),
                            'job_name': job.get('jobname', 'unknown')
                        })
                    
                    if latency_count > 0:
                        avg_latency = total_latency / latency_count
                        latency_values.append(avg_latency)
                else:
                    # For single operations (read, write, randread, randwrite)
                    data_key = 'read' if operation in ['read', 'randread'] else 'write'
                    
                    if data_key in job and bw_field in job[data_key]:
                        bw_val = job[data_key][bw_field]
                        # Skip zero values
                        if bw_val > 0:
                            bw_values.append({
                                'operation': operation,
                                'bw_mean': bw_kib_s_to_mb_s(bw_val),
                                'job_name': job.get('jobname', 'unknown')
                            })
                            
                            # Extract latency data if available
                            if 'lat_ns' in job[data_key] and 'mean' in job[data_key]['lat_ns']:
                                latency_ns = job[data_key]['lat_ns']['mean']
                                latency_ms = latency_ns / 1000000  # Convert nanoseconds to milliseconds
                                latency_values.append(latency_ms)
        
        # Fallback regex only for bw_mean (exact "bw" also matches other keys poorly in edge cases)
        if not bw_values and bw_field == 'bw_mean':
            json_str = json.dumps(data)
            bw_mean_matches = re.findall(r'"bw_mean"\s*:\s*([0-9.]+)', json_str)
            for i, bw_val in enumerate(bw_mean_matches):
                try:
                    bw_val_float = float(bw_val)
                    if bw_val_float > 0:
                        bw_values.append({
                            'operation': 'unknown',
                            'bw_mean': bw_kib_s_to_mb_s(bw_val_float),
                            'job_name': f'job_{i}'
                        })
                except ValueError:
                    continue
        

        # Extract FIO configuration for subtitles
        config_data = extract_fio_config_from_json(file_path)
        
        # Calculate average latency if we have latency data
        avg_latency_ms = sum(latency_values) / len(latency_values) if latency_values else 0
        
        # Add latency data to each bandwidth entry
        for bw_entry in bw_values:
            bw_entry['avg_latency_ms'] = avg_latency_ms
        
        return bw_values, config_data
    
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
        return [], {}

def parse_filename_info(filename):
    """Parse operation and block size from filename."""
    # Pattern: fio-test-{operation}-bs-{blocksize}.json
    match = re.match(r'fio-test-(\w+)-bs-(\w+)\.json', filename)
    if match:
        operation = match.group(1).lower()  # Convert to lowercase for consistency
        block_size = match.group(2).lower()  # Convert to lowercase for consistency
        
        # Normalize operation names
        if operation in ['readwrite', 'rw']:
            operation = 'rw'
        elif operation in ['randrw', 'randreadwrite']:
            operation = 'randrw'
        elif operation == 'randread':
            operation = 'randread'
        elif operation == 'randwrite':
            operation = 'randwrite'
        elif operation == 'read':
            operation = 'read'
        elif operation == 'write':
            operation = 'write'
        
        return operation, block_size
    return None, None

def analyze_all_directories(input_dir='.'):
    """Analyze all directories for JSON files and extract bandwidth (BW_JSON_FIELD)."""
    
    # Find all directories containing FIO JSON files
    test_dirs = []
    for item in os.listdir(input_dir):
        item_path = os.path.join(input_dir, item)
        if os.path.isdir(item_path):
            # Check if this directory contains FIO JSON files
            json_files = glob.glob(os.path.join(item_path, "*.json"))
            if json_files:
                test_dirs.append(item_path)
    
    test_dirs.sort()  # Sort to ensure consistent order
    
    if not test_dirs:
        print("No directories containing FIO JSON files found!")
        return {}, {}
    
    # Results storage
    results = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    all_machines_results = defaultdict(lambda: defaultdict(list))
    
    print(f"Bandwidth field: {bw_field_label()} (JSON key '{BW_JSON_FIELD}')")
    print(f"Found {len(test_dirs)} directories with FIO JSON files:")
    for test_dir in test_dirs:
        dir_name = os.path.basename(test_dir)
        json_count = len(glob.glob(os.path.join(test_dir, "*.json")))
        print(f"  - {dir_name} ({json_count} JSON files)")
    
    for directory in test_dirs:
        dir_name = os.path.basename(directory)
        print(f"\nAnalyzing directory: {dir_name}")
        
        # Find all JSON files in this directory
        json_files = glob.glob(os.path.join(directory, "*.json"))
        
        for json_file in json_files:
            filename = os.path.basename(json_file)
            operation, block_size = parse_filename_info(filename)
            
            if operation and block_size:
                print(f"  Processing: {filename} (op: {operation}, bs: {block_size})")
                
                # Extract bandwidth values (bw_mean or bw per BW_JSON_FIELD)
                bw_values, config_data = extract_bw_mean_from_json(json_file)
                
                # Store FIO configuration for subtitles
                config_key = (operation, block_size)
                FIO_CONFIGS[config_key] = config_data
                
                for bw_data in bw_values:
                    # Store per machine
                    results[directory][operation][block_size].append({
                        'bw_mean': bw_data['bw_mean'],
                        'job_name': bw_data['job_name'],
                        'avg_latency_ms': bw_data.get('avg_latency_ms', 0)
                    })
                    
                    # Store for all machines aggregation
                    all_machines_results[operation][block_size].append({
                        'machine': directory,
                        'bw_mean': bw_data['bw_mean'],
                        'job_name': bw_data['job_name'],
                        'avg_latency_ms': bw_data.get('avg_latency_ms', 0)
                    })
            else:

                print(f"  Skipping: {filename} (could not parse operation/block_size)")
    
    return results, all_machines_results

def calculate_statistics(values):
    """Calculate statistics for a list of values (IOPS or bandwidth)."""
    if not values:
        return {}
    
    # Check if values are IOPS (integers) or bandwidth (dictionaries)
    if isinstance(values[0], (int, float)):
        # IOPS values are direct numbers
        bw_values = [v for v in values if isinstance(v, (int, float))]
    else:
        # Bandwidth values are dictionaries with 'bw_mean' key
        bw_values = [v['bw_mean'] for v in values if isinstance(v.get('bw_mean'), (int, float))]
    
    if not bw_values:
        return {}
    
    return {
        'count': len(bw_values),
        'min': min(bw_values),
        'max': max(bw_values),
        'mean': sum(bw_values) / len(bw_values),
        'values': bw_values
    }

def generate_report(results, all_machines_results):
    """Generate a comprehensive report."""
    
    print("\n" + "="*80)
    print("BANDWIDTH MEAN ANALYSIS REPORT")
    print("="*80)
    
    # Per machine results
    print("\nPER MACHINE RESULTS:")
    print("-" * 50)
    
    # Check if results is using tuple keys (IOPS) or nested dict structure (bandwidth)
    if results and isinstance(next(iter(results.keys())), tuple):
        # IOPS results structure: {(vm_name, operation, block_size): value}
        for (vm_name, operation, block_size), value in sorted(results.items()):
            print(f"\nMachine: {vm_name}")
            print("-" * 30)
            print(f"  Operation: {operation}")
            print(f"    Block Size: {block_size}")
            if isinstance(value, dict):
                # New structure with latency data
                print(f"      Total IOPS: {value.get('total_iops', 0):.0f}")
                print(f"      Avg Latency: {value.get('avg_latency_ms', 0):.2f} ms")
            else:
                # Old structure (backward compatibility)
                print(f"      Value: {value:.2f}")
    else:
        # Bandwidth results structure: {machine: {operation: {block_size: value}}}
        for machine in sorted(results.keys()):
            print(f"\nMachine: {machine}")
            print("-" * 30)
            
            for operation in sorted(results[machine].keys()):
                print(f"  Operation: {operation}")
                for block_size in sorted(results[machine][operation].keys()):
                    stats = calculate_statistics(results[machine][operation][block_size])
                    if stats:
                        print(f"    Block Size: {block_size}")
                        print(f"      Count: {stats['count']}")
                        print(f"      Min: {stats['min']:.2f}")
                        print(f"      Max: {stats['max']:.2f}")
                        print(f"      Mean: {stats['mean']:.2f}")
                        print(f"      Values: {[f'{v:.2f}' for v in stats['values']]}")
    
    # Aggregated results across all machines
    print("\n\nAGGREGATED RESULTS (ALL MACHINES):")
    print("-" * 50)
    
    for operation in sorted(all_machines_results.keys()):
        print(f"\nOperation: {operation}")
        for block_size in sorted(all_machines_results[operation].keys()):
            stats = calculate_statistics(all_machines_results[operation][block_size])
            if stats:
                print(f"  Block Size: {block_size}")
                print(f"    Total Count: {stats['count']}")
                print(f"    Overall Min: {stats['min']:.2f}")
                print(f"    Overall Max: {stats['max']:.2f}")
                print(f"    Overall Mean: {stats['mean']:.2f}")
                
                # Show per-machine breakdown
                machine_stats = defaultdict(list)
                for item in all_machines_results[operation][block_size]:
                    if isinstance(item, (int, float)):
                        # IOPS data - direct values
                        machine_stats['all_machines'].append(item)
                    else:
                        # Bandwidth data - dictionary with machine and bw_mean
                        machine_stats[item['machine']].append(item['bw_mean'])
                
                print(f"    Per Machine:")
                for machine in sorted(machine_stats.keys()):
                    machine_mean = sum(machine_stats[machine]) / len(machine_stats[machine])
                    print(f"      {machine}: {machine_mean:.2f} (n={len(machine_stats[machine])})")

def filter_results_by_block_sizes(results, all_machines_results, selected_block_sizes):
    """Filter results to only include selected block sizes."""
    # Check if results is using tuple keys (IOPS) or nested dict structure (bandwidth)
    if results and isinstance(next(iter(results.keys())), tuple):
        # IOPS results structure: {(vm_name, operation, block_size): value}
        filtered_results = {}
        for (vm_name, operation, block_size), value in results.items():
            if block_size in selected_block_sizes:
                filtered_results[(vm_name, operation, block_size)] = value
    else:
        # Bandwidth results structure: {machine: {operation: {block_size: value}}}
        filtered_results = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
        for machine in results:
            for operation in results[machine]:
                for block_size in results[machine][operation]:
                    if block_size in selected_block_sizes:
                        filtered_results[machine][operation][block_size] = results[machine][operation][block_size]
    
    # Filter all-machines results (same structure for both)
    filtered_all_machines_results = defaultdict(lambda: defaultdict(list))
    for operation in all_machines_results:
        for block_size in all_machines_results[operation]:
            if block_size in selected_block_sizes:
                filtered_all_machines_results[operation][block_size] = all_machines_results[operation][block_size]
    
    return filtered_results, filtered_all_machines_results

def write_operation_summary_csv_files(all_machines_results, selected_block_sizes=None, output_dir='.'):
    """
    Write CSV files that combine all block sizes for each operation type.
    Creates files like: summary-write-all-blocks.csv, summary-read-all-blocks.csv, etc.
    """
    # Group results by operation only (combining all block sizes)
    operation_results = {}
    
    for operation, block_data in all_machines_results.items():
        if operation not in operation_results:
            operation_results[operation] = {}
        
        for block_size, items in block_data.items():
            # Check if items are IOPS (integers) or bandwidth (dictionaries)
            if items and isinstance(items[0], (int, float)):
                # IOPS data - items are direct values (old structure), need to create generic machine names
                for i, iops_value in enumerate(items):
                    vm_name = f"machine-{i+1}"  # Create generic machine names for IOPS data
                    if vm_name not in operation_results[operation]:
                        operation_results[operation][vm_name] = {}
                    operation_results[operation][vm_name][block_size] = iops_value
            elif items and isinstance(items[0], dict) and 'total_iops' in items[0]:
                # IOPS data - new structure with latency data
                for i, iops_data in enumerate(items):
                    # Use actual machine name if available, otherwise create generic machine name
                    vm_name = iops_data.get('machine', f"machine-{i+1}")
                    if vm_name not in operation_results[operation]:
                        operation_results[operation][vm_name] = {}
                    operation_results[operation][vm_name][block_size] = iops_data.get('total_iops', 0)
            else:
                # Bandwidth data - items are dictionaries with machine and bw_mean
                # Group by machine and sum all jobs per machine
                machine_groups = {}
                for item in items:
                    machine = item['machine']
                    if machine not in machine_groups:
                        machine_groups[machine] = []
                    machine_groups[machine].append(item['bw_mean'])
                
                # Sum all jobs per machine and store the total
                for vm_name, bw_values in machine_groups.items():
                    total_bw = sum(bw_values) if bw_values else 0
                    
                    if vm_name not in operation_results[operation]:
                        operation_results[operation][vm_name] = {}
                    operation_results[operation][vm_name][block_size] = total_bw
    
    # Write CSV files for each operation
    csv_files_created = []
    csv_dir = ensure_csv_directory(output_dir)
    
    for operation, vm_data in operation_results.items():
        filename = f"summary-{operation}-all-blocks.csv"
        filepath = os.path.join(csv_dir, filename)
        
        # Get all unique block sizes for this operation
        all_block_sizes = set()
        for vm_name, block_data in vm_data.items():
            all_block_sizes.update(block_data.keys())
        all_block_sizes = sorted(all_block_sizes)
        
        # Filter block sizes if selection is specified
        if selected_block_sizes:
            all_block_sizes = [bs for bs in all_block_sizes if bs in selected_block_sizes]
            if not all_block_sizes:
                print(f"No selected block sizes found for operation {operation}, skipping...")
                continue
        
        with open(filepath, 'w', newline='') as csvfile:
            writer = csv.writer(csvfile)
            
            # Write header: vm_name, block_size_1, block_size_2, ...
            header = ['vm_name'] + all_block_sizes
            writer.writerow(header)
            
            # Sort by vm_name for consistent output
            sorted_vms = sorted(vm_data.keys())
            
            # Write data
            for vm_name in sorted_vms:
                row = [vm_name]
                for block_size in all_block_sizes:
                    bw_value = vm_data[vm_name].get(block_size, 0)  # 0 if not found
                    row.append(bw_value)
                writer.writerow(row)
        
        display_names = [get_block_size_display_name(bs) for bs in all_block_sizes]
        print(f"Created {filepath} with {len(sorted_vms)} Machines and {len(all_block_sizes)} block sizes: {', '.join(display_names)}")
        csv_files_created.append(filepath)
    
    return csv_files_created

def save_results_to_files(results, all_machines_results, output_dir='.', selected_block_sizes=None):
    """Save results to CSV files for further analysis."""
    
    # Save per-machine results
    csv_dir = ensure_csv_directory(output_dir)
    for machine in results.keys():
        filename = f"{os.path.basename(machine)}_bw_mean_results.csv"
        filepath = os.path.join(csv_dir, filename)
        with open(filepath, 'w') as f:
            f.write("Operation,BlockSize,JobName,BwMean\n")
            for operation in results[machine].keys():
                for block_size in results[machine][operation].keys():
                    for item in results[machine][operation][block_size]:
                        f.write(f"{operation},{block_size},{item['job_name']},{item['bw_mean']}\n")
        print(f"Saved per-machine results to: {filepath}")
    
    # Save aggregated results per operation
    for operation in all_machines_results.keys():
        filename = f"{operation}_all_machines_bw_mean_results.csv"
        filepath = os.path.join(csv_dir, filename)
        with open(filepath, 'w') as f:
            f.write("BlockSize,Machine,JobName,BwMean\n")
            for block_size in all_machines_results[operation].keys():
                for item in all_machines_results[operation][block_size]:
                    f.write(f"{block_size},{item['machine']},{item['job_name']},{item['bw_mean']}\n")
        print(f"Saved {operation} aggregated results to: {filepath}")
    
    # Save results per block size and operation combination
    for operation in all_machines_results.keys():
        for block_size in all_machines_results[operation].keys():
            # Skip if block size filtering is enabled and this block size is not selected
            if selected_block_sizes and block_size not in selected_block_sizes:
                continue
                
            # Create filename with block size and operation
            # Replace 'k' with 'k' and format the filename
            if block_size.endswith('k'):
                size_part = block_size
            else:
                size_part = block_size
            
            filename = f"all_machines_block_size_{size_part}_operation_{operation}.csv"
            filepath = os.path.join(csv_dir, filename)
            with open(filepath, 'w') as f:
                f.write("Machine,JobName,BwMean\n")
                for item in all_machines_results[operation][block_size]:
                    f.write(f"{item['machine']},{item['job_name']},{item['bw_mean']}\n")
            print(f"Saved block size {block_size} operation {operation} results to: {filepath}")
    
    # Also save combined results for backward compatibility
    filename = "all_machines_bw_mean_results.csv"
    filepath = os.path.join(csv_dir, filename)
    with open(filepath, 'w') as f:
        f.write("Operation,BlockSize,Machine,JobName,BwMean\n")
        for operation in all_machines_results.keys():
            for block_size in all_machines_results[operation].keys():
                for item in all_machines_results[operation][block_size]:
                    f.write(f"{operation},{block_size},{item['machine']},{item['job_name']},{item['bw_mean']}\n")
    print(f"Saved combined aggregated results to: {filepath}")
    
    # Save job-summarized results (sum of all jobs per machine)
    save_job_summarized_results(results, all_machines_results, output_dir, selected_block_sizes)

def save_job_summarized_results(results, all_machines_results, output_dir='.', selected_block_sizes=None):
    """Save results with sum of all jobs per machine, operation, and block size."""
    
    # Save per-machine job-summarized results
    csv_dir = ensure_csv_directory(output_dir)
    for machine in results.keys():
        filename = f"{os.path.basename(machine)}_bw_mean_job_summary.csv"
        filepath = os.path.join(csv_dir, filename)
        with open(filepath, 'w') as f:
            f.write("Operation,BlockSize,TotalBwMean\n")
            for operation in results[machine].keys():
                for block_size in results[machine][operation].keys():
                    items = results[machine][operation][block_size]
                    total_bw = sum(item['bw_mean'] for item in items) if items else 0
                    
                    f.write(f"{operation},{block_size},{total_bw}\n")
        print(f"Saved job-summarized results to: {filepath}")
    
    # Save all machines job-summarized results
    filename = "all_machines_bw_mean_job_summary.csv"
    filepath = os.path.join(csv_dir, filename)
    with open(filepath, 'w') as f:
        f.write("Operation,BlockSize,Machine,TotalBwMean\n")
        for operation in all_machines_results.keys():
            for block_size in all_machines_results[operation].keys():
                # Group by machine
                machine_groups = {}
                for item in all_machines_results[operation][block_size]:
                    machine = item['machine']
                    if machine not in machine_groups:
                        machine_groups[machine] = []
                    machine_groups[machine].append(item)
                
                # Calculate totals for each machine
                for machine, items in machine_groups.items():
                    total_bw = sum(item['bw_mean'] for item in items) if items else 0
                    
                    f.write(f"{operation},{block_size},{machine},{total_bw}\n")
    
    print(f"Saved all machines job-summarized results to: {filepath}")
    
    # Save block size and operation specific job-summarized results
    for operation in all_machines_results.keys():
        for block_size in all_machines_results[operation].keys():
            # Skip if block size filtering is enabled and this block size is not selected
            if selected_block_sizes and block_size not in selected_block_sizes:
                continue
                
            filename = f"all_machines_block_size_{block_size}_operation_{operation}_job_summary.csv"
            filepath = os.path.join(csv_dir, filename)
            with open(filepath, 'w') as f:
                f.write("Machine,TotalBwMean\n")
                
                # Group by machine
                machine_groups = {}
                for item in all_machines_results[operation][block_size]:
                    machine = item['machine']
                    if machine not in machine_groups:
                        machine_groups[machine] = []
                    machine_groups[machine].append(item)
                
                # Calculate totals for each machine
                for machine, items in machine_groups.items():
                    total_bw = sum(item['bw_mean'] for item in items) if items else 0
                    
                    f.write(f"{machine},{total_bw}\n")
            
            print(f"Saved job-summarized results to: {filepath}")


def compute_per_machine_totals(all_machines_results, operation, block_size, data_type='bandwidth'):
    """Return per-machine total performance values for one operation and block size."""
    items = all_machines_results.get(operation, {}).get(block_size, [])
    machine_groups = defaultdict(list)
    for item in items:
        machine_groups[item['machine']].append(item)

    totals = []
    for group in machine_groups.values():
        if data_type == 'iops':
            totals.append(sum(item.get('total_iops', 0) for item in group))
        else:
            totals.append(sum(item.get('bw_mean', 0) for item in group))
    return totals


def build_multi_run_comparison(input_dirs, run_labels, data_type, selected_block_sizes=None):
    """
    Build per-run mean total-per-machine metrics for each operation/block size.

    Returns:
        dict[(operation, block_size)][run_label] = {
            'mean_total_per_machine': float,
            'total_all_machines': float,
            'machine_count': int,
            'min_total_per_machine': float,
            'max_total_per_machine': float,
        }
    """
    analyze_fn = analyze_all_directories_iops if data_type == 'iops' else analyze_all_directories
    comparison = {}

    for input_dir, run_label in zip(input_dirs, run_labels):
        print(f"\nCollecting {data_type} data for run '{run_label}' from: {input_dir}")
        _, all_machines_results = analyze_fn(input_dir)
        if not all_machines_results:
            print(f"  Warning: no {data_type} data found in {input_dir}")
            continue

        if selected_block_sizes:
            _, all_machines_results = filter_results_by_block_sizes(
                {}, all_machines_results, selected_block_sizes
            )

        for operation in all_machines_results:
            for block_size in all_machines_results[operation]:
                totals = compute_per_machine_totals(
                    all_machines_results, operation, block_size, data_type
                )
                if not totals:
                    continue

                key = (operation, block_size)
                total_all_machines = sum(totals)
                comparison.setdefault(key, {})[run_label] = {
                    'mean_total_per_machine': sum(totals) / len(totals),
                    'total_all_machines': total_all_machines,
                    'machine_count': len(totals),
                    'min_total_per_machine': min(totals),
                    'max_total_per_machine': max(totals),
                }
                print(
                    f"  {operation} {block_size}: "
                    f"{comparison[key][run_label]['mean_total_per_machine']:.1f} "
                    f"({'IOPS' if data_type == 'iops' else BW_RATE_UNIT}) per machine, "
                    f"total all machines {total_all_machines:.0f} "
                    f"over {len(totals)} machines"
                )

    return comparison


def format_combined_vm_axis_label(run_label, machine_count):
    """Format run label + VM count for combined run comparison x-axis ticks."""
    label = (run_label or '').strip() or 'run'
    count = int(machine_count) if machine_count else 0
    vm_suffix = 'VM' if count == 1 else 'VMs'
    return f'{label}\n({count} {vm_suffix})'


def _create_combined_metric_graph_set(
    comparison,
    output_dir,
    graph_types,
    data_type,
    metric_key,
    csv_suffix,
    metric_column,
    title_suffix,
    y_label,
    unit,
    value_format,
    show_values=False,
):
    """Create CSV and graphs for one combined comparison metric."""
    csv_dir = ensure_csv_directory(output_dir)
    created = 0

    for (operation, block_size), runs in sorted(comparison.items()):
        if len(runs) < 2:
            continue

        labels = list(runs.keys())
        values = [runs[label][metric_key] for label in labels]
        counts = [runs[label]['machine_count'] for label in labels]
        x_labels = [
            format_combined_vm_axis_label(label, count)
            for label, count in zip(labels, counts)
        ]
        x_positions = range(len(labels))

        csv_filename = f"combined_block_size_{block_size}_operation_{operation}_{csv_suffix}.csv"
        csv_path = os.path.join(csv_dir, csv_filename)
        with open(csv_path, 'w', newline='') as csvfile:
            writer = csv.writer(csvfile)
            if metric_key == 'mean_total_per_machine':
                writer.writerow([
                    'RunLabel',
                    metric_column,
                    'MachineCount',
                    'MinPerMachine',
                    'MaxPerMachine',
                ])
                for label in labels:
                    run_data = runs[label]
                    writer.writerow([
                        label,
                        f"{run_data['mean_total_per_machine']:.2f}",
                        run_data['machine_count'],
                        f"{run_data['min_total_per_machine']:.2f}",
                        f"{run_data['max_total_per_machine']:.2f}",
                    ])
            else:
                writer.writerow(['RunLabel', metric_column, 'MachineCount'])
                for label, value, machine_count in zip(labels, values, counts):
                    writer.writerow([label, f"{value:.2f}", machine_count])
        print(f"Saved combined comparison CSV: {csv_path}")

        display_bs = get_block_size_display_name(block_size)
        title = f'Run Comparison: {operation.upper()} {display_bs} - {title_suffix}'

        for current_graph_type in graph_types:
            fig, ax = plt.subplots(figsize=(max(8, len(labels) * 1.8), 6))

            if current_graph_type == 'bar':
                colors = plt.cm.tab10(range(len(labels)))
                bars = ax.bar(
                    x_positions,
                    values,
                    color=colors,
                    alpha=0.85,
                    edgecolor='black',
                    linewidth=0.5,
                )
                ax.set_xticks(list(x_positions))
                ax.set_xticklabels(x_labels, rotation=15, ha='right')
                ax.grid(axis='y', alpha=0.3, linestyle='--')
                if show_values:
                    annotate_bar_values(ax, bars, fontsize=graph_value_fontsize(len(values)), rotation=0)
            else:
                ax.plot(
                    x_positions,
                    values,
                    marker='o',
                    linewidth=2,
                    markersize=8,
                    color='steelblue',
                )
                ax.set_xticks(list(x_positions))
                ax.set_xticklabels(x_labels, rotation=15, ha='right')
                ax.grid(alpha=0.3, linestyle='--')
                if show_values:
                    annotate_line_values(
                        ax, x_positions, values, fontsize=graph_value_fontsize(len(values))
                    )

            # No right-side legend: run labels are already on the X-axis
            ax.set_title(title, fontsize=14, fontweight='bold', pad=16)
            ax.set_xlabel('Run / Machines', fontsize=12, fontweight='bold')
            ax.set_ylabel(y_label, fontsize=12, fontweight='bold')
            if values:
                ax.set_ylim(0, max(values) * (1.25 if show_values else 1.2))

            fig.tight_layout()
            png_filename = (
                f"combined_block_size_{block_size}_operation_{operation}_"
                f"{csv_suffix}_{current_graph_type}.png"
            )
            png_path = os.path.join(output_dir, png_filename)
            plt.savefig(png_path, dpi=300, bbox_inches='tight')
            plt.close(fig)
            print(f"Created combined graph: {png_path}")
            created += 1

    return created


def create_combined_run_comparison_graphs(comparison, output_dir, graph_type='bar', data_type='bandwidth',
                                          show_values=False):
    """Create combined_* graphs comparing per-machine and total-all-machines metrics across runs."""
    if not comparison:
        print("No multi-run comparison data available for combined graphs.")
        return 0

    plt.switch_backend('Agg')
    graph_types = ['bar', 'line'] if graph_type == 'both' else [graph_type]

    if data_type == 'iops':
        per_machine_column = 'TotalIOPSPerMachine'
        all_machines_column = 'TotalIOPSAllMachines'
        per_machine_ylabel = 'Mean Total IOPS per Machine'
        all_machines_ylabel = 'Total IOPS All Machines'
        unit = 'IOPS'
    else:
        per_machine_column = 'TotalBandwidthPerMachine'
        all_machines_column = 'TotalBandwidthAllMachines'
        per_machine_ylabel = f'Mean Total Bandwidth per Machine ({BW_RATE_UNIT})'
        all_machines_ylabel = f'Total Bandwidth All Machines ({BW_RATE_UNIT})'
        unit = BW_RATE_UNIT

    created = 0
    created += _create_combined_metric_graph_set(
        comparison,
        output_dir,
        graph_types,
        data_type,
        metric_key='mean_total_per_machine',
        csv_suffix='total_per_machine',
        metric_column=per_machine_column,
        title_suffix='Total per Machine',
        y_label=per_machine_ylabel,
        unit=unit,
        value_format='.1f',
        show_values=show_values,
    )
    created += _create_combined_metric_graph_set(
        comparison,
        output_dir,
        graph_types,
        data_type,
        metric_key='total_all_machines',
        csv_suffix='total_all_machines',
        metric_column=all_machines_column,
        title_suffix='Total All Machines',
        y_label=all_machines_ylabel,
        unit=unit,
        value_format='.0f',
        show_values=show_values,
    )

    return created


def run_multi_run_comparison(input_dirs, run_labels, output_dir, graph_type, selected_block_sizes, analyze_iops, analyze_bw,
                             show_values=False, create_combined=False):
    """Analyze multiple FIO result directories; optionally emit combined_* graphs.

    Returns:
        (iops_comparison, bw_comparison) dicts (either may be None).
    """
    print("\n" + "=" * 60)
    print("MULTI-RUN COMPARISON")
    print("=" * 60)
    print("Runs:")
    for label, input_dir in zip(run_labels, input_dirs):
        print(f"  - {label}: {input_dir}")

    total_created = 0
    bw_comparison = None
    iops_comparison = None
    if analyze_bw:
        print("\nBuilding bandwidth run comparison...")
        bw_comparison = build_multi_run_comparison(
            input_dirs, run_labels, 'bandwidth', selected_block_sizes
        )
        if create_combined:
            total_created += create_combined_run_comparison_graphs(
                bw_comparison, output_dir, graph_type, 'bandwidth', show_values=show_values
            )

    if analyze_iops:
        print("\nBuilding IOPS run comparison...")
        iops_comparison = build_multi_run_comparison(
            input_dirs, run_labels, 'iops', selected_block_sizes
        )
        if create_combined:
            total_created += create_combined_run_comparison_graphs(
                iops_comparison, output_dir, graph_type, 'iops', show_values=show_values
            )

    if create_combined:
        print(f"\nCreated {total_created} combined comparison graph(s) in: {output_dir}")
    else:
        print("\nSkipping combined_* graphs (pass --combined to generate them)")
    return iops_comparison, bw_comparison


def main():
    """Main function."""
    parser = argparse.ArgumentParser(
        description='Unified FIO Analysis Tool - IOPS and Bandwidth Analysis',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 analyze_bw_mean_with_graphs.py --iops                    # IOPS via FIO iops (I/Os/runtime)
  python3 analyze_bw_mean_with_graphs.py --iops-mean               # IOPS via FIO iops_mean
  python3 analyze_bw_mean_with_graphs.py --bw-mean                 # Bandwidth via FIO bw_mean
  python3 analyze_bw_mean_with_graphs.py --bw                      # Bandwidth via FIO bw (bytes/runtime)
  python3 analyze_bw_mean_with_graphs.py --iops --input-dir /path/to/data  # Analyze IOPS from specific directory
  python3 analyze_bw_mean_with_graphs.py --bw-mean --output-dir /path/to/results
  python3 analyze_bw_mean_with_graphs.py --iops-mean --graph-type line  # Generate IOPS line graphs
  python3 analyze_bw_mean_with_graphs.py --bw --graph-type both  # Bandwidth graphs using FIO bw
  python3 analyze_bw_mean_with_graphs.py --iops --block-sizes 4k,8k,128k  # Analyze specific block sizes for IOPS
  python3 analyze_bw_mean_with_graphs.py --bw-mean --operation-summary  # Bandwidth operation summary graphs
  python3 analyze_bw_mean_with_graphs.py --iops-mean --input-dir /data --output-dir /results --graph-type line --block-sizes 4k,8k --operation-summary
  python3 analyze_bw_mean_with_graphs.py --iops --bw --input-dir /data/run100 /data/run1000 --run-labels "Large,Medium"
  python3 analyze_bw_mean_with_graphs.py --bw --input-dirs /data/run100 /data/run1000 --run-labels "100 VMs,1000 VMs"
  python3 analyze_bw_mean_with_graphs.py --bw-mean --combined --input-dirs /data/run100 /data/run1000 --run-labels "100 VMs,1000 VMs"
  python3 analyze_bw_mean_with_graphs.py --iops --bw --input-dir /data/tiers/ \\
      --run-labels "Large,Medium,Standard,Small,Tiny" --output-dir compare/ \\
      --update-perf-report containers-bench/high-scale/perf-report.md
  python3 analyze_bw_mean_with_graphs.py --iops-mean --bw-mean --input-dirs tier-250/ tier-500/ tier-1000/ \\
      --run-labels "250vm,500vm,1000vm" --output-dir compare/   # cross-tier uses these labels as-is
  python3 analyze_bw_mean_with_graphs.py --perf-report-table --input-dir fio-results/ --output-dir report-out/
  python3 analyze_bw_mean_with_graphs.py --perf-report-table --perf-report-bw-graph --input-dir fio-results/ --output-dir report-out/
  python3 analyze_bw_mean_with_graphs.py --perf-report-bw-graph --input-dir result_fio-results/ --output-dir result_fio-results/
        """
    )
    
    # Analysis type selection — IOPS field (mutually exclusive)
    iops_metric = parser.add_mutually_exclusive_group()
    iops_metric.add_argument('--iops',
                             action='store_true',
                             help='Analyze IOPS using FIO overall iops '
                                  '(total_ios/runtime; better match to sustained rate with fsync)')
    iops_metric.add_argument('--iops-mean', '--iops_mean',
                             dest='iops_mean',
                             action='store_true',
                             help='Analyze IOPS using FIO iops_mean '
                                  '(mean of status samples; previous --iops behavior)')

    bw_metric = parser.add_mutually_exclusive_group()
    bw_metric.add_argument('--bw',
                           action='store_true',
                           help='Analyze bandwidth using FIO overall bw '
                                '(io_bytes/runtime; better match to Grafana/RBD client BW)')
    bw_metric.add_argument('--bw-mean', '--bw_mean',
                           dest='bw_mean',
                           action='store_true',
                           help='Analyze bandwidth using FIO bw_mean '
                                '(mean of status samples; previous --bw behavior)')
    
    parser.add_argument('--input-dir',
                       nargs='+',
                       type=str,
                       default=None,
                       help='One or more FIO result directories (JSON in subdirs). '
                            'Multiple dirs enable compare/cross-tier mode. '
                            'A single parent with 2+ child result dirs is auto-expanded. '
                            'Default: current directory')
    
    parser.add_argument('--input-dirs',
                       nargs='+',
                       type=str,
                       help='Alias for multi-dir --input-dir (compare / cross-tier; '
                            'add --combined for combined_* graphs)')
    
    parser.add_argument('--run-labels',
                       type=str,
                       help='Comma-separated display labels for input dirs, paired by '
                            'position (1st label → 1st dir, 2nd → 2nd, …). Count must match '
                            'resolved input dirs. Used as-is for cross-tier X-axis/table '
                            'Tier names. Label names need not equal directory names, but '
                            'order must match the data or graphs will be mislabeled. '
                            'Default: directory basenames')

    parser.add_argument('--combined',
                       action='store_true',
                       help='Generate combined_* run-comparison graphs '
                            '(per block size / operation). Off by default')
    
    parser.add_argument('--output-dir',
                       type=str,
                       default='.',
                       help='Directory to save output files (CSV and PNG). Default: current directory')
    
    parser.add_argument('--graph-type',
                       choices=['bar', 'line', 'both'],
                       default='bar',
                       help='Type of graphs to generate (default: bar)')
    
    parser.add_argument('--block-sizes',
                       type=str,
                       help='Comma-separated list of block sizes to analyze (e.g., "4k,8k,128k")')
    
    parser.add_argument('--operation-summary',
                       action='store_true',
                       help='Generate operation summary files and graphs (all block sizes combined)')
    
    parser.add_argument('--summary-only',
                       action='store_true',
                       help='Generate only summary graphs (skip per-Machine comparison graphs)')

    parser.add_argument('--show-values',
                       action='store_true',
                       help='Annotate numeric values on bar and line graph data points')

    parser.add_argument('--perf-report-table',
                       action='store_true',
                       help='Build perf-report.md style throughput + latency tables (CSV, Markdown, PNG)')

    parser.add_argument('--perf-report-bw-graph',
                       action='store_true',
                       help='Plot Total BW (MB/s) by Operation from throughput summary '
                            '(grouped by block size; title: "{VMs} Machines Performance Test"). '
                            'Uses rows from --perf-report-table, or loads '
                            'csv_files/perf_report_throughput_summary.csv from --input-dir/--output-dir')

    parser.add_argument('--cross-tier-latency',
                       action='store_true',
                       help='Build per-operation latency cross-tier multipanel graphs '
                            '(latency_crosstier/{operation}_latency_cross_tier_comparison.png) '
                            'from perf_report_latency_summary data across --input-dirs')

    parser.add_argument('--latency-bs',
                       type=str,
                       default=None,
                       metavar='SIZES',
                       help='Cross-tier only: comma-separated block sizes for latency detail '
                            'tables (e.g. "4k,8k"). Writes PNG + CSV under output-dir/latency-bs/ '
                            'with per-tier mean IOPS, clat p50/p99, and iodepth stats.')

    parser.add_argument('--latency-bs-ops',
                       type=str,
                       default=None,
                       metavar='OPS',
                       help='With --latency-bs: comma-separated operations to include '
                            '(e.g. "read,write,randread,randwrite"). Default: all found.')

    parser.add_argument('--latency-bs-baseline',
                       type=str,
                       default=None,
                       metavar='LABEL',
                       help='With --latency-bs: baseline tier for ratio column. Must match one '
                            'of --run-labels. With 3+ tiers adds a "vs LABEL" column; with 2 '
                            'tiers uses that column instead of "Ratio".')

    parser.add_argument('--update-perf-report',
                       type=str,
                       default=None,
                       metavar='PATH',
                       help='After compare mode, replace the Cross-Tier Comparison '
                            'section in PATH (e.g. containers-bench/high-scale/perf-report.md) '
                            'with the four key workload tables and graph links. '
                            'All tested op×block-size Aggregate tables are always written '
                            'to --output-dir as perf_report_cross_tier_comparison.md')
    
    args = parser.parse_args()

    # IOPS / BW enablement + which FIO JSON fields to use.
    # Keep pairs consistent: wall-clock (iops+bw) or sample-mean (iops_mean+bw_mean).
    # If only one side is chosen, the unset companion defaults to the same style.
    analyze_iops = bool(args.iops or args.iops_mean)
    analyze_bw = bool(args.bw or args.bw_mean)

    if args.iops:
        set_iops_json_field('iops')
    elif args.iops_mean:
        set_iops_json_field('iops_mean')
    elif args.bw:
        # Companion for --bw in perf-report tables (both columns always filled)
        set_iops_json_field('iops')
    elif args.bw_mean:
        set_iops_json_field('iops_mean')
    else:
        # perf-report-table alone: prefer wall-clock (matches Grafana/RBD better)
        set_iops_json_field('iops')

    if args.bw:
        set_bw_json_field('bw')
    elif args.bw_mean:
        set_bw_json_field('bw_mean')
    elif args.iops:
        set_bw_json_field('bw')
    elif args.iops_mean:
        set_bw_json_field('bw_mean')
    else:
        # perf-report-table alone: prefer wall-clock (matches Grafana/RBD better)
        set_bw_json_field('bw')
    
    raw_input_dirs = args.input_dirs or args.input_dir or ['.']
    if args.input_dirs and args.input_dir:
        print("Note: --input-dirs takes precedence over --input-dir")

    try:
        input_dirs = resolve_compare_input_dirs(raw_input_dirs)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    if args.combined and len(input_dirs) < 2:
        print("Error: --combined requires two or more input directories "
              "(via --input-dir/--input-dirs, or a parent with multiple child result dirs)")
        sys.exit(1)

    if args.run_labels:
        run_labels = [label.strip() for label in args.run_labels.split(',') if label.strip()]
        if len(run_labels) != len(input_dirs):
            print(
                f"Error: --run-labels count ({len(run_labels)}) must match "
                f"resolved input-dir count ({len(input_dirs)})"
            )
            sys.exit(1)
    else:
        run_labels = [os.path.basename(path.rstrip(os.sep)) or path for path in input_dirs]
    
    # Create output directory if it doesn't exist
    output_dir = os.path.abspath(args.output_dir)
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        print(f"Created output directory: {output_dir}")
    else:
        print(f"Using output directory: {output_dir}")
    
    # Validate that at least one analysis type is selected
    if (not analyze_iops and not analyze_bw and not args.perf_report_table
            and not args.perf_report_bw_graph and not args.cross_tier_latency
            and not args.latency_bs):
        print(
            "Error: You must specify --iops, --iops-mean, --bw, --bw-mean, "
            "--perf-report-table, --perf-report-bw-graph, --cross-tier-latency, "
            "and/or --latency-bs"
        )
        sys.exit(1)

    if (args.latency_bs_ops or args.latency_bs_baseline) and not args.latency_bs:
        print('Error: --latency-bs-ops and --latency-bs-baseline require --latency-bs')
        sys.exit(1)

    if analyze_iops or args.perf_report_table:
        print(f"IOPS metric: {iops_field_label()}")
    if analyze_bw or args.perf_report_table:
        print(f"Bandwidth metric: {bw_field_label()}")

    # Extract Grafana-friendly test start/complete times from harness logs
    print("\nExtracting test times from harness logs...")
    extract_test_times_for_inputs(input_dirs, output_dir, run_labels=run_labels)
    
    # Parse selected block sizes if provided
    selected_block_sizes = None
    if args.block_sizes:
        selected_block_sizes = [bs.strip().lower() for bs in args.block_sizes.split(',')]
        display_selected = [get_block_size_display_name(bs) for bs in selected_block_sizes]
        print(f"Selected block sizes for analysis: {', '.join(display_selected)}")
    
    if len(input_dirs) >= 2:
        print("=" * 60)
        print("UNIFIED FIO ANALYSIS TOOL - MULTI-RUN MODE")
        print("=" * 60)
        print(f"Using output directory: {output_dir}")
        iops_comparison = None
        bw_comparison = None
        if analyze_iops or analyze_bw:
            iops_comparison, bw_comparison = run_multi_run_comparison(
                input_dirs,
                run_labels,
                output_dir,
                args.graph_type,
                selected_block_sizes,
                analyze_iops,
                analyze_bw,
                show_values=args.show_values,
                create_combined=args.combined,
            )
        if args.perf_report_table:
            for input_dir, run_label in zip(input_dirs, run_labels):
                run_out = os.path.join(output_dir, f"perf_report_{run_label.replace(' ', '_')}")
                os.makedirs(run_out, exist_ok=True)
                print(f"\nBuilding perf-report tables for '{run_label}' -> {run_out}")
                create_perf_report_tables(
                    input_dir, run_out, selected_block_sizes,
                    include_bw_graph=args.perf_report_bw_graph,
                    graph_type=args.graph_type,
                    show_values=args.show_values,
                )
        elif args.perf_report_bw_graph:
            for input_dir, run_label in zip(input_dirs, run_labels):
                run_out = os.path.join(output_dir, f"perf_report_{run_label.replace(' ', '_')}")
                os.makedirs(run_out, exist_ok=True)
                create_perf_report_bw_graph_from_csv(
                    input_dir, run_out, graph_type=args.graph_type, show_values=args.show_values
                )

        # Cross-tier tables + IOPS/BW scaling graphs
        if analyze_iops or analyze_bw:
            create_cross_tier_comparison(
                input_dirs,
                run_labels,
                output_dir,
                selected_block_sizes=selected_block_sizes,
                graph_type=args.graph_type,
                iops_comparison=iops_comparison,
                bw_comparison=bw_comparison,
                show_values=args.show_values,
                include_iops=analyze_iops,
                include_bw=analyze_bw,
                update_perf_report=args.update_perf_report,
            )
        elif args.update_perf_report:
            print("Warning: --update-perf-report requires --iops/--iops-mean and/or "
                  "--bw/--bw-mean in compare mode; skipping report patch")

        # Latency cross-tier multipanel graphs (from latency summaries)
        if args.cross_tier_latency or args.perf_report_table:
            create_cross_tier_latency_comparison(
                input_dirs,
                run_labels,
                output_dir,
                selected_block_sizes=selected_block_sizes,
                graph_type=args.graph_type,
                show_values=args.show_values,
            )

        if args.latency_bs:
            latency_bs_sizes = [bs.strip().lower() for bs in args.latency_bs.split(',') if bs.strip()]
            if not latency_bs_sizes:
                print("Error: --latency-bs requires at least one block size (e.g. 4k,8k)")
                sys.exit(1)
            latency_bs_ops = None
            if args.latency_bs_ops:
                latency_bs_ops = [
                    op.strip().lower() for op in args.latency_bs_ops.split(',') if op.strip()
                ]
                if not latency_bs_ops:
                    print('Error: --latency-bs-ops requires at least one operation '
                          '(e.g. randread,randwrite)')
                    sys.exit(1)
            baseline_label = None
            if args.latency_bs_baseline:
                baseline_label = args.latency_bs_baseline.strip()
                if baseline_label not in run_labels:
                    print(
                        f"Error: --latency-bs-baseline '{baseline_label}' must match one of "
                        f"--run-labels: {', '.join(run_labels)}"
                    )
                    sys.exit(1)
            create_latency_bs_tables(
                input_dirs,
                run_labels,
                output_dir,
                latency_bs_sizes,
                operations=latency_bs_ops,
                baseline_label=baseline_label,
            )

        print("\nAnalysis complete!")
        return
    
    input_dir = input_dirs[0]
    
    print("=" * 60)
    print("UNIFIED FIO ANALYSIS TOOL")
    print("=" * 60)
    print(f"Using input directory: {input_dir}")
    print(f"Using output directory: {output_dir}")
    
    if selected_block_sizes:
        display_selected = [get_block_size_display_name(bs) for bs in selected_block_sizes]
        print(f"Selected block sizes for analysis: {', '.join(display_selected)}")

    cached_iops_all_machines = None
    cached_bw_all_machines = None
    
    # Handle IOPS analysis
    if analyze_iops:
        print("\n" + "=" * 60)
        print(f"IOPS ANALYSIS ({iops_field_label()})")
        print("=" * 60)
        
        # Analyze all directories for IOPS
        results, all_machines_results = analyze_all_directories_iops(input_dir)
        
        if not results:
            print("No IOPS data found to analyze.")
        else:
            # Filter results to only include selected block sizes
            if selected_block_sizes:
                results, all_machines_results = filter_results_by_block_sizes(results, all_machines_results, selected_block_sizes)
                
                if not results:
                    print("No IOPS data found for the selected block sizes.")
                else:
                    cached_iops_all_machines = all_machines_results
                    # Generate report
                    generate_report(results, all_machines_results)
                    
                    # Save results to files
                    save_results_to_files_iops(results, all_machines_results, output_dir, selected_block_sizes)
                    
                    # Save job summarized results
                    save_job_summarized_results_iops(results, all_machines_results, output_dir, selected_block_sizes)
                    
                    # Create graphs from job summaries
                    if args.summary_only:
                        print(f"\nCreating {args.graph_type} summary graphs only (per-Machine graphs skipped)...")
                    else:
                        print(f"\nCreating {args.graph_type} graphs...")
                    create_graphs_from_job_summaries(output_dir, args.graph_type, args.summary_only, 'iops', args.show_values)
                    
                    # Create latency correlation graph
                    print(f"\nCreating latency correlation graph...")
                    create_latency_performance_correlation_graph(all_machines_results, output_dir, 'iops')
                    
                    # Save latency data to files
                    print(f"\nSaving latency data to files...")
                    save_latency_data_to_files(all_machines_results, output_dir, 'iops')
                    
                    # Save job summarized results
                    save_job_summarized_results_iops(results, all_machines_results, output_dir, selected_block_sizes)
                    
                    # Create graphs from job summaries
                    if args.summary_only:
                        print(f"\nCreating {args.graph_type} summary graphs only (per-Machine graphs skipped)...")
                    else:
                        print(f"\nCreating {args.graph_type} graphs...")
                    create_graphs_from_job_summaries(output_dir, args.graph_type, args.summary_only, 'iops', args.show_values)
            else:
                cached_iops_all_machines = all_machines_results
                # Generate report
                generate_report(results, all_machines_results)
                
                # Save results to files
                save_results_to_files_iops(results, all_machines_results, output_dir, selected_block_sizes)
                
                # Create latency correlation graph
                print(f"\nCreating latency correlation graph...")
                create_latency_performance_correlation_graph(all_machines_results, output_dir, 'iops')
                
                # Save latency data to files
                print(f"\nSaving latency data to files...")
                save_latency_data_to_files(all_machines_results, output_dir, 'iops')
                
                # Save job summarized results
                save_job_summarized_results_iops(results, all_machines_results, output_dir, selected_block_sizes)
                
                # Create graphs from job summaries
                if args.summary_only:
                    print(f"\nCreating {args.graph_type} summary graphs only (per-Machine graphs skipped)...")
                else:
                    print(f"\nCreating {args.graph_type} graphs...")
                create_graphs_from_job_summaries(output_dir, args.graph_type, args.summary_only, 'iops', args.show_values)
    
    # Handle bandwidth analysis
    if analyze_bw:
        print("\n" + "=" * 60)
        print(f"BANDWIDTH ANALYSIS ({bw_field_label()})")
        print("=" * 60)
        
        # Analyze all directories for bandwidth
        results, all_machines_results = analyze_all_directories(input_dir)
        
        if not results:
            print("No bandwidth data found to analyze.")
        else:
            # Filter results to only include selected block sizes
            if selected_block_sizes:
                results, all_machines_results = filter_results_by_block_sizes(results, all_machines_results, selected_block_sizes)
                
                if not results:
                    print("No bandwidth data found for the selected block sizes.")
                else:
                    cached_bw_all_machines = all_machines_results
                    # Generate report
                    generate_report(results, all_machines_results)
                    
                    # Save results to files
                    save_results_to_files(results, all_machines_results, output_dir, selected_block_sizes)
                    
                    # Save job summarized results
                    save_job_summarized_results(results, all_machines_results, output_dir, selected_block_sizes)
                    
                    # Create graphs from job summaries
                    if args.summary_only:
                        print(f"\nCreating {args.graph_type} summary graphs only (per-Machine graphs skipped)...")
                    else:
                        print(f"\nCreating {args.graph_type} graphs...")
                    create_graphs_from_job_summaries(output_dir, args.graph_type, args.summary_only, 'bandwidth', args.show_values)
                    
                    # Create latency correlation graph
                    print(f"\nCreating latency correlation graph...")
                    create_latency_performance_correlation_graph(all_machines_results, output_dir, 'bandwidth')
                    
                    # Save latency data to files
                    print(f"\nSaving latency data to files...")
                    save_latency_data_to_files(all_machines_results, output_dir, 'bandwidth')
            else:
                cached_bw_all_machines = all_machines_results
                # Generate report
                generate_report(results, all_machines_results)
                
                # Save results to files
                save_results_to_files(results, all_machines_results, output_dir, selected_block_sizes)
                
                # Create latency correlation graph
                print(f"\nCreating latency correlation graph...")
                create_latency_performance_correlation_graph(all_machines_results, output_dir, 'bandwidth')
                
                # Save latency data to files
                print(f"\nSaving latency data to files...")
                save_latency_data_to_files(all_machines_results, output_dir, 'bandwidth')
                
                # Save job summarized results
                save_job_summarized_results(results, all_machines_results, output_dir, selected_block_sizes)
                
                # Create graphs from job summaries
                if args.summary_only:
                    print(f"\nCreating {args.graph_type} summary graphs only (per-Machine graphs skipped)...")
                else:
                    print(f"\nCreating {args.graph_type} graphs...")
                create_graphs_from_job_summaries(output_dir, args.graph_type, args.summary_only, 'bandwidth', args.show_values)
    
    # Generate operation summary files and graphs (if requested)
    if args.operation_summary:
        if analyze_iops:
            print(f"\nGenerating IOPS operation summary files...")
            print("-" * 50)
            # Get IOPS results for operation summary
            iops_results, iops_all_machines_results = analyze_all_directories_iops(input_dir)
            if selected_block_sizes:
                iops_results, iops_all_machines_results = filter_results_by_block_sizes(iops_results, iops_all_machines_results, selected_block_sizes)
            
            if iops_all_machines_results:
                operation_summary_files = write_operation_summary_csv_files(iops_all_machines_results, selected_block_sizes, output_dir)
                
                if operation_summary_files:
                    print(f"\nGenerating IOPS operation summary graphs...")
                    print("-" * 50)
                    summary_success_count = create_operation_summary_graphs(operation_summary_files, args.graph_type, output_dir, 'iops', args.show_values)
                    print(f"\nSuccessfully created {summary_success_count} IOPS operation summary graphs")
        
        if analyze_bw:
            print(f"\nGenerating bandwidth operation summary files...")
            print("-" * 50)
            # Get bandwidth results for operation summary
            bw_results, bw_all_machines_results = analyze_all_directories(input_dir)
            if selected_block_sizes:
                bw_results, bw_all_machines_results = filter_results_by_block_sizes(bw_results, bw_all_machines_results, selected_block_sizes)
            
            if bw_all_machines_results:
                operation_summary_files = write_operation_summary_csv_files(bw_all_machines_results, selected_block_sizes, output_dir)
                
                if operation_summary_files:
                    print(f"\nGenerating bandwidth operation summary graphs...")
                    print("-" * 50)
                    summary_success_count = create_operation_summary_graphs(operation_summary_files, args.graph_type, output_dir, 'bandwidth', args.show_values)
                    print(f"\nSuccessfully created {summary_success_count} bandwidth operation summary graphs")
        
        # List generated operation summary PNG files
        import glob
        summary_png_files = glob.glob(os.path.join(output_dir, "summary-*-all-blocks_comparison-*.png"))
        if summary_png_files:
            print(f"\nGenerated operation summary PNG files:")
            for png_file in sorted(summary_png_files):
                print(f"  - {os.path.basename(png_file)}")

    if args.perf_report_table:
        create_perf_report_tables(
            input_dir,
            output_dir,
            selected_block_sizes,
            iops_all_machines=cached_iops_all_machines,
            bw_all_machines=cached_bw_all_machines,
            include_bw_graph=args.perf_report_bw_graph,
            graph_type=args.graph_type,
            show_values=args.show_values,
        )
    elif args.perf_report_bw_graph:
        create_perf_report_bw_graph_from_csv(
            input_dir, output_dir, graph_type=args.graph_type, show_values=args.show_values
        )
    
    print("\nAnalysis complete!")

# IOPS Processing Functions
def process_vm_directory_iops(vm_dir):
    """
    Process all JSON files in a directory for IOPS data.
    Returns a dictionary with (operation, block_size) as key and iops as value.
    """
    results = {}
    vm_name = os.path.basename(vm_dir)
    
    # Find all JSON files in the directory
    json_files = glob.glob(os.path.join(vm_dir, "*.json"))
    
    for json_file in json_files:
        iops_data = extract_iops_from_json(json_file)
        
        for (operation, block_size), iops in iops_data.items():
            key = (vm_name, operation, block_size)
            results[key] = iops
    
    return results

def analyze_all_directories_iops(input_dir='.'):
    """Analyze all directories for JSON files and extract IOPS (IOPS_JSON_FIELD)."""
    
    # Find all directories containing FIO JSON files
    test_dirs = []
    for item in os.listdir(input_dir):
        item_path = os.path.join(input_dir, item)
        if os.path.isdir(item_path):
            # Check if this directory contains FIO JSON files
            json_files = glob.glob(os.path.join(item_path, "*.json"))
            if json_files:
                test_dirs.append(item_path)
    
    if not test_dirs:
        print(f"No directories with JSON files found in {input_dir}")
        return {}, {}
    
    print(f"IOPS field: {iops_field_label()} (JSON key '{IOPS_JSON_FIELD}')")
    print(f"Found {len(test_dirs)} directories with FIO JSON files:")
    for test_dir in test_dirs:
        print(f"  - {os.path.basename(test_dir)}")
    
    # Process each directory
    all_results = {}
    all_machines_results = defaultdict(lambda: defaultdict(list))
    
    for test_dir in test_dirs:
        print(f"\nAnalyzing directory: {os.path.basename(test_dir)}")
        vm_results = process_vm_directory_iops(test_dir)
        all_results.update(vm_results)
        
        # Group by operation and block size for aggregated results
        for (vm_name, operation, block_size), iops in vm_results.items():
            # Store with machine name for latency correlation graphs
            all_machines_results[operation][block_size].append({
                'machine': vm_name,
                'total_iops': iops['total_iops'],
                'avg_latency_ms': iops['avg_latency_ms']
            })
            print(f"  Processing: {vm_name} (op: {operation}, bs: {block_size})")
    
    return all_results, all_machines_results

def save_results_to_files_iops(results, all_machines_results, output_dir='.', selected_block_sizes=None):
    """Save IOPS results to CSV files."""
    
    csv_dir = ensure_csv_directory(output_dir)
    
    # Filter results by selected block sizes if specified
    if selected_block_sizes:
        filtered_results = {}
        for (vm_name, operation, block_size), iops in results.items():
            if block_size in selected_block_sizes:
                filtered_results[(vm_name, operation, block_size)] = iops
        results = filtered_results
        
        # Also filter all_machines_results
        filtered_all_machines = defaultdict(lambda: defaultdict(list))
        for operation, block_sizes in all_machines_results.items():
            for block_size, iops_list in block_sizes.items():
                if block_size in selected_block_sizes:
                    filtered_all_machines[operation][block_size] = iops_list
        all_machines_results = filtered_all_machines
    
    # Save per-machine results
    # Group results by Machine name
    vm_results = defaultdict(lambda: defaultdict(dict))
    for (vm_name, operation, block_size), iops in results.items():
        vm_results[vm_name][operation][block_size] = iops
    
    for vm_name in vm_results.keys():
        filename = f"{vm_name}_iops_results.csv"
        filepath = os.path.join(csv_dir, filename)
        
        with open(filepath, 'w', newline='') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(['Operation', 'BlockSize', 'IOPS'])
            
            for operation in sorted(vm_results[vm_name].keys()):
                for block_size in sorted(vm_results[vm_name][operation].keys()):
                    iops_data = vm_results[vm_name][operation][block_size]
                    if isinstance(iops_data, dict):
                        # New structure with latency data
                        writer.writerow([operation, block_size, iops_data.get('total_iops', 0)])
                    else:
                        # Old structure (backward compatibility)
                        writer.writerow([operation, block_size, iops_data])
        
        print(f"Saved per-machine results to: {filepath}")
    
    # Save aggregated results by operation
    for operation, block_sizes in all_machines_results.items():
        filename = f"{operation}_all_machines_iops_results.csv"
        filepath = os.path.join(csv_dir, filename)
        
        with open(filepath, 'w', newline='') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(['BlockSize', 'TotalIOPS', 'MinIOPS', 'MaxIOPS', 'MeanIOPS'])
            
            for block_size, iops_list in block_sizes.items():
                # Extract IOPS values from the new data structure
                iops_values = []
                for iops_data in iops_list:
                    if isinstance(iops_data, dict):
                        # New structure with latency data
                        iops_values.append(iops_data.get('total_iops', 0))
                    else:
                        # Old structure (backward compatibility)
                        iops_values.append(iops_data)
                
                if iops_values:
                    total_iops = sum(iops_values)
                    min_iops = min(iops_values)
                    max_iops = max(iops_values)
                    mean_iops = sum(iops_values) / len(iops_values)
                    writer.writerow([block_size, total_iops, min_iops, max_iops, mean_iops])
        
        print(f"Saved {operation} aggregated results to: {filepath}")
    
    # Save combined aggregated results
    filename = "all_machines_iops_results.csv"
    filepath = os.path.join(csv_dir, filename)
    
    with open(filepath, 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(['Operation', 'BlockSize', 'TotalIOPS', 'MinIOPS', 'MaxIOPS', 'MeanIOPS'])
        
        for operation, block_sizes in all_machines_results.items():
            for block_size, iops_list in block_sizes.items():
                # Extract IOPS values from the new data structure
                iops_values = []
                for iops_data in iops_list:
                    if isinstance(iops_data, dict):
                        # New structure with latency data
                        iops_values.append(iops_data.get('total_iops', 0))
                    else:
                        # Old structure (backward compatibility)
                        iops_values.append(iops_data)
                
                if iops_values:
                    total_iops = sum(iops_values)
                    min_iops = min(iops_values)
                    max_iops = max(iops_values)
                    mean_iops = sum(iops_values) / len(iops_values)
                    writer.writerow([operation, block_size, total_iops, min_iops, max_iops, mean_iops])
    
    print(f"Saved combined aggregated results to: {filepath}")

def save_job_summarized_results_iops(results, all_machines_results, output_dir='.', selected_block_sizes=None):
    """Save IOPS job summarized results to CSV files."""
    
    csv_dir = ensure_csv_directory(output_dir)
    
    # Filter results by selected block sizes if specified
    if selected_block_sizes:
        filtered_results = {}
        for (vm_name, operation, block_size), iops in results.items():
            if block_size in selected_block_sizes:
                filtered_results[(vm_name, operation, block_size)] = iops
        results = filtered_results
        
        # Also filter all_machines_results
        filtered_all_machines = defaultdict(lambda: defaultdict(list))
        for operation, block_sizes in all_machines_results.items():
            for block_size, iops_list in block_sizes.items():
                if block_size in selected_block_sizes:
                    filtered_all_machines[operation][block_size] = iops_list
        all_machines_results = filtered_all_machines
    
    # Save per-machine job summarized results
    # Group results by Machine name
    vm_results = defaultdict(lambda: defaultdict(dict))
    for (vm_name, operation, block_size), iops in results.items():
        vm_results[vm_name][operation][block_size] = iops
    
    for vm_name in vm_results.keys():
        filename = f"{vm_name}_iops_job_summary.csv"
        filepath = os.path.join(csv_dir, filename)
        
        with open(filepath, 'w', newline='') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(['Operation', 'BlockSize', 'TotalIOPS'])
            
            for operation in sorted(vm_results[vm_name].keys()):
                for block_size in sorted(vm_results[vm_name][operation].keys()):
                    iops_data = vm_results[vm_name][operation][block_size]
                    if isinstance(iops_data, dict):
                        # New structure with latency data
                        writer.writerow([operation, block_size, iops_data.get('total_iops', 0)])
                    else:
                        # Old structure (backward compatibility)
                        writer.writerow([operation, block_size, iops_data])
        
        print(f"Saved job-summarized results to: {filepath}")
    
    # Save all machines job summarized results
    filename = "all_machines_iops_job_summary.csv"
    filepath = os.path.join(csv_dir, filename)
    
    with open(filepath, 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(['Machine', 'TotalIOPS'])
        
        for (vm_name, operation, block_size), iops in results.items():
            if isinstance(iops, dict):
                # New structure with latency data
                writer.writerow([vm_name, iops.get('total_iops', 0)])
            else:
                # Old structure (backward compatibility)
                writer.writerow([vm_name, iops])
    
    print(f"Saved all machines job-summarized results to: {filepath}")
    
    # Save block size + operation job summarized results
    for operation, block_sizes in all_machines_results.items():
        for block_size, iops_list in block_sizes.items():
            filename = f"all_machines_block_size_{block_size}_operation_{operation}_iops_job_summary.csv"
            filepath = os.path.join(csv_dir, filename)
            
            with open(filepath, 'w', newline='') as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow(['Machine', 'TotalIOPS'])
                
                for i, iops in enumerate(iops_list):
                    if isinstance(iops, dict):
                        # New structure with latency data
                        machine_name = iops.get('machine', f"machine-{i+1}")
                        writer.writerow([machine_name, iops.get('total_iops', 0)])
                    else:
                        # Old structure (backward compatibility)
                        writer.writerow([f"machine-{i+1}", iops])
            
            print(f"Saved job-summarized results to: {filepath}")

def create_graphs_from_job_summaries(output_dir='.', graph_type='bar', summary_only=False, data_type='bandwidth',
                                     show_values=False):
    """
    Create graphs from block size + operation job summary CSV files.
    
    Args:
        output_dir: Directory to save output files
        graph_type: Type of graphs ('bar', 'line', or 'both')
        summary_only: If True, skip per-Machine graphs and only create summary graphs
        show_values: If True, annotate numeric values on bars/points
    """
    try:
        import matplotlib.pyplot as plt
        import pandas as pd
        
        # Set matplotlib to use a non-interactive backend
        plt.switch_backend('Agg')
        
        # Find all job summary CSV files in the csv_files subdirectory
        csv_dir = ensure_csv_directory(output_dir)
        if data_type == 'iops':
            job_summary_files = glob.glob(os.path.join(csv_dir, "all_machines_block_size_*_operation_*_iops_job_summary.csv"))
        else:
            # Bandwidth summaries end in _job_summary.csv but must not match *_iops_job_summary.csv
            job_summary_files = [
                f for f in glob.glob(os.path.join(csv_dir, "all_machines_block_size_*_operation_*_job_summary.csv"))
                if '_iops_job_summary.csv' not in os.path.basename(f)
                and '_temp_iops_job_summary.csv' not in os.path.basename(f)
            ]
        
        # Filter files based on data type by checking the column names
        filtered_files = []
        for file_path in job_summary_files:
            try:
                df = pd.read_csv(file_path)
                if data_type == 'iops' and 'TotalIOPS' in df.columns:
                    filtered_files.append(file_path)
                elif data_type == 'bandwidth' and 'TotalBwMean' in df.columns:
                    filtered_files.append(file_path)
                elif data_type == 'iops' and 'TotalBwMean' in df.columns:
                    # For IOPS graphs, we can use bandwidth files and convert them
                    # Create a temporary IOPS file
                    df_iops = df.copy()
                    df_iops['TotalIOPS'] = df_iops['TotalBwMean']  # Use bandwidth data as IOPS
                    df_iops = df_iops.drop('TotalBwMean', axis=1)
                    
                    # Save temporary IOPS file
                    temp_file = file_path.replace('_job_summary.csv', '_temp_iops_job_summary.csv')
                    df_iops.to_csv(temp_file, index=False)
                    filtered_files.append(temp_file)
            except Exception as e:
                print(f"Error reading {file_path}: {e}")
                continue
        
        job_summary_files = filtered_files
        
        if not job_summary_files:
            print("No block size + operation job summary files found to create graphs from.")
            return
        
        if summary_only:
            print(f"\nSkipping per-Machine graphs (summary-only mode enabled)")
            return
        
        print(f"\nCreating {graph_type} graphs from {len(job_summary_files)} block size + operation job summary files...")
        
        if graph_type == 'both':
            # Generate both bar and line graphs
            for graph_subtype in ['bar', 'line']:
                print(f"Creating {graph_subtype} graphs...")
                for csv_file in job_summary_files:
                    try:
                        create_single_graph(csv_file, graph_subtype, output_dir, show_values=show_values)
                    except Exception as e:
                        print(f"Error creating {graph_subtype} graph for {csv_file}: {e}")
                        continue
        else:
            # Generate single graph type
            for csv_file in job_summary_files:
                try:
                    create_single_graph(csv_file, graph_type, output_dir, show_values=show_values)
                except Exception as e:
                    print(f"Error creating {graph_type} graph for {csv_file}: {e}")
                    continue
        
        print(f"\nGraph creation complete! Created {graph_type} graphs.")
        
        # Clean up temporary files if any were created
        for csv_file in job_summary_files:
            if '_temp_iops_job_summary.csv' in csv_file:
                try:
                    os.remove(csv_file)
                    print(f"Cleaned up temporary file: {csv_file}")
                except Exception as e:
                    print(f"Error cleaning up temporary file {csv_file}: {e}")
        
    except ImportError as e:
        print(f"Error importing required libraries: {e}")
        print("Please install required dependencies: pip install matplotlib pandas")
    except Exception as e:
        print(f"Error in graph creation: {e}")


def extract_latency_data_for_graph(operation, block_size, output_dir, data_type='iops'):
    """
    Extract average latency data for a specific operation and block size.
    
    Args:
        operation: Operation name (e.g., 'read', 'write', 'randread', 'randwrite')
        block_size: Block size (e.g., '4k', '8k', '128k')
        output_dir: Output directory where latencydata is stored
        data_type: Data type ('iops' or 'bandwidth')
    
    Returns:
        Dictionary with machine names as keys and average latency as values
    """
    try:
        import os
        import glob
        
        # Construct the latency file path
        data_type_suffix = 'bw' if data_type == 'bandwidth' else 'iops'
        # Convert block size to lowercase for file naming consistency
        block_size_lower = block_size.lower()
        
        # Search for latency files in multiple possible locations
        possible_paths = [
            # Original path
            os.path.join(output_dir, 'latencydata', operation, f"{block_size_lower}_{data_type_suffix}.txt"),
            # Path in output-dir-results_lat
            os.path.join(output_dir, 'output-dir-results_lat', '*', 'latencydata', operation, f"{block_size_lower}_{data_type_suffix}.txt"),
            # Path in double_graphs_outdir
            os.path.join(output_dir, 'double_graphs_outdir', '*', 'latencydata', operation, f"{block_size_lower}_{data_type_suffix}.txt"),
            # Path in results_to_analyze
            os.path.join(output_dir, 'results_to_analyze', '*', 'latencydata', operation, f"{block_size_lower}_{data_type_suffix}.txt"),
            # Path in output-dir-results
            os.path.join(output_dir, 'output-dir-results', '*', 'latencydata', operation, f"{block_size_lower}_{data_type_suffix}.txt"),
        ]
        
        latency_file = None
        for path_pattern in possible_paths:
            if '*' in path_pattern:
                # Use glob to find matching files
                matching_files = glob.glob(path_pattern)
                if matching_files:
                    latency_file = matching_files[0]  # Use the first match
                    break
            else:
                # Direct path
                if os.path.exists(path_pattern):
                    latency_file = path_pattern
                    break
        
        if not latency_file or not os.path.exists(latency_file):
            print(f"Latency file not found for {operation} {block_size} {data_type_suffix}. Searched paths:")
            for path in possible_paths:
                print(f"  - {path}")
            return {}
        
        print(f"Found latency file: {latency_file}")
        latency_data = {}
        
        with open(latency_file, 'r') as f:
            lines = f.readlines()
            
        machine_name = None
        for line in lines:
            if line.startswith('Machine:'):
                # Extract machine name (remove 'Machine: ' prefix)
                machine_name = line.replace('Machine:', '').strip()
            elif line.startswith('Average Latency:') and machine_name:
                # Extract latency value (remove 'Average Latency: ' prefix and ' ms' suffix)
                latency_str = line.replace('Average Latency:', '').replace('ms', '').strip()
                try:
                    latency_value = float(latency_str)
                    latency_data[machine_name] = latency_value
                except ValueError:
                    continue
                machine_name = None  # Reset for next machine
        
        print(f"Extracted latency data for {len(latency_data)} machines")
        return latency_data
        
    except Exception as e:
        print(f"Error extracting latency data: {e}")
        return {}


def create_single_graph(csv_file, graph_type, output_dir, show_values=False):
    """Create a single graph from a CSV file with dual-axis for latency data."""
    try:
        # Read the CSV file
        df = pd.read_csv(csv_file)
        
        # Skip if file doesn't have the expected columns
        if 'Machine' not in df.columns:
            print(f"Skipping {csv_file}: Missing Machine column")
            return
        
        # Determine if this is IOPS or bandwidth data
        if 'TotalIOPS' in df.columns:
            data_column = 'TotalIOPS'
            data_type = 'IOPS'
            y_label = 'Total IOPS per Machine (sum of all jobs)'
            data_type_for_latency = 'iops'
        elif 'TotalBwMean' in df.columns:
            data_column = 'TotalBwMean'
            data_type = 'Bandwidth'
            y_label = f'Total Bandwidth per Machine (sum of all jobs) [{BW_RATE_UNIT}]'
            data_type_for_latency = 'iops'  # Use IOPS latency data since it's the same source
        else:
            print(f"Skipping {csv_file}: Missing TotalIOPS or TotalBwMean column")
            return
        
        # Extract block size and operation from filename for latency data
        operation = None
        block_size = None
        latency_data = {}
        
        if 'block_size_' in csv_file and '_operation_' in csv_file:
            filename = os.path.basename(csv_file)
            parts = filename.replace('_job_summary.csv', '').split('_')
            if len(parts) >= 7:
                block_size = parts[4]  # 4k
                operation = parts[6]   # read
                
                # Extract latency data for this operation and block size
                latency_data = extract_latency_data_for_graph(operation, block_size, output_dir, data_type_for_latency)
        
        # Create the plot with dual axes
        fig, ax1 = plt.subplots(figsize=(12, 6))
        
        # Create numeric x-axis positions for all data points
        machines = df['Machine'].tolist()
        total_data = df[data_column].tolist()
        all_positions = range(len(machines))
        annotate = show_values and len(machines) <= SHOW_VALUES_MAX_POINTS
        if show_values and not annotate:
            print(
                f"Note: --show-values skipped for {os.path.basename(csv_file)} "
                f"({len(machines)} points > {SHOW_VALUES_MAX_POINTS})"
            )
        
        # Get X-axis labels and positions based on number of Machines
        x_positions, x_labels = get_x_axis_labels_and_positions(df)
        
        # Plot primary data (IOPS or Bandwidth)
        if graph_type == 'bar':
            bars = ax1.bar(all_positions, total_data, color='skyblue', edgecolor='navy', alpha=0.7)
            if annotate:
                annotate_bar_values(ax1, bars, fontsize=graph_value_fontsize(len(machines)))
        else:  # line graph
            ax1.plot(all_positions, total_data, 
                    marker='o', linewidth=3, markersize=4, 
                    color='steelblue', markerfacecolor='lightblue', 
                    markeredgecolor='navy', markeredgewidth=2)
            if annotate:
                annotate_line_values(
                    ax1, all_positions, total_data, fontsize=graph_value_fontsize(len(machines))
                )

        # Set x-axis ticks based on visibility rules
        ax1.set_xticks(x_positions)
        ax1.set_xticklabels(x_labels, rotation=45, ha='right')
        
        # Calculate average (Total / number of machines)
        total_data_sum = sum(total_data)
        num_machines = len(machines)
        average_data = total_data_sum / num_machines if num_machines > 0 else 0
        
        # Add horizontal line for average
        if data_type == 'IOPS':
            ax1.axhline(y=average_data, color='red', linestyle='--', linewidth=2, alpha=0.8, 
                       label=f'Average: {average_data:.1f} IOPS')
        else:
            ax1.axhline(y=average_data, color='red', linestyle='--', linewidth=2, alpha=0.8, 
                       label=f'Average: {average_data:.1f} {BW_RATE_UNIT}')
                
        # Customize the primary axis
        ax1.set_ylabel(y_label, fontsize=10, fontweight='bold', color='blue')
        ax1.set_xlabel('Machine Index', fontsize=10, fontweight='bold')
        ax1.tick_params(axis='y', labelcolor='blue')
        
        # Add latency data on secondary axis if available
        if latency_data and len(latency_data) > 0:
            # Create secondary y-axis for latency
            ax2 = ax1.twinx()
            
            # Match latency data to machines in the same order
            latency_values = []
            for machine in machines:
                # Try to find matching latency data (handle different machine name formats)
                found_latency = None
                # Extract machine name from full path (e.g., /path/to/machine-name -> machine-name)
                machine_basename = os.path.basename(machine)
                
                # Try exact match first (case-insensitive)
                for lat_machine, lat_value in latency_data.items():
                    if machine_basename.lower() == lat_machine.lower():
                        found_latency = lat_value
                        break
                
                # If no exact match, try partial matching (case-insensitive)
                if found_latency is None:
                    for lat_machine, lat_value in latency_data.items():
                        # Check if machine name contains latency machine name or vice versa
                        if (lat_machine.lower() in machine_basename.lower() or 
                            machine_basename.lower() in lat_machine.lower()):
                            found_latency = lat_value
                            break
                
                # If still no match, try path-based matching
                if found_latency is None:
                    for lat_machine, lat_value in latency_data.items():
                        # Check if machine path ends with latency machine name
                        if machine.rstrip('/').endswith('/' + lat_machine):
                            found_latency = lat_value
                            break
                
                if found_latency is not None:
                    latency_values.append(found_latency)
                else:
                    latency_values.append(0)  # Default to 0 if no latency data found
            
            # Plot latency data
            if graph_type == 'bar':
                # For bar charts, show latency as a line overlay
                ax2.plot(all_positions, latency_values, 
                        marker='s', linewidth=2, markersize=3, 
                        color='orange', markerfacecolor='orange', 
                        markeredgecolor='darkorange', markeredgewidth=1,
                        label='Average Latency (ms)')
            else:  # line graph
                ax2.plot(all_positions, latency_values, 
                        marker='s', linewidth=2, markersize=3, 
                        color='orange', markerfacecolor='orange', 
                        markeredgecolor='darkorange', markeredgewidth=1,
                        label='Average Latency (ms)')
            
            # Calculate average latency
            avg_latency = sum(latency_values) / len(latency_values) if latency_values else 0
            ax2.axhline(y=avg_latency, color='darkorange', linestyle=':', linewidth=2, alpha=0.8,
                       label=f'Avg Latency: {avg_latency:.2f} ms')
            
            # Customize secondary axis
            ax2.set_ylabel('Average Latency (ms)', fontsize=10, fontweight='bold', color='orange')
            ax2.tick_params(axis='y', labelcolor='orange')
            
            # Set latency axis limits
            if latency_values:
                ax2.set_ylim(0, max(latency_values) * 1.1)
        
        # Extract block size and operation from filename for title
        if 'block_size_' in csv_file and '_operation_' in csv_file:
            filename = os.path.basename(csv_file)
            parts = filename.replace('_job_summary.csv', '').split('_')
            if len(parts) >= 7:
                block_size = parts[4]  # 4k
                operation = parts[6]   # read
                
                # Get FIO configuration for subtitle
                config_key = (operation, block_size)
                subtitle = ""
                if config_key in FIO_CONFIGS:
                    subtitle = format_fio_subtitle(FIO_CONFIGS[config_key])
                
                # Customize the plot
                num_machines = len(df)
                chart_type = "Bar Chart" if graph_type == 'bar' else "Line Chart"
                plt.title(f'{data_type} Performance ({chart_type}): {operation.upper()} - {block_size.upper()} Block Size ({num_machines} Machines)', 
                         fontsize=16, fontweight='bold', pad=30)
                
                # Add subtitle with FIO configuration
                if subtitle:
                    # Position subtitle in the center of the graph area (not the entire figure)
                    # Since we have right=0.65, the graph area is from 0 to 0.65, so center is at 0.325
                    plt.figtext(0.325, 0.92, subtitle, fontsize=10, ha='center', va='top')
                else:
                    plt.title(csv_file.replace('_job_summary.csv', ''), fontsize=14, fontweight='bold')
        else:
            # For per-machine files like: vm-1_bw_mean_job_summary.csv
            plt.title(csv_file.replace('_bw_mean_job_summary.csv', ''), fontsize=14, fontweight='bold')

        # Set axis limits to include zero
        ax1.set_xlim(-0.5, len(machines) - 0.5)
        ax1.set_ylim(0, max(total_data) * (1.2 if annotate else 1.1))
        
        # Customize grid and layout
        if graph_type == 'bar':
            ax1.grid(axis='y', alpha=0.3, linestyle='--')
        else:  # line graph
            ax1.grid(True, alpha=0.3, linestyle='--', linewidth=0.5)
        
        # Add legend for both axes (positioned outside the graph area)
        lines1, labels1 = ax1.get_legend_handles_labels()
        if latency_data and len(latency_data) > 0:
            lines2, labels2 = ax2.get_legend_handles_labels()
            ax1.legend(lines1 + lines2, labels1 + labels2, loc='center left', bbox_to_anchor=(1.15, 0.5), fontsize=9)
        else:
            ax1.legend(loc='center left', bbox_to_anchor=(1.15, 0.5), fontsize=9)
        
        # Adjust layout to accommodate legend outside the graph
        plt.tight_layout()
        plt.subplots_adjust(right=0.65)  # Make more room for the legend on the right
                
        # Create output filename with data type included
        csv_basename = os.path.basename(csv_file)
        data_type_suffix = 'bw' if data_type == 'Bandwidth' else 'iops'
        if graph_type == 'bar':
            output_filename = csv_basename.replace('.csv', f'_{data_type_suffix}.png')
        else:  # line graph
            output_filename = csv_basename.replace('.csv', f'_{graph_type}_{data_type_suffix}.png')
        output_file = os.path.join(output_dir, output_filename)
                
        # Save the plot
        plt.savefig(output_file, dpi=300, bbox_inches='tight', 
                   facecolor='white', edgecolor='none')
        plt.close()
                
        print(f"Created {graph_type} graph: {output_file}")
        return True
        
    except Exception as e:
        print(f"Error creating {graph_type} graph for {csv_file}: {e}")
        return False


def create_operation_summary_graphs(csv_files, graph_type='bar', output_dir='.', data_type='bandwidth',
                                    show_values=False):
    """
    Create graphs for operation summary CSV files (all block sizes combined).
    Supports both bar and line graphs with individual block size averages.
    
    Args:
        csv_files: List of CSV files to process
        graph_type: Type of graph ('bar', 'line', or 'both')
        output_dir: Directory to save output files
        data_type: Type of data ('iops' or 'bandwidth')
        show_values: If True, annotate numeric values on bars/points
    """
    try:
        import matplotlib.pyplot as plt
        import pandas as pd
        import numpy as np
        import re
        
        # Set matplotlib to use a non-interactive backend
        import matplotlib
        matplotlib.use('Agg')
        
        success_count = 0
        
        # Handle 'both' option by creating both bar and line graphs
        graph_types = ['bar', 'line'] if graph_type == 'both' else [graph_type]
        
        for csv_file in csv_files:
            for current_graph_type in graph_types:
                try:
                    # Read CSV file
                    df = pd.read_csv(csv_file)
                    
                    # Extract operation from filename
                    filename = os.path.basename(csv_file)
                    match = re.search(r'summary-(\w+)-all-blocks\.csv', filename)
                    if not match:
                        continue
                    operation = match.group(1)
                    
                    # Get block sizes (all columns except vm_name)
                    block_sizes = [col for col in df.columns if col != 'vm_name']
                    
                    # Create the plot with dual axes - increased size for better spacing
                    fig, ax1 = plt.subplots(figsize=(20, 12))
                    
                    # Sort by vm_name for consistent ordering
                    df_sorted = df.sort_values('vm_name')
                    
                    # Get X-axis labels and positions based on number of Machines
                    x_positions, x_labels = get_x_axis_labels_and_positions(df_sorted)
                    
                    # Create numeric x-axis positions for all data points
                    all_positions = range(len(df_sorted))
                    
                    # Create plot based on graph type - use more readable colors
                    # Define a set of colors that provide good contrast and readability
                    readable_colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf']
                    colors = [readable_colors[i % len(readable_colors)] for i in range(len(block_sizes))]
                    
                    if current_graph_type == 'bar':
                        # Create bar plot for each block size
                        width = 0.8 / len(block_sizes)  # Width of each bar group
                        annotate = (
                            show_values
                            and len(df_sorted) * max(len(block_sizes), 1) <= SHOW_VALUES_MAX_POINTS
                        )
                        if show_values and not annotate:
                            print(
                                f"Note: --show-values skipped for {filename} "
                                f"({len(df_sorted)} machines × {len(block_sizes)} sizes "
                                f"> {SHOW_VALUES_MAX_POINTS} labels)"
                            )
                        
                        for i, block_size in enumerate(block_sizes):
                            offset = (i - len(block_sizes)/2 + 0.5) * width
                            display_name = get_block_size_display_name(block_size)
                            bars = ax1.bar([pos + offset for pos in all_positions], 
                                          df_sorted[block_size], 
                                          width=width, 
                                          label=display_name,
                                          color=colors[i], 
                                          alpha=0.8,
                                          edgecolor='black',
                                          linewidth=0.5)
                            if annotate:
                                annotate_bar_values(
                                    ax1, bars, fontsize=graph_value_fontsize(len(df_sorted))
                                )
                    else:  # line graph
                        # Create line plot for each block size
                        annotate = (
                            show_values
                            and len(df_sorted) * max(len(block_sizes), 1) <= SHOW_VALUES_MAX_POINTS
                        )
                        if show_values and not annotate:
                            print(
                                f"Note: --show-values skipped for {filename} "
                                f"({len(df_sorted)} machines × {len(block_sizes)} sizes "
                                f"> {SHOW_VALUES_MAX_POINTS} labels)"
                            )
                        for i, block_size in enumerate(block_sizes):
                            display_name = get_block_size_display_name(block_size)
                            y_vals = df_sorted[block_size].tolist()
                            ax1.plot(all_positions, y_vals, 
                                    marker='o', linewidth=2, markersize=3,
                                    label=display_name,
                                    color=colors[i], 
                                    markerfacecolor=colors[i],
                                    markeredgecolor='black',
                                    markeredgewidth=1)
                            if annotate:
                                annotate_line_values(
                                    ax1,
                                    all_positions,
                                    y_vals,
                                    fontsize=graph_value_fontsize(len(df_sorted)),
                                    color=colors[i],
                                )
                
                    # Calculate and display average for each individual block size
                    for i, block_size in enumerate(block_sizes):
                        block_data = df_sorted[block_size]
                        # Calculate average for this specific block size
                        block_average = block_data.mean()
                        
                        # Add horizontal line for this block size's average
                        ax1.axhline(y=block_average, color=colors[i], linestyle='--', linewidth=2, alpha=0.7)
                
                    # Get FIO configuration for subtitle (use first block size as reference)
                    # For comparison graphs, exclude block size since multiple block sizes are shown
                    subtitle = ""
                    if block_sizes:
                        # Use the first block size to get FIO config for subtitle
                        first_block_size = block_sizes[0]
                        config_key = (operation, first_block_size)
                        if config_key in FIO_CONFIGS:
                            subtitle = format_fio_subtitle(FIO_CONFIGS[config_key], exclude_bs=True)
                    
                    # Customize the plot
                    num_vms = len(df_sorted)
                    chart_type = "Bar Chart" if current_graph_type == 'bar' else "Line Chart"
                    
                    # Set title and Y-axis label based on data type
                    if data_type == 'iops':
                        ax1.set_title(f'Total IOPS per Machine Performance Comparison ({chart_type}): {operation.upper()} - Selected Block Sizes ({num_vms} Machines)', 
                                     fontsize=14, fontweight='bold', pad=40)
                        ax1.set_ylabel('Total IOPS per Machine (sum of all jobs)', fontsize=12, fontweight='bold', color='blue')
                        data_type_for_latency = 'iops'
                    else:
                        ax1.set_title(f'Total Bandwidth per Machine Performance Comparison ({chart_type}): {operation.upper()} - Selected Block Sizes ({num_vms} Machines)', 
                                     fontsize=14, fontweight='bold', pad=40)
                        ax1.set_ylabel(f'Total Bandwidth per Machine (sum of all jobs) [{BW_RATE_UNIT}]', fontsize=12, fontweight='bold', color='blue')
                        data_type_for_latency = 'iops'  # Use IOPS latency data since it's the same source
                    
                    # Add latency data on secondary axis if available
                    latency_data_available = False
                    if block_sizes:
                        # Create secondary y-axis for latency
                        ax2 = ax1.twinx()
                        
                        # Define colors for different block sizes
                        latency_colors = ['orange', 'red', 'purple', 'brown', 'pink', 'gray', 'olive', 'cyan']
                        latency_markers = ['s', 'o', '^', 'D', 'v', '<', '>', 'p']
                        
                        all_latency_values = []
                        
                        # Plot latency data for each block size
                        for i, block_size in enumerate(block_sizes):
                            latency_data = extract_latency_data_for_graph(operation, block_size, output_dir, data_type_for_latency)
                            
                            if latency_data and len(latency_data) > 0:
                                latency_data_available = True
                                
                                # Match latency data to machines in the same order
                                latency_values = []
                                for machine in df_sorted['vm_name']:
                                    # Try to find matching latency data (handle different machine name formats and case)
                                    found_latency = None
                                    
                                    # Extract machine name from full path (e.g., /path/to/machine-name -> machine-name)
                                    machine_basename = os.path.basename(machine)
                                    
                                    # Try exact match first (case-insensitive)
                                    for lat_machine, lat_value in latency_data.items():
                                        if machine_basename.lower() == lat_machine.lower():
                                            found_latency = lat_value
                                            break
                                    
                                    # If no exact match, try partial matching (case-insensitive)
                                    if found_latency is None:
                                        for lat_machine, lat_value in latency_data.items():
                                            # Check if machine name contains latency machine name or vice versa
                                            if (lat_machine.lower() in machine_basename.lower() or 
                                                machine_basename.lower() in lat_machine.lower()):
                                                found_latency = lat_value
                                                break
                                    
                                    # If still no match, try path-based matching
                                    if found_latency is None:
                                        for lat_machine, lat_value in latency_data.items():
                                            # Check if machine path ends with latency machine name
                                            if machine.rstrip('/').endswith('/' + lat_machine):
                                                found_latency = lat_value
                                                break
                                    
                                    if found_latency is not None:
                                        latency_values.append(found_latency)
                                    else:
                                        latency_values.append(0)  # Default to 0 if no latency data found
                                
                                # Store all latency values for axis limits
                                all_latency_values.extend([v for v in latency_values if v > 0])
                                
                                # Plot latency data as a line overlay for this block size
                                color = latency_colors[i % len(latency_colors)]
                                marker = latency_markers[i % len(latency_markers)]
                                
                                ax2.plot(all_positions, latency_values, 
                                        marker=marker, linewidth=2, markersize=2, 
                                        color=color, markerfacecolor=color, 
                                        markeredgecolor=color, markeredgewidth=1,
                                        label=f'Latency {block_size.upper()}', alpha=0.8)
                                
                                # Calculate and plot average latency for this block size
                                avg_latency = sum(latency_values) / len(latency_values) if latency_values else 0
                                ax2.axhline(y=avg_latency, color=color, linestyle=':', linewidth=2, alpha=0.8,
                                           label=f'Avg Latency {block_size.upper()}: {avg_latency:.2f} ms')
                        
                        if latency_data_available:
                            # Customize secondary axis
                            ax2.set_ylabel('Average Latency (ms)', fontsize=12, fontweight='bold', color='orange')
                            ax2.tick_params(axis='y', labelcolor='orange')
                            
                            # Set latency axis limits based on all latency values
                            if all_latency_values:
                                ax2.set_ylim(0, max(all_latency_values) * 1.1)
                    
                    # Add subtitle with FIO configuration
                    if subtitle:
                        # Position subtitle in the center of the graph area (not the entire figure)
                        # Since we have right=0.55, the graph area is from 0 to 0.55, so center is at 0.275
                        # Position below the title (which has pad=40), so subtitle goes at 0.88 to be below title
                        plt.figtext(0.275, 0.88, subtitle, fontsize=10, ha='center', va='top')
                    
                    # Set X-axis label
                    ax1.set_xlabel('Machine Index', fontsize=12, fontweight='bold')
                    
                    # Set x-axis ticks based on visibility rules
                    ax1.set_xticks(x_positions)
                    ax1.set_xticklabels(x_labels, rotation=45, ha='right')
                    
                    # Set axis limits
                    ax1.set_xlim(-0.5, len(df_sorted) - 0.5)
                    max_value = df_sorted[block_sizes].max().max()
                    ax1.set_ylim(0, max_value * (1.2 if show_values else 1.1))
                    
                    # Add legend on the right side of the graph (aligned with top of graph)
                    if latency_data_available:
                        # Combine legends from both axes
                        lines1, labels1 = ax1.get_legend_handles_labels()
                        lines2, labels2 = ax2.get_legend_handles_labels()
                        ax1.legend(lines1 + lines2, labels1 + labels2, loc='center left', bbox_to_anchor=(1.15, 0.95), fontsize=9)
                    else:
                        ax1.legend(loc='center left', bbox_to_anchor=(1.15, 0.95), fontsize=9)
                    
                    # Add average, total per Machine, and total all Machines value text boxes below the legend
                    for i, block_size in enumerate(block_sizes):
                        block_data = df_sorted[block_size]
                        block_average = block_data.mean()
                        block_total = block_data.sum()
                        display_name = get_block_size_display_name(block_size)
                        
                        # Format text based on data type
                        if data_type == 'iops':
                            avg_text = f'{display_name} Average: {block_average:.1f} IOPS'
                            total_per_machine_text = f'{display_name} Total per Machine: {block_average:.1f} IOPS'
                            total_all_machines_text = f'{display_name} Total All Machines: {block_total:.0f} IOPS'
                        else:
                            avg_text = f'{display_name} Average: {block_average:.1f} {BW_RATE_UNIT}'
                            total_per_machine_text = f'{display_name} Total per Machine: {block_average:.1f} {BW_RATE_UNIT}'
                            total_all_machines_text = f'{display_name} Total All Machines: {block_total:.0f} {BW_RATE_UNIT}'
                        
                        # Position average text box (top) - white background for better readability
                        # Use normal weight so legend-area labels do not consume excess horizontal space
                        ax1.text(1.15, 0.75 - (i * 0.16), avg_text, 
                                transform=ax1.transAxes, fontsize=9, fontweight='normal',
                                verticalalignment='top', bbox=dict(boxstyle='round', facecolor='white', alpha=0.9, edgecolor=colors[i], linewidth=1),
                                color=colors[i])
                        
                        # Position total per machine text box (middle) - white background for better readability
                        ax1.text(1.15, 0.75 - (i * 0.16) - 0.04, total_per_machine_text, 
                                transform=ax1.transAxes, fontsize=9, fontweight='normal',
                                verticalalignment='top', bbox=dict(boxstyle='round', facecolor='white', alpha=0.9, edgecolor=colors[i], linewidth=1),
                                color=colors[i])
                        
                        # Position total all machines text box (bottom) - white background for better readability
                        ax1.text(1.15, 0.75 - (i * 0.16) - 0.08, total_all_machines_text, 
                                transform=ax1.transAxes, fontsize=9, fontweight='normal',
                                verticalalignment='top', bbox=dict(boxstyle='round', facecolor='white', alpha=0.9, edgecolor=colors[i], linewidth=1),
                                color=colors[i])
                    
                    # Add grid for better readability
                    if current_graph_type == 'bar':
                        ax1.grid(axis='y', alpha=0.3, linestyle='--')
                    else:  # line graph
                        ax1.grid(True, alpha=0.3, linestyle='--', linewidth=0.5)
                
                    # Adjust layout to accommodate legend and text boxes on the right
                    plt.tight_layout()
                    plt.subplots_adjust(right=0.55, top=0.85)  # Increased space on right for latency axis label and legend boxes
                    
                    # Generate PNG filename with number of machines, block sizes, graph type, and data type included
                    block_sizes_str = '-'.join(block_sizes)
                    csv_basename = os.path.basename(csv_file)
                    graph_suffix = 'bar' if current_graph_type == 'bar' else 'line'
                    data_type_suffix = 'bw' if data_type == 'bandwidth' else 'iops'
                    png_filename = f"{num_vms}_machines_{csv_basename.replace('.csv', f'_comparison-{block_sizes_str}_average_{graph_suffix}_{data_type_suffix}.png')}"
                    png_filepath = os.path.join(output_dir, png_filename)
                    
                    # Save the plot
                    plt.savefig(png_filepath, dpi=300, bbox_inches='tight', 
                               facecolor='white', edgecolor='none')
                    plt.close()  # Close the figure to free memory
                    
                    print(f"Created operation summary graph: {png_filename}")
                    success_count += 1
                    
                    # Create summary text file for this operation
                    create_operation_summary_file(df_sorted, operation, block_sizes, data_type, output_dir)
                    
                except Exception as e:
                    print(f"Error creating operation summary graph for {csv_file}: {e}")
        
        return success_count
        
    except ImportError as e:
        print(f"Error importing required libraries: {e}")
        print("Please install required dependencies: pip install matplotlib pandas numpy")
        return 0
    except Exception as e:
        print(f"Error in operation summary graph creation: {e}")
        return 0


def create_latency_performance_correlation_graph(all_machines_results, output_dir='.', data_type='iops'):
    """
    Create latency vs performance correlation graphs.
    Creates separate graphs for each operation, with subplots for different block sizes.
    X-axis: Performance (IOPS or bandwidth) per Machine
    Y-axis: Average latency in milliseconds
    Each dot represents a Machine's performance vs latency.
    
    Args:
        all_machines_results: Dictionary with aggregated results from all machines
        output_dir: Output directory for the graph
        data_type: 'iops' or 'bandwidth'
    
    Returns:
        Success count (number of graphs created successfully)
    """
    try:
        import matplotlib.pyplot as plt
        import numpy as np
        
        
        # Set matplotlib to use a non-interactive backend
        plt.switch_backend('Agg')
        
        if not all_machines_results:
            print("ERROR: No data available for latency correlation graph")
            return 0
        
        print(f"Creating latency correlation graphs for {data_type} data...")
        
        # Group data by operation and block size
        operations = set()
        block_sizes = set()
        
        for operation, block_data in all_machines_results.items():
            operations.add(operation)
            for block_size in block_data.keys():
                block_sizes.add(block_size)
        
        operations = sorted(operations)
        block_sizes = sorted(block_sizes, key=lambda x: int(x.replace('k', '')))
        
        print(f"Found operations: {operations}")
        print(f"Found block sizes: {block_sizes}")
        
        if len(operations) == 0:
            print("ERROR: No operations found in data")
            return 0
        
        success_count = 0
        
        # Create a separate graph for each operation
        for operation in operations:
            print(f"Processing operation: {operation}")
            
            # Get block sizes available for this operation
            op_block_sizes = [bs for bs in block_sizes 
                            if operation in all_machines_results and bs in all_machines_results[operation]]
            
            if not op_block_sizes:
                print(f"No block sizes found for operation {operation}")
                continue
            
            print(f"Block sizes for {operation}: {op_block_sizes}")
            
            # Create subplots for each block size of this operation
            num_block_sizes = len(op_block_sizes)
            cols = min(3, num_block_sizes)  # Max 3 columns
            rows = (num_block_sizes + cols - 1) // cols
            
            print(f"Creating subplot layout: {rows}x{cols} for {num_block_sizes} block sizes")
            
            # Increase figure height to accommodate title and subtitle
            fig_height = 4*rows + 1.5  # Add extra height for title and subtitle
            fig, axes = plt.subplots(rows, cols, figsize=(5*cols, fig_height))
            
            # Ensure axes is always a list for consistent indexing
            if num_block_sizes == 1:
                axes = [axes]
            elif rows == 1 and cols == 1:
                axes = [axes]
            elif rows == 1:
                # Single row, multiple columns - axes is already a list
                axes = list(axes) if hasattr(axes, '__iter__') else [axes]
            else:
                # Multiple rows - flatten the 2D array
                axes = axes.flatten()
            
            # Use a single color for this operation (since all subplots are the same operation)
            color = plt.cm.Set1(operations.index(operation) / len(operations))
            
            for bs_idx, block_size in enumerate(op_block_sizes):
                print(f"Processing block size: {block_size}")
                ax = axes[bs_idx]
                
                data = all_machines_results[operation][block_size]
                print(f"Data points for {operation}-{block_size}: {len(data)}")
                
                # Extract performance and latency data from the list of items
                performance_values = []
                latency_values = []
                
                if data_type == 'iops':
                    # IOPS data: one entry per machine with pre-averaged latency
                    for item in data:
                        if isinstance(item, dict) and 'total_iops' in item and 'avg_latency_ms' in item:
                            performance_values.append(item['total_iops'])
                            latency_values.append(item['avg_latency_ms'])
                else:  # bandwidth
                    # Bandwidth data: multiple entries per machine (one per job), need to aggregate
                    machine_data = {}
                    for item in data:
                        if isinstance(item, dict) and 'bw_mean' in item and 'avg_latency_ms' in item and 'machine' in item:
                            machine = os.path.basename(item['machine'])  # Extract basename to match IOPS format
                            bw_value = item['bw_mean']
                            latency_value = item['avg_latency_ms']
                            
                            if machine not in machine_data:
                                machine_data[machine] = {'bw_values': [], 'latency_values': []}
                            machine_data[machine]['bw_values'].append(bw_value)
                            machine_data[machine]['latency_values'].append(latency_value)
                    
                    # Calculate average performance and latency per machine
                    for machine, values in machine_data.items():
                        if values['bw_values'] and values['latency_values']:
                            avg_bw = sum(values['bw_values']) / len(values['bw_values'])
                            avg_latency = sum(values['latency_values']) / len(values['latency_values'])
                            performance_values.append(avg_bw)
                            latency_values.append(avg_latency)
                
                if performance_values and latency_values:
                    # For large datasets (>1000 points), use smaller markers and lower alpha for better performance
                    num_points = len(performance_values)
                    if num_points > 1000:
                        marker_size = max(10, 60 - (num_points - 1000) // 100)  # Reduce size for large datasets
                        alpha = max(0.3, 0.7 - (num_points - 1000) // 2000)  # Reduce alpha for large datasets
                        
                        # For very large datasets (>5000 points), sample the data to prevent memory issues
                        if num_points > 5000:
                            step = num_points // 5000
                            performance_values = performance_values[::step]
                            latency_values = latency_values[::step]
                            num_points = len(performance_values)
                    else:
                        marker_size = 60
                        alpha = 0.7
                    
                    # Create scatter plot
                    ax.scatter(performance_values, latency_values, 
                              color=color, alpha=alpha, s=marker_size,
                              edgecolors='black', linewidth=0.5)
                    
                    # Set reasonable axis limits
                    if performance_values:
                        ax.set_xlim(0, max(performance_values) * 1.1)
                    if latency_values:
                        ax.set_ylim(0, max(latency_values) * 1.1)
                    
                    # Format X-axis labels for better readability with large numbers
                    def format_large_numbers(x, pos):
                        if x >= 1e6:
                            return f'{x/1e6:.1f}M'
                        elif x >= 1e4:  # Only use K format for numbers >= 10,000
                            return f'{x/1e3:.0f}K'
                        else:
                            return f'{x:.0f}'
                    
                    ax.xaxis.set_major_formatter(plt.FuncFormatter(format_large_numbers))
                
                # Customize subplot
                metric_name = 'IOPS' if data_type == 'iops' else f'Bandwidth ({BW_RATE_UNIT})'
                ax.set_xlabel(f'Total {metric_name} per Machine', fontsize=10, fontweight='bold')
                ax.set_ylabel('Average Latency (ms)', fontsize=10, fontweight='bold')
                ax.set_title(f'Block Size: {block_size}', fontsize=12, fontweight='bold')
                ax.grid(True, alpha=0.3)
            
            # Hide unused subplots
            for idx in range(num_block_sizes, len(axes)):
                axes[idx].set_visible(False)
            
            # Calculate number of machines from the data
            num_machines = 0
            if op_block_sizes and operation in all_machines_results:
                # Get the first available block size to count unique machines
                first_bs = op_block_sizes[0]
                if first_bs in all_machines_results[operation]:
                    # Count unique machine names, not total data points
                    unique_machines = set()
                    for data_point in all_machines_results[operation][first_bs]:
                        if 'machine' in data_point:
                            unique_machines.add(data_point['machine'])
                    num_machines = len(unique_machines)
            
            print(f"Calculated {num_machines} unique machines for {operation}")
            
            # Get FIO configuration for subtitle (use first available block size)
            # Exclude block size from subtitle since it's already shown in each subplot title
            subtitle = ""
            if op_block_sizes:
                first_bs = op_block_sizes[0]
                config_key = (operation, first_bs)
                if config_key in FIO_CONFIGS:
                    subtitle = format_fio_subtitle(FIO_CONFIGS[config_key], exclude_bs=True)
            
            # Set main title
            metric_name = 'IOPS' if data_type == 'iops' else f'Bandwidth ({BW_RATE_UNIT})'
            fig.suptitle(f'{operation.upper()} - Latency vs {metric_name} Performance Correlation ({num_machines} Machines)',
                        fontsize=16, fontweight='bold', y=0.95)
            
            # Add subtitle with FIO configuration
            if subtitle:
                plt.figtext(0.5, 0.85, subtitle, fontsize=10, ha='center', va='top')
            
            # Adjust layout to ensure title and subtitle don't overlap
            plt.tight_layout()
            # Add more space between rows when there are multiple rows
            if rows > 1:
                plt.subplots_adjust(top=0.75, hspace=0.4)  # Increased space for title/subtitle and between rows
            else:
                plt.subplots_adjust(top=0.75)  # Increased space for title and subtitle
            
            # Save the plot
            graph_suffix = 'latency_correlation'
            data_type_suffix = 'bw' if data_type == 'bandwidth' else 'iops'
            png_filename = f"{graph_suffix}_{operation}_{data_type_suffix}.png"
            png_filepath = os.path.join(output_dir, png_filename)
            
            # Ensure output directory exists
            os.makedirs(output_dir, exist_ok=True)
            
            print(f"Saving graph to: {png_filepath}")
            plt.savefig(png_filepath, dpi=300, bbox_inches='tight', 
                       facecolor='white', edgecolor='none')
            plt.close()
            
            print(f"Successfully created latency correlation graph: {png_filename}")
            success_count += 1
        
        print(f"Latency correlation graph creation complete. Created {success_count} graphs.")
        return success_count
        
    except ImportError as e:
        print(f"Error importing required libraries: {e}")
        print("Please install required dependencies: pip install matplotlib pandas numpy")
        return 0
    except Exception as e:
        print(f"Error creating latency correlation graph: {e}")
        import traceback
        print(f"Full traceback: {traceback.format_exc()}")
        return 0


def save_latency_data_to_files(all_machines_results, output_dir='.', data_type='iops'):
    """
    Save latency data for each machine to text files in a latencydata directory.
    Creates separate files for each operation and block size combination.
    
    Args:
        all_machines_results: Dictionary with aggregated results from all machines
        output_dir: Output directory for the latency data files
        data_type: 'iops' or 'bandwidth'
    
    Returns:
        Success count (number of files created successfully)
    """
    try:
        import os
        
        # Create latencydata directory
        latency_dir = os.path.join(output_dir, 'latencydata')
        os.makedirs(latency_dir, exist_ok=True)
        
        success_count = 0
        
        if not all_machines_results:
            print("No data available for latency extraction")
            return 0
        
        operations = sorted(set(all_machines_results.keys()))
        print(f"Extracting latency data for operations: {operations}")
        
        for operation in operations:
            op_block_sizes = sorted([bs for bs in all_machines_results[operation].keys() if all_machines_results[operation][bs]], 
                                   key=lambda x: int(x.replace('k', '')))
            
            if not op_block_sizes:
                print(f"No block sizes found for operation {operation}")
                continue
            
            print(f"Processing operation: {operation}")
            print(f"Block sizes for {operation}: {op_block_sizes}")
            
            for block_size in op_block_sizes:
                data = all_machines_results[operation][block_size]
                
                if not data:
                    print(f"No data found for {operation}-{block_size}")
                    continue
                
                # Extract latency data for each machine
                machine_latency_data = []
                
                if data_type == 'iops':
                    # IOPS data: one entry per machine with pre-averaged latency
                    for item in data:
                        if isinstance(item, dict) and 'avg_latency_ms' in item and 'machine' in item:
                            machine_latency_data.append({
                                'machine': item['machine'],
                                'avg_latency_ms': item['avg_latency_ms']
                            })
                else:
                    # Bandwidth data: multiple entries per machine (one per job), need to aggregate
                    machine_latency_groups = {}
                    for item in data:
                        if isinstance(item, dict) and 'avg_latency_ms' in item and 'machine' in item:
                            machine = item['machine']
                            # Extract basename from machine path to match IOPS format
                            machine_basename = os.path.basename(machine)
                            latency = item['avg_latency_ms']
                            
                            if machine_basename not in machine_latency_groups:
                                machine_latency_groups[machine_basename] = []
                            machine_latency_groups[machine_basename].append(latency)
                    
                    # Calculate average latency per machine
                    for machine_basename, latencies in machine_latency_groups.items():
                        if latencies:
                            avg_latency = sum(latencies) / len(latencies)
                            machine_latency_data.append({
                                'machine': machine_basename,
                                'avg_latency_ms': avg_latency
                            })
                
                if not machine_latency_data:
                    print(f"No valid latency data found for {operation}-{block_size}")
                    continue
                
                # Sort by machine name for consistent output
                machine_latency_data.sort(key=lambda x: x['machine'])
                
                # Create nested directory structure: latencydata/operation/block_size.txt
                operation_dir = os.path.join(latency_dir, operation)
                os.makedirs(operation_dir, exist_ok=True)
                
                # Create filename
                data_type_suffix = 'bw' if data_type == 'bandwidth' else 'iops'
                filename = f"{block_size}_{data_type_suffix}.txt"
                filepath = os.path.join(operation_dir, filename)
                
                # Write latency data to file
                try:
                    with open(filepath, 'w') as f:
                        f.write(f"Latency Data for {operation.upper()} - Block Size: {block_size.upper()}\n")
                        f.write(f"Data Type: {data_type.upper()}\n")
                        f.write(f"Number of machines: {len(machine_latency_data)}\n")
                        f.write("=" * 60 + "\n\n")
                        
                        for item in machine_latency_data:
                            f.write(f"Machine: {item['machine']}\n")
                            f.write(f"Average Latency: {item['avg_latency_ms']:.3f} ms\n")
                            f.write("-" * 40 + "\n")
                    
                    print(f"Saved latency data to: {filepath}")
                    success_count += 1
                    
                except Exception as e:
                    print(f"Error writing file {filepath}: {e}")
        
        print(f"Latency data extraction complete. Created {success_count} files.")
        return success_count
        
    except Exception as e:
        print(f"Error in save_latency_data_to_files: {e}")
        return 0


def create_operation_summary_file(df_sorted, operation, block_sizes, data_type, output_dir):
    """
    Create a text file with summary values for an operation.
    
    Args:
        df_sorted: DataFrame with sorted data
        operation: Operation name (read, write, randread, randwrite)
        block_sizes: List of block sizes
        data_type: 'iops' or 'bandwidth'
        output_dir: Output directory path
    """
    try:
        # Get number of machines from DataFrame
        num_machines = len(df_sorted)
        
        # Create filename with number of machines prefix
        data_type_suffix = 'bw' if data_type == 'bandwidth' else 'iops'
        txt_filename = f"{num_machines}machines_{operation}_{data_type_suffix}.txt"
        txt_filepath = os.path.join(output_dir, txt_filename)
        
        # Open file for writing
        with open(txt_filepath, 'w') as f:
            f.write(f"Summary for {operation.upper()} operation ({data_type.upper()})\n")
            f.write("=" * 50 + "\n")
            f.write(f"Tested: {num_machines} machines in test\n\n")
            
            # Write values for each block size
            for block_size in block_sizes:
                block_data = df_sorted[block_size]
                block_average = block_data.mean()
                block_total = block_data.sum()
                display_name = get_block_size_display_name(block_size)
                
                f.write(f"Block size: {display_name}\n")
                
                # Format values based on data type
                if data_type == 'iops':
                    f.write(f"  Average: {block_average:.1f} IOPS\n")
                    f.write(f"  Total per Machine: {block_average:.1f} IOPS\n")
                    f.write(f"  Total All Machines: {block_total:.0f} IOPS\n")
                else:
                    f.write(f"  Average: {block_average:.1f} {BW_RATE_UNIT}\n")
                    f.write(f"  Total per Machine: {block_average:.1f} {BW_RATE_UNIT}\n")
                    f.write(f"  Total All Machines: {block_total:.0f} {BW_RATE_UNIT}\n")
                
                f.write("\n")  # Empty line between block sizes
        
        print(f"Created summary file: {txt_filename}")
        
    except Exception as e:
        print(f"Error creating summary file for {operation}: {e}")


# ---------------------------------------------------------------------------
# perf-report.md style summary tables (throughput + latency)
# ---------------------------------------------------------------------------

REPORT_OPERATION_ORDER = ('read', 'write', 'randread', 'randwrite', 'randrw', 'rw', 'readwrite')


def _block_size_sort_key(block_size):
    """Sort block sizes like 4k, 8k, 128k, 1024k numerically."""
    try:
        return int(str(block_size).lower().replace('k', '').replace('b', ''))
    except ValueError:
        return str(block_size)


def _kb_s_to_mb_s(kb_per_s):
    """Deprecated alias — bandwidth is already converted at extraction."""
    return float(kb_per_s)


def _ns_to_us(ns_value):
    """Convert nanoseconds to microseconds."""
    return float(ns_value) / 1000.0


def _pick_percentile(percentile_map, targets):
    """Return first matching percentile value from FIO clat percentile dict."""
    if not percentile_map:
        return None
    # Exact / float-key tolerant lookup
    for target in targets:
        for key, value in percentile_map.items():
            try:
                if abs(float(key) - float(target)) < 0.01:
                    return value
            except (TypeError, ValueError):
                continue
    return None


def extract_latency_percentiles_from_json(json_file_path):
    """
    Extract completion-latency percentiles from one FIO JSON file.

    Returns:
        dict[(operation, block_size)] = {
            'p50_us', 'p95_us', 'p99_us', 'max_us', 'mean_us'
        }
    """
    results = {}
    try:
        with open(json_file_path, 'r') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Warning: could not read {json_file_path}: {e}")
        return results

    filename = os.path.basename(json_file_path)
    operation, block_size = parse_filename_info(filename)
    if not operation or not block_size or block_size == 'unknown':
        return results

    jobs = data.get('jobs') or []
    if not jobs:
        return results

    p50_vals, p95_vals, p99_vals, max_vals, mean_vals = [], [], [], [], []

    for job in jobs:
        # Prefer the direction matching the workload; fall back to whichever exists
        candidates = []
        if operation in ('read', 'randread'):
            candidates = ['read', 'write']
        elif operation in ('write', 'randwrite'):
            candidates = ['write', 'read']
        else:
            candidates = ['read', 'write']

        for direction in candidates:
            section = job.get(direction) or {}
            clat = section.get('clat_ns') or section.get('lat_ns') or {}
            if not clat:
                continue
            percentiles = clat.get('percentile') or {}
            p50 = _pick_percentile(percentiles, (50, 50.0, '50.000000'))
            p95 = _pick_percentile(percentiles, (95, 95.0, '95.000000'))
            p99 = _pick_percentile(percentiles, (99, 99.0, '99.000000'))
            if p50 is not None:
                p50_vals.append(_ns_to_us(p50))
            if p95 is not None:
                p95_vals.append(_ns_to_us(p95))
            if p99 is not None:
                p99_vals.append(_ns_to_us(p99))
            if 'max' in clat:
                max_vals.append(_ns_to_us(clat['max']))
            if 'mean' in clat:
                mean_vals.append(_ns_to_us(clat['mean']))
            break  # one direction per job

    if not (p50_vals or p95_vals or p99_vals or max_vals or mean_vals):
        return results

    def _avg(values):
        return sum(values) / len(values) if values else None

    results[(operation, block_size)] = {
        'p50_us': _avg(p50_vals),
        'p95_us': _avg(p95_vals),
        'p99_us': _avg(p99_vals),
        'max_us': _avg(max_vals),
        'mean_us': _avg(mean_vals),
    }
    return results


def analyze_latency_percentiles(input_dir='.'):
    """
    Collect per-VM latency percentiles from all FIO JSON files under input_dir.

    Returns:
        dict[operation][block_size] = list of per-VM percentile dicts
    """
    all_results = defaultdict(lambda: defaultdict(list))
    test_dirs = []
    for item in os.listdir(input_dir):
        item_path = os.path.join(input_dir, item)
        if os.path.isdir(item_path) and glob.glob(os.path.join(item_path, "*.json")):
            test_dirs.append(item_path)

    if not test_dirs:
        print(f"No directories with JSON files found in {input_dir} for latency percentiles")
        return {}

    print(f"Collecting latency percentiles from {len(test_dirs)} machine directories...")
    for test_dir in test_dirs:
        vm_name = os.path.basename(test_dir)
        for json_file in glob.glob(os.path.join(test_dir, "*.json")):
            if os.path.basename(json_file) == 'write_dataset.json':
                continue
            for (operation, block_size), lat in extract_latency_percentiles_from_json(json_file).items():
                entry = dict(lat)
                entry['machine'] = vm_name
                all_results[operation][block_size].append(entry)
    return all_results


def _pct_at_iodepth_dist(dist, target):
    """Return percentage at a given iodepth bucket from FIO iodepth_level/complete dict."""
    if not dist:
        return None
    key = str(target)
    if key in dist:
        return float(dist[key])
    if target >= 64 and '>=64' in dist:
        return float(dist['>=64'])
    return 0.0


def _mean_or_none(values):
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def extract_latency_bs_metrics_from_json(json_file_path, operation, iops_field=None):
    """
    Extract per-VM latency detail metrics for --latency-bs tables.

    Returns dict with iops, clat percentiles (us), and iodepth histogram stats.
    """
    iops_field = iops_field or IOPS_JSON_FIELD
    try:
        with open(json_file_path, 'r') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Warning: could not read {json_file_path}: {e}")
        return None

    jobs = data.get('jobs') or []
    if not jobs:
        return None

    iops_vals, p50_vals, p99_vals = [], [], []
    idl_target_vals, idl_8_vals, idc_4_vals = [], [], []
    iodepth_cfg = 16

    for job in jobs:
        opts = job.get('job options') or {}
        try:
            iodepth_cfg = int(opts.get('iodepth', iodepth_cfg))
        except (TypeError, ValueError):
            pass

        if operation in ('read', 'randread'):
            directions = ['read']
        elif operation in ('write', 'randwrite'):
            directions = ['write']
        else:
            directions = ['read', 'write']

        for direction in directions:
            section = job.get(direction) or {}
            if not section:
                continue

            raw_iops = section.get(iops_field)
            if raw_iops is None:
                raw_iops = section.get('iops')
            if raw_iops is not None:
                try:
                    iops_vals.append(float(raw_iops))
                except (TypeError, ValueError):
                    pass

            clat = section.get('clat_ns') or section.get('lat_ns') or {}
            percentiles = clat.get('percentile') or {}
            p50 = _pick_percentile(percentiles, (50, 50.0, '50.000000'))
            p99 = _pick_percentile(percentiles, (99, 99.0, '99.000000'))
            if p50 is not None:
                p50_vals.append(_ns_to_us(p50))
            if p99 is not None:
                p99_vals.append(_ns_to_us(p99))

            idl = job.get('iodepth_level') or section.get('iodepth_level') or {}
            idc = job.get('iodepth_complete') or section.get('iodepth_complete') or {}
            idl_target = _pct_at_iodepth_dist(idl, iodepth_cfg)
            idl_8 = _pct_at_iodepth_dist(idl, 8)
            idc_4 = _pct_at_iodepth_dist(idc, 4)
            if idl_target is not None:
                idl_target_vals.append(idl_target)
            if idl_8 is not None:
                idl_8_vals.append(idl_8)
            if idc_4 is not None:
                idc_4_vals.append(idc_4)

            if operation in ('read', 'randread', 'write', 'randwrite'):
                break

    if not (iops_vals or p50_vals or p99_vals):
        return None

    return {
        'iops': _mean_or_none(iops_vals),
        'p50_us': _mean_or_none(p50_vals),
        'p99_us': _mean_or_none(p99_vals),
        'iodepth_cfg': iodepth_cfg,
        'idl_at_target_pct': _mean_or_none(idl_target_vals),
        'idl_at_8_pct': _mean_or_none(idl_8_vals),
        'idc_at_4_pct': _mean_or_none(idc_4_vals),
    }


def analyze_latency_bs_for_tier(input_dir, block_sizes, operations=None):
    """
    Collect per-VM latency-bs metrics for selected block sizes under one tier directory.

    Returns:
        dict[(operation, block_size)] = list of per-VM metric dicts
    """
    block_set = {str(bs).lower() for bs in block_sizes}
    op_set = {str(op).lower() for op in operations} if operations else None
    results = defaultdict(list)
    test_dirs = []
    for item in os.listdir(input_dir):
        item_path = os.path.join(input_dir, item)
        if os.path.isdir(item_path) and glob.glob(os.path.join(item_path, '*.json')):
            test_dirs.append(item_path)

    if not test_dirs:
        print(f"No VM directories with JSON found in {input_dir} for --latency-bs")
        return results

    for test_dir in test_dirs:
        for json_file in glob.glob(os.path.join(test_dir, '*.json')):
            base = os.path.basename(json_file)
            if base == 'write_dataset.json':
                continue
            operation, block_size = parse_filename_info(base)
            if not operation or not block_size:
                continue
            if op_set is not None and operation.lower() not in op_set:
                continue
            if block_size.lower() not in block_set:
                continue
            metrics = extract_latency_bs_metrics_from_json(json_file, operation)
            if metrics:
                results[(operation, block_size.lower())].append(metrics)

    return results


def _aggregate_latency_bs_vm_list(vm_metrics, iodepth_cfg=16):
    """Mean across VMs for one tier."""
    if not vm_metrics:
        return None
    cfg = iodepth_cfg
    if vm_metrics[0].get('iodepth_cfg'):
        cfg = vm_metrics[0]['iodepth_cfg']
    return {
        'iops': _mean_or_none([m.get('iops') for m in vm_metrics]),
        'p50_us': _mean_or_none([m.get('p50_us') for m in vm_metrics]),
        'p99_us': _mean_or_none([m.get('p99_us') for m in vm_metrics]),
        'iodepth_cfg': cfg,
        'idl_at_target_pct': _mean_or_none([m.get('idl_at_target_pct') for m in vm_metrics]),
        'idl_at_8_pct': _mean_or_none([m.get('idl_at_8_pct') for m in vm_metrics]),
        'idc_at_4_pct': _mean_or_none([m.get('idc_at_4_pct') for m in vm_metrics]),
        'vms': len(vm_metrics),
    }


def _format_latency_bs_iops(value):
    if value is None:
        return 'N/A'
    return f'{int(round(value)):,}'


def _format_latency_bs_us(value):
    if value is None:
        return 'N/A'
    if value >= 1000:
        return f'{value:,.0f} µs'
    return f'{value:.0f} µs'


def _format_latency_bs_pct(value, approximate=False):
    if value is None:
        return 'N/A'
    prefix = '~' if approximate else ''
    return f'{prefix}{value:.1f}%'


def _latency_bs_ratios_vs_baseline(tier_stats, run_labels, baseline_label, value_key, format_kind):
    """Build comma-separated ratio text for non-baseline tiers."""
    base_stats = tier_stats.get(baseline_label)
    if not base_stats:
        return 'N/A'
    base_val = base_stats.get(value_key)
    if base_val is None or base_val == 0:
        return 'N/A'

    parts = []
    for label in run_labels:
        if label == baseline_label:
            continue
        stats = tier_stats.get(label)
        if not stats:
            continue
        val = stats.get(value_key)
        if val is None:
            continue
        ratio = val / base_val
        if format_kind == 'iops':
            parts.append(f'{label} = {ratio:.2f}× {baseline_label}')
        elif format_kind == 'p50':
            parts.append(f'{label} = {ratio:.2f}× slower')
        elif format_kind == 'p99':
            parts.append(f'{label} = {ratio:.2f}× p99')
    return ', '.join(parts) if parts else ''


def build_latency_bs_table_rows(tier_stats, run_labels, iodepth_cfg=16, baseline_label=None):
    """
    Build transposed table rows for one operation × block size.

    Columns: Metric | <tier labels...>
    Optional Ratio column when exactly two tiers (no baseline), or vs-baseline column
    when --latency-bs-baseline is set.
    """
    fieldnames = ['Metric'] + list(run_labels)
    rows = []

    def _tier_cell(label, formatter):
        stats = tier_stats.get(label)
        if not stats:
            return 'N/A'
        return formatter(stats)

    rows.append({
        'Metric': 'IOPS',
        **{label: _tier_cell(label, lambda s: _format_latency_bs_iops(s.get('iops')))
           for label in run_labels},
    })
    rows.append({
        'Metric': 'clat p50',
        **{label: _tier_cell(label, lambda s: _format_latency_bs_us(s.get('p50_us')))
           for label in run_labels},
    })
    rows.append({
        'Metric': 'clat p99',
        **{label: _tier_cell(label, lambda s: _format_latency_bs_us(s.get('p99_us')))
           for label in run_labels},
    })

    idl_label = f'`iodepth_level` @ {iodepth_cfg}'
    rows.append({
        'Metric': idl_label,
        **{label: _tier_cell(label, lambda s: _format_latency_bs_pct(s.get('idl_at_target_pct')))
           for label in run_labels},
    })
    rows.append({
        'Metric': '`iodepth_level` @ 8',
        **{label: _tier_cell(label, lambda s: _format_latency_bs_pct(s.get('idl_at_8_pct')))
           for label in run_labels},
    })
    rows.append({
        'Metric': '`iodepth_complete` @ 4',
        **{label: _tier_cell(label, lambda s: _format_latency_bs_pct(s.get('idc_at_4_pct'), approximate=True))
           for label in run_labels},
    })

    if baseline_label:
        vs_col = f'vs {baseline_label}'
        fieldnames.append(vs_col)
        for row in rows:
            if row['Metric'] == 'IOPS':
                row[vs_col] = _latency_bs_ratios_vs_baseline(
                    tier_stats, run_labels, baseline_label, 'iops', 'iops')
            elif row['Metric'].startswith('clat p50'):
                row[vs_col] = _latency_bs_ratios_vs_baseline(
                    tier_stats, run_labels, baseline_label, 'p50_us', 'p50')
            elif row['Metric'].startswith('clat p99'):
                row[vs_col] = _latency_bs_ratios_vs_baseline(
                    tier_stats, run_labels, baseline_label, 'p99_us', 'p99')
            else:
                row[vs_col] = ''
    elif len(run_labels) == 2:
        left, right = run_labels
        fieldnames.append('Ratio')
        for row in rows:
            if row['Metric'] == 'IOPS':
                lv = tier_stats.get(left, {}).get('iops')
                rv = tier_stats.get(right, {}).get('iops')
                if lv and rv:
                    row['Ratio'] = f'{right} = {rv / lv:.2f}× {left}'
                else:
                    row['Ratio'] = 'N/A'
            elif row['Metric'].startswith('clat p50'):
                lv = tier_stats.get(left, {}).get('p50_us')
                rv = tier_stats.get(right, {}).get('p50_us')
                if lv and rv and lv > 0:
                    row['Ratio'] = f'{right} = {rv / lv:.2f}× slower'
                else:
                    row['Ratio'] = 'N/A'
            elif row['Metric'].startswith('clat p99'):
                lv = tier_stats.get(left, {}).get('p99_us')
                rv = tier_stats.get(right, {}).get('p99_us')
                if lv and rv and lv > 0:
                    row['Ratio'] = f'{right} = {rv / lv:.2f}× p99'
                else:
                    row['Ratio'] = 'N/A'
            elif 'iodepth_level' in row['Metric'] or 'iodepth_complete' in row['Metric']:
                row['Ratio'] = ''
            else:
                row['Ratio'] = ''

    return fieldnames, rows


def create_latency_bs_tables(input_dirs, run_labels, output_dir, block_sizes,
                             operations=None, baseline_label=None):
    """
    Build per op×block-size latency detail tables across tiers (--latency-bs).

    Outputs under {output_dir}/latency-bs/:
      {operation}_{bs}_latency_detail.png
      csv_files/latency_bs_{operation}_{bs}.csv
      latency_bs_summary.md
    """
    if len(input_dirs) < 2:
        print('Error: --latency-bs requires two or more --input-dir entries (cross-tier mode)')
        sys.exit(1)

    print('\n' + '=' * 60)
    print('LATENCY-BS DETAIL TABLES')
    print('=' * 60)
    if operations:
        print(f"Operations filter: {', '.join(operations)}")
    if baseline_label:
        print(f"Baseline for ratios: {baseline_label}")

    op_set = {op.lower() for op in operations} if operations else None

    tier_aggregates = defaultdict(dict)
    for input_dir, run_label in zip(input_dirs, run_labels):
        print(f"Collecting latency-bs metrics for tier '{run_label}'...")
        per_vm = analyze_latency_bs_for_tier(input_dir, block_sizes, operations=operations)
        for key, vm_list in per_vm.items():
            agg = _aggregate_latency_bs_vm_list(vm_list)
            if agg:
                tier_aggregates[key][run_label] = agg
            else:
                print(f"  Warning: no latency-bs data for {key} in {run_label}")

    if not tier_aggregates:
        if operations:
            print('No latency-bs data found for the requested block sizes and operations.')
        else:
            print('No latency-bs data found for the requested block sizes.')
        return {}

    out_dir = os.path.join(output_dir, 'latency-bs')
    csv_dir = ensure_csv_directory(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    operations_found = sorted(
        {op for op, _ in tier_aggregates.keys()},
        key=lambda op: (
            REPORT_OPERATION_ORDER.index(op)
            if op in REPORT_OPERATION_ORDER else len(REPORT_OPERATION_ORDER),
            op,
        ),
    )
    operations = operations_found
    if op_set is not None:
        missing_ops = op_set - {op.lower() for op in operations_found}
        for op in sorted(missing_ops):
            print(f"  Warning: no latency-bs data for operation '{op}' in any tier")

    block_sizes_found = sorted(
        {bs for _, bs in tier_aggregates.keys()},
        key=_block_size_sort_key,
    )

    md_lines = [
        '### Latency detail tables (`--latency-bs`)',
        '',
        f'Block sizes: {", ".join(block_sizes_found)}',
    ]
    if operations:
        md_lines.append(f'Operations: {", ".join(operations)}')
    if baseline_label:
        md_lines.append(f'Baseline: {baseline_label} (`vs {baseline_label}` ratio column)')
    md_lines.extend(['', ''])
    created = 0

    for operation in operations:
        for block_size in block_sizes_found:
            key = (operation, block_size)
            tier_stats = tier_aggregates.get(key)
            if not tier_stats:
                continue

            iodepth_cfg = 16
            for stats in tier_stats.values():
                if stats.get('iodepth_cfg'):
                    iodepth_cfg = stats['iodepth_cfg']
                    break

            fieldnames, rows = build_latency_bs_table_rows(
                tier_stats, run_labels, iodepth_cfg, baseline_label=baseline_label)
            bs_display = get_block_size_display_name(block_size)
            op_display = CROSS_TIER_OP_DISPLAY.get(operation, operation)
            title = f'{bs_display} {op_display} — Latency Detail (mean across VMs)'
            slug = f'{operation}_{block_size.lower()}'
            png_path = os.path.join(out_dir, f'{slug}_latency_detail.png')
            csv_path = os.path.join(csv_dir, f'latency_bs_{slug}.csv')

            render_perf_report_table_png(rows, fieldnames, title, png_path)
            save_perf_report_table_csv(rows, csv_path, fieldnames)
            created += 1

            md_lines.append(f'#### {title}')
            md_lines.append('')
            md_lines.append(f'- PNG: `latency-bs/{slug}_latency_detail.png`')
            md_lines.append(f'- CSV: `latency-bs/csv_files/latency_bs_{slug}.csv`')
            md_lines.append('')

    md_path = os.path.join(out_dir, 'latency_bs_summary.md')
    with open(md_path, 'w') as f:
        f.write('\n'.join(md_lines))
    print(f'Saved latency-bs summary: {md_path}')
    print(f'Created {created} latency-bs table(s) under {out_dir}')
    return tier_aggregates


def _average_field(items, field):
    values = [item[field] for item in items if item.get(field) is not None]
    return sum(values) / len(values) if values else None


def build_throughput_report_rows(iops_all_machines, bw_all_machines, selected_block_sizes=None):
    """
    Build rows matching perf-report.md Aggregate Throughput table.

    Columns: Operation, Block Size, Total IOPS, Total BW (MB/s), Avg IOPS/VM, Avg BW/VM (MB/s)
    """
    ops = set()
    if iops_all_machines:
        ops.update(iops_all_machines.keys())
    if bw_all_machines:
        ops.update(bw_all_machines.keys())

    ordered_ops = [op for op in REPORT_OPERATION_ORDER if op in ops]
    ordered_ops.extend(sorted(op for op in ops if op not in REPORT_OPERATION_ORDER))

    rows = []
    for operation in ordered_ops:
        block_sizes = set()
        if iops_all_machines and operation in iops_all_machines:
            block_sizes.update(iops_all_machines[operation].keys())
        if bw_all_machines and operation in bw_all_machines:
            block_sizes.update(bw_all_machines[operation].keys())
        if selected_block_sizes:
            block_sizes = {bs for bs in block_sizes if bs in selected_block_sizes}

        for block_size in sorted(block_sizes, key=_block_size_sort_key):
            iops_totals = compute_per_machine_totals(
                iops_all_machines or {}, operation, block_size, 'iops'
            )
            bw_totals_kb = compute_per_machine_totals(
                bw_all_machines or {}, operation, block_size, 'bandwidth'
            )
            total_iops = sum(iops_totals) if iops_totals else None
            avg_iops = (sum(iops_totals) / len(iops_totals)) if iops_totals else None
            total_bw_mb = sum(bw_totals_kb) if bw_totals_kb else None
            avg_bw_mb = (sum(bw_totals_kb) / len(bw_totals_kb)) if bw_totals_kb else None
            rows.append({
                'Operation': operation,
                'Block Size': block_size,
                'Total IOPS': total_iops,
                'Total BW (MB/s)': total_bw_mb,
                'Avg IOPS/VM': avg_iops,
                'Avg BW/VM (MB/s)': avg_bw_mb,
                'VMs': max(len(iops_totals), len(bw_totals_kb)) if (iops_totals or bw_totals_kb) else 0,
            })
    return rows


def build_latency_report_rows(latency_all_machines, selected_block_sizes=None,
                              focus_only_4k_random=False):
    """
    Build rows matching perf-report.md Latency table.

    Columns: Operation, Block Size, p50 (us), p95 (us), p99 (us), Max (us)
    Values are averages across VMs.
    """
    ops = list(latency_all_machines.keys()) if latency_all_machines else []
    ordered_ops = [op for op in REPORT_OPERATION_ORDER if op in ops]
    ordered_ops.extend(sorted(op for op in ops if op not in REPORT_OPERATION_ORDER))

    rows = []
    for operation in ordered_ops:
        block_sizes = set(latency_all_machines.get(operation, {}).keys())
        if selected_block_sizes:
            block_sizes = {bs for bs in block_sizes if bs in selected_block_sizes}
        if focus_only_4k_random:
            if operation not in ('randread', 'randwrite'):
                continue
            block_sizes = {bs for bs in block_sizes if str(bs).lower() in ('4k', '4kb')}

        for block_size in sorted(block_sizes, key=_block_size_sort_key):
            items = latency_all_machines[operation][block_size]
            rows.append({
                'Operation': operation,
                'Block Size': block_size,
                'p50 (us)': _average_field(items, 'p50_us'),
                'p95 (us)': _average_field(items, 'p95_us'),
                'p99 (us)': _average_field(items, 'p99_us'),
                'Max (us)': _average_field(items, 'max_us'),
                'Mean (us)': _average_field(items, 'mean_us'),
                'VMs': len(items),
            })
    return rows


def _format_report_number(value, digits=1):
    if value is None or value == '':
        return 'TBD'
    if isinstance(value, str):
        try:
            value = float(value) if ('.' in value or 'e' in value.lower()) else int(value)
        except ValueError:
            return value
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return f'{value:,}'
    if isinstance(value, float):
        if digits == 0:
            return f'{value:,.0f}'
        if abs(value) >= 1000:
            return f'{value:,.{digits}f}'
        return f'{value:.{digits}f}'
    return str(value)


def save_perf_report_table_csv(rows, csv_path, fieldnames):
    """Write report table rows to CSV (import-friendly)."""
    os.makedirs(os.path.dirname(csv_path) or '.', exist_ok=True)
    with open(csv_path, 'w', newline='') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            out = {}
            for key in fieldnames:
                val = row.get(key)
                out[key] = '' if val is None else val
            writer.writerow(out)
    print(f"Saved perf-report CSV: {csv_path}")


def save_perf_report_table_markdown(rows, md_path, fieldnames, title):
    """Write a markdown table matching perf-report.md style."""
    os.makedirs(os.path.dirname(md_path) or '.', exist_ok=True)
    lines = [f'#### {title}', '', '| ' + ' | '.join(fieldnames) + ' |',
             '|' + '|'.join(['-----------'] * len(fieldnames)) + '|']
    for row in rows:
        cells = []
        for key in fieldnames:
            val = row.get(key)
            if key in ('Operation', 'Block Size', 'Tier'):
                cells.append(str(val) if val is not None else '')
            elif key == 'VMs':
                cells.append(str(int(val)) if val is not None else '0')
            elif 'IOPS' in key:
                cells.append(_format_report_number(val, 0 if key.startswith('Total') else 1))
            else:
                cells.append(_format_report_number(val, 1))
        lines.append('| ' + ' | '.join(cells) + ' |')
    lines.append('')
    with open(md_path, 'w') as f:
        f.write('\n'.join(lines))
    print(f"Saved perf-report markdown: {md_path}")


def _load_table_fonts(size=18, title_size=24, title_bold=False):
    """Load TTF fonts for pixel-accurate table PNGs; fall back to default bitmap fonts.

    Title uses regular weight by default so long titles stay readable when smaller.
    """
    try:
        from PIL import ImageFont
    except ImportError:
        return None, None, None

    regular_paths = [
        '/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/usr/share/fonts/gnu-free/FreeSans.ttf',
        '/usr/share/fonts/liberation-sans/LiberationSans-Regular.ttf',
    ]
    bold_paths = [
        '/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
        '/usr/share/fonts/gnu-free/FreeSansBold.ttf',
        '/usr/share/fonts/liberation-sans/LiberationSans-Bold.ttf',
    ]

    def _first_font(paths, fallback_size):
        for path in paths:
            if os.path.exists(path):
                try:
                    return ImageFont.truetype(path, fallback_size)
                except OSError:
                    continue
        return ImageFont.load_default()

    title_paths = bold_paths if title_bold else regular_paths
    return (
        _first_font(regular_paths, size),
        _first_font(bold_paths, size),
        _first_font(title_paths, title_size),
    )


def render_perf_report_table_png(rows, fieldnames, title, png_path):
    """Render the report table as a tightly cropped PNG (PIL; minimal whitespace)."""
    try:
        from PIL import Image, ImageDraw
    except ImportError as e:
        print(f"Skipping PNG table (Pillow required): {e}")
        return

    if not rows:
        print(f"No rows to render for {title}")
        return

    display_rows = []
    for row in rows:
        cells = []
        for key in fieldnames:
            val = row.get(key)
            if key in ('Operation', 'Block Size', 'VMs', 'Tier', 'Metric', 'Ratio') or key.startswith('vs '):
                cells.append(str(val) if val is not None else '')
            elif isinstance(val, str):
                cells.append(val)
            else:
                cells.append(_format_report_number(
                    val,
                    0 if (key.startswith('Total IOPS') or key == 'VMs') else 1
                ))
        display_rows.append(cells)

    # Body text stays readable; title is regular (not bold) and smaller so long
    # names like "1024K Sequential Write — Aggregate Bandwidth" fit.
    font, font_bold, font_title = _load_table_fonts(
        size=28, title_size=20, title_bold=False
    )
    pad_x = 16
    pad_y = 5
    outer_pad = 6
    title_gap = 4
    grid = '#b0b0b0'
    header_bg = (47, 84, 150)
    alt_bg = (242, 242, 242)
    white = (255, 255, 255)
    black = (0, 0, 0)

    def _text_size(text, fnt):
        bbox = fnt.getbbox(str(text))
        return bbox[2] - bbox[0], bbox[3] - bbox[1]

    def _text_offset_y(text, fnt, row_height):
        _, h = _text_size(text, fnt)
        return max(0, (row_height - h) // 2)

    # Measure columns from header + body
    col_widths = []
    for col_idx, key in enumerate(fieldnames):
        header_w, _ = _text_size(key, font_bold)
        max_w = header_w
        for drow in display_rows:
            cell_w, _ = _text_size(drow[col_idx], font)
            max_w = max(max_w, cell_w)
        col_widths.append(max_w + 2 * pad_x)

    _, sample_h = _text_size('Ag', font)
    row_h = sample_h + 2 * pad_y
    table_w = sum(col_widths)
    table_h = row_h * (1 + len(display_rows))

    # Shrink title font until it fits within the table width (min size 12)
    title_w, title_h = _text_size(title, font_title)
    if title_w > table_w:
        for size in range(19, 11, -1):
            candidate = _load_table_fonts(size=28, title_size=size, title_bold=False)[2]
            tw, th = _text_size(title, candidate)
            font_title = candidate
            title_w, title_h = tw, th
            if tw <= table_w:
                break

    img_w = max(table_w, title_w) + 2 * outer_pad
    img_h = title_h + title_gap + table_h + 2 * outer_pad
    img = Image.new('RGB', (img_w, img_h), white)
    draw = ImageDraw.Draw(img)

    draw.text(((img_w - title_w) // 2, outer_pad), title, fill=black, font=font_title)

    y = outer_pad + title_h + title_gap
    # Header
    x = outer_pad
    for col_idx, header in enumerate(fieldnames):
        w = col_widths[col_idx]
        draw.rectangle([x, y, x + w, y + row_h], fill=header_bg, outline=grid)
        tw, _ = _text_size(header, font_bold)
        draw.text(
            (x + (w - tw) // 2, y + _text_offset_y(header, font_bold, row_h)),
            header,
            fill=white,
            font=font_bold,
        )
        x += w
    y += row_h

    for row_idx, row in enumerate(display_rows):
        bg = alt_bg if row_idx % 2 == 1 else white
        x = outer_pad
        for col_idx, cell in enumerate(row):
            w = col_widths[col_idx]
            draw.rectangle([x, y, x + w, y + row_h], fill=bg, outline=grid)
            tw, _ = _text_size(cell, font)
            draw.text(
                (x + (w - tw) // 2, y + _text_offset_y(cell, font, row_h)),
                cell,
                fill=black,
                font=font,
            )
            x += w
        y += row_h

    os.makedirs(os.path.dirname(png_path) or '.', exist_ok=True)
    img.save(png_path, 'PNG')
    print(f"Saved perf-report PNG: {png_path}")


def render_merged_cross_tier_operation_table_pngs(panels, output_dir='.'):
    """
    Merge per-block-size cross-tier table panels into one PNG (and CSV) per
    operation × metric.

    Example outputs:
      perf_report_cross_tier_write_bw_table.png
      perf_report_cross_tier_randread_iops_table.png

    Each image includes a Block Size column plus the usual Tier/VMs/Total/Avg
    columns for every tested block size of that operation.
    """
    if not panels:
        return []

    groups = defaultdict(list)
    for panel in panels:
        workload = panel.get('workload') or {}
        operation = workload.get('operation')
        metric = workload.get('metric')
        if not operation or not metric:
            continue
        groups[(operation, metric)].append(panel)

    created = []
    csv_dir = ensure_csv_directory(output_dir)

    for operation, metric in sorted(
        groups.keys(),
        key=lambda t: (_cross_tier_op_sort_key(t[0]), 0 if t[1] == 'iops' else 1),
    ):
        group_panels = sorted(
            groups[(operation, metric)],
            key=lambda p: _block_size_sort_key(p['workload']['block_size']),
        )
        if not group_panels:
            continue

        base_fields = list(group_panels[0]['workload']['fields'])
        fields = ['Block Size'] + base_fields
        merged_rows = []
        for panel in group_panels:
            workload = panel['workload']
            bs_display = get_block_size_display_name(workload['block_size'])
            for row in panel.get('rows') or []:
                merged = {'Block Size': bs_display}
                for key in base_fields:
                    merged[key] = row.get(key)
                merged_rows.append(merged)

        if not merged_rows:
            continue

        op_display = CROSS_TIER_OP_DISPLAY.get(operation, operation)
        if metric == 'iops':
            metric_suffix = 'iops'
            metric_label = 'Aggregate IOPS'
        else:
            metric_suffix = 'bw'
            metric_label = 'Aggregate Bandwidth'

        bs_list = [
            get_block_size_display_name(p['workload']['block_size'])
            for p in group_panels
        ]
        title = f'{op_display} — {metric_label} (all block sizes: {", ".join(bs_list)})'
        out_name = f'perf_report_cross_tier_{operation}_{metric_suffix}_table.png'
        png_path = os.path.join(output_dir, out_name)
        csv_path = os.path.join(
            csv_dir, f'perf_report_cross_tier_{operation}_{metric_suffix}_table.csv'
        )

        save_perf_report_table_csv(merged_rows, csv_path, fields)
        render_perf_report_table_png(merged_rows, fields, title, png_path)
        created.append(png_path)
        print(
            f"Merged cross-tier table: {operation}/{metric_suffix} "
            f"({len(group_panels)} block size(s), {len(merged_rows)} row(s))"
        )

    return created


def _report_vm_count(rows):
    """Return the VM count used in a perf-report table (max VMs across rows)."""
    vm_counts = []
    for row in rows or []:
        vm = _to_float(row.get('VMs'))
        if vm is not None:
            vm_counts.append(int(vm))
    return max(vm_counts) if vm_counts else 0


def _to_float(value):
    """Parse a numeric report cell (float/int/comma-formatted string) to float."""
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(',', '')
    try:
        return float(text)
    except ValueError:
        return None


def load_perf_report_throughput_csv(csv_path):
    """Load throughput summary rows from CSV."""
    with open(csv_path, newline='') as csvfile:
        reader = csv.DictReader(csvfile)
        return list(reader), reader.fieldnames


def find_perf_report_latency_csv(*search_dirs):
    """Locate csv_files/perf_report_latency_summary.csv under candidate dirs."""
    candidates = []
    for base in search_dirs:
        if not base:
            continue
        candidates.extend([
            os.path.join(base, 'csv_files', 'perf_report_latency_summary.csv'),
            os.path.join(base, 'perf_report_latency_summary.csv'),
        ])
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def load_perf_report_latency_csv(csv_path):
    """Load latency summary rows from CSV."""
    with open(csv_path, newline='') as csvfile:
        reader = csv.DictReader(csvfile)
        return list(reader), reader.fieldnames


def _latency_rows_for_run(input_dir, run_label, selected_block_sizes=None, output_dir=None):
    """
    Load or compute latency summary rows for one run/tier.

    Preference order:
      1) output_dir/perf_report_{label}/csv_files/perf_report_latency_summary.csv
      2) input_dir/csv_files/perf_report_latency_summary.csv
      3) analyze FIO JSONs under input_dir
    """
    search = []
    if output_dir and run_label:
        safe = run_label.replace(' ', '_')
        search.append(os.path.join(output_dir, f'perf_report_{safe}'))
    search.append(input_dir)

    csv_path = find_perf_report_latency_csv(*search)
    if csv_path:
        print(f"Loading latency summary for '{run_label}': {csv_path}")
        rows, _ = load_perf_report_latency_csv(csv_path)
    else:
        print(f"Analyzing latency percentiles for '{run_label}': {input_dir}")
        latency_all = analyze_latency_percentiles(input_dir)
        rows = build_latency_report_rows(latency_all, selected_block_sizes)

    if selected_block_sizes:
        selected_norm = {_normalize_bs_key(bs) for bs in selected_block_sizes}
        rows = [
            r for r in rows
            if _normalize_bs_key(r.get('Block Size')) in selected_norm
        ]
    return rows


def collect_cross_tier_latency_panels(input_dirs, run_labels, selected_block_sizes=None,
                                      output_dir=None):
    """
    Build latency cross-tier panels from each run's latency summary.

    Returns:
      dict[operation][block_size] = list of tier rows:
        {Tier, VMs, p50 (us), p95 (us), p99 (us), Max (us)}
    """
    panels = defaultdict(lambda: defaultdict(list))

    for input_dir, run_label in zip(input_dirs, run_labels):
        rows = _latency_rows_for_run(
            input_dir, run_label, selected_block_sizes, output_dir=output_dir
        )
        # Infer VM count from first row if present
        default_vms = 0
        for row in rows:
            vms = _to_float(row.get('VMs'))
            if vms:
                default_vms = int(vms)
                break
        tier, _ = resolve_tier_name_and_vms(run_label, default_vms)

        for row in rows:
            operation = (row.get('Operation') or '').strip().lower()
            block_size = _normalize_bs_key(row.get('Block Size'))
            if not operation or not block_size:
                continue
            vms = int(_to_float(row.get('VMs')) or 0)
            tier_name, _ = resolve_tier_name_and_vms(run_label, vms)
            panels[operation][block_size].append({
                'Tier': tier_name or tier or run_label,
                'VMs': vms,
                'p50 (us)': _to_float(row.get('p50 (us)')),
                'p95 (us)': _to_float(row.get('p95 (us)')),
                'p99 (us)': _to_float(row.get('p99 (us)')),
                'Max (us)': _to_float(row.get('Max (us)')),
            })

    # Stable tier order matching run_labels
    label_order = {}
    for idx, run_label in enumerate(run_labels):
        tier_name, _ = resolve_tier_name_and_vms(run_label, 0)
        label_order[tier_name or run_label] = idx
        label_order[run_label] = idx

    for operation in panels:
        for block_size in panels[operation]:
            panels[operation][block_size].sort(
                key=lambda r: label_order.get(r.get('Tier'), 999)
            )

    return panels


LATENCY_CROSS_TIER_METRICS = (
    ('p50 (us)', 'p50'),
    ('p95 (us)', 'p95'),
    ('p99 (us)', 'p99'),
    ('Max (us)', 'max'),
)


def format_percent_change(pct):
    """Format a percentage delta for on-graph labels (e.g. +12.3%, -5.1%)."""
    if pct is None or pct != pct:
        return ''
    sign = '+' if pct > 0 else ''
    if abs(pct) >= 100:
        return f'{sign}{pct:.0f}%'
    if abs(pct) >= 10:
        return f'{sign}{pct:.1f}%'
    return f'{sign}{pct:.1f}%'


def _percent_change(new_value, baseline):
    """Return percent change of new_value vs baseline, or None if undefined."""
    try:
        baseline = float(baseline)
        new_value = float(new_value)
    except (TypeError, ValueError):
        return None
    if baseline == 0 or baseline != baseline or new_value != new_value:
        return None
    return ((new_value - baseline) / baseline) * 100.0


def annotate_percent_changes(ax, x_positions, values, fontsize=8, y_offset_frac=0.04,
                             lower_is_better=True, series_index=0, rotation=0,
                             return_texts=False):
    """
    Annotate % change vs first (baseline) value at each subsequent point.

    Color meaning depends on lower_is_better:
      - latency (lower_is_better=True):  green = decrease, red = increase
      - BW/IOPS (lower_is_better=False): green = increase, red = decrease

    Args:
        series_index: when multiple series share the same x-axis, stagger labels
                      vertically (0 = highest, 1 = lower, etc.)
        rotation: text rotation in degrees (e.g., 90 for vertical, 45 for diagonal)
        return_texts: if True, return list of Text artists instead of None
    """
    if not values or len(values) < 2:
        return [] if return_texts else None
    baseline = values[0]
    if not baseline:
        return [] if return_texts else None

    try:
        from matplotlib import patheffects as pe
        text_effects = [pe.withStroke(linewidth=3.0, foreground='white')]
    except Exception:
        text_effects = None

    ymax = max(values) if values else 0
    base_offset = ymax * y_offset_frac if ymax > 0 else 1.0
    # Stagger multiple series: series_index=0 highest, 1 lower, etc.
    stagger_step = ymax * 0.08 if ymax > 0 else 1.0
    series_offset = series_index * stagger_step

    # For rotated text, adjust alignment
    if rotation == 90:
        ha, va = 'center', 'bottom'
    elif rotation == -90:
        ha, va = 'center', 'top'
    elif rotation > 0:
        ha, va = 'left', 'bottom'
    elif rotation < 0:
        ha, va = 'right', 'bottom'
    else:
        ha, va = 'center', 'bottom'

    texts = []
    for x, value in zip(x_positions[1:], values[1:]):
        pct = _percent_change(value, baseline)
        if pct is None:
            continue
        if pct == 0:
            color = '#555555'
        elif lower_is_better:
            color = '#1a7f1a' if pct < 0 else '#c41e1e'
        else:
            color = '#1a7f1a' if pct > 0 else '#c41e1e'
        txt = ax.text(
            x,
            value + base_offset + series_offset,
            format_percent_change(pct),
            ha=ha,
            va=va,
            fontsize=fontsize,
            fontweight='bold',
            color=color,
            clip_on=False,
            zorder=20,
            rotation=rotation,
        )
        if text_effects is not None:
            txt.set_path_effects(text_effects)
        texts.append(txt)

    if return_texts:
        return texts
    return None


def _plot_latency_cross_tier_on_axes(ax, tier_rows, value_key, graph_type='bar',
                                     show_values=False, title=None, show_legend=False,
                                     xlabel=True, series_label=None,
                                     show_percent_change=True, lower_is_better=True,
                                     compact_subplot=False):
    """Draw one metric vs tiers on an axes (latency/BW/IOPS single-series panels)."""
    import numpy as np

    labels = []
    values = []
    for row in tier_rows:
        tier = row.get('Tier') or ''
        vms = int(row.get('VMs') or 0)
        vm_label = f"({vms} VM)" if vms == 1 else f"({vms} VMs)"
        labels.append(f"{tier}\n{vm_label}" if vms else str(tier))
        values.append(_to_float(row.get(value_key)) or 0.0)

    if not any(values):
        ax.set_visible(False)
        return False

    x = np.arange(len(labels))
    colors = plt.cm.tab10(np.linspace(0, 1, max(len(labels), 1)))
    ylabel = series_label or value_key
    n_tiers = len(labels)
    headroom = _cross_tier_plot_headroom(
        n_tiers, show_values, show_percent_change, compact_subplot=compact_subplot
    )
    chart_max = max(values) * headroom if values else 1.0
    value_fontsize = graph_value_fontsize(n_tiers)
    if compact_subplot:
        value_fontsize = max(6, value_fontsize - 1)
    value_metric = _infer_cross_tier_metric(value_key=value_key)

    if graph_type == 'bar':
        bars = ax.bar(
            x, values, color=colors, alpha=0.85, edgecolor='black', linewidth=0.5,
            label=ylabel,
        )
        ax.set_ylim(0, chart_max)
        value_texts = []
        if show_values:
            value_texts = annotate_bar_values_smart(
                ax,
                bars,
                fontsize=value_fontsize,
                n_tiers=n_tiers,
                compact_subplot=compact_subplot,
                compact_numbers=False,
                chart_max=chart_max,
                metric=value_metric,
            )
    else:
        value_texts = []
        ax.plot(
            x, values, marker='o', linewidth=2.5, markersize=7,
            color='#1f77b4', label=ylabel,
        )
        if show_values:
            annotate_line_values(
                ax, x, values, fontsize=value_fontsize, color='#1f77b4',
                metric=value_metric, compact_subplot=compact_subplot,
            )

    pct_texts = []
    if show_percent_change and len(values) >= 2:
        y_offset_frac = 0.14 if show_values else 0.04
        if show_values and compact_subplot:
            y_offset_frac = 0.18
        pct_rotation = _cross_tier_percent_rotation(
            n_tiers, show_values, compact_subplot=compact_subplot
        )
        pct_texts = annotate_percent_changes(
            ax,
            list(x),
            values,
            fontsize=max(7, value_fontsize),
            y_offset_frac=y_offset_frac,
            lower_is_better=lower_is_better,
            rotation=pct_rotation,
            return_texts=True,
        )
        baseline_name = (tier_rows[0].get('Tier') or 'baseline').split('\n')[0]
        ax.text(
            0.98,
            0.98,
            f'% vs {baseline_name}',
            transform=ax.transAxes,
            ha='right',
            va='top',
            fontsize=7,
            color='#555555',
            style='italic',
        )

    if graph_type == 'bar' and show_values:
        fig = ax.figure
        all_texts = value_texts + pct_texts
        if all_texts:
            _resolve_text_collisions(ax, all_texts, fig, chart_max)
            _expand_ylim_for_texts(ax, all_texts, fig)

    ax.set_title(title or value_key, fontsize=11, fontweight='bold', pad=8)
    if xlabel:
        ax.set_xlabel('Tier / VMs', fontsize=9, fontweight='bold')
    ax.set_ylabel(ylabel, fontsize=9, fontweight='bold')
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=8)
    ax.tick_params(axis='y', labelsize=8)
    ax.grid(True, axis='y', linestyle='--', alpha=0.4)
    if graph_type != 'bar' and values:
        ax.set_ylim(0, chart_max)
    if show_legend:
        ax.legend(loc='upper left', fontsize=8)
    return True


def render_operation_latency_cross_tier_comparison_graph(operation, block_panels,
                                                        output_dir='.', graph_type='bar',
                                                        show_values=False):
    """
    Create one multipanel latency graph per operation.

    block_panels: dict[block_size] -> list of tier rows
    Output: latency_crosstier/{operation}_latency_cross_tier_comparison.png
            (caller typically passes output_dir=.../latency_crosstier)

    Layout:
      columns = block sizes
      rows    = p50 / p95 / p99 / Max
    """
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError as e:
        print(f"Skipping latency cross-tier graph (matplotlib required): {e}")
        return 0

    block_sizes = sorted(block_panels.keys(), key=_block_size_sort_key)
    if not block_sizes:
        return 0

    # Keep only metrics that have at least one non-zero value
    active_metrics = []
    for value_key, slug in LATENCY_CROSS_TIER_METRICS:
        has_data = any(
            (_to_float(row.get(value_key)) or 0) > 0
            for bs in block_sizes
            for row in block_panels.get(bs, [])
        )
        if has_data:
            active_metrics.append((value_key, slug))
    if not active_metrics:
        print(f"No latency values for operation '{operation}' — skipping multipanel graph")
        return 0

    graph_types = ['bar', 'line'] if graph_type == 'both' else [graph_type]
    op_display = CROSS_TIER_OP_DISPLAY.get(operation, operation)
    nrows = len(active_metrics)
    ncols = len(block_sizes)
    created = 0
    compact_subplot = show_values and (ncols * nrows > 4 or ncols >= 3)

    for current_type in graph_types:
        if show_values:
            fig_w = max(6.5 * ncols, 12)
            fig_h = max(4.5 * nrows, 6.5)
        else:
            fig_w = max(5.5 * ncols, 10)
            fig_h = max(3.8 * nrows, 6)
        fig, axes = plt.subplots(
            nrows=nrows,
            ncols=ncols,
            figsize=(fig_w, fig_h),
            squeeze=False,
        )
        fig.suptitle(
            f'{op_display} — Latency Cross-Tier Comparison\n'
            f'(labels: absolute latency; % change vs first tier — green=lower/better, red=higher)',
            fontsize=14,
            fontweight='bold',
            y=0.995,
        )

        for row_idx, (value_key, _slug) in enumerate(active_metrics):
            for col_idx, block_size in enumerate(block_sizes):
                ax = axes[row_idx][col_idx]
                tier_rows = block_panels.get(block_size) or []
                bs_display = get_block_size_display_name(block_size)
                subplot_title = f'{bs_display} — {value_key}'
                _plot_latency_cross_tier_on_axes(
                    ax,
                    tier_rows,
                    value_key,
                    graph_type=current_type,
                    show_values=show_values,
                    title=subplot_title,
                    show_legend=False,
                    xlabel=(row_idx == nrows - 1),
                    series_label=value_key,
                    compact_subplot=compact_subplot,
                )

        fig.tight_layout(rect=[0, 0, 1, 0.95])

        if graph_type == 'both' and current_type == 'line':
            out_name = f'{operation}_latency_cross_tier_comparison_line.png'
        else:
            out_name = f'{operation}_latency_cross_tier_comparison.png'

        out_path = os.path.join(output_dir, out_name)
        fig.savefig(out_path, dpi=150, bbox_inches='tight')
        plt.close(fig)
        print(f"Saved operation latency cross-tier graph: {out_path}")
        created += 1

    return created


def _throughput_cross_tier_metric_defs(metric_family):
    """Return (value_key, slug) rows for BW or IOPS multipanel graphs."""
    if metric_family == 'bandwidth':
        return (
            ('Total BW (MB/s)', 'total'),
            ('Avg BW/VM (MB/s)', 'avg'),
        )
    return (
        ('Total IOPS', 'total'),
        ('Avg IOPS/VM', 'avg'),
    )


def _parse_cross_tier_csv_slug(filename, metric_suffix):
    """
    Parse perf_report_cross_tier_{bs}_{operation}_{bw|iops}.csv -> (operation, block_size).
    """
    prefix = 'perf_report_cross_tier_'
    suffix = f'_{metric_suffix}.csv'
    if not (filename.startswith(prefix) and filename.endswith(suffix)):
        return None, None
    rest = filename[len(prefix):-len(suffix)]
    for candidate in ('randread', 'randwrite', 'readwrite', 'randrw', 'read', 'write', 'rw'):
        token = '_' + candidate
        if rest.endswith(token):
            return candidate, rest[: -len(token)]
    return None, None


def load_throughput_cross_tier_panels_from_csv(output_dir, metric_family='bandwidth'):
    """
    Rebuild operation -> block_size -> rows panels from csv_files/perf_report_cross_tier_*_{bw|iops}.csv.
    """
    csv_dir = ensure_csv_directory(output_dir)
    metric_suffix = 'bw' if metric_family == 'bandwidth' else 'iops'
    panels = defaultdict(dict)

    if not os.path.isdir(csv_dir):
        return panels

    for name in os.listdir(csv_dir):
        operation, block_size = _parse_cross_tier_csv_slug(name, metric_suffix)
        if not operation or not block_size:
            continue
        path = os.path.join(csv_dir, name)
        try:
            with open(path, newline='') as csvfile:
                rows = list(csv.DictReader(csvfile))
        except Exception as e:
            print(f"Warning: could not read {path}: {e}")
            continue
        for row in rows:
            for key, value in list(row.items()):
                if key == 'Tier':
                    continue
                parsed = _to_float(value)
                if parsed is not None:
                    row[key] = parsed
        panels[operation][_normalize_bs_key(block_size)] = rows

    return panels


def render_throughput_cross_tier_graphs_from_csv(
    output_dir='.',
    graph_type='bar',
    show_values=False,
    metric_families=('bandwidth', 'iops'),
):
    """
    Create {op}_bw_cross_tier_comparison.png / {op}_iops_cross_tier_comparison.png
    from already-written cross-tier CSVs under output_dir/csv_files/.
    """
    created = 0
    for metric_family in metric_families:
        panels = load_throughput_cross_tier_panels_from_csv(output_dir, metric_family)
        if not panels:
            print(f"No cross-tier {metric_family} CSVs found under {output_dir}/csv_files/")
            continue
        print(
            f"Creating {metric_family} cross-tier multipanel graphs for "
            f"operations={sorted(panels.keys(), key=_cross_tier_op_sort_key)}..."
        )
        for operation in sorted(panels.keys(), key=_cross_tier_op_sort_key):
            try:
                created += render_operation_throughput_cross_tier_comparison_graph(
                    operation,
                    panels[operation],
                    metric_family=metric_family,
                    output_dir=output_dir,
                    graph_type=graph_type,
                    show_values=show_values,
                )
            except Exception as e:
                print(
                    f"Error creating {metric_family} cross-tier graph for "
                    f"{operation}: {e}"
                )
    return created


def render_operation_throughput_cross_tier_comparison_graph(
    operation,
    metric_panels,
    metric_family='bandwidth',
    output_dir='.',
    graph_type='bar',
    show_values=False,
):
    """
    Create one multipanel BW or IOPS graph per operation (latency-style layout).

    metric_panels: dict[block_size] -> {'workload': ..., 'rows': [...]}
                   OR dict[block_size] -> list of tier rows

    Outputs:
      {operation}_bw_cross_tier_comparison.png   (metric_family='bandwidth')
      {operation}_iops_cross_tier_comparison.png (metric_family='iops')

    Layout:
      columns = block sizes
      rows    = Total + Avg (/VM)
      % change vs first tier (green = higher/better for throughput)
    """
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError as e:
        print(f"Skipping {metric_family} cross-tier graph (matplotlib required): {e}")
        return 0

    # Normalize to block_size -> list of tier rows
    block_panels = {}
    for block_size, entry in (metric_panels or {}).items():
        if isinstance(entry, dict) and 'rows' in entry:
            block_panels[block_size] = entry['rows']
        else:
            block_panels[block_size] = entry

    block_sizes = sorted(block_panels.keys(), key=_block_size_sort_key)
    if not block_sizes:
        return 0

    metric_defs = _throughput_cross_tier_metric_defs(metric_family)
    active_metrics = []
    for value_key, slug in metric_defs:
        has_data = any(
            (_to_float(row.get(value_key)) or 0) > 0
            for bs in block_sizes
            for row in (block_panels.get(bs) or [])
        )
        if has_data:
            active_metrics.append((value_key, slug))
    if not active_metrics:
        print(
            f"No {metric_family} values for operation '{operation}' — "
            f"skipping {metric_family} multipanel graph"
        )
        return 0

    file_tag = 'bw' if metric_family == 'bandwidth' else 'iops'
    title_metric = 'Bandwidth' if metric_family == 'bandwidth' else 'IOPS'
    better_note = 'green=higher/better, red=lower'
    graph_types = ['bar', 'line'] if graph_type == 'both' else [graph_type]
    op_display = CROSS_TIER_OP_DISPLAY.get(operation, operation)
    nrows = len(active_metrics)
    ncols = len(block_sizes)
    created = 0
    compact_subplot = show_values and (ncols * nrows > 4 or ncols >= 3)

    for current_type in graph_types:
        if show_values:
            fig_w = max(6.5 * ncols, 12)
            fig_h = max(5.0 * nrows, 6.5)
        else:
            fig_w = max(5.5 * ncols, 10)
            fig_h = max(4.0 * nrows, 6)
        fig, axes = plt.subplots(
            nrows=nrows,
            ncols=ncols,
            figsize=(fig_w, fig_h),
            squeeze=False,
        )
        fig.suptitle(
            f'{op_display} — {title_metric} Cross-Tier Comparison\n'
            f'(labels: absolute values; % change vs first tier — {better_note})',
            fontsize=14,
            fontweight='bold',
            y=0.995,
        )

        for row_idx, (value_key, _slug) in enumerate(active_metrics):
            for col_idx, block_size in enumerate(block_sizes):
                ax = axes[row_idx][col_idx]
                tier_rows = block_panels.get(block_size) or []
                bs_display = get_block_size_display_name(block_size)
                # Prefer display units from ylabel constants when BW
                display_key = value_key
                if metric_family == 'bandwidth':
                    if value_key.startswith('Total'):
                        display_key = f'Total BW ({BW_RATE_UNIT})'
                    elif value_key.startswith('Avg'):
                        display_key = f'Avg BW/VM ({BW_RATE_UNIT})'
                subplot_title = f'{bs_display} — {display_key}'
                _plot_latency_cross_tier_on_axes(
                    ax,
                    tier_rows,
                    value_key,
                    graph_type=current_type,
                    show_values=show_values,
                    title=subplot_title,
                    show_legend=False,
                    xlabel=(row_idx == nrows - 1),
                    series_label=display_key,
                    show_percent_change=True,
                    lower_is_better=False,
                    compact_subplot=compact_subplot,
                )

        fig.tight_layout(rect=[0, 0, 1, 0.93])

        if graph_type == 'both' and current_type == 'line':
            out_name = f'{operation}_{file_tag}_cross_tier_comparison_line.png'
        else:
            out_name = f'{operation}_{file_tag}_cross_tier_comparison.png'

        out_path = os.path.join(output_dir, out_name)
        fig.savefig(out_path, dpi=150, bbox_inches='tight')
        plt.close(fig)
        print(f"Saved operation {file_tag} cross-tier graph: {out_path}")
        created += 1

    return created


def create_cross_tier_latency_comparison(input_dirs, run_labels, output_dir='.',
                                         selected_block_sizes=None, graph_type='bar',
                                         show_values=False):
    """
    Build cross-tier latency multipanel graphs from perf_report_latency_summary data.

    Outputs (under output_dir/latency_crosstier/):
      {operation}_latency_cross_tier_comparison.png
      perf_report_cross_tier_latency_comparison.md
      csv_files/perf_report_cross_tier_latency_{operation}_{bs}.csv
    """
    print("\n" + "=" * 60)
    print("CROSS-TIER LATENCY COMPARISON")
    print("=" * 60)

    if len(input_dirs) < 2:
        print("Cross-tier latency comparison requires two or more --input-dirs")
        return {}

    panels = collect_cross_tier_latency_panels(
        input_dirs, run_labels, selected_block_sizes, output_dir=output_dir
    )
    if not panels:
        print("No latency summary data found for cross-tier latency graphs.")
        return {}

    ops_found = sorted(panels.keys(), key=_cross_tier_op_sort_key)
    bs_found = sorted(
        {bs for op in panels for bs in panels[op].keys()},
        key=_block_size_sort_key,
    )
    print(
        f"Generating latency cross-tier graphs for "
        f"operations={ops_found}, block_sizes={bs_found}"
    )

    latency_out_dir = os.path.join(output_dir, 'latency_crosstier')
    os.makedirs(latency_out_dir, exist_ok=True)
    csv_dir = ensure_csv_directory(latency_out_dir)
    md_path = os.path.join(latency_out_dir, 'perf_report_cross_tier_latency_comparison.md')
    md_sections = [
        '### Cross-Tier Latency Comparison',
        '',
        f'_Auto-generated from perf_report_latency_summary for block sizes: {", ".join(bs_found)}_',
        '',
        '_Per-operation multipanel graphs (in `latency_crosstier/`): '
        '`{operation}_latency_cross_tier_comparison.png` '
        '(columns = block sizes; rows = p50 / p95 / p99 / Max)._',
        '',
    ]
    results = {}
    fields = ['Tier', 'VMs', 'p50 (us)', 'p95 (us)', 'p99 (us)', 'Max (us)']

    for operation in ops_found:
        block_panels = panels[operation]
        for block_size in sorted(block_panels.keys(), key=_block_size_sort_key):
            rows = block_panels[block_size]
            if not any(
                (_to_float(r.get(k)) or 0) > 0
                for r in rows
                for k in ('p50 (us)', 'p95 (us)', 'p99 (us)', 'Max (us)')
            ):
                continue

            slug = f'{_normalize_bs_key(block_size)}_{operation}'
            thr_csv = os.path.join(csv_dir, f'perf_report_cross_tier_latency_{slug}.csv')
            save_perf_report_table_csv(rows, thr_csv, fields)

            bs_display = get_block_size_display_name(block_size)
            op_display = CROSS_TIER_OP_DISPLAY.get(operation, operation)
            title = f'{bs_display} {op_display} — Latency'
            md_sections.append(f'#### {title}')
            md_sections.append('')
            md_sections.append('| ' + ' | '.join(fields) + ' |')
            md_sections.append('|' + '|'.join(['-----------'] * len(fields)) + '|')
            for row in rows:
                cells = []
                for key in fields:
                    val = row.get(key)
                    if key == 'Tier':
                        cells.append(str(val) if val is not None else '')
                    elif key == 'VMs':
                        cells.append(str(int(val)) if val is not None else '0')
                    else:
                        cells.append(_format_report_number(val, 1))
                md_sections.append('| ' + ' | '.join(cells) + ' |')
            md_sections.append('')
            results[slug] = rows

        render_operation_latency_cross_tier_comparison_graph(
            operation,
            block_panels,
            output_dir=latency_out_dir,
            graph_type=graph_type,
            show_values=show_values,
        )

    with open(md_path, 'w') as f:
        f.write('\n'.join(md_sections))
    print(f"Saved cross-tier latency markdown: {md_path}")
    print(f"Cross-tier latency comparison written under: {latency_out_dir}")
    return results


def find_perf_report_throughput_csv(*search_dirs):
    """Locate csv_files/perf_report_throughput_summary.csv under candidate dirs."""
    candidates = []
    for base in search_dirs:
        if not base:
            continue
        candidates.extend([
            os.path.join(base, 'csv_files', 'perf_report_throughput_summary.csv'),
            os.path.join(base, 'perf_report_throughput_summary.csv'),
        ])
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def render_perf_report_bw_by_operation_graph(rows, png_path, graph_type='bar', show_values=False):
    """
    Plot Total BW (MB/s) by Operation from throughput summary rows.

    X-axis: Operation
    Y-axis: Total BW (MB/s)
    Series: Block Size (grouped bars / multi-line)
    Title: "{VMs} Machines Performance Test"
    """
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError as e:
        print(f"Skipping BW-by-operation graph (matplotlib required): {e}")
        return False

    if not rows:
        print("No throughput rows available for BW-by-operation graph")
        return False

    # Preserve report operation order; block sizes sorted by size
    ops_seen = []
    for row in rows:
        op = row.get('Operation')
        if op and op not in ops_seen:
            ops_seen.append(op)
    operations = [op for op in REPORT_OPERATION_ORDER if op in ops_seen]
    operations.extend(op for op in ops_seen if op not in REPORT_OPERATION_ORDER)

    block_sizes = sorted(
        {row.get('Block Size') for row in rows if row.get('Block Size')},
        key=_block_size_sort_key,
    )
    if not operations or not block_sizes:
        print("Throughput rows missing Operation/Block Size for BW graph")
        return False

    value_map = {}
    vm_counts = []
    for row in rows:
        op = row.get('Operation')
        bs = row.get('Block Size')
        bw = _to_float(row.get('Total BW (MB/s)'))
        if op is None or bs is None:
            continue
        value_map[(op, bs)] = 0.0 if bw is None else bw
        vm = _to_float(row.get('VMs'))
        if vm is not None:
            vm_counts.append(int(vm))

    vm_number = max(vm_counts) if vm_counts else 0
    title = f"{vm_number} Machines Performance Test"

    readable_colors = [
        '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd',
        '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf',
    ]
    colors = [readable_colors[i % len(readable_colors)] for i in range(len(block_sizes))]

    graph_types = ['bar', 'line'] if graph_type == 'both' else [graph_type]
    os.makedirs(os.path.dirname(png_path) or '.', exist_ok=True)
    created = 0
    n_labels = len(operations) * max(len(block_sizes), 1)
    annotate = show_values and n_labels <= SHOW_VALUES_MAX_POINTS

    for current_type in graph_types:
        fig, ax = plt.subplots(figsize=(12, 7))
        x = np.arange(len(operations))

        if current_type == 'bar':
            width = 0.8 / max(len(block_sizes), 1)
            for i, block_size in enumerate(block_sizes):
                values = [value_map.get((op, block_size), 0.0) for op in operations]
                offset = (i - len(block_sizes) / 2 + 0.5) * width
                bars = ax.bar(
                    x + offset,
                    values,
                    width=width,
                    label=get_block_size_display_name(block_size),
                    color=colors[i],
                    alpha=0.85,
                    edgecolor='black',
                    linewidth=0.5,
                )
                if annotate:
                    annotate_bar_values(ax, bars, fontsize=graph_value_fontsize(n_labels), rotation=90)
        else:
            for i, block_size in enumerate(block_sizes):
                values = [value_map.get((op, block_size), 0.0) for op in operations]
                ax.plot(
                    x,
                    values,
                    marker='o',
                    linewidth=2,
                    markersize=7,
                    label=get_block_size_display_name(block_size),
                    color=colors[i],
                )
                if annotate:
                    annotate_line_values(
                        ax, x, values, fontsize=graph_value_fontsize(n_labels), color=colors[i]
                    )

        ax.set_title(title, fontsize=16, fontweight='bold', pad=12)
        ax.set_xlabel('Operation', fontsize=12, fontweight='bold')
        ax.set_ylabel(f'Total BW ({BW_RATE_UNIT})', fontsize=12, fontweight='bold')
        ax.set_xticks(x)
        ax.set_xticklabels(operations)
        ax.grid(True, axis='y', linestyle='--', alpha=0.4)
        ax.legend(title='Block Size', loc='upper left')
        if annotate:
            ymax = max((v for v in value_map.values()), default=0)
            if ymax > 0:
                ax.set_ylim(0, ymax * 1.25)
        fig.tight_layout()

        out_path = png_path
        if current_type == 'line' and graph_type == 'both':
            root, ext = os.path.splitext(png_path)
            out_path = f"{root}_line{ext}"
        elif current_type == 'line' and graph_type == 'line':
            root, ext = os.path.splitext(png_path)
            if not root.endswith('_line'):
                out_path = f"{root}_line{ext}"

        fig.savefig(out_path, dpi=160, bbox_inches='tight', facecolor='white')
        plt.close(fig)
        print(f"Saved perf-report BW graph: {out_path}")
        created += 1

    return created > 0


def create_perf_report_bw_graph_from_csv(search_dir, output_dir='.', graph_type='bar', show_values=False):
    """Build BW-by-operation graph from an existing throughput summary CSV."""
    csv_path = find_perf_report_throughput_csv(search_dir, output_dir)
    if not csv_path:
        print(
            "Error: perf_report_throughput_summary.csv not found. "
            "Run --perf-report-table first, or point --input-dir at a results folder "
            "that contains csv_files/perf_report_throughput_summary.csv"
        )
        return False

    print(f"Loading throughput summary for BW graph: {csv_path}")
    rows, _ = load_perf_report_throughput_csv(csv_path)
    png_path = os.path.join(output_dir, 'perf_report_throughput_bw_by_operation.png')
    return render_perf_report_bw_by_operation_graph(
        rows, png_path, graph_type=graph_type, show_values=show_values
    )


def create_perf_report_tables(input_dir, output_dir='.', selected_block_sizes=None,
                              iops_all_machines=None, bw_all_machines=None,
                              include_bw_graph=False, graph_type='bar', show_values=False):
    """
    Create perf-report.md style Aggregate Throughput and Latency tables.

    Outputs (under output_dir):
      csv_files/perf_report_throughput_summary.csv
      csv_files/perf_report_latency_summary.csv
      csv_files/perf_report_latency_4k_random.csv
      perf_report_throughput_summary.md / .png
      perf_report_latency_summary.md / .png
      perf_report_latency_4k_random.md / .png
      perf_report_throughput_bw_by_operation.png  (if include_bw_graph)
    """
    print("\n" + "=" * 60)
    print("PERF-REPORT TABLES")
    print("=" * 60)

    if iops_all_machines is None:
        print("Analyzing IOPS for throughput table...")
        _, iops_all_machines = analyze_all_directories_iops(input_dir)
    if bw_all_machines is None:
        print("Analyzing bandwidth for throughput table...")
        _, bw_all_machines = analyze_all_directories(input_dir)
    if selected_block_sizes:
        if iops_all_machines:
            _, iops_all_machines = filter_results_by_block_sizes({}, iops_all_machines, selected_block_sizes)
        if bw_all_machines:
            _, bw_all_machines = filter_results_by_block_sizes({}, bw_all_machines, selected_block_sizes)

    print("Analyzing latency percentiles...")
    latency_all_machines = analyze_latency_percentiles(input_dir)
    if selected_block_sizes and latency_all_machines:
        filtered_lat = defaultdict(lambda: defaultdict(list))
        for operation, block_map in latency_all_machines.items():
            for block_size, items in block_map.items():
                if block_size in selected_block_sizes:
                    filtered_lat[operation][block_size] = items
        latency_all_machines = filtered_lat

    csv_dir = ensure_csv_directory(output_dir)

    throughput_fields = [
        'Operation', 'Block Size', 'Total IOPS', 'Total BW (MB/s)',
        'Avg IOPS/VM', 'Avg BW/VM (MB/s)', 'VMs',
    ]
    throughput_rows = build_throughput_report_rows(
        iops_all_machines, bw_all_machines, selected_block_sizes
    )
    thr_csv = os.path.join(csv_dir, 'perf_report_throughput_summary.csv')
    thr_md = os.path.join(output_dir, 'perf_report_throughput_summary.md')
    thr_png = os.path.join(output_dir, 'perf_report_throughput_summary.png')
    vm_count = _report_vm_count(throughput_rows)
    throughput_title = (
        f"{vm_count} Machines Aggregate Throughput "
        f"({iops_field_label()}; {bw_field_label()})"
    )
    save_perf_report_table_csv(throughput_rows, thr_csv, throughput_fields)
    save_perf_report_table_markdown(
        throughput_rows, thr_md, throughput_fields, throughput_title
    )
    render_perf_report_table_png(
        throughput_rows, throughput_fields, throughput_title, thr_png
    )

    if include_bw_graph:
        bw_graph_png = os.path.join(output_dir, 'perf_report_throughput_bw_by_operation.png')
        render_perf_report_bw_by_operation_graph(
            throughput_rows, bw_graph_png, graph_type=graph_type, show_values=show_values
        )

    latency_fields = [
        'Operation', 'Block Size', 'p50 (us)', 'p95 (us)', 'p99 (us)', 'Max (us)', 'VMs',
    ]
    latency_rows = build_latency_report_rows(
        latency_all_machines, selected_block_sizes, focus_only_4k_random=False
    )
    lat_csv = os.path.join(csv_dir, 'perf_report_latency_summary.csv')
    lat_md = os.path.join(output_dir, 'perf_report_latency_summary.md')
    lat_png = os.path.join(output_dir, 'perf_report_latency_summary.png')
    save_perf_report_table_csv(latency_rows, lat_csv, latency_fields)
    save_perf_report_table_markdown(
        latency_rows, lat_md, latency_fields, 'Latency (all operations)'
    )
    render_perf_report_table_png(
        latency_rows, latency_fields, 'Latency (average across VMs)', lat_png
    )

    # Match perf-report.md focus section: 4k randread/randwrite
    latency_4k_rows = build_latency_report_rows(
        latency_all_machines, selected_block_sizes, focus_only_4k_random=True
    )
    lat4_csv = os.path.join(csv_dir, 'perf_report_latency_4k_random.csv')
    lat4_md = os.path.join(output_dir, 'perf_report_latency_4k_random.md')
    lat4_png = os.path.join(output_dir, 'perf_report_latency_4k_random.png')
    save_perf_report_table_csv(latency_4k_rows, lat4_csv, latency_fields)
    save_perf_report_table_markdown(
        latency_4k_rows, lat4_md, latency_fields, 'Latency (4k Random I/O)'
    )
    render_perf_report_table_png(
        latency_4k_rows, latency_fields, 'Latency (4k Random I/O)', lat4_png
    )

    print(f"\nPerf-report tables written under: {output_dir}")
    return {
        'throughput_rows': throughput_rows,
        'latency_rows': latency_rows,
        'latency_4k_rows': latency_4k_rows,
    }


# ---------------------------------------------------------------------------
# Cross-tier comparison — multi input-dirs
# ---------------------------------------------------------------------------

CROSS_TIER_OP_DISPLAY = {
    'randread': 'Random Read',
    'randwrite': 'Random Write',
    'read': 'Sequential Read',
    'write': 'Sequential Write',
    'randrw': 'Random Mixed RW',
    'rw': 'Sequential Mixed RW',
    'readwrite': 'Sequential Mixed RW',
}

# Default key workloads for cross-tier summary (IOPS + large-block BW)
KEY_CROSS_TIER_WORKLOADS = (
    {'operation': 'randread', 'block_size': '4k', 'metric': 'iops'},
    {'operation': 'randwrite', 'block_size': '4k', 'metric': 'iops'},
    {'operation': 'read', 'block_size': '1024k', 'metric': 'bandwidth'},
    {'operation': 'write', 'block_size': '1024k', 'metric': 'bandwidth'},
)


def is_fio_result_dir(path):
    """
    True if path looks like a FIO result tree (VM/subdir with *.json),
    or already has analyzer CSV summaries under csv_files/.
    """
    if not path or not os.path.isdir(path):
        return False
    # Direct JSON in path (unusual) or in immediate children (vm-* style)
    if glob.glob(os.path.join(path, '*.json')):
        return True
    try:
        for item in os.listdir(path):
            child = os.path.join(path, item)
            if os.path.isdir(child) and glob.glob(os.path.join(child, '*.json')):
                return True
    except OSError:
        return False
    csv_markers = (
        os.path.join(path, 'csv_files', 'perf_report_throughput_summary.csv'),
        os.path.join(path, 'csv_files', 'perf_report_latency_summary.csv'),
    )
    return any(os.path.isfile(p) for p in csv_markers)


def discover_child_result_dirs(parent_dir):
    """Return sorted child directories under parent that look like FIO result runs."""
    children = []
    try:
        names = os.listdir(parent_dir)
    except OSError:
        return children
    for name in sorted(names):
        child = os.path.join(parent_dir, name)
        if os.path.isdir(child) and is_fio_result_dir(child):
            children.append(child)
    return children


def resolve_compare_input_dirs(raw_dirs):
    """
    Normalize CLI input dirs for single-run or compare mode.

    - Multiple paths: each must exist (compare mode).
    - One path that is itself a result dir: single-run.
    - One path that is a parent of 2+ result children: expand to those children.
    """
    if not raw_dirs:
        raw_dirs = ['.']
    abs_dirs = [os.path.abspath(path) for path in raw_dirs]
    for path in abs_dirs:
        if not os.path.exists(path):
            raise ValueError(f"Input directory does not exist: {path}")
        if not os.path.isdir(path):
            raise ValueError(f"Input path is not a directory: {path}")

    if len(abs_dirs) >= 2:
        return abs_dirs

    sole = abs_dirs[0]
    if is_fio_result_dir(sole):
        return [sole]

    children = discover_child_result_dirs(sole)
    if len(children) >= 2:
        print(
            f"Compare mode: expanded --input-dir {sole} into "
            f"{len(children)} child result directories:"
        )
        for child in children:
            print(f"  - {os.path.basename(child)}")
        return children

    # Fall back to single-dir analysis of the given path (may still find nested JSON)
    return [sole]


def resolve_tier_name_and_vms(run_label, machine_count):
    """
    Map a multi-run label / machine count to (Tier, VMs).

    Cross-tier display names come from --run-labels (or directory basenames).
    VMs is the measured machine count when available, else the first number in the label.
    """
    label = (run_label or '').strip() or 'Unknown'
    vms = int(machine_count) if machine_count else 0
    if vms <= 0:
        match = re.search(r'(\d+)', label)
        if match:
            vms = int(match.group(1))
    return label, vms


def _normalize_bs_key(block_size):
    """Normalize block-size labels for lookup (4K/4kb/4k → 4k)."""
    text = str(block_size).strip().lower().replace(' ', '')
    text = text.replace('kb', 'k')
    if text.endswith('b') and not text.endswith('k'):
        text = text[:-1] + 'k'
    return text


def _cross_tier_op_sort_key(operation):
    try:
        return REPORT_OPERATION_ORDER.index(operation)
    except ValueError:
        return len(REPORT_OPERATION_ORDER)


def _make_cross_tier_workload(operation, block_size, metric):
    """Build one cross-tier workload definition for op × block size × metric."""
    bs_norm = _normalize_bs_key(block_size)
    bs_display = get_block_size_display_name(bs_norm)
    op_display = CROSS_TIER_OP_DISPLAY.get(operation, operation)
    if metric == 'iops':
        return {
            'title': f'{bs_display} {op_display} — Aggregate IOPS',
            'slug': f'{bs_norm}_{operation}_iops',
            'metric': 'iops',
            'operation': operation,
            'block_size': bs_norm,
            'fields': ('Tier', 'VMs', 'Total IOPS', 'Avg IOPS/VM'),
            'total_key': 'Total IOPS',
            'avg_key': 'Avg IOPS/VM',
            'total_ylabel': 'Total IOPS',
            'avg_ylabel': 'Avg IOPS/VM',
        }
    return {
        'title': f'{bs_display} {op_display} — Aggregate Bandwidth',
        'slug': f'{bs_norm}_{operation}_bw',
        'metric': 'bandwidth',
        'operation': operation,
        'block_size': bs_norm,
        'fields': ('Tier', 'VMs', 'Total BW (MB/s)', 'Avg BW/VM (MB/s)'),
        'total_key': 'Total BW (MB/s)',
        'avg_key': 'Avg BW/VM (MB/s)',
        'total_ylabel': f'Total BW ({BW_RATE_UNIT})',
        'avg_ylabel': f'Avg BW/VM ({BW_RATE_UNIT})',
    }


def _format_key_cross_tier_table_markdown(workload, rows):
    """Markdown table body for one key cross-tier workload (header + rows)."""
    fields = list(workload['fields'])
    lines = [
        f"#### {workload['title']}",
        '',
        '| ' + ' | '.join(fields) + ' |',
        '|' + '|'.join(['------'] * len(fields)) + '|',
    ]
    for row in rows:
        cells = []
        for key in fields:
            val = row.get(key)
            if key == 'Tier':
                cells.append(str(val) if val is not None else '')
            elif key == 'VMs':
                cells.append(str(int(val)) if val is not None else '0')
            elif 'IOPS' in key and key.startswith('Total'):
                cells.append(_format_report_number(val, 0))
            else:
                cells.append(_format_report_number(val, 1))
        lines.append('| ' + ' | '.join(cells) + ' |')
    lines.append('')
    return lines


def build_key_cross_tier_panels(iops_comparison, bw_comparison, run_labels):
    """
    Build the four key cross-tier workloads (rows + workload defs).

    Returns list of {'workload': ..., 'rows': ...} only when every tier has data
    (partial coverage would produce TBD and is skipped).
    """
    panels = []
    for spec in KEY_CROSS_TIER_WORKLOADS:
        metric = spec['metric']
        comparison = iops_comparison if metric == 'iops' else bw_comparison
        if not comparison:
            continue
        workload = _make_cross_tier_workload(
            spec['operation'], spec['block_size'], metric
        )
        rows = build_cross_tier_workload_rows(
            workload, iops_comparison, bw_comparison, run_labels
        )
        if not cross_tier_rows_complete(rows, workload):
            missing = cross_tier_incomplete_tiers(rows, workload)
            print(
                f"Skipping key cross-tier workload (incomplete across tiers "
                f"{missing or 'all'}): {workload['title']}"
            )
            continue
        panels.append({'workload': workload, 'rows': rows})
    return panels


def render_key_cross_tier_graphs(panels, output_dir='.', graph_type='bar', show_values=False):
    """
    Create individual graphs for key cross-tier workloads plus one 2×2 summary figure.

    Outputs:
      key_cross_tier_comparison.png          (2×2 summary)
      perf_report_cross_tier_{slug}.png              (per key workload; may already exist)
      key_cross_tier/{slug}.png                         (copies under key_cross_tier/)
    """
    if not panels:
        print("No key cross-tier workload data to graph")
        return []

    key_dir = os.path.join(output_dir, 'key_cross_tier')
    os.makedirs(key_dir, exist_ok=True)
    created = []

    for panel in panels:
        workload = panel['workload']
        rows = panel['rows']
        slug = workload['slug']
        # Canonical per-workload graph (shared with full cross-tier set)
        main_png = os.path.join(output_dir, f'perf_report_cross_tier_{slug}.png')
        ok = render_cross_tier_scaling_graph(
            rows, workload, main_png, graph_type=graph_type, show_values=show_values
        )
        if ok:
            created.append(main_png)
        # Dedicated copy under key_cross_tier/ for report packaging
        key_png = os.path.join(key_dir, f'{slug}.png')
        try:
            import shutil
            if os.path.isfile(main_png):
                shutil.copy2(main_png, key_png)
                created.append(key_png)
            else:
                render_cross_tier_scaling_graph(
                    rows, workload, key_png, graph_type=graph_type, show_values=show_values
                )
                if os.path.isfile(key_png):
                    created.append(key_png)
        except Exception as e:
            print(f"Warning: could not write key_cross_tier/{slug}.png: {e}")

    # 2×2 summary matching the four key cross-tier tables
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError as e:
        print(f"Skipping key cross-tier summary graph (matplotlib required): {e}")
        return created

    graph_types = ['bar', 'line'] if graph_type == 'both' else [graph_type]
    for current_type in graph_types:
        fig, axes = plt.subplots(2, 2, figsize=(16, 11), squeeze=False)
        fig.suptitle(
            'Cross-Tier Comparison — Key Workloads\n'
            '(% change vs first tier — green=higher/better, red=lower)',
            fontsize=14,
            fontweight='bold',
            y=0.98,
        )
        for idx in range(4):
            ax = axes[idx // 2][idx % 2]
            if idx >= len(panels):
                ax.set_visible(False)
                continue
            workload = panels[idx]['workload']
            rows = panels[idx]['rows']
            twin, _, _ = _plot_cross_tier_scaling_on_axes(
                ax,
                rows,
                workload,
                graph_type=current_type,
                show_values=show_values,
                title=workload['title'],
                show_legend=False,
                xlabel=True,
                compact_subplot=show_values,
            )
            if twin is None:
                ax.set_visible(False)

        fig.tight_layout(rect=[0, 0, 1, 0.94])
        if graph_type == 'both' and current_type == 'line':
            out_name = 'key_cross_tier_comparison_line.png'
        elif current_type == 'line':
            out_name = 'key_cross_tier_comparison_line.png'
        else:
            out_name = 'key_cross_tier_comparison.png'
        out_path = os.path.join(output_dir, out_name)
        fig.savefig(out_path, dpi=150, bbox_inches='tight')
        plt.close(fig)
        print(f"Saved key cross-tier summary graph: {out_path}")
        created.append(out_path)
        # Also under key_cross_tier/
        key_summary = os.path.join(key_dir, out_name)
        try:
            import shutil
            shutil.copy2(out_path, key_summary)
        except Exception:
            pass

    return created


def format_cross_tier_markdown(
    panels,
    include_graph_links=True,
    image_prefix='',
    heading='### Cross-Tier Comparison',
    intro=None,
    graph_subdir='',
    graph_name_template='{slug}.png',
    table_png_template=None,
    summary_graph=None,
    empty_message='_No matching cross-tier workload data found._',
):
    """
    Build markdown for cross-tier Aggregate IOPS/BW tables.

    image_prefix: optional path prefix for PNG links (e.g. '' or 'key_cross_tier_assets/').
    graph_subdir: subdirectory under image_prefix for per-workload PNGs.
    graph_name_template: filename pattern with {slug} for scaling graphs.
    table_png_template: optional filename pattern with {slug} for table PNGs.
    summary_graph: optional summary PNG filename at image_prefix root.
    """
    prefix = image_prefix or ''
    if prefix and not prefix.endswith('/'):
        prefix = prefix + '/'

    bs_found = sorted(
        {_normalize_bs_key(p['workload']['block_size']) for p in panels},
        key=_block_size_sort_key,
    )
    if intro is None:
        if bs_found:
            intro = (
                f'Workload comparison across tiers for every tested block size '
                f'({", ".join(bs_found)}):'
            )
        else:
            intro = 'Workload comparison across tiers for every tested block size:'

    lines = [
        heading,
        '',
        intro,
        '',
    ]
    if include_graph_links and summary_graph:
        lines.extend([
            f'![Cross-Tier Comparison]({prefix}{summary_graph})',
            '',
        ])
    if not panels:
        lines.append(empty_message)
        lines.append('')
        return lines

    for panel in panels:
        lines.extend(_format_key_cross_tier_table_markdown(panel['workload'], panel['rows']))
        if include_graph_links:
            slug = panel['workload']['slug']
            sub = f'{graph_subdir}/' if graph_subdir else ''
            graph_name = graph_name_template.format(slug=slug)
            lines.append(
                f"![{panel['workload']['title']}]({prefix}{sub}{graph_name})"
            )
            lines.append('')
            if table_png_template:
                table_name = table_png_template.format(slug=slug)
                lines.append(
                    f"![{panel['workload']['title']} table]({prefix}{sub}{table_name})"
                )
                lines.append('')
    return lines


def format_key_cross_tier_markdown(panels, include_graph_links=True, image_prefix='',
                                   heading='### Cross-Tier Comparison — Key Workloads'):
    """Build markdown for the four key cross-tier workload tables."""
    return format_cross_tier_markdown(
        panels,
        include_graph_links=include_graph_links,
        image_prefix=image_prefix,
        heading=heading,
        intro='Key workload comparison across tiers:',
        graph_subdir='key_cross_tier',
        graph_name_template='{slug}.png',
        summary_graph='key_cross_tier_comparison.png',
        empty_message='_No matching key cross-tier workload data found._',
    )


def _copy_key_cross_tier_assets_near_report(output_dir, perf_report_path):
    """
    Copy key cross-tier PNGs next to the report and return a relative image prefix.
    """
    import shutil

    report_dir = os.path.dirname(os.path.abspath(perf_report_path))
    output_dir = os.path.abspath(output_dir)

    assets_dir = os.path.join(report_dir, 'key_cross_tier_assets')
    os.makedirs(assets_dir, exist_ok=True)
    section_src = os.path.join(output_dir, 'key_cross_tier')
    section_dst = os.path.join(assets_dir, 'key_cross_tier')
    os.makedirs(section_dst, exist_ok=True)

    summary = os.path.join(output_dir, 'key_cross_tier_comparison.png')
    if os.path.isfile(summary):
        shutil.copy2(summary, os.path.join(assets_dir, 'key_cross_tier_comparison.png'))
    line_summary = os.path.join(output_dir, 'key_cross_tier_comparison_line.png')
    if os.path.isfile(line_summary):
        shutil.copy2(
            line_summary,
            os.path.join(assets_dir, 'key_cross_tier_comparison_line.png'),
        )

    if os.path.isdir(section_src):
        for name in os.listdir(section_src):
            src = os.path.join(section_src, name)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(section_dst, name))

    return 'key_cross_tier_assets'


def update_perf_report_cross_tier_section(perf_report_path, panels, output_dir='.'):
    """
    Replace the Cross-Tier Comparison section in an existing perf-report.md
    (heading ### 6.6 Cross-Tier Comparison … through ### 6.7 / next ###).

    Copies graph assets under <report_dir>/key_cross_tier_assets/ and links to them.
    Used for the four key workloads only; all op×bs tables live in --output-dir.
    """
    path = os.path.abspath(perf_report_path)
    if not os.path.isfile(path):
        print(f"Error: --update-perf-report path not found: {path}")
        return False

    with open(path, 'r', encoding='utf-8') as f:
        text = f.read()

    start_re = re.compile(r'^### 6\.6 Cross-Tier Comparison\s*$', re.MULTILINE)
    start_match = start_re.search(text)
    if not start_match:
        print(f"Error: could not find '### 6.6 Cross-Tier Comparison' in {path}")
        return False

    rest = text[start_match.end():]
    end_re = re.compile(r'^### 6\.7\b|^### (?!6\.6)', re.MULTILINE)
    end_match = end_re.search(rest)
    end_idx = start_match.end() + end_match.start() if end_match else len(text)
    suffix = text[end_idx:]

    image_prefix = _copy_key_cross_tier_assets_near_report(output_dir, path)
    body_lines = format_key_cross_tier_markdown(
        panels,
        include_graph_links=True,
        image_prefix=image_prefix,
        heading='### 6.6 Cross-Tier Comparison',
    )
    while body_lines and body_lines[-1] == '':
        body_lines.pop()
    body = '\n'.join(body_lines) + '\n\n---\n\n'

    if suffix.lstrip().startswith('---'):
        body = '\n'.join(body_lines) + '\n\n'
        suffix = suffix.lstrip('\n')

    new_text = text[:start_match.start()] + body + suffix
    with open(path, 'w', encoding='utf-8') as f:
        f.write(new_text)
    print(f"Updated Cross-Tier Comparison section in perf report: {path}")
    print(f"Key cross-tier graph assets copied to: {os.path.join(os.path.dirname(path), image_prefix)}")
    return True


def write_key_cross_tier_outputs(
    iops_comparison,
    bw_comparison,
    run_labels,
    output_dir='.',
    graph_type='bar',
    show_values=False,
    update_perf_report=None,
):
    """
    Populate the four key cross-tier tables + graphs from compare-mode data.

    Always writes:
      perf_report_key_cross_tier.md
      key_cross_tier_comparison.png
      key_cross_tier/*.png
    Optionally patches --update-perf-report with those key tables only.
    """
    panels = build_key_cross_tier_panels(iops_comparison, bw_comparison, run_labels)
    if not panels:
        print("Key cross-tier workloads: no data (need 4k randread/randwrite IOPS and/or "
              "1024k read/write BW across tiers)")
        return []

    print("\n" + "=" * 60)
    print("KEY CROSS-TIER WORKLOADS")
    print("=" * 60)
    print(f"Building {len(panels)} key cross-tier table(s)/graph(s)")

    render_key_cross_tier_graphs(
        panels, output_dir=output_dir, graph_type=graph_type, show_values=show_values
    )

    md_path = os.path.join(output_dir, 'perf_report_key_cross_tier.md')
    md_lines = format_key_cross_tier_markdown(panels, include_graph_links=True)
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(md_lines) + '\n')
    print(f"Saved key cross-tier markdown: {md_path}")

    csv_dir = ensure_csv_directory(output_dir)
    for panel in panels:
        workload = panel['workload']
        fields = list(workload['fields'])
        csv_path = os.path.join(csv_dir, f"perf_report_key_cross_tier_{workload['slug']}.csv")
        save_perf_report_table_csv(panel['rows'], csv_path, fields)

    if update_perf_report:
        update_perf_report_cross_tier_section(
            update_perf_report, panels, output_dir=output_dir
        )

    return panels


def discover_cross_tier_workloads(iops_comparison, bw_comparison, selected_block_sizes=None):
    """
    Build cross-tier workloads for every tested operation × block size.

    For each (operation, block_size) found in the multi-run data, emit Aggregate
    IOPS and/or Aggregate Bandwidth tables/graphs for metrics that have data
    (pass None for a comparison to skip that metric).
    """
    selected_norm = None
    if selected_block_sizes:
        selected_norm = {_normalize_bs_key(bs) for bs in selected_block_sizes}

    # (operation, normalized_bs) -> canonical block_size label from data
    pairs = {}
    for comparison in (iops_comparison, bw_comparison):
        if not comparison:
            continue
        for (operation, block_size) in comparison.keys():
            bs_norm = _normalize_bs_key(block_size)
            if selected_norm is not None and bs_norm not in selected_norm:
                continue
            pairs.setdefault((operation, bs_norm), block_size)

    workloads = []
    for operation, bs_norm in sorted(
        pairs.keys(), key=lambda t: (_cross_tier_op_sort_key(t[0]), _block_size_sort_key(t[1]))
    ):
        # Prefer IOPS then BW for each op×bs
        if iops_comparison and any(
            op == operation and _normalize_bs_key(bs) == bs_norm
            for (op, bs) in iops_comparison.keys()
        ):
            workloads.append(_make_cross_tier_workload(operation, bs_norm, 'iops'))
        if bw_comparison and any(
            op == operation and _normalize_bs_key(bs) == bs_norm
            for (op, bs) in bw_comparison.keys()
        ):
            workloads.append(_make_cross_tier_workload(operation, bs_norm, 'bandwidth'))

    return workloads


def _lookup_comparison_entry(comparison, operation, block_size, run_label):
    """Find comparison stats for op/bs/run with flexible block-size matching."""
    by_run = comparison.get((operation, block_size))
    if by_run and run_label in by_run:
        return by_run[run_label]

    target = _normalize_bs_key(block_size)
    for (op, bs), runs in comparison.items():
        if op != operation:
            continue
        if _normalize_bs_key(bs) == target and run_label in runs:
            return runs[run_label]
    return None


def build_cross_tier_workload_rows(workload, iops_comparison, bw_comparison, run_labels):
    """Build one cross-tier table's rows for a workload definition."""
    comparison = iops_comparison if workload['metric'] == 'iops' else bw_comparison
    rows = []
    for run_label in run_labels:
        entry = _lookup_comparison_entry(
            comparison or {}, workload['operation'], workload['block_size'], run_label
        )
        machine_count = int(entry['machine_count']) if entry else 0
        tier, vms = resolve_tier_name_and_vms(run_label, machine_count)
        row = {'Tier': tier, 'VMs': vms if vms else machine_count}
        if entry:
            total = entry['total_all_machines']
            avg = entry['mean_total_per_machine']
            row[workload['total_key']] = total
            row[workload['avg_key']] = avg
        else:
            row[workload['total_key']] = None
            row[workload['avg_key']] = None
        rows.append(row)

    # Keep --run-labels / --input-dirs order (do not reorder by VM count)
    return rows


def cross_tier_rows_complete(rows, workload):
    """
    True when every tier/run has numeric Total and Avg for this workload.

    Used to drop block sizes that were not tested in all configurations
    (avoids TBD cells in cross-tier tables/graphs).
    """
    if not rows:
        return False
    total_key = workload['total_key']
    avg_key = workload['avg_key']
    for row in rows:
        if _to_float(row.get(total_key)) is None:
            return False
        if _to_float(row.get(avg_key)) is None:
            return False
    return True


def cross_tier_incomplete_tiers(rows, workload):
    """Return Tier labels missing Total/Avg data for this workload."""
    missing = []
    total_key = workload['total_key']
    avg_key = workload['avg_key']
    for row in rows or []:
        if (
            _to_float(row.get(total_key)) is None
            or _to_float(row.get(avg_key)) is None
        ):
            missing.append(str(row.get('Tier') or '?'))
    return missing


def _extract_cross_tier_plot_series(rows, workload):
    """Return (labels, totals, avgs) for one cross-tier table."""
    labels = []
    totals = []
    avgs = []
    for row in rows:
        tier = row.get('Tier') or ''
        vms = int(row.get('VMs') or 0)
        vm_label = f"({vms} VM)" if vms == 1 else f"({vms} VMs)"
        labels.append(f"{tier}\n{vm_label}" if vms else str(tier))
        totals.append(_to_float(row.get(workload['total_key'])) or 0.0)
        avgs.append(_to_float(row.get(workload['avg_key'])) or 0.0)
    return labels, totals, avgs


def _plot_cross_tier_scaling_on_axes(ax1, rows, workload, graph_type='bar', show_values=False,
                                    title=None, show_legend=True, xlabel=True,
                                    show_percent_change=True, compact_subplot=False):
    """
    Draw Total + Avg cross-tier series onto an existing axes (with twin y-axis).
    Optionally annotate % change vs first tier (same style as latency cross-tier:
    green = higher/better for BW/IOPS, red = lower).
    Returns (twin_axes, legend_handles, legend_labels), or (None, [], []) if empty.
    """
    import numpy as np

    labels, totals, avgs = _extract_cross_tier_plot_series(rows, workload)
    if not any(totals) and not any(avgs):
        ax1.set_visible(False)
        return None, [], []

    ax2 = ax1.twinx()
    # Twin axes draw a solid patch over ax1 by default, which hides ax1 artists
    # (including green % labels on the Total series). Keep the twin transparent.
    ax2.patch.set_visible(False)
    color_total = '#1f77b4'
    color_avg = '#ff7f0e'
    x = np.arange(len(labels))
    width = 0.35
    n_tiers = len(labels)

    headroom = _cross_tier_plot_headroom(
        n_tiers, show_values, show_percent_change, compact_subplot=compact_subplot
    )
    total_ymax = max(totals) * headroom if any(totals) else 1.0
    avg_ymax = max(avgs) * headroom if any(avgs) else 1.0
    value_fontsize = graph_value_fontsize(n_tiers)
    if compact_subplot:
        value_fontsize = max(6, value_fontsize - 1)
    value_metric = _infer_cross_tier_metric(
        workload=workload, value_key=workload.get('total_key')
    )

    if graph_type == 'bar':
        bars1 = ax1.bar(
            x - width / 2, totals, width=width, label=workload['total_ylabel'],
            color=color_total, alpha=0.85, edgecolor='black', linewidth=0.5,
        )
        bars2 = ax2.bar(
            x + width / 2, avgs, width=width, label=workload['avg_ylabel'],
            color=color_avg, alpha=0.85, edgecolor='black', linewidth=0.5,
        )
        if any(totals):
            ax1.set_ylim(0, total_ymax)
        if any(avgs):
            ax2.set_ylim(0, avg_ymax)

        value_texts_1 = []
        value_texts_2 = []
        if show_values:
            value_texts_1 = annotate_bar_values_smart(
                ax1,
                bars1,
                fontsize=value_fontsize,
                n_tiers=n_tiers,
                compact_subplot=compact_subplot,
                compact_numbers=False,
                chart_max=total_ymax,
                x_stagger_frac=-0.12,
                metric=value_metric,
            )
            value_texts_2 = annotate_bar_values_smart(
                ax2,
                bars2,
                fontsize=value_fontsize,
                n_tiers=n_tiers,
                compact_subplot=compact_subplot,
                compact_numbers=False,
                chart_max=avg_ymax,
                x_stagger_frac=0.12,
                metric=value_metric,
            )
        total_x = x - width / 2
        avg_x = x + width / 2
    else:
        ax1.plot(
            x, totals, marker='o', linewidth=2.5, markersize=7,
            color=color_total, label=workload['total_ylabel'],
        )
        ax2.plot(
            x, avgs, marker='s', linewidth=2.5, markersize=7,
            color=color_avg, label=workload['avg_ylabel'],
        )
        value_texts_1 = []
        value_texts_2 = []
        if show_values:
            annotate_line_values(
                ax1, x, totals, fontsize=value_fontsize, color=color_total,
                metric=value_metric, compact_subplot=compact_subplot,
            )
            annotate_line_values(
                ax2, x, avgs, fontsize=value_fontsize, color=color_avg,
                metric=value_metric, compact_subplot=compact_subplot,
            )
        total_x = x
        avg_x = x

    pct_texts_1 = []
    pct_texts_2 = []
    if show_percent_change and len(labels) >= 2:
        pct_fontsize = max(7, value_fontsize)
        y_offset_frac = 0.14 if show_values else 0.04
        if show_values and compact_subplot:
            y_offset_frac = 0.18
        pct_rotation = _cross_tier_percent_rotation(
            n_tiers, show_values, compact_subplot=compact_subplot
        )
        if any(totals):
            pct_texts_1 = annotate_percent_changes(
                ax1,
                list(total_x),
                totals,
                fontsize=pct_fontsize,
                y_offset_frac=y_offset_frac,
                lower_is_better=False,
                series_index=0,
                rotation=pct_rotation,
                return_texts=True,
            )
        if any(avgs):
            pct_texts_2 = annotate_percent_changes(
                ax2,
                list(avg_x),
                avgs,
                fontsize=pct_fontsize,
                y_offset_frac=y_offset_frac,
                lower_is_better=False,
                series_index=1,
                rotation=pct_rotation,
                return_texts=True,
            )
        baseline_tier = (rows[0].get('Tier') or 'baseline')
        baseline_name = str(baseline_tier).split('\n')[0]
        ax1.text(
            0.98,
            0.98,
            f'% vs {baseline_name} (green=higher)',
            transform=ax1.transAxes,
            ha='right',
            va='top',
            fontsize=7,
            color='#555555',
            style='italic',
        )

    if graph_type == 'bar' and show_values:
        fig = ax1.figure
        all_texts_1 = value_texts_1 + pct_texts_1
        all_texts_2 = value_texts_2 + pct_texts_2
        if all_texts_1:
            _resolve_text_collisions(ax1, all_texts_1, fig, total_ymax)
            _expand_ylim_for_texts(ax1, all_texts_1, fig)
        if all_texts_2:
            _resolve_text_collisions(ax2, all_texts_2, fig, avg_ymax)
            _expand_ylim_for_texts(ax2, all_texts_2, fig)

    ax1.set_title(title or workload['title'], fontsize=11, fontweight='bold', pad=8)
    if xlabel:
        ax1.set_xlabel('Tier / VMs', fontsize=9, fontweight='bold')
    ax1.set_ylabel(workload['total_ylabel'], fontsize=9, fontweight='bold', color=color_total)
    ax2.set_ylabel(workload['avg_ylabel'], fontsize=9, fontweight='bold', color=color_avg)
    ax1.set_xticks(x)
    ax1.set_xticklabels(labels, fontsize=8)
    ax1.tick_params(axis='y', labelcolor=color_total, labelsize=8)
    ax2.tick_params(axis='y', labelcolor=color_avg, labelsize=8)
    ax1.grid(True, axis='y', linestyle='--', alpha=0.4)

    if graph_type != 'bar':
        if any(totals):
            ax1.set_ylim(0, total_ymax)
        if any(avgs):
            ax2.set_ylim(0, avg_ymax)

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    legend_handles = lines1 + lines2
    legend_labels = labels1 + labels2

    if show_legend:
        ax1.legend(legend_handles, legend_labels, loc='upper left', fontsize=8)

    return ax2, legend_handles, legend_labels


def _add_figure_legend_below_title(fig, handles, labels, ncol=None):
    """Place a shared legend centered under the figure title (outside subplots)."""
    if not handles or not labels:
        return
    # Deduplicate while preserving order
    seen = set()
    uniq_handles = []
    uniq_labels = []
    for handle, label in zip(handles, labels):
        if label in seen:
            continue
        seen.add(label)
        uniq_handles.append(handle)
        uniq_labels.append(label)
    if ncol is None:
        ncol = min(len(uniq_labels), 4)
    fig.legend(
        uniq_handles,
        uniq_labels,
        loc='upper center',
        bbox_to_anchor=(0.5, 0.955),
        ncol=ncol,
        fontsize=9,
        frameon=True,
        fancybox=True,
        framealpha=0.95,
        edgecolor='#cccccc',
    )


def render_cross_tier_scaling_graph(rows, workload, png_path, graph_type='bar', show_values=False):
    """Plot Total and Avg metrics vs tier/VMs for one cross-tier workload."""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError as e:
        print(f"Skipping cross-tier graph (matplotlib required): {e}")
        return False

    if not rows:
        print(f"No rows for cross-tier graph: {workload['title']}")
        return False

    labels, totals, avgs = _extract_cross_tier_plot_series(rows, workload)
    if not any(totals) and not any(avgs):
        print(f"No numeric data for cross-tier graph: {workload['title']}")
        return False

    graph_types = ['bar', 'line'] if graph_type == 'both' else [graph_type]
    os.makedirs(os.path.dirname(png_path) or '.', exist_ok=True)
    created = 0

    for current_type in graph_types:
        fig, ax1 = plt.subplots(figsize=(12, 7))
        twin, _, _ = _plot_cross_tier_scaling_on_axes(
            ax1,
            rows,
            workload,
            graph_type=current_type,
            show_values=show_values,
            title=workload['title'],
            show_legend=False,
            xlabel=True,
        )
        if twin is None:
            plt.close(fig)
            continue

        # Restore larger fonts for standalone graphs
        ax1.set_title(
            f"{workload['title']}\n"
            f"(% change vs first tier — green=higher/better, red=lower)",
            fontsize=14,
            fontweight='bold',
            pad=12,
        )
        ax1.set_xlabel('Tier / VMs', fontsize=12, fontweight='bold')
        ax1.set_ylabel(workload['total_ylabel'], fontsize=12, fontweight='bold', color='#1f77b4')
        twin.set_ylabel(workload['avg_ylabel'], fontsize=12, fontweight='bold', color='#ff7f0e')
        # No legend: left Y-axis = Total, right Y-axis = Avg/VM (color-coded)
        fig.tight_layout()

        out_path = png_path
        if current_type == 'line' and graph_type == 'both':
            root, ext = os.path.splitext(png_path)
            out_path = f"{root}_line{ext}"
        elif current_type == 'line' and graph_type == 'line':
            root, ext = os.path.splitext(png_path)
            if not root.endswith('_line'):
                out_path = f"{root}_line{ext}"

        fig.savefig(out_path, dpi=150, bbox_inches='tight')
        plt.close(fig)
        print(f"Saved cross-tier graph: {out_path}")
        created += 1

    return created > 0


def render_operation_cross_tier_comparison_graph(operation, panels, output_dir='.',
                                                graph_type='bar', show_values=False):
    """
    Create one multipanel graph per operation with a subplot for each block size.

    panels: dict metric -> {block_size -> {'workload': ..., 'rows': ...}}
            metric in {'iops', 'bandwidth'}

    Output:
      {operation}_cross_tier_comparison.png
      (and _line variant when graph_type is line/both)
    """
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError as e:
        print(f"Skipping operation cross-tier graph (matplotlib required): {e}")
        return 0

    metrics = []
    if panels.get('iops'):
        metrics.append('iops')
    if panels.get('bandwidth'):
        metrics.append('bandwidth')
    if not metrics:
        return 0

    block_sizes = sorted(
        {bs for metric in metrics for bs in panels[metric].keys()},
        key=_block_size_sort_key,
    )
    if not block_sizes:
        return 0

    graph_types = ['bar', 'line'] if graph_type == 'both' else [graph_type]
    op_display = CROSS_TIER_OP_DISPLAY.get(operation, operation)
    nrows = len(metrics)
    ncols = len(block_sizes)
    created = 0
    compact_subplot = show_values and (ncols * nrows > 4 or ncols >= 3)

    for current_type in graph_types:
        if show_values:
            fig_w = max(6.5 * ncols, 12)
            fig_h = max(5.0 * nrows, 6.5)
        else:
            fig_w = max(5.5 * ncols, 10)
            fig_h = max(4.2 * nrows, 5.5)
        fig, axes = plt.subplots(
            nrows=nrows,
            ncols=ncols,
            figsize=(fig_w, fig_h),
            squeeze=False,
        )
        fig.suptitle(
            f'{op_display} — Cross-Tier Comparison\n'
            f'(% change vs first tier — green=higher/better, red=lower)',
            fontsize=14,
            fontweight='bold',
            y=0.995,
        )

        for row_idx, metric in enumerate(metrics):
            for col_idx, block_size in enumerate(block_sizes):
                ax = axes[row_idx][col_idx]
                entry = panels.get(metric, {}).get(block_size)
                if not entry:
                    ax.set_visible(False)
                    continue
                workload = entry['workload']
                rows = entry['rows']
                bs_display = get_block_size_display_name(block_size)
                metric_label = 'IOPS' if metric == 'iops' else f'Bandwidth ({BW_RATE_UNIT})'
                subplot_title = f'{bs_display} — {metric_label}'
                twin, _, _ = _plot_cross_tier_scaling_on_axes(
                    ax,
                    rows,
                    workload,
                    graph_type=current_type,
                    show_values=show_values,
                    title=subplot_title,
                    show_legend=False,
                    xlabel=(row_idx == nrows - 1),
                    compact_subplot=compact_subplot,
                )
                if twin is None:
                    continue

        # No figure legend: left Y-axis color = Total, right Y-axis color = Avg/VM
        fig.tight_layout(rect=[0, 0, 1, 0.96])

        # Naming:
        # - bar/line alone -> {op}_cross_tier_comparison.png
        # - both -> {op}_cross_tier_comparison.png + {op}_cross_tier_comparison_line.png
        if graph_type == 'both' and current_type == 'line':
            out_name = f'{operation}_cross_tier_comparison_line.png'
        else:
            out_name = f'{operation}_cross_tier_comparison.png'

        out_path = os.path.join(output_dir, out_name)
        fig.savefig(out_path, dpi=150, bbox_inches='tight')
        plt.close(fig)
        print(f"Saved operation cross-tier graph: {out_path}")
        created += 1

    return created


def create_cross_tier_comparison(input_dirs, run_labels, output_dir='.',
                                 selected_block_sizes=None, graph_type='bar',
                                 iops_comparison=None, bw_comparison=None,
                                 show_values=False, include_iops=True, include_bw=True,
                                 update_perf_report=None):
    """
    Build Cross-Tier Comparison tables + IOPS/BW graphs for all tested
    operation × block-size combinations.

    Also creates one multipanel graph per operation with all block sizes:
      {operation}_cross_tier_comparison.png
      {operation}_bw_cross_tier_comparison.png   (when include_bw)
      {operation}_iops_cross_tier_comparison.png (when include_iops)

    And the four key cross-tier workloads:
      perf_report_key_cross_tier.md
      key_cross_tier_comparison.png
      key_cross_tier/*.png

    Cross-tier Aggregate IOPS/BW outputs (all tested op × block size) land in
    output_dir next to the other PNGs:
      perf_report_cross_tier_comparison.md
      perf_report_cross_tier_{bs}_{op}_{iops|bw}.png
      perf_report_cross_tier_{bs}_{op}_{iops|bw}_table.png
      perf_report_cross_tier_{op}_{iops|bw}_table.png   (all block sizes merged)

    With --update-perf-report PATH, patches ### 6.6 with the four key workloads only.

    Requires two or more result directories (multi-run / multi-tier compare).
    include_iops / include_bw follow --iops|--iops-mean / --bw|--bw-mean (only requested metrics).
    """
    print("\n" + "=" * 60)
    print("CROSS-TIER COMPARISON (all tested block sizes)")
    print("=" * 60)

    if not include_iops and not include_bw:
        print("Skipping cross-tier comparison (neither IOPS nor BW requested).")
        return {}

    # Only collect metrics requested via --iops|--iops-mean / --bw|--bw-mean
    if include_iops:
        if iops_comparison is None:
            iops_comparison = build_multi_run_comparison(
                input_dirs, run_labels, 'iops', selected_block_sizes
            )
    else:
        iops_comparison = None

    if include_bw:
        if bw_comparison is None:
            bw_comparison = build_multi_run_comparison(
                input_dirs, run_labels, 'bandwidth', selected_block_sizes
            )
    else:
        bw_comparison = None

    workloads = discover_cross_tier_workloads(
        iops_comparison, bw_comparison, selected_block_sizes
    )
    if not workloads:
        print("No cross-tier workloads found (no matching operation/block-size data).")
        return {}

    bs_found = sorted({w['block_size'] for w in workloads}, key=_block_size_sort_key)
    ops_found = sorted({w['operation'] for w in workloads}, key=_cross_tier_op_sort_key)
    metric_note = []
    if include_iops:
        metric_note.append('IOPS')
    if include_bw:
        metric_note.append('BW')
    print(
        f"Generating {len(workloads)} cross-tier table(s)/graph(s) "
        f"({' + '.join(metric_note)}) for "
        f"operations={ops_found}, block_sizes={bs_found}"
    )

    csv_dir = ensure_csv_directory(output_dir)
    md_path = os.path.join(output_dir, 'perf_report_cross_tier_comparison.md')
    results = {}
    # operation -> metric -> block_size -> {workload, rows}
    operation_panels = defaultdict(lambda: defaultdict(dict))
    all_panels = []

    for workload in workloads:
        rows = build_cross_tier_workload_rows(
            workload, iops_comparison, bw_comparison, run_labels
        )
        # Only keep block sizes present in EVERY tier/run (no TBD cells)
        if not cross_tier_rows_complete(rows, workload):
            missing = cross_tier_incomplete_tiers(rows, workload)
            print(
                f"Skipping incomplete cross-tier table "
                f"(missing data for tier(s): {', '.join(missing)}): {workload['title']}"
            )
            continue

        fields = list(workload['fields'])
        slug = workload['slug']
        title = workload['title']

        thr_csv = os.path.join(csv_dir, f'perf_report_cross_tier_{slug}.csv')
        # Table PNG + scaling graph both live in output_dir with other PNGs
        thr_png = os.path.join(output_dir, f'perf_report_cross_tier_{slug}_table.png')
        graph_png = os.path.join(output_dir, f'perf_report_cross_tier_{slug}.png')

        save_perf_report_table_csv(rows, thr_csv, fields)
        render_perf_report_table_png(rows, fields, title, thr_png)
        render_cross_tier_scaling_graph(
            rows, workload, graph_png, graph_type=graph_type, show_values=show_values
        )

        panel = {'workload': workload, 'rows': rows}
        all_panels.append(panel)
        operation_panels[workload['operation']][workload['metric']][workload['block_size']] = panel

        results[slug] = rows
        print(f"Cross-tier table ready: {title} ({len(rows)} tier(s))")

    # One table PNG per operation × metric with all block sizes merged
    print("\nCreating merged per-operation cross-tier table PNGs...")
    try:
        merged = render_merged_cross_tier_operation_table_pngs(
            all_panels, output_dir=output_dir
        )
        if merged:
            print(f"Merged operation table PNGs ({len(merged)}):")
            for path in merged:
                print(f"  - {os.path.basename(path)}")
        else:
            print("No merged operation table PNGs produced")
    except Exception as e:
        print(f"Error creating merged cross-tier table PNGs: {e}")

    # One multipanel PNG per operation (all block sizes as subgraphs)
    print("\nCreating per-operation cross-tier multipanel graphs...")
    for operation in sorted(operation_panels.keys(), key=_cross_tier_op_sort_key):
        try:
            render_operation_cross_tier_comparison_graph(
                operation,
                operation_panels[operation],
                output_dir=output_dir,
                graph_type=graph_type,
                show_values=show_values,
            )
        except Exception as e:
            print(f"Error creating combined cross-tier graph for {operation}: {e}")

        # Latency-style dedicated BW / IOPS graphs (with % change)
        if include_bw and operation_panels[operation].get('bandwidth'):
            try:
                n = render_operation_throughput_cross_tier_comparison_graph(
                    operation,
                    operation_panels[operation]['bandwidth'],
                    metric_family='bandwidth',
                    output_dir=output_dir,
                    graph_type=graph_type,
                    show_values=show_values,
                )
                if not n:
                    print(f"Warning: no BW cross-tier graph produced for {operation}")
            except Exception as e:
                print(f"Error creating BW cross-tier graph for {operation}: {e}")
        if include_iops and operation_panels[operation].get('iops'):
            try:
                n = render_operation_throughput_cross_tier_comparison_graph(
                    operation,
                    operation_panels[operation]['iops'],
                    metric_family='iops',
                    output_dir=output_dir,
                    graph_type=graph_type,
                    show_values=show_values,
                )
                if not n:
                    print(f"Warning: no IOPS cross-tier graph produced for {operation}")
            except Exception as e:
                print(f"Error creating IOPS cross-tier graph for {operation}: {e}")

    # Reliable fallback: rebuild from the CSVs we just wrote (requested metrics only)
    metric_families = []
    if include_bw:
        metric_families.append('bandwidth')
    if include_iops:
        metric_families.append('iops')
    print(
        "\nEnsuring {} cross-tier multipanel graphs from CSV fallback...".format(
            '/'.join('IOPS' if m == 'iops' else 'BW' for m in metric_families)
        )
    )
    render_throughput_cross_tier_graphs_from_csv(
        output_dir=output_dir,
        graph_type=graph_type,
        show_values=show_values,
        metric_families=tuple(metric_families),
    )

    # Markdown Aggregate tables for all tested block sizes (same dir as PNGs)
    multipanel_notes = ['`{operation}_cross_tier_comparison.png`']
    if include_bw:
        multipanel_notes.append('`{operation}_bw_cross_tier_comparison.png`')
    if include_iops:
        multipanel_notes.append('`{operation}_iops_cross_tier_comparison.png`')
    md_lines = format_cross_tier_markdown(
        all_panels,
        include_graph_links=True,
        image_prefix='',
        heading='### Cross-Tier Comparison',
        intro=None,
        graph_subdir='',
        graph_name_template='perf_report_cross_tier_{slug}.png',
        table_png_template='perf_report_cross_tier_{slug}_table.png',
        summary_graph=None,
    )
    # Insert notes after intro
    insert_at = 4 if len(md_lines) > 4 else len(md_lines)
    note_block = [
        f'_Per-operation multipanel graphs: {", ".join(multipanel_notes)}._',
        '',
        '_Scaling graphs and per-block-size table PNGs are in this same directory._',
        '',
        '_Merged all-block-size table PNGs: '
        '`perf_report_cross_tier_{operation}_{iops|bw}_table.png`._',
        '',
        '_Key cross-tier tables/graphs: `perf_report_key_cross_tier.md`, '
        '`key_cross_tier_comparison.png`._',
        '',
    ]
    md_lines = md_lines[:insert_at] + note_block + md_lines[insert_at:]
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(md_lines) + '\n')
    print(f"Saved cross-tier markdown: {md_path}")
    print(
        f"Wrote {len(all_panels)} Aggregate IOPS/BW table(s) + PNGs under: {output_dir}"
    )
    if include_bw:
        bw_pngs = sorted(
            f for f in os.listdir(output_dir)
            if f.endswith('_bw_cross_tier_comparison.png')
        )
        print(f"BW cross-tier graphs ({len(bw_pngs)}): {', '.join(bw_pngs) or '(none)'}")
    if include_iops:
        iops_pngs = sorted(
            f for f in os.listdir(output_dir)
            if f.endswith('_iops_cross_tier_comparison.png')
        )
        print(f"IOPS cross-tier graphs ({len(iops_pngs)}): {', '.join(iops_pngs) or '(none)'}")

    # Key cross-tier workloads (4 tables + summary graph; optional key-only report patch)
    write_key_cross_tier_outputs(
        iops_comparison=iops_comparison,
        bw_comparison=bw_comparison,
        run_labels=run_labels,
        output_dir=output_dir,
        graph_type=graph_type,
        show_values=show_values,
        update_perf_report=update_perf_report,
    )

    print(f"Cross-tier comparison written under: {output_dir}")
    return results


if __name__ == "__main__":
    main()
