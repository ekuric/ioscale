#!/usr/bin/env python3
"""
MSSQL HammerDB TPM Graph Generator

Reads HammerDB result files that include lines like:
  TEST RESULT : System achieved 256825 NOPM from 596387 SQL Server TPM

Generates:
- Per-user graphs (TPM vs machine)
- Combined graph with all user counts on one plot
"""

import argparse
import json
import math
import os
import re
from collections import defaultdict
from datetime import datetime
from typing import Optional, Union

import matplotlib.pyplot as plt


def get_machine_number(machine_name: str) -> int:
    """
    Extract machine number from directory name like 'vm-1', 'vm-10', etc.
    If it's an IP address or no number, return 0 (caller assigns order).
    """
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", machine_name):
        return 0
    match = re.search(r"vm-(\d+)", machine_name)
    if match:
        return int(match.group(1))
    match = re.search(r"(\d+)", machine_name)
    if match:
        return int(match.group(1))
    return 0


def extract_user_count(filename: str) -> int:
    """
    Extract user count from filename patterns like:
      mssqls_tprocc_010vu_run1.json -> 10
    """
    match = re.search(r"_(\d+)vu", filename.lower())
    if match:
        return int(match.group(1))
    match = re.search(r"_(\d+)_users", filename.lower())
    if match:
        return int(match.group(1))
    match = re.search(r"_(\d+)\.json$", filename.lower())
    if match:
        return int(match.group(1))
    return 0


def extract_tpm(content: str) -> Optional[int]:
    """
    Extract SQL Server TPM from HammerDB result text.
    """
    patterns = [
        r"TEST RESULT\s*:\s*System achieved\s+\d+\s+NOPM\s+from\s+(\d+)\s+SQL Server TPM",
        r"System achieved\s+(\d+)\s+SQL Server TPM\s+at\s+\d+\s+NOPM",
        r"System achieved\s+\d+\s+NOPM\s+from\s+(\d+)\s+SQL Server TPM",
    ]
    for pattern in patterns:
        match = re.search(pattern, content, re.IGNORECASE)
        if match:
            return int(match.group(1))

    for line in content.splitlines():
        if "SQL Server TPM" not in line:
            continue
        match = re.search(r"from\s+(\d+)\s+SQL Server TPM", line, re.IGNORECASE)
        if not match:
            match = re.search(r"achieved\s+(\d+)\s+SQL Server TPM", line, re.IGNORECASE)
        if match:
            return int(match.group(1))

    return None


def build_machine_labels(machine_order):
    return [str(idx) for idx in range(1, len(machine_order) + 1)]


def format_machine_count(machine_count: int) -> str:
    suffix = "machine" if machine_count == 1 else "machines"
    return f"{machine_count} test {suffix}"


def machine_count_from_data(data) -> int:
    return len({machine for values in data.values() for machine in values})


def extract_transaction_counts(content: str) -> list[tuple[str, int]]:
    """
    Extract TRANSACTION COUNT series from HammerDB JSON output.
    Returns list of (time_label, tpm) sorted by timestamp.
    """
    tpm_series = None
    try:
        payload = json.loads(content)
        if isinstance(payload, dict):
            for key, value in payload.items():
                if isinstance(value, dict) and "tpm" in key.lower():
                    tpm_series = value
                    break
    except json.JSONDecodeError:
        match = re.search(
            r"TRANSACTION COUNT\s*(\{.*?\})\s*HAMMERDB RESULT",
            content,
            re.DOTALL | re.IGNORECASE,
        )
        if match:
            try:
                payload = json.loads(match.group(1))
                for key, value in payload.items():
                    if isinstance(value, dict) and "tpm" in key.lower():
                        tpm_series = value
                        break
            except json.JSONDecodeError:
                return []
    if not tpm_series:
        return []

    rows = []
    for timestamp, tpm_val in tpm_series.items():
        try:
            tpm_int = int(str(tpm_val))
        except ValueError:
            continue
        try:
            dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
            time_label = dt.strftime("%H:%M:%S")
        except ValueError:
            time_label = timestamp
        rows.append((timestamp, time_label, tpm_int))

    rows.sort(key=lambda row: row[0])
    return [(time_label, tpm_int) for _, time_label, tpm_int in rows]


def read_results(input_dir: str):
    """
    Walk input_dir and extract TPM per machine per user count.
    Returns: data[user_count][machine_name] = tpm
    """
    data = defaultdict(dict)
    machine_names = set()
    files_found = 0

    for root, _, files in os.walk(input_dir):
        for filename in files:
            if not filename.lower().endswith(".json"):
                continue
            file_path = os.path.join(root, filename)
            files_found += 1
            machine_name = os.path.basename(root)
            user_count = extract_user_count(filename)
            if user_count == 0:
                continue
            try:
                content = read_text_file(file_path)
                tpm = extract_tpm(content)
                if tpm is None:
                    continue
                data[user_count][machine_name] = tpm
                machine_names.add(machine_name)
            except Exception as exc:
                print(f"Warning: failed to read {file_path}: {exc}")

    return data, sorted(machine_names, key=lambda name: (get_machine_number(name) or 10**9, name)), files_found


def read_transaction_series(input_dir: str):
    """
    Walk input_dir and extract TRANSACTION COUNT per machine per user count.
    Returns: series[user_count][machine_name] = list[(time_label, tpm)]
    """
    series = defaultdict(dict)
    machine_names = set()
    files_found = 0

    for root, _, files in os.walk(input_dir):
        for filename in files:
            if not filename.lower().endswith(".json"):
                continue
            file_path = os.path.join(root, filename)
            files_found += 1
            machine_name = os.path.basename(root)
            user_count = extract_user_count(filename)
            if user_count == 0:
                continue
            try:
                content = read_text_file(file_path)
                tcount_series = extract_transaction_counts(content)
                if not tcount_series:
                    continue
                series[user_count][machine_name] = tcount_series
                machine_names.add(machine_name)
            except Exception as exc:
                print(f"Warning: failed to read {file_path}: {exc}")

    return series, sorted(machine_names, key=lambda name: (get_machine_number(name) or 10**9, name)), files_found


def merge_machine_order(primary_order, fallback_order):
    merged = []
    seen = set()
    for name in primary_order + fallback_order:
        if name not in seen:
            merged.append(name)
            seen.add(name)
    return merged


def peak_from_series(series):
    values = [value for _, value in series]
    return max(values) if values else 0


def read_text_file(file_path: str) -> str:
    """
    Read a text file that may be UTF-8 or UTF-16LE.
    """
    with open(file_path, "rb") as f:
        raw = f.read()
    if b"\x00" in raw:
        try:
            return raw.decode("utf-16le", errors="ignore")
        except Exception:
            return raw.decode("utf-16", errors="ignore")
    return raw.decode("utf-8", errors="ignore")


def format_tpm_value(value: Union[int, float]) -> str:
    if value >= 1000000:
        return f"{value/1000000:.1f}M"
    if value >= 1000:
        return f"{value/1000:.1f}K"
    return str(value)


def apply_y_axis_scale(ax, y_values):
    if not y_values:
        return
    y_max = max(y_values)
    if y_max <= 0:
        ax.set_ylim(0, 1)
        ax.set_yticks([0, 1])
        ax.axhline(0, color="black", linewidth=0.8, alpha=0.6)
        return
    desired_ticks = 6
    raw_step = y_max / (desired_ticks - 1)
    magnitude = 10 ** math.floor(math.log10(raw_step))
    nice_steps = [1, 2, 5, 10]
    step = nice_steps[-1] * magnitude
    for candidate in nice_steps:
        candidate_step = candidate * magnitude
        if raw_step <= candidate_step:
            step = candidate_step
            break
    upper = math.ceil(y_max / step) * step
    ax.set_ylim(0, upper)
    ticks = []
    current = 0.0
    max_ticks = 200
    while current <= upper + (step * 0.5) and len(ticks) < max_ticks:
        ticks.append(current)
        current += step
    ax.set_yticks(ticks)
    ax.axhline(0, color="black", linewidth=0.8, alpha=0.6)


def add_value_label_padding(ax, y_values, padding_ratio=0.15):
    if not y_values:
        return
    _, y_max = ax.get_ylim()
    if y_max <= 0:
        return
    ax.set_ylim(0, y_max * (1 + padding_ratio))


def plot_per_user(data, machine_order, output_dir, chart_type="line", show_values=False):
    for user_count in sorted(data.keys()):
        machine_values = data[user_count]
        x_vals = []
        y_vals = []
        for idx, machine in enumerate(machine_order, start=1):
            if machine in machine_values:
                x_vals.append(idx)
                y_vals.append(machine_values[machine])
        labels = build_machine_labels(machine_order)

        if not y_vals:
            continue

        plt.figure(figsize=(14, 7))
        if chart_type == "bar":
            bars = plt.bar(x_vals, y_vals, alpha=0.8, width=0.7)
            if show_values:
                max_val = max(y_vals)
                for bar, value in zip(bars, y_vals):
                    plt.text(
                        bar.get_x() + bar.get_width() / 2,
                        bar.get_height() + max_val * 0.01,
                        format_tpm_value(value),
                        ha="center",
                        va="bottom",
                        fontsize=9,
                        fontweight="bold",
                    )
        elif chart_type == "scatter":
            plt.plot(x_vals, y_vals, linewidth=1.5, alpha=0.7)
            plt.scatter(x_vals, y_vals, s=35, alpha=0.8)
            if show_values:
                max_val = max(y_vals)
                for x, y in zip(x_vals, y_vals):
                    plt.text(x, y + max_val * 0.01, format_tpm_value(y), ha="center", va="bottom", fontsize=9)
        else:
            plt.plot(x_vals, y_vals, marker="o", linewidth=2, markersize=6)
            if show_values:
                max_val = max(y_vals)
                for x, y in zip(x_vals, y_vals):
                    plt.text(x, y + max_val * 0.01, format_tpm_value(y), ha="center", va="bottom", fontsize=9)

        avg_val = sum(y_vals) / len(y_vals)
        plt.axhline(
            avg_val,
            color="#555555",
            linestyle="--",
            linewidth=1,
            alpha=0.7,
            label=f"Avg ({format_tpm_value(avg_val)})",
        )

        plt.xlabel("Machines", fontsize=12, fontweight="bold")
        plt.ylabel("TPM (SQL Server TPM)", fontsize=12, fontweight="bold")
        if y_vals:
            apply_y_axis_scale(plt.gca(), y_vals)
            if show_values:
                add_value_label_padding(plt.gca(), y_vals)
        plt.title(
            f"{format_machine_count(len(machine_order))} - MSSQL TPM - {user_count} Users",
            fontsize=15,
            fontweight="bold",
        )
        plt.grid(True, alpha=0.3)

        if len(machine_order) <= 20:
            plt.xticks(range(1, len(machine_order) + 1), labels, rotation=45, ha="right")
        else:
            plt.xticks(range(1, len(machine_order) + 1), rotation=45, ha="right")

        plt.legend(bbox_to_anchor=(1.02, 1), loc="upper left", fontsize=9)
        plt.tight_layout()
        output_path = os.path.join(output_dir, f"mssql_tpm_{user_count}_users_{chart_type}.png")
        plt.savefig(output_path, dpi=300, bbox_inches="tight")
        plt.close()
        print(f"Saved: {output_path}")


def plot_combined(data, machine_order, output_dir, chart_type="line", show_values=False):
    if not data:
        return

    if chart_type == "bar":
        return

    plt.figure(figsize=(15, 8))
    colors = [
        "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
        "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
    ]
    markers = ["o", "s", "^", "D", "v", "p", "*", "h", "H", "x"]

    for idx, user_count in enumerate(sorted(data.keys())):
        machine_values = data[user_count]
        x_vals = []
        y_vals = []
        for i, machine in enumerate(machine_order, start=1):
            if machine in machine_values:
                x_vals.append(i)
                y_vals.append(machine_values[machine])
        if not y_vals:
            continue
        color = colors[idx % len(colors)]
        marker = markers[idx % len(markers)]

        if chart_type == "scatter":
            plt.plot(x_vals, y_vals, linewidth=1.5, alpha=0.7, color=color)
            plt.scatter(x_vals, y_vals, s=35, alpha=0.8, label=f"{user_count} users", color=color, marker=marker)
        else:
            plt.plot(x_vals, y_vals, marker=marker, linewidth=2, markersize=6, label=f"{user_count} users", color=color)

        if show_values:
            max_val = max(y_vals)
            for x, y in zip(x_vals, y_vals):
                plt.text(x, y + max_val * 0.01, format_tpm_value(y), ha="center", va="bottom", fontsize=8)

    plt.xlabel("Machines", fontsize=12, fontweight="bold")
    plt.ylabel("TPM (SQL Server TPM)", fontsize=12, fontweight="bold")
    plt.title(
        f"{format_machine_count(len(machine_order))} - MSSQL TPM - All User Counts",
        fontsize=16,
        fontweight="bold",
    )
    plt.grid(True, alpha=0.3)
    apply_y_axis_scale(plt.gca(), [value for user in data.values() for value in user.values()])
    if show_values:
        add_value_label_padding(plt.gca(), [value for user in data.values() for value in user.values()])

    if len(machine_order) <= 20:
        plt.xticks(range(1, len(machine_order) + 1), build_machine_labels(machine_order), rotation=45, ha="right")
    else:
        plt.xticks(range(1, len(machine_order) + 1), rotation=45, ha="right")

    plt.legend(bbox_to_anchor=(1.02, 1), loc="upper left", fontsize=9)
    plt.tight_layout()
    output_path = os.path.join(output_dir, f"mssql_tpm_combined_{chart_type}.png")
    plt.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close()
    print(f"Saved: {output_path}")


def plot_combined_average(data, output_dir, chart_type="line", show_values=False):
    if not data:
        return

    user_counts = sorted(data.keys())
    avg_tpms = []
    for user_count in user_counts:
        values = list(data[user_count].values())
        avg_tpms.append(sum(values) / len(values) if values else 0)

    plt.figure(figsize=(12, 7))
    if chart_type == "bar":
        x_positions = list(range(len(user_counts)))
        bars = plt.bar(x_positions, avg_tpms, alpha=0.8, color="#1f77b4")
        if show_values and avg_tpms:
            max_val = max(avg_tpms)
            for bar, value in zip(bars, avg_tpms):
                plt.text(
                    bar.get_x() + bar.get_width() / 2,
                    bar.get_height() + max_val * 0.01,
                    format_tpm_value(value),
                    ha="center",
                    va="bottom",
                    fontsize=9,
                    fontweight="bold",
                )
    elif chart_type == "scatter":
        plt.plot(user_counts, avg_tpms, linewidth=1.5, alpha=0.7, color="#1f77b4")
        plt.scatter(user_counts, avg_tpms, s=35, alpha=0.8, color="#1f77b4")
        if show_values and avg_tpms:
            max_val = max(avg_tpms)
            for x, y in zip(user_counts, avg_tpms):
                plt.text(x, y + max_val * 0.01, format_tpm_value(y), ha="center", va="bottom", fontsize=9)
    else:
        plt.plot(user_counts, avg_tpms, marker="o", linewidth=2, markersize=6, color="#1f77b4")
        if show_values and avg_tpms:
            max_val = max(avg_tpms)
            for x, y in zip(user_counts, avg_tpms):
                plt.text(x, y + max_val * 0.01, format_tpm_value(y), ha="center", va="bottom", fontsize=9)

    plt.xlabel("Users", fontsize=12, fontweight="bold")
    plt.ylabel("Average TPM (SQL Server TPM)", fontsize=12, fontweight="bold")
    plt.title(
        f"{format_machine_count(machine_count_from_data(data))} - "
        "MSSQL TPM - Average by User Count",
        fontsize=16,
        fontweight="bold",
    )
    if chart_type == "bar":
        plt.xticks(x_positions, [str(u) for u in user_counts])
    else:
        plt.xticks(user_counts, [str(u) for u in user_counts])
    plt.grid(True, alpha=0.3, axis="y")
    apply_y_axis_scale(plt.gca(), avg_tpms)
    if show_values and avg_tpms:
        add_value_label_padding(plt.gca(), avg_tpms)
    plt.tight_layout()
    output_path = os.path.join(output_dir, f"mssql_tpm_combined_average_{chart_type}.png")
    plt.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close()
    print(f"Saved: {output_path}")


def plot_total_tpm(data, output_dir, chart_type="line", show_values=False, selected_users=None):
    if not data:
        return

    user_counts = sorted(data.keys())
    if selected_users:
        user_counts = [u for u in user_counts if u in selected_users]
    if not user_counts:
        return

    total_tpms = []
    for user_count in user_counts:
        values = list(data[user_count].values())
        total_tpms.append(sum(values) if values else 0)

    plt.figure(figsize=(12, 7))
    if chart_type == "bar":
        x_positions = list(range(len(user_counts)))
        bars = plt.bar(x_positions, total_tpms, alpha=0.8, color="#1f77b4")
        if show_values and total_tpms:
            max_val = max(total_tpms)
            for bar, value in zip(bars, total_tpms):
                plt.text(
                    bar.get_x() + bar.get_width() / 2,
                    bar.get_height() + max_val * 0.01,
                    format_tpm_value(value),
                    ha="center",
                    va="bottom",
                    fontsize=9,
                    fontweight="bold",
                )
    elif chart_type == "scatter":
        plt.plot(user_counts, total_tpms, linewidth=1.5, alpha=0.7, color="#1f77b4")
        plt.scatter(user_counts, total_tpms, s=35, alpha=0.8, color="#1f77b4")
        if show_values and total_tpms:
            max_val = max(total_tpms)
            for x, y in zip(user_counts, total_tpms):
                plt.text(x, y + max_val * 0.01, format_tpm_value(y), ha="center", va="bottom", fontsize=9)
    else:
        plt.plot(user_counts, total_tpms, marker="o", linewidth=2, markersize=6, color="#1f77b4")
        if show_values and total_tpms:
            max_val = max(total_tpms)
            for x, y in zip(user_counts, total_tpms):
                plt.text(x, y + max_val * 0.01, format_tpm_value(y), ha="center", va="bottom", fontsize=9)

    plt.xlabel("Users", fontsize=12, fontweight="bold")
    plt.ylabel("Total TPM (SQL Server TPM)", fontsize=12, fontweight="bold")
    plt.title(
        f"{format_machine_count(machine_count_from_data(data))} - MSSQL TPM - Total by User Count",
        fontsize=16,
        fontweight="bold",
    )
    if chart_type == "bar":
        plt.xticks(x_positions, [str(u) for u in user_counts])
    else:
        plt.xticks(user_counts, [str(u) for u in user_counts])
    plt.grid(True, alpha=0.3, axis="y")
    apply_y_axis_scale(plt.gca(), total_tpms)
    if show_values and total_tpms:
        add_value_label_padding(plt.gca(), total_tpms)
    plt.tight_layout()
    output_path = os.path.join(output_dir, f"mssql_tpm_total_{chart_type}.png")
    plt.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close()
    print(f"Saved: {output_path}")

def plot_combined_selected(data, machine_order, output_dir, chart_type, show_values, selected_users):
    filtered_users = [u for u in sorted(data.keys()) if u in selected_users]
    if not filtered_users:
        print(f"No matching user counts found for combine-users: {sorted(selected_users)}")
        return

    if chart_type == "bar":
        avg_tpms = []
        for user_count in filtered_users:
            values = list(data[user_count].values())
            avg_tpms.append(sum(values) / len(values) if values else 0)

        plt.figure(figsize=(12, 7))
        x_positions = list(range(len(filtered_users)))
        bars = plt.bar(x_positions, avg_tpms, alpha=0.8, color="#1f77b4")
        if show_values and avg_tpms:
            max_val = max(avg_tpms)
            for bar, value in zip(bars, avg_tpms):
                plt.text(
                    bar.get_x() + bar.get_width() / 2,
                    bar.get_height() + max_val * 0.01,
                    format_tpm_value(value),
                    ha="center",
                    va="bottom",
                    fontsize=9,
                    fontweight="bold",
                )

        plt.xlabel("Users", fontsize=12, fontweight="bold")
        plt.ylabel("Average TPM (SQL Server TPM)", fontsize=12, fontweight="bold")
        plt.title(
            f"{format_machine_count(len(machine_order))} - MSSQL TPM - Selected Users",
            fontsize=16,
            fontweight="bold",
        )
        plt.grid(True, alpha=0.3, axis="y")
        plt.xticks(x_positions, [str(u) for u in filtered_users])
        apply_y_axis_scale(plt.gca(), avg_tpms)
        if show_values and avg_tpms:
            add_value_label_padding(plt.gca(), avg_tpms)
        plt.tight_layout()
        output_path = os.path.join(output_dir, f"mssql_tpm_combined_user-{'-'.join(str(u) for u in filtered_users)}.png")
        plt.savefig(output_path, dpi=300, bbox_inches="tight")
        plt.close()
        print(f"Saved: {output_path}")
        return

    plt.figure(figsize=(15, 8))
    colors = [
        "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
        "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
    ]
    markers = ["o", "s", "^", "D", "v", "p", "*", "h", "H", "x"]

    for idx, user_count in enumerate(filtered_users):
        machine_values = data[user_count]
        x_vals = []
        y_vals = []
        for i, machine in enumerate(machine_order, start=1):
            if machine in machine_values:
                x_vals.append(i)
                y_vals.append(machine_values[machine])
        if not y_vals:
            continue
        color = colors[idx % len(colors)]
        marker = markers[idx % len(markers)]
        if chart_type == "scatter":
            plt.plot(x_vals, y_vals, linewidth=1.5, alpha=0.7, color=color)
            plt.scatter(x_vals, y_vals, s=35, alpha=0.8, label=f"{user_count} users", color=color, marker=marker)
        else:
            plt.plot(x_vals, y_vals, marker=marker, linewidth=2, markersize=6, label=f"{user_count} users", color=color)

        avg_val = sum(y_vals) / len(y_vals)
        plt.axhline(
            avg_val,
            color=color,
            linestyle="--",
            linewidth=1,
            alpha=0.7,
            label=f"Avg {user_count} users ({format_tpm_value(avg_val)})",
        )
        if show_values:
            max_val = max(y_vals)
            for x, y in zip(x_vals, y_vals):
                plt.text(x, y + max_val * 0.01, format_tpm_value(y), ha="center", va="bottom", fontsize=8)

    plt.xlabel("Machines", fontsize=12, fontweight="bold")
    plt.ylabel("TPM (SQL Server TPM)", fontsize=12, fontweight="bold")
    plt.title(
        f"{format_machine_count(len(machine_order))} - MSSQL TPM - Selected Users",
        fontsize=16,
        fontweight="bold",
    )
    plt.grid(True, alpha=0.3)
    apply_y_axis_scale(plt.gca(), [value for user in filtered_users for value in data[user].values()])
    if show_values:
        add_value_label_padding(plt.gca(), [value for user in filtered_users for value in data[user].values()])

    if len(machine_order) <= 20:
        plt.xticks(range(1, len(machine_order) + 1), build_machine_labels(machine_order), rotation=45, ha="right")
    else:
        plt.xticks(range(1, len(machine_order) + 1), rotation=45, ha="right")

    plt.legend(bbox_to_anchor=(1.02, 1), loc="upper left", fontsize=9)
    plt.tight_layout()
    output_path = os.path.join(output_dir, f"mssql_tpm_combined_user-{'-'.join(str(u) for u in filtered_users)}.png")
    plt.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close()
    print(f"Saved: {output_path}")


def plot_vm_timeseries(series, machine_order, output_dir):
    colors = [
        "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
        "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
    ]
    for machine in machine_order:
        has_data = any(machine in series[user] for user in series)
        if not has_data:
            continue
        plt.figure(figsize=(16, 7))
        for idx, user_count in enumerate(sorted(series.keys())):
            if machine not in series[user_count]:
                continue
            times, values = zip(*series[user_count][machine])
            plt.plot(times, values, linewidth=2, label=f"{user_count} users", color=colors[idx % len(colors)])
        plt.xlabel("")
        plt.ylabel("TPM (Transaction Count)", fontsize=12, fontweight="bold")
        plt.title(
            f"{format_machine_count(len(machine_order))} - MSSQL TPM Time Series - {machine}",
            fontsize=14,
            fontweight="bold",
        )
        plt.grid(True, alpha=0.3)
        plt.xticks([])
        apply_y_axis_scale(plt.gca(), [value for user in series for _, value in series[user].get(machine, [])])
        plt.legend(bbox_to_anchor=(1.02, 1), loc="upper left", fontsize=9)
        plt.tight_layout()
        output_path = os.path.join(output_dir, f"mssql_tpm_timeseries_{machine}.png")
        plt.savefig(output_path, dpi=300, bbox_inches="tight")
        plt.close()
        print(f"Saved: {output_path}")


def plot_combined_timeseries(series, machine_order, output_dir):
    colors = [
        "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
        "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
    ]
    for user_count in sorted(series.keys()):
        plt.figure(figsize=(16, 7))
        for idx, machine in enumerate(machine_order):
            if machine not in series[user_count]:
                continue
            times, values = zip(*series[user_count][machine])
            plt.plot(times, values, linewidth=2, label=machine, color=colors[idx % len(colors)])
        plt.xlabel("")
        plt.ylabel("TPM (Transaction Count)", fontsize=12, fontweight="bold")
        plt.title(
            f"{format_machine_count(len(machine_order))} - MSSQL TPM Time Series - "
            f"{user_count} Users (All Machines)",
            fontsize=14,
            fontweight="bold",
        )
        plt.grid(True, alpha=0.3)
        plt.xticks([])
        apply_y_axis_scale(plt.gca(), [value for machine in series[user_count].values() for _, value in machine])
        plt.legend(bbox_to_anchor=(1.02, 1), loc="upper left", fontsize=9)
        plt.tight_layout()
        output_path = os.path.join(output_dir, f"mssql_tpm_timeseries_{user_count}_users_all.png")
        plt.savefig(output_path, dpi=300, bbox_inches="tight")
        plt.close()
        print(f"Saved: {output_path}")


def build_run_label(input_dir: str, machine_count: int) -> str:
    label = os.path.basename(os.path.normpath(input_dir)) or input_dir
    return f"Average: {label}"


def plot_compare_runs(compare_runs, output_dir, chart_type="line", show_values=False, all_user_counts=None, output_path=None):
    if not compare_runs:
        return

    plt.figure(figsize=(15, 8))
    colors = [
        "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
        "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
    ]
    markers = ["o", "s", "^", "D", "v", "p", "*", "h", "H", "x"]

    all_values = []
    if all_user_counts:
        base_users = list(all_user_counts)
    else:
        base_users = sorted({u for run in compare_runs for u in run["avg"].keys()})

    for idx, run in enumerate(compare_runs):
        user_counts = sorted(run["avg"].keys())
        avg_tpms = [run["avg"][u] for u in user_counts]
        if not avg_tpms:
            continue
        color = colors[idx % len(colors)]
        marker = markers[idx % len(markers)]
        label = build_run_label(run["input_dir"], run["machine_count"])
        if chart_type == "scatter":
            plt.plot(user_counts, avg_tpms, linewidth=1.5, alpha=0.7, color=color)
            plt.scatter(user_counts, avg_tpms, s=35, alpha=0.8, color=color, marker=marker, label=label)
        elif chart_type == "bar":
            aligned_values = [run["avg"].get(u, 0) for u in base_users]
            base_positions = list(range(len(base_users)))
            offset = (idx - (len(compare_runs) - 1) / 2) * 0.15
            bar_positions = [pos + offset for pos in base_positions]
            plt.bar(bar_positions, aligned_values, alpha=0.6, width=0.12, color=color, label=label)
        else:
            plt.plot(user_counts, avg_tpms, marker=marker, linewidth=2, markersize=6, color=color, label=label)
        if show_values and avg_tpms:
            max_val = max(avg_tpms)
            for x, y in zip(user_counts, avg_tpms):
                plt.text(x, y + max_val * 0.01, format_tpm_value(y), ha="center", va="bottom", fontsize=8)
        all_values.extend(avg_tpms)

    plt.xlabel("Users", fontsize=12, fontweight="bold")
    plt.ylabel("Average TPM (SQL Server TPM)", fontsize=12, fontweight="bold")
    plt.title("MSSQL TPM - Compare Runs", fontsize=16, fontweight="bold")
    plt.grid(True, alpha=0.3)
    apply_y_axis_scale(plt.gca(), all_values)
    if show_values and all_values:
        add_value_label_padding(plt.gca(), all_values)
    if chart_type == "bar":
        plt.xticks(range(len(base_users)), [str(u) for u in base_users])
    elif all_user_counts:
        plt.xticks(all_user_counts, [str(u) for u in all_user_counts])
    plt.legend(bbox_to_anchor=(1.02, 1), loc="upper left", fontsize=9)
    plt.tight_layout()
    if not output_path:
        output_path = os.path.join(output_dir, "mssql_tpm_compare_combined.png")
    plt.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close()
    print(f"Saved: {output_path}")


def plot_compare_runs_total(compare_runs, output_dir, chart_type="line", show_values=False, all_user_counts=None, output_path=None):
    plt.figure(figsize=(12, 7))
    colors = [
        "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
        "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
    ]
    markers = ["o", "s", "^", "D", "v", "p", "*", "h", "H", "x"]
    all_values = []

    if all_user_counts:
        base_users = list(all_user_counts)
    else:
        base_users = sorted({u for run in compare_runs for u in run["total"].keys()})

    for idx, run in enumerate(compare_runs):
        user_counts = sorted(run["total"].keys())
        totals = [run["total"][user] for user in user_counts]
        color = colors[idx % len(colors)]
        marker = markers[idx % len(markers)]
        label = os.path.basename(run["input_dir"])

        if chart_type == "bar":
            aligned_values = [run["total"].get(u, 0) for u in base_users]
            base_positions = list(range(len(base_users)))
            offset = (idx - (len(compare_runs) - 1) / 2) * 0.15
            bar_positions = [pos + offset for pos in base_positions]
            plt.bar(bar_positions, aligned_values, width=0.12, alpha=0.8, label=label, color=color)
        elif chart_type == "scatter":
            plt.plot(user_counts, totals, linewidth=1.5, alpha=0.7, color=color)
            plt.scatter(user_counts, totals, s=35, alpha=0.8, label=label, color=color, marker=marker)
        else:
            plt.plot(user_counts, totals, marker=marker, linewidth=2, markersize=6, label=label, color=color)

        if show_values and totals:
            max_val = max(totals)
            for x, y in zip(user_counts, totals):
                plt.text(x, y + max_val * 0.01, format_tpm_value(y), ha="center", va="bottom", fontsize=8)
        all_values.extend(totals)

    plt.xlabel("Users", fontsize=12, fontweight="bold")
    plt.ylabel("Total TPM (SQL Server TPM)", fontsize=12, fontweight="bold")
    plt.title("MSSQL TPM - Compare Runs (Total)", fontsize=16, fontweight="bold")
    plt.grid(True, alpha=0.3)
    apply_y_axis_scale(plt.gca(), all_values)
    if show_values and all_values:
        add_value_label_padding(plt.gca(), all_values)
    if chart_type == "bar":
        plt.xticks(range(len(base_users)), [str(u) for u in base_users])
    elif all_user_counts:
        plt.xticks(all_user_counts, [str(u) for u in all_user_counts])
    plt.legend(bbox_to_anchor=(1.02, 1), loc="upper left", fontsize=9)
    plt.tight_layout()
    if not output_path:
        output_path = os.path.join(output_dir, "mssql_tpm_compare_combined_total.png")
    plt.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close()
    print(f"Saved: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Generate MSSQL TPM graphs from HammerDB result files")
    parser.add_argument("--input-dir", required=True, nargs="+",
                        help="Input directory with per-machine subfolders (or multiple dirs for --compare)")
    parser.add_argument("--output-dir", default="mssql_tpm_graphs", help="Output directory for graphs")
    parser.add_argument("--chart-type", choices=["line", "scatter", "bar"], default="line",
                        help="Chart type to generate")
    parser.add_argument("--show-values", action="store_true", help="Show TPM value labels on graphs")
    parser.add_argument("--combine-users", default=None,
                        help="Comma-separated user counts to combine into one graph (e.g. 100,500,1000)")
    parser.add_argument("--compare", action="store_true",
                        help="Compare multiple input dirs on one combined graph")
    args = parser.parse_args()

    input_dirs = args.input_dir
    if args.compare and len(input_dirs) < 2:
        print("Error: --compare requires at least two --input-dir values")
        return 1
    for input_dir in input_dirs:
        if not os.path.exists(input_dir):
            print(f"Error: input directory not found: {input_dir}")
            return 1

    os.makedirs(args.output_dir, exist_ok=True)
    selected_users = None
    if args.combine_users:
        try:
            selected_users = {int(v.strip()) for v in args.combine_users.split(",") if v.strip()}
        except ValueError:
            print(f"Invalid --combine-users value: {args.combine_users}")
            return 1

    if args.compare:
        compare_runs = []
        all_user_counts = set()
        for input_dir in input_dirs:
            data, machine_order, files_found = read_results(input_dir)
            if files_found == 0:
                print(f"No .json files found under input directory: {input_dir}")
                continue
            series, series_machine_order, _ = read_transaction_series(input_dir)
            merged_machine_order = merge_machine_order(machine_order, series_machine_order)
            if series:
                for user_count, machine_series in series.items():
                    for machine_name, time_series in machine_series.items():
                        if user_count not in data or machine_name not in data[user_count]:
                            data[user_count][machine_name] = peak_from_series(time_series)
            avg_tpms = {}
            total_tpms = {}
            for user_count in sorted(data.keys()):
                values = list(data[user_count].values())
                if values:
                    avg_tpms[user_count] = sum(values) / len(values)
                    total_tpms[user_count] = sum(values)
            if selected_users:
                avg_tpms = {u: v for u, v in avg_tpms.items() if u in selected_users}
                total_tpms = {u: v for u, v in total_tpms.items() if u in selected_users}
            all_user_counts.update(avg_tpms.keys())
            compare_runs.append({
                "input_dir": input_dir,
                "avg": avg_tpms,
                "total": total_tpms,
                "machine_count": len(merged_machine_order),
            })

        if not compare_runs:
            print("No input directories with usable data for --compare")
            return 1
        if selected_users:
            selected_label = "_".join(str(u) for u in sorted(selected_users))
            compare_output = os.path.join(
                args.output_dir,
                f"mssql_tpm_compare_combined_users_{selected_label}.png",
            )
        else:
            compare_output = os.path.join(args.output_dir, "mssql_tpm_compare_combined.png")
        plot_compare_runs(
            compare_runs,
            args.output_dir,
            args.chart_type,
            args.show_values,
            all_user_counts=sorted(all_user_counts),
            output_path=compare_output,
        )
        compare_total_output = os.path.join(args.output_dir, "mssql_tpm_compare_combined_total.png")
        plot_compare_runs_total(
            compare_runs,
            args.output_dir,
            args.chart_type,
            args.show_values,
            all_user_counts=sorted(all_user_counts),
            output_path=compare_total_output,
        )
        return 0

    data, machine_order, files_found = read_results(input_dirs[0])
    if files_found == 0:
        print("No .json files found under input directory")
        return 1
    series, series_machine_order, _ = read_transaction_series(input_dirs[0])
    merged_machine_order = merge_machine_order(machine_order, series_machine_order)
    all_user_counts = sorted(set(data.keys()) | set(series.keys()))

    if not data and not series:
        print("No TPM results or TRANSACTION COUNT series found in input directory")
        return 1

    print(f"Machines found: {len(merged_machine_order)}")
    print(f"User counts found: {all_user_counts}")

    if series:
        for user_count, machine_series in series.items():
            for machine_name, time_series in machine_series.items():
                if user_count not in data or machine_name not in data[user_count]:
                    data[user_count][machine_name] = peak_from_series(time_series)

    if data:
        plot_per_user(data, merged_machine_order, args.output_dir, args.chart_type, args.show_values)
        if selected_users:
            plot_combined_selected(data, merged_machine_order, args.output_dir, args.chart_type, args.show_values, selected_users)
        else:
            plot_combined(data, merged_machine_order, args.output_dir, args.chart_type, args.show_values)
        plot_combined_average(data, args.output_dir, args.chart_type, args.show_values)
        plot_total_tpm(data, args.output_dir, args.chart_type, args.show_values, selected_users)

    if series:
        timeseries_dir = os.path.join(args.output_dir, "timeseries")
        os.makedirs(timeseries_dir, exist_ok=True)
        plot_vm_timeseries(series, merged_machine_order, timeseries_dir)
    else:
        print("No TRANSACTION COUNT series found in input directory")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
