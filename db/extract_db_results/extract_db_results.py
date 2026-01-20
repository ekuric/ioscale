#!/usr/bin/env python3
"""
Database Results Extractor and Graph Generator

This script extracts "System achieved" TPM (Transactions Per Minute) values
from PostgreSQL and MariaDB benchmark result files. It processes all .out files in any 
subdirectories (directory name agnostic), creates CSV reports with the extracted data, 
and generates PNG graphs showing VM numbers vs TPM performance.

Supports both:
- PostgreSQL: Files containing "test_postgresql_pg" or "test_ESX_pg" and "PostgreSQL TPM" results
- MariaDB: Files containing "test_mariadb" and "MySQL TPM" results

Usage:
    # Single directory processing (new recommended syntax)
    python3 extract_db_results.py --input-dir input_directory --output-dir output_directory
    
    # Multiple directories with comparison graphs (new recommended syntax)
    python3 extract_db_results.py --input-dir dir1 --input-dir dir2 --input-dir dir3 --output-dir output_directory
    
    # Force comparison graphs even with single directory
    python3 extract_db_results.py --input-dir input_directory --output-dir output_directory --compare
    
    # Choose chart type (scatter, line, or bar)
    python3 extract_db_results.py --input-dir input_directory --output-dir output_directory --chart-type bar
    
    # Filter by user counts (only show specified user counts on graphs)
    python3 extract_db_results.py --input-dir input_directory --output-dir output_directory --users 1,20
    python3 extract_db_results.py --input-dir input_directory --output-dir output_directory --users "1, 20, 30"
    
    # Legacy syntax (still supported for backward compatibility)
    python3 extract_db_results.py [input_directory] [output_directory]
    python3 extract_db_results.py dir1 dir2 dir3 output_directory

Arguments:
    --input-dir: Input directory containing subdirectories with .out files (can be specified multiple times)
    --output-dir: Directory to save CSV and PNG output files (default: postgresql_analysis)
    --compare: Force creation of comparison graphs
    --chart-type: Type of chart to create - scatter (dots only), line (with connections), or bar (default: scatter)
    --users: Comma-separated list of user counts to include in graphs (e.g., "1,20" or "1, 20"). Only specified user counts will be shown on graphs.
    
    Legacy positional arguments (deprecated):
    input_directories: One or more directories containing subdirectories with .out files (default: postgresql-results-20250828-140741)
    output_directory: Directory to save CSV and PNG output files (default: postgresql_analysis)

Note: The new --input-dir and --output-dir options are recommended. Legacy positional arguments are still supported for backward compatibility.

Output Files:
    - {database}_detailed_results.csv: All individual results (postgresql_ or mariadb_)
    - {database}_summary_*.csv: Summary files by test type
    - {database}_overall_summary.csv: Overall statistics
    - *.png: Performance graphs showing VM numbers vs TPM values with appropriate database labels
    - comparison/: Directory containing comparison graphs when multiple directories are processed
      - Average_TPM_Comparison.png: Bar chart comparing average TPM across test runs
      - TPM_Comparison_*.png: Line graphs comparing TPM performance for each test type
"""

import os
import re
import csv
import argparse
import tempfile
import shutil
from pathlib import Path
from collections import defaultdict
import matplotlib.pyplot as plt
import matplotlib.cm as cm
from matplotlib.colors import hsv_to_rgb
import pandas as pd


def format_tpm_value(value):
    """
    Format TPM value for display - use K/M suffixes for large numbers to improve readability.
    
    Args:
        value: Numeric TPM value
        
    Returns:
        str: Formatted value (e.g., "2423K", "2.4M", "1234")
    """
    if value >= 1000000:
        # Millions
        if value >= 10000000:
            return f'{value/1000000:.0f}M'
        else:
            return f'{value/1000000:.1f}M'
    elif value >= 1000:
        # Thousands
        if value >= 10000:
            return f'{value/1000:.0f}K'
        else:
            return f'{value/1000:.1f}K'
    else:
        return f'{value:.0f}'


def add_value_labels_with_offset(x_positions, y_values, max_y, fontsize=10, min_offset_ratio=0.02):
    """
    Add value labels to a plot with intelligent offset to prevent overlap.
    Detects when values are close and offsets them vertically.
    
    Args:
        x_positions: List of x positions
        y_values: List of y values
        max_y: Maximum y value in the dataset (for offset calculation)
        fontsize: Font size for labels
        min_offset_ratio: Minimum offset as ratio of max_y (default 0.02 = 2%)
    """
    offset_step = max_y * min_offset_ratio
    used_offsets = {}  # Track offsets used at each x position
    
    for i, (x, y) in enumerate(zip(x_positions, y_values)):
        if pd.isna(y) or y <= 0:
            continue
            
        # Check if there are other points at similar y-values nearby
        base_offset = max_y * 0.01  # Base offset (1% of max)
        offset = base_offset
        
        # Check for nearby points with similar y-values
        for j, (x_other, y_other) in enumerate(zip(x_positions, y_values)):
            if i != j and not pd.isna(y_other) and y_other > 0:
                # If x positions are close (within 2 units) and y values are very close (within 1% of max)
                if abs(x - x_other) <= 2 and abs(y - y_other) < max_y * 0.01:
                    # Use a different offset for this point
                    offset_key = f"{x:.1f}_{y:.0f}"
                    if offset_key in used_offsets:
                        offset = used_offsets[offset_key] + offset_step
                    else:
                        # Alternate offset direction for visual separation
                        offset = base_offset if i % 2 == 0 else base_offset + offset_step
                    used_offsets[offset_key] = offset
                    break
        
        formatted_value = format_tpm_value(y)
        plt.text(x, y + offset, formatted_value, ha='center', va='bottom', 
                fontsize=fontsize, fontweight='bold')


def save_figure_atomic(fig, output_path):
    """
    Save a matplotlib figure using atomic write to prevent _tmp_ files.
    Sets file permissions to be readable by everyone (644: rw-r--r--).
    
    Args:
        fig: matplotlib figure object (or None to use current figure)
        output_path: Final destination path for the saved figure
    """
    # Create temp file in same directory for atomic write
    output_dir = os.path.dirname(output_path)
    temp_fd, temp_path = tempfile.mkstemp(suffix='.png', dir=output_dir)
    try:
        # Set permissions on the file descriptor before closing (644: rw-r--r--)
        os.fchmod(temp_fd, 0o644)
        os.close(temp_fd)  # Close file descriptor, we'll use shutil.move
        if fig is not None:
            fig.savefig(temp_path, dpi=300, bbox_inches='tight')
        else:
            plt.savefig(temp_path, dpi=300, bbox_inches='tight')
        # Ensure permissions are still correct after matplotlib writes (644: rw-r--r--)
        os.chmod(temp_path, 0o644)
        # Atomic move to final location
        shutil.move(temp_path, output_path)
        # Ensure final file has correct permissions (644: rw-r--r--)
        os.chmod(output_path, 0o644)
    except Exception as e:
        # Clean up temp file on error
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except:
                pass
        raise e


def get_distinct_colors(n, colormap_name='tab20'):
    """
    Generate n distinct colors using matplotlib colormap.
    If n > colormap size, cycles through multiple colormaps for variety.
    
    Args:
        n (int): Number of distinct colors needed
        colormap_name (str): Base colormap name (default: 'tab20' for 20 distinct colors)
        
    Returns:
        list: List of color strings (hex or named colors)
    """
    # Use tab20 which has 20 distinct colors, then tab20b and tab20c for more
    colormaps = ['tab20', 'tab20b', 'tab20c', 'Set3', 'Set2', 'Set1', 'Pastel1', 'Pastel2']
    colors = []
    
    for i in range(n):
        cmap_idx = i // 20
        color_idx = i % 20
        
        if cmap_idx < len(colormaps):
            cmap = cm.get_cmap(colormaps[cmap_idx % len(colormaps)])
            # Get color from colormap (normalize index to 0-1 range)
            color = cmap(color_idx / 20.0)
            # Convert RGBA to hex
            hex_color = '#{:02x}{:02x}{:02x}'.format(
                int(color[0] * 255), 
                int(color[1] * 255), 
                int(color[2] * 255)
            )
            colors.append(hex_color)
        else:
            # Fallback: generate colors using HSV color space for maximum distinctness
            hue = (i * 0.618033988749895) % 1.0  # Golden ratio for even distribution
            saturation = 0.7 + (i % 3) * 0.1  # Vary saturation
            value = 0.8 + (i % 2) * 0.15  # Vary brightness
            # hsv_to_rgb expects array-like input, returns array
            rgb_array = hsv_to_rgb([[hue, saturation, value]])
            rgb = rgb_array[0]
            hex_color = '#{:02x}{:02x}{:02x}'.format(
                int(rgb[0] * 255),
                int(rgb[1] * 255),
                int(rgb[2] * 255)
            )
            colors.append(hex_color)
    
    return colors


def extract_tpm_from_file(file_path):
    """
    Extract TPM (Transactions Per Minute) value from a PostgreSQL or MariaDB result file.
    
    Args:
        file_path (str): Path to the .out file
        
    Returns:
        tuple: (tpm_value, nopm_value, database_type) or (None, None, None) if not found
    """
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
            
        # Look for PostgreSQL pattern: "System achieved XXXX PostgreSQL TPM at YYYY NOPM"
        postgresql_pattern = r'System achieved (\d+) PostgreSQL TPM at (\d+) NOPM'
        postgresql_match = re.search(postgresql_pattern, content)
        
        if postgresql_match:
            tpm = int(postgresql_match.group(1))
            nopm = int(postgresql_match.group(2))
            return tpm, nopm, 'PostgreSQL'
        
        # Look for MariaDB/MySQL pattern: "System achieved XXXX MySQL TPM at YYYY NOPM"
        mariadb_pattern = r'System achieved (\d+) MySQL TPM at (\d+) NOPM'
        mariadb_match = re.search(mariadb_pattern, content)
        
        if mariadb_match:
            tpm = int(mariadb_match.group(1))
            nopm = int(mariadb_match.group(2))
            return tpm, nopm, 'MariaDB'
        
        return None, None, None
            
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
        return None, None, None


def get_vm_number(vm_dir_name):
    """
    Extract VM number from directory name like 'vm-1', 'vm-10', etc.
    For non-vm- prefixed directories, use alphabetical order as VM number.
    
    Args:
        vm_dir_name (str): Directory name like 'vm-1' or any other name
        
    Returns:
        int: VM number or 0 if not found
    """
    # If the directory name is an IPv4 address, avoid collapsing different IPs
    # into the same numeric ID (e.g., 1.2.3.4 and 1.2.3.5 would both map to 1).
    # Returning 0 lets the caller assign a stable index instead.
    if re.fullmatch(r'\d{1,3}(?:\.\d{1,3}){3}', vm_dir_name):
        return 0

    # First try to extract number from vm-* pattern
    match = re.search(r'vm-(\d+)', vm_dir_name)
    if match:
        return int(match.group(1))
    
    # For non-vm- directories, try to extract any number from the name
    match = re.search(r'(\d+)', vm_dir_name)
    if match:
        return int(match.group(1))
    
    # If no number found, return 0 (will be handled in sorting)
    return 0


def get_user_count_from_test_type(test_type):
    """
    Extract user count from test type name for proper sorting.
    
    Args:
        test_type (str): Test type name (e.g., "1_user", "10_users", "100_users")
        
    Returns:
        int: User count, or 0 if no number found
    """
    # Look for patterns like "1_user", "10_users", "100_users", etc.
    match = re.search(r'(\d+)_user', test_type.lower())
    if match:
        return int(match.group(1))
    
    # If no number found, return 0 (will be handled in sorting)
    return 0


def parse_user_filter(users_arg):
    """
    Parse --users argument and convert to test type format.
    
    Args:
        users_arg (str): Comma-separated list of user counts (e.g., "1,20" or "1, 20")
        
    Returns:
        set: Set of test type strings (e.g., {"1_user", "20_users"}) or None if no filter
    """
    if not users_arg:
        return None
    
    # Split by comma and strip whitespace
    user_counts = [u.strip() for u in users_arg.split(',') if u.strip()]
    
    if not user_counts:
        return None
    
    # Convert to test type format
    test_types = set()
    for user_count in user_counts:
        try:
            count = int(user_count)
            # Handle both "1_user" and "10_users" formats
            if count == 1:
                test_types.add("1_user")
            else:
                test_types.add(f"{count}_users")
        except ValueError:
            # If not a number, try to use as-is (might already be in test type format)
            test_types.add(user_count)
    
    return test_types if test_types else None


def filter_test_types(df, user_filter):
    """
    Filter dataframe to only include specified test types.
    
    Args:
        df (DataFrame): DataFrame with Test_Type column
        user_filter (set): Set of test type strings to include, or None for no filtering
        
    Returns:
        DataFrame: Filtered dataframe
    """
    if user_filter is None or 'Test_Type' not in df.columns:
        return df
    
    return df[df['Test_Type'].isin(user_filter)].copy()


def create_tpm_graphs(csv_file_path, output_dir, chart_type='scatter', user_filter=None, show_values=False):
    """
    Create PNG graphs from database CSV files showing VM numbers vs TPM values.
    
    Args:
        csv_file_path (str): Path to the CSV file
        output_dir (str): Output directory for PNG files
        chart_type (str): Type of chart to create ('scatter', 'line', 'bar')
        user_filter (set): Optional set of test types to include (e.g., {"1_user", "20_users"})
    """
    try:
        # Read the CSV file
        df = pd.read_csv(csv_file_path)
        
        # Apply user filter if provided
        if user_filter is not None:
            df = filter_test_types(df, user_filter)
        
        # Get the base filename without extension
        base_name = os.path.splitext(os.path.basename(csv_file_path))[0]
        
        # Create separate graphs for each test type
        test_types = df['Test_Type'].unique() if 'Test_Type' in df.columns else ['all']
        
        for test_type in test_types:
            # Determine database type from filename or data
            if 'postgresql' in base_name.lower():
                db_name = 'PostgreSQL'
            elif 'mariadb' in base_name.lower():
                db_name = 'MariaDB'
            else:
                db_name = 'Database'
            
            if 'Test_Type' in df.columns:
                # Filter data for this test type
                test_data = df[df['Test_Type'] == test_type].copy()
                graph_title = f"{db_name} TPM Performance - {test_type.replace('_', ' ').title()}"
                filename = f"{base_name}_{test_type}_tpm_graph_{chart_type}.png"
            else:
                # Use all data if no Test_Type column
                test_data = df.copy()
                graph_title = f"{db_name} TPM Performance - {base_name.replace('_', ' ').title()}"
                filename = f"{base_name}_tpm_graph_{chart_type}.png"
            
            # Sort by VM number and remove any rows with NaN values
            test_data = test_data.sort_values('VM_Number')
            test_data = test_data.dropna(subset=['VM_Number', 'TPM'])
            
            if test_data.empty:
                print(f"No valid data found for {test_type}, skipping graph")
                continue
            
            # Create the graph
            plt.figure(figsize=(15, 8))
            
            if chart_type == 'bar':
                bars = plt.bar(test_data['VM_Number'], test_data['TPM'], alpha=0.7, width=0.8)
                # Add value labels on top of bars if show_values is enabled
                if show_values:
                    max_tpm = max(test_data['TPM'])
                    for bar, (_, row) in zip(bars, test_data.iterrows()):
                        height = bar.get_height()
                        plt.text(bar.get_x() + bar.get_width()/2., height + max_tpm * 0.01,
                                format_tpm_value(row['TPM']), ha='center', va='bottom', 
                                fontsize=10, fontweight='bold')
            elif chart_type == 'line':
                plt.plot(test_data['VM_Number'], test_data['TPM'], marker='o', linewidth=2, markersize=6)
                # Add value labels on top of markers if show_values is enabled
                if show_values:
                    max_tpm = max(test_data['TPM'])
                    x_positions = test_data['VM_Number'].tolist()
                    y_values = test_data['TPM'].tolist()
                    add_value_labels_with_offset(x_positions, y_values, max_tpm, fontsize=10)
                # Set axes to start at 0 for better visibility
                plt.xlim(left=0)
                plt.ylim(bottom=0)
            else:  # scatter (default)
                plt.scatter(test_data['VM_Number'], test_data['TPM'], s=50, alpha=0.7)
            
            # Customize the graph (title will be updated later with machine count)
            plt.xlabel('Machines', fontsize=12, fontweight='bold')
            plt.ylabel('TPM (Transactions Per Minute)', fontsize=12, fontweight='bold')
            
            # Calculate number of machines for title and X-axis labels
            num_machines = len(test_data['VM_Number'].unique())
            
            # Set X-axis labels based on number of machines
            if num_machines <= 20:
                # Show all machine numbers if 20 or fewer
                vm_numbers = sorted(test_data['VM_Number'].unique())
                plt.xticks(vm_numbers, [int(x) for x in vm_numbers], rotation=45, ha='right')
            else:
                # Show every 10th machine if more than 20 machines
                vm_numbers = sorted(test_data['VM_Number'].unique())
                x_positions = []
                x_labels = []
                for i, vm_num in enumerate(vm_numbers):
                    if (i + 1) % 10 == 1 or i == 0:  # 1st, 11th, 21st, etc.
                        x_positions.append(vm_num)
                        x_labels.append(int(vm_num))
                plt.xticks(x_positions, x_labels, rotation=45, ha='right')
            
            # Add grid for better readability
            plt.grid(True, alpha=0.3)
            
            # Add statistics text box outside the graph area at top right
            avg_tpm = test_data['TPM'].mean()
            max_tpm = test_data['TPM'].max()
            min_tpm = test_data['TPM'].min()
            stats_text = f'Average: {avg_tpm:.0f} TPM\nMax: {max_tpm:.0f} TPM\nMin: {min_tpm:.0f} TPM'
            plt.text(1.02, 1, stats_text, transform=plt.gca().transAxes, 
                    verticalalignment='top', horizontalalignment='left',
                    bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.8))
            
            # Update title to include machine count
            plt.title(f"{graph_title} ({num_machines} Machines)", fontsize=16, fontweight='bold', pad=20)
            
            # Adjust layout to make room for statistics box
            plt.subplots_adjust(right=0.75)
            plt.tight_layout()
            
            # Save the graph using atomic write to prevent _tmp_ files
            output_path = os.path.join(output_dir, filename)
            save_figure_atomic(None, output_path)
            plt.close()
            print(f"Graph saved: {output_path}")
            
    except Exception as e:
        print(f"Error creating graph from {csv_file_path}: {e}")


def create_average_tpm_graph(csv_file_path, output_dir, chart_type='bar', user_filter=None, show_values=False):
    """
    Create a graph showing average TPM values for all tested user counts.
    
    Args:
        csv_file_path (str): Path to the detailed CSV file
        output_dir (str): Output directory for PNG files
        chart_type (str): Type of chart to create ('scatter', 'line', 'bar') - default 'bar'
        user_filter (set): Optional set of test types to include (e.g., {"1_user", "20_users"})
    """
    try:
        # Read the CSV file
        df = pd.read_csv(csv_file_path)
        
        if 'Test_Type' not in df.columns:
            print("No Test_Type column found, skipping average TPM graph")
            return
        
        # Apply user filter if provided
        if user_filter is not None:
            df = filter_test_types(df, user_filter)
            if df.empty:
                print("No data matches user filter, skipping average TPM graph")
                return
        
        # Calculate average TPM for each test type
        test_type_averages = {}
        test_types = sorted(df['Test_Type'].unique(), key=get_user_count_from_test_type)
        
        for test_type in test_types:
            test_data = df[df['Test_Type'] == test_type]
            avg_tpm = test_data['TPM'].mean()
            test_type_averages[test_type] = avg_tpm
        
        # Create the graph with extra space for legend
        plt.figure(figsize=(14, 8))
        
        # Prepare data for plotting
        test_type_labels = [test_type.replace('_', ' ').title() for test_type in test_types]
        avg_tpm_values = [test_type_averages[test_type] for test_type in test_types]
        x_positions = range(len(test_type_labels))
        
        # Create chart based on chart_type - use distinct colors for each test type
        colors = get_distinct_colors(len(test_types))
        
        if chart_type == 'line':
            # Create line chart with markers
            plt.plot(x_positions, avg_tpm_values, marker='o', linewidth=2, markersize=8, 
                    color=colors[0] if len(colors) > 0 else 'steelblue', alpha=0.8)
            # Add value labels on top of markers if show_values is enabled
            if show_values:
                max_tpm = max(avg_tpm_values) if avg_tpm_values else 0
                add_value_labels_with_offset(x_positions, avg_tpm_values, max_tpm, fontsize=12)
            # Set axes to start at 0 for better visibility
            plt.xlim(left=-0.5, right=len(x_positions) - 0.5)
            plt.ylim(bottom=0)
        elif chart_type == 'scatter':
            # Create scatter chart
            plt.scatter(x_positions, avg_tpm_values, s=100, alpha=0.8, 
                       color=colors[0] if len(colors) > 0 else 'steelblue')
            # Add value labels next to points
            for i, (x_pos, value) in enumerate(zip(x_positions, avg_tpm_values)):
                plt.text(x_pos, value + max(avg_tpm_values) * 0.01,
                        f'{value:.0f}', ha='center', va='bottom', fontsize=12, fontweight='bold')
        else:  # bar (default)
            # Create bar chart with labels for legend
            bars = plt.bar(test_type_labels, avg_tpm_values, color=colors, alpha=0.8, 
                          label=[f'{label}: {value:.0f} TPM' for label, value in zip(test_type_labels, avg_tpm_values)])
            # Add value labels on top of bars if show_values is enabled
            if show_values:
                max_value = max(avg_tpm_values) if avg_tpm_values else 0
                for bar, value in zip(bars, avg_tpm_values):
                    plt.text(bar.get_x() + bar.get_width()/2, bar.get_height() + max_value * 0.01,
                            format_tpm_value(value), ha='center', va='bottom', fontsize=12, fontweight='bold')
        
        # Customize the graph (title will be updated later with machine count)
        plt.xlabel('Number of Users', fontsize=12, fontweight='bold')
        plt.ylabel('Average TPM (Transactions Per Minute)', fontsize=12, fontweight='bold')
        
        # Set x-axis labels and rotation
        if chart_type == 'bar':
            plt.xticks(x_positions, test_type_labels, rotation=45, ha='right')
        else:
            # For line and scatter, use numeric positions
            plt.xticks(x_positions, test_type_labels, rotation=45, ha='right')
        
        # Add grid for better readability
        if chart_type == 'bar':
            plt.grid(True, alpha=0.3, axis='y')
        else:
            plt.grid(True, alpha=0.3)
        
        # Determine database type from filename
        base_name = os.path.splitext(os.path.basename(csv_file_path))[0]
        if 'postgresql' in base_name.lower():
            db_name = 'PostgreSQL'
        elif 'mariadb' in base_name.lower():
            db_name = 'MariaDB'
        else:
            db_name = 'Database'
        
        # Calculate total number of machines tested
        total_machines = len(df['VM_Number'].unique())
        
        # Update title to include database type and machine count
        plt.title(f'{db_name} Average TPM ({total_machines} Machines)', fontsize=16, fontweight='bold', pad=20)
        
        # Add legend outside the plot area at top right (only for bar charts)
        if chart_type == 'bar':
            plt.legend(bbox_to_anchor=(1.02, 1), loc='upper left', fontsize=10)
            # Adjust layout to make room for legend on the right
            plt.subplots_adjust(right=0.75)
        else:
            # For line and scatter, no legend needed, use standard layout
            plt.subplots_adjust(right=0.95)
        plt.tight_layout()
        
        # Save the graph using atomic write to prevent _tmp_ files
        output_path = os.path.join(output_dir, f'Average_tpm_{chart_type}_{"_".join(test_types)}.png')
        try:
            save_figure_atomic(None, output_path)
            print(f"Average TPM graph saved: {output_path}")
        except Exception as save_error:
            print(f"Error saving average TPM graph to {output_path}: {save_error}")
            raise
        finally:
            plt.close()
        
    except Exception as e:
        print(f"Error creating average TPM graph from {csv_file_path}: {e}")
        import traceback
        traceback.print_exc()


def create_total_tpm_graph(csv_file_path, output_dir, chart_type='bar', user_filter=None, show_values=False):
    """
    Create a graph showing total TPM values (sum of all machines) for all tested user counts.
    
    Args:
        csv_file_path (str): Path to the detailed CSV file
        output_dir (str): Output directory for PNG files
        chart_type (str): Type of chart to create ('scatter', 'line', 'bar') - default 'bar'
        user_filter (set): Optional set of test types to include (e.g., {"1_user", "20_users"})
    """
    try:
        # Read the CSV file
        df = pd.read_csv(csv_file_path)
        
        if 'Test_Type' not in df.columns:
            print("No Test_Type column found, skipping total TPM graph")
            return
        
        # Apply user filter if provided
        if user_filter is not None:
            df = filter_test_types(df, user_filter)
            if df.empty:
                print("No data matches user filter, skipping total TPM graph")
                return
        
        # Calculate total TPM (sum) for each test type
        test_type_totals = {}
        test_types = sorted(df['Test_Type'].unique(), key=get_user_count_from_test_type)
        
        for test_type in test_types:
            test_data = df[df['Test_Type'] == test_type]
            total_tpm = test_data['TPM'].sum()  # Sum instead of mean
            test_type_totals[test_type] = total_tpm
        
        # Create the graph with extra space for legend
        plt.figure(figsize=(14, 8))
        
        # Prepare data for plotting
        test_type_labels = [test_type.replace('_', ' ').title() for test_type in test_types]
        total_tpm_values = [test_type_totals[test_type] for test_type in test_types]
        x_positions = range(len(test_type_labels))
        
        # Create chart based on chart_type - use distinct colors for each test type
        colors = get_distinct_colors(len(test_types))
        
        if chart_type == 'line':
            # Create line chart with markers
            plt.plot(x_positions, total_tpm_values, marker='o', linewidth=2, markersize=8, 
                    color=colors[0] if len(colors) > 0 else 'steelblue', alpha=0.8)
            # Add value labels on top of markers if show_values is enabled
            if show_values:
                max_tpm = max(total_tpm_values) if total_tpm_values else 0
                add_value_labels_with_offset(x_positions, total_tpm_values, max_tpm, fontsize=12)
            # Set axes to start at 0 for better visibility
            plt.xlim(left=-0.5, right=len(x_positions) - 0.5)
            plt.ylim(bottom=0)
        elif chart_type == 'scatter':
            # Create scatter chart
            plt.scatter(x_positions, total_tpm_values, s=100, alpha=0.8, 
                       color=colors[0] if len(colors) > 0 else 'steelblue')
            # Add value labels next to points (always show for scatter, use formatted values)
            max_tpm = max(total_tpm_values) if total_tpm_values else 0
            for i, (x_pos, value) in enumerate(zip(x_positions, total_tpm_values)):
                plt.text(x_pos, value + max_tpm * 0.01,
                        format_tpm_value(value), ha='center', va='bottom', fontsize=12, fontweight='bold')
        else:  # bar (default)
            # Create bar chart with labels for legend (use formatted values in legend)
            bars = plt.bar(test_type_labels, total_tpm_values, color=colors, alpha=0.8, 
                          label=[f'{label}: {format_tpm_value(value)} TPM' for label, value in zip(test_type_labels, total_tpm_values)])
            # Add value labels on top of bars if show_values is enabled
            if show_values:
                max_value = max(total_tpm_values) if total_tpm_values else 0
                for bar, value in zip(bars, total_tpm_values):
                    plt.text(bar.get_x() + bar.get_width()/2, bar.get_height() + max_value * 0.01,
                            format_tpm_value(value), ha='center', va='bottom', fontsize=12, fontweight='bold')
        
        # Customize the graph (title will be updated later with machine count)
        plt.xlabel('Number of Users', fontsize=12, fontweight='bold')
        plt.ylabel('Total TPM (Transactions Per Minute)', fontsize=12, fontweight='bold')
        
        # Set x-axis labels and rotation
        if chart_type == 'bar':
            plt.xticks(x_positions, test_type_labels, rotation=45, ha='right')
        else:
            # For line and scatter, use numeric positions
            plt.xticks(x_positions, test_type_labels, rotation=45, ha='right')
        
        # Add grid for better readability
        if chart_type == 'bar':
            plt.grid(True, alpha=0.3, axis='y')
        else:
            plt.grid(True, alpha=0.3)
        
        # Determine database type from filename
        base_name = os.path.splitext(os.path.basename(csv_file_path))[0]
        if 'postgresql' in base_name.lower():
            db_name = 'PostgreSQL'
        elif 'mariadb' in base_name.lower():
            db_name = 'MariaDB'
        else:
            db_name = 'Database'
        
        # Calculate total number of machines tested
        total_machines = len(df['VM_Number'].unique())
        
        # Update title to include database type and machine count
        plt.title(f'{db_name} Total TPM ({total_machines} Machines)', fontsize=16, fontweight='bold', pad=20)
        
        # Add legend outside the plot area at top right (only for bar charts)
        if chart_type == 'bar':
            plt.legend(bbox_to_anchor=(1.02, 1), loc='upper left', fontsize=10)
            # Adjust layout to make room for legend on the right
            plt.subplots_adjust(right=0.75)
        else:
            # For line and scatter, no legend needed, use standard layout
            plt.subplots_adjust(right=0.95)
        plt.tight_layout()
        
        # Save the graph using atomic write to prevent _tmp_ files
        output_path = os.path.join(output_dir, f'Total_tpm_{chart_type}_{"_".join(test_types)}.png')
        try:
            save_figure_atomic(None, output_path)
            print(f"Total TPM graph saved: {output_path}")
        except Exception as save_error:
            print(f"Error saving total TPM graph to {output_path}: {save_error}")
            raise
        finally:
            plt.close()
        
    except Exception as e:
        print(f"Error creating total TPM graph from {csv_file_path}: {e}")
        import traceback
        traceback.print_exc()


def create_combined_tpm_graph(csv_file_path, output_dir, chart_type='scatter', user_filter=None, show_values=False):
    """
    Create a combined graph showing both test types on the same plot.
    
    Args:
        csv_file_path (str): Path to the detailed CSV file
        output_dir (str): Output directory for PNG files
        chart_type (str): Type of chart to create ('scatter', 'line', 'bar')
        user_filter (set): Optional set of test types to include (e.g., {"1_user", "20_users"})
    """
    try:
        # Read the CSV file
        df = pd.read_csv(csv_file_path)
        
        if 'Test_Type' not in df.columns:
            print("No Test_Type column found, skipping combined graph")
            return
        
        # Apply user filter if provided
        if user_filter is not None:
            df = filter_test_types(df, user_filter)
            if df.empty:
                print("No data matches user filter, skipping combined graph")
                return
        
        # Create the combined graph
        plt.figure(figsize=(15, 8))
        
        # Plot each test type with different colors and markers
        # Get sorted test types for consistent ordering
        sorted_test_types = sorted(df['Test_Type'].unique(), key=get_user_count_from_test_type)
        print(f"Creating combined graph with test types: {sorted_test_types}")
        
        # Generate distinct colors for each test type
        colors = get_distinct_colors(len(sorted_test_types))
        markers = ['o', 's', '^', 'D', 'v', 'p', '*', 'h', 'H', '+', 'x', 'X', 'd', '|', '_', 
                  '1', '2', '3', '4', '8', '<', '>', 'P', 'X', 'D', 'o', 's', '^', 'v', 'p']
        
        for i, test_type in enumerate(sorted_test_types):
            test_data = df[df['Test_Type'] == test_type].sort_values('VM_Number')
            color = colors[i]  # Use unique color for each test type
            marker = markers[i % len(markers)]  # Cycle markers if needed
            print(f"  {test_type}: color={color}, marker={marker}")
            
            if chart_type == 'bar':
                bars = plt.bar(test_data['VM_Number'], test_data['TPM'], 
                       alpha=0.7, width=0.8, 
                       label=test_type.replace('_', ' ').title(), color=color)
                # Add value labels on top of bars if show_values is enabled
                if show_values and not test_data.empty:
                    max_tpm = max(test_data['TPM'])
                    for bar, (_, row) in zip(bars, test_data.iterrows()):
                        height = bar.get_height()
                        plt.text(bar.get_x() + bar.get_width()/2., height + max_tpm * 0.01,
                                format_tpm_value(row['TPM']), ha='center', va='bottom', 
                                fontsize=10, fontweight='bold')
            elif chart_type == 'line':
                plt.plot(test_data['VM_Number'], test_data['TPM'], 
                        marker=marker, linewidth=2, markersize=6, 
                        label=test_type.replace('_', ' ').title(), color=color)
            else:  # scatter (default)
                plt.scatter(test_data['VM_Number'], test_data['TPM'], 
                           s=50, alpha=0.7, marker=marker,
                           label=test_type.replace('_', ' ').title(), color=color)
        
        # Customize the graph (title will be updated later with machine count)
        plt.xlabel('Machines', fontsize=12, fontweight='bold')
        plt.ylabel('TPM (Transactions Per Minute)', fontsize=12, fontweight='bold')
        
        # Calculate number of machines for title and X-axis labels
        num_machines = len(df['VM_Number'].unique())
        
        # Set X-axis labels based on number of machines
        all_vm_numbers = sorted(df['VM_Number'].unique())
        if num_machines <= 20:
            # Show all machine numbers if 20 or fewer
            plt.xticks(all_vm_numbers, [int(x) for x in all_vm_numbers], rotation=45, ha='right')
        else:
            # Show every 10th machine if more than 20 machines
            x_positions = []
            x_labels = []
            for i, vm_num in enumerate(all_vm_numbers):
                if (i + 1) % 10 == 1 or i == 0:  # 1st, 11th, 21st, etc.
                    x_positions.append(vm_num)
                    x_labels.append(int(vm_num))
            plt.xticks(x_positions, x_labels, rotation=45, ha='right')
        
        # Determine database type from filename
        filename = os.path.basename(csv_file_path)
        if 'postgresql' in filename.lower():
            db_name = 'PostgreSQL'
        elif 'mariadb' in filename.lower():
            db_name = 'MariaDB'
        else:
            db_name = 'Database'
        
        # Update title to include machine count
        plt.title(f'{db_name} TPM Performance Comparison ({num_machines} Machines)', fontsize=16, fontweight='bold', pad=20)
        
        # Add legend outside the plot area at top right
        plt.legend(bbox_to_anchor=(1.02, 1), loc='upper left', fontsize=10)
        
        # Add grid for better readability
        plt.grid(True, alpha=0.3)
        
        # Adjust layout to make room for legend
        plt.subplots_adjust(right=0.75)
        plt.tight_layout()
        
        # Save the graph using atomic write
        output_path = os.path.join(output_dir, f'{db_name.lower()}_combined_tpm_comparison.png')
        save_figure_atomic(None, output_path)
        plt.close()
        print(f"Combined graph saved: {output_path}")
        
    except Exception as e:
        print(f"Error creating combined graph from {csv_file_path}: {e}")


def process_postgresql_results(input_dir, output_dir, chart_type='scatter', user_filter=None, show_values=False):
    """
    Process all database result files (PostgreSQL and MariaDB) and extract TPM values.
    
    Args:
        input_dir (str): Input directory containing subdirectories with .out files
        output_dir (str): Output directory for CSV files
        chart_type (str): Type of chart to create ('scatter', 'line', 'bar')
        user_filter (set): Optional set of test types to include (e.g., {"1_user", "20_users"})
    """
    # Create output directory if it doesn't exist
    os.makedirs(output_dir, exist_ok=True)
    
    # Data structures to store results
    vm_results = defaultdict(dict)  # vm_number -> {test_type: (tpm, nopm)}
    all_results = []  # List of all results for summary
    
    # Process each directory containing .out files (directory name agnostic)
    vm_dirs = []
    for item in os.listdir(input_dir):
        item_path = os.path.join(input_dir, item)
        if os.path.isdir(item_path):
            # Check if this directory contains .out files (PostgreSQL or MariaDB)
            out_files = [f for f in os.listdir(item_path) if f.endswith('.out') and ('test_postgresql_pg' in f or 'test_ESX_pg' in f or 'test_mariadb' in f)]
            if out_files:
                vm_dirs.append(item)
    
    # Sort directories by VM number (extracted from name) or alphabetically if no number
    vm_dirs = sorted(vm_dirs, key=lambda x: (get_vm_number(x), x))
    
    print(f"Processing {len(vm_dirs)} VM directories...")
    
    for vm_dir in vm_dirs:
        vm_path = os.path.join(input_dir, vm_dir)
        if not os.path.isdir(vm_path):
            continue
            
        vm_number = get_vm_number(vm_dir)
        # If no number found in directory name, use index + 1 as VM number
        if vm_number == 0:
            vm_number = vm_dirs.index(vm_dir) + 1
        print(f"Processing {vm_dir} (VM {vm_number})...")
        
        # Find all .out files in the VM directory (PostgreSQL or MariaDB)
        out_files = [f for f in os.listdir(vm_path) if f.endswith('.out') and ('test_postgresql_pg' in f or 'test_ESX_pg' in f or 'test_mariadb' in f)]
        
        for out_file in out_files:
            file_path = os.path.join(vm_path, out_file)
            
            # Determine test type from filename
            if '_1.out' in out_file:
                test_type = '1_user'
            elif '_10.out' in out_file:
                test_type = '10_users'
            else:
                # Extract number from filename if it's different
                match = re.search(r'_(\d+)\.out$', out_file)
                test_type = f"{match.group(1)}_users" if match else 'unknown'
            
            # Extract TPM and NOPM values
            tpm, nopm, database_type = extract_tpm_from_file(file_path)
            
            if tpm is not None:
                vm_results[vm_number][test_type] = (tpm, nopm)
                all_results.append({
                    'vm_number': vm_number,
                    'vm_name': vm_dir,
                    'test_type': test_type,
                    'tpm': tpm,
                    'nopm': nopm,
                    'database_type': database_type,
                    'file': out_file
                })
                print(f"  {test_type}: {tpm} TPM, {nopm} NOPM ({database_type})")
            else:
                print(f"  {test_type}: No TPM data found")
    
    # Determine database type for output file naming
    database_types = set(result['database_type'] for result in all_results if result['database_type'])
    if len(database_types) == 1:
        db_type = list(database_types)[0].lower()
    else:
        db_type = 'database'  # Mixed or unknown
    
    # Save detailed results to CSV
    detailed_csv = os.path.join(output_dir, f'{db_type}_detailed_results.csv')
    with open(detailed_csv, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['VM_Number', 'VM_Name', 'Test_Type', 'TPM', 'NOPM', 'Database_Type', 'File'])
        
        for result in sorted(all_results, key=lambda x: (x['vm_number'], x['test_type'])):
            writer.writerow([
                result['vm_number'],
                result['vm_name'],
                result['test_type'],
                result['tpm'],
                result['nopm'],
                result['database_type'],
                result['file']
            ])
    
    print(f"\nDetailed results saved to: {detailed_csv}")
    
    # Create summary by test type
    test_types = set(result['test_type'] for result in all_results)
    
    for test_type in sorted(test_types, key=get_user_count_from_test_type):
        summary_csv = os.path.join(output_dir, f'{db_type}_summary_{test_type}.csv')
        
        with open(summary_csv, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['VM_Number', 'VM_Name', 'TPM', 'NOPM'])
            
            test_results = [r for r in all_results if r['test_type'] == test_type]
            test_results.sort(key=lambda x: x['vm_number'])
            
            total_tpm = 0
            total_nopm = 0
            
            for result in test_results:
                writer.writerow([
                    result['vm_number'],
                    result['vm_name'],
                    result['tpm'],
                    result['nopm']
                ])
                total_tpm += result['tpm']
                total_nopm += result['nopm']
            
            # Add summary row
            if test_results:
                avg_tpm = total_tpm / len(test_results)
                avg_nopm = total_nopm / len(test_results)
                writer.writerow(['', 'AVERAGE', f'{avg_tpm:.2f}', f'{avg_nopm:.2f}'])
                writer.writerow(['', 'TOTAL', total_tpm, total_nopm])
        
        print(f"Summary for {test_type} saved to: {summary_csv}")
    
    # Create overall summary
    overall_csv = os.path.join(output_dir, f'{db_type}_overall_summary.csv')
    with open(overall_csv, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['Test_Type', 'VM_Count', 'Total_TPM', 'Total_NOPM', 'Avg_TPM', 'Avg_NOPM'])
        
        for test_type in sorted(test_types, key=get_user_count_from_test_type):
            test_results = [r for r in all_results if r['test_type'] == test_type]
            if test_results:
                total_tpm = sum(r['tpm'] for r in test_results)
                total_nopm = sum(r['nopm'] for r in test_results)
                avg_tpm = total_tpm / len(test_results)
                avg_nopm = total_nopm / len(test_results)
                
                writer.writerow([
                    test_type,
                    len(test_results),
                    total_tpm,
                    total_nopm,
                    f'{avg_tpm:.2f}',
                    f'{avg_nopm:.2f}'
                ])
    
    print(f"Overall summary saved to: {overall_csv}")
    
    # Generate graphs from CSV files
    print(f"\n=== GENERATING GRAPHS ===")
    
    # Apply user filter to test_types if provided
    if user_filter is not None:
        test_types = [tt for tt in test_types if tt in user_filter]
        if not test_types:
            print("Warning: No test types match the user filter, no graphs will be created")
    
    # Create graphs from detailed results
    create_tpm_graphs(detailed_csv, output_dir, chart_type, user_filter, show_values)
    
    # Create combined comparison graph (disabled - confusing)
    # create_combined_tpm_graph(detailed_csv, output_dir, chart_type, user_filter)
    
    # Create average TPM graph
    create_average_tpm_graph(detailed_csv, output_dir, chart_type, user_filter, show_values)
    
    # Create total TPM graph (sum of all machines)
    create_total_tpm_graph(detailed_csv, output_dir, chart_type, user_filter, show_values)
    
    # Create graphs from summary files
    for test_type in sorted(test_types, key=get_user_count_from_test_type):
        summary_csv = os.path.join(output_dir, f'{db_type}_summary_{test_type}.csv')
        if os.path.exists(summary_csv):
            create_tpm_graphs(summary_csv, output_dir, chart_type, user_filter, show_values)
    
    # Print summary statistics
    print(f"\n=== SUMMARY STATISTICS ===")
    print(f"Total VMs processed: {len(vm_dirs)}")
    print(f"Total test results: {len(all_results)}")
    
    for test_type in sorted(test_types, key=get_user_count_from_test_type):
        test_results = [r for r in all_results if r['test_type'] == test_type]
        if test_results:
            total_tpm = sum(r['tpm'] for r in test_results)
            avg_tpm = total_tpm / len(test_results)
            print(f"{test_type}: {len(test_results)} VMs, Avg TPM: {avg_tpm:.2f}")


def create_comparison_graphs(input_dirs, output_dir, chart_type='scatter', database_type=None, user_filter=None, show_values=False):
    """
    Create comparison graphs across multiple test result directories.
    
    Args:
        input_dirs (list): List of input directories to compare
        output_dir (str): Output directory for comparison graphs
        chart_type (str): Type of chart to create ('scatter', 'line', 'bar')
        database_type (str): Optional database type ('MariaDB' or 'PostgreSQL') for graph titles
        user_filter (set): Optional set of test types to include (e.g., {"1_user", "20_users"})
    """
    print(f"\n=== CREATING COMPARISON GRAPHS ===")
    
    # Create output directory if it doesn't exist
    os.makedirs(output_dir, exist_ok=True)
    
    # Process each directory and collect data
    all_data = {}
    test_types_union = set()
    
    for input_dir in input_dirs:
        dir_name = os.path.basename(input_dir.rstrip('/'))
        print(f"Processing {dir_name} for comparison...")
        
        # Process this directory
        temp_output = os.path.join(output_dir, f'temp_{dir_name}')
        process_postgresql_results(input_dir, temp_output, chart_type, user_filter, show_values)
        
        # Read the detailed results
        detailed_csv = None
        for file in os.listdir(temp_output):
            if file.endswith('_detailed_results.csv'):
                detailed_csv = os.path.join(temp_output, file)
                break
        
        if detailed_csv and os.path.exists(detailed_csv):
            df = pd.read_csv(detailed_csv)
            all_data[dir_name] = df
            
            # Collect all test types
            if 'Test_Type' in df.columns:
                test_types_union.update(df['Test_Type'].unique())
        
        # Clean up temp directory
        import shutil
        shutil.rmtree(temp_output, ignore_errors=True)
    
    if not all_data:
        print("No data found for comparison")
        return
    
    # Apply user filter to test_types_union if provided
    if user_filter is not None:
        test_types_union = test_types_union.intersection(user_filter)
        if not test_types_union:
            print("Warning: No test types match the user filter, no comparison graphs will be created")
            return
    
    # Create comparison graphs
    create_average_tpm_comparison_graph(all_data, test_types_union, output_dir, chart_type, database_type, user_filter, show_values)
    create_total_tpm_comparison_graph(all_data, test_types_union, output_dir, chart_type, database_type, user_filter, show_values)
    create_detailed_comparison_graphs(all_data, test_types_union, output_dir, chart_type, database_type, user_filter, show_values)


def create_average_tpm_comparison_graph(all_data, test_types_union, output_dir, chart_type='line', database_type=None, user_filter=None, show_values=False):
    """
    Create a comparison graph showing average TPM values across different test directories.
    
    Args:
        all_data (dict): Dictionary mapping directory names to DataFrames
        test_types_union (set): Set of all test types found across directories
        output_dir (str): Output directory for the graph
        chart_type (str): Type of chart to create ('scatter', 'line', 'bar') - default 'line' for comparison
        database_type (str): Optional database type ('MariaDB' or 'PostgreSQL') for graph titles
        user_filter (set): Optional set of test types to include (e.g., {"1_user", "20_users"})
    """
    try:
        # Apply user filter if provided
        if user_filter is not None:
            test_types_union = test_types_union.intersection(user_filter)
            if not test_types_union:
                print("No test types match user filter for average TPM comparison")
                return
        
        # Calculate average TPM for each directory and test type
        comparison_data = {}
        
        for dir_name, df in all_data.items():
            if 'Test_Type' not in df.columns:
                continue
            
            # Apply filter to dataframe
            if user_filter is not None:
                df = filter_test_types(df, user_filter)
                
            dir_averages = {}
            for test_type in test_types_union:
                test_data = df[df['Test_Type'] == test_type]
                if not test_data.empty:
                    avg_tpm = test_data['TPM'].mean()
                    dir_averages[test_type] = avg_tpm
            
            comparison_data[dir_name] = dir_averages
        
        if not comparison_data:
            print("No comparison data available")
            return
        
        # Create the comparison graph
        plt.figure(figsize=(16, 10))
        
        # Prepare data for plotting - sort by user count, not alphabetically
        sorted_test_types = sorted(test_types_union, key=get_user_count_from_test_type)
        test_type_labels = [test_type.replace('_', ' ').title() for test_type in sorted_test_types]
        x_pos = range(len(test_type_labels))
        
        # Colors for different directories
        colors = ['steelblue', 'orange', 'green', 'red', 'purple', 'brown', 'pink', 'gray', 'olive', 'cyan']
        markers = ['o', 's', '^', 'D', 'v', 'p', '*', 'h', 'H', '+']
        
        # Calculate bar_width for bar charts (needed for x-axis positioning)
        bar_width = 0.8 / len(comparison_data) if chart_type == 'bar' else None
        
        # Calculate max value across all directories for label positioning
        max_value = 0
        if comparison_data:
            for dir_averages in comparison_data.values():
                for test_type in sorted_test_types:
                    max_value = max(max_value, dir_averages.get(test_type, 0))
        
        # Plot based on chart_type
        if chart_type == 'line':
            # Plot lines for each directory
            for i, (dir_name, dir_averages) in enumerate(comparison_data.items()):
                values = [dir_averages.get(test_type, 0) for test_type in sorted_test_types]
                plt.plot(x_pos, values, marker=markers[i % len(markers)], linewidth=2, markersize=8,
                        label=dir_name, color=colors[i % len(colors)], alpha=0.8)
                # Add value labels on top of markers if show_values is enabled
                if show_values and max_value > 0:
                    # Collect all x positions and values for this directory (filter out zeros)
                    dir_x_pos = [x_pos[j] for j, v in enumerate(values) if v > 0]
                    dir_values = [v for v in values if v > 0]
                    if dir_values:
                        add_value_labels_with_offset(dir_x_pos, dir_values, max_value, fontsize=10)
        elif chart_type == 'scatter':
            # Plot scatter points for each directory
            for i, (dir_name, dir_averages) in enumerate(comparison_data.items()):
                values = [dir_averages.get(test_type, 0) for test_type in sorted_test_types]
                plt.scatter(x_pos, values, s=100, marker=markers[i % len(markers)],
                          label=dir_name, color=colors[i % len(colors)], alpha=0.8)
        else:  # bar (default)
            # Plot bars for each directory
            max_bar_value = 0
            all_bars = []  # Store all bars for value labeling
            for i, (dir_name, dir_averages) in enumerate(comparison_data.items()):
                values = [dir_averages.get(test_type, 0) for test_type in sorted_test_types]
                bars = plt.bar([x + i * bar_width for x in x_pos], values, 
                              bar_width, label=dir_name, color=colors[i % len(colors)], alpha=0.8)
                all_bars.append((bars, values))
                max_bar_value = max(max_bar_value, max(values) if values else 0)
            
            # Add value labels on top of bars if show_values is enabled
            if show_values and max_bar_value > 0:
                for bars, values in all_bars:
                    for bar, value in zip(bars, values):
                        if value > 0:
                            plt.text(bar.get_x() + bar.get_width()/2, bar.get_height() + max_bar_value * 0.01,
                                    format_tpm_value(value), ha='center', va='bottom', 
                                    fontsize=9, fontweight='bold')
        
        # Determine database type - use provided type or auto-detect from directory names
        if database_type:
            db_name = f'{database_type} Database'
        else:
            db_name = 'Database'  # Default
            for dir_name in all_data.keys():
                if 'postgresql' in dir_name.lower():
                    db_name = 'PostgreSQL Database'
                    break
                elif 'mariadb' in dir_name.lower():
                    db_name = 'MariaDB Database'
                    break
        
        # Customize the graph
        plt.xlabel('Number of Users', fontsize=12, fontweight='bold')
        plt.ylabel('Average TPM (Transactions Per Minute)', fontsize=12, fontweight='bold')
        plt.title(f'{db_name} Average TPM Comparison Across Test Runs', fontsize=16, fontweight='bold', pad=20)
        
        # Set x-axis labels
        if chart_type == 'bar':
            plt.xticks([x + bar_width * (len(comparison_data) - 1) / 2 for x in x_pos], test_type_labels, rotation=45, ha='right')
        else:
            plt.xticks(x_pos, test_type_labels, rotation=45, ha='right')
            # Set axes to start at 0 for line/scatter charts for better visibility
            plt.xlim(left=-0.5, right=len(x_pos) - 0.5)
            plt.ylim(bottom=0)
        
        # Add grid and legend
        if chart_type == 'bar':
            plt.grid(True, alpha=0.3, axis='y')
        else:
            plt.grid(True, alpha=0.3)
        
        # Create a table with all values below the legend
        # Prepare table data: rows are test types, columns are directory names
        table_data = []
        dir_names_list = list(comparison_data.keys())
        for test_type in sorted_test_types:
            row = [test_type.replace('_', ' ').title()]
            for dir_name in dir_names_list:
                value = comparison_data[dir_name].get(test_type, 0)
                row.append(f'{value:.0f}' if value > 0 else '-')
            table_data.append(row)
        
        # Create column headers - allow longer directory names (up to 50 chars)
        table_headers = ['Test Type'] + [name[:50] + '...' if len(name) > 50 else name for name in dir_names_list]
        
        # Add table - position it to the right with more width for directory names
        ax = plt.gca()
        table = ax.table(cellText=table_data,
                        colLabels=table_headers,
                        cellLoc='left',  # Left align for better readability of directory names
                        loc='right',
                        bbox=[1.08, 0.05, 0.55, 0.92])  # Increased width from 0.42 to 0.55
        
        # Add legend after table to position it at top
        legend = plt.legend(bbox_to_anchor=(1.7, 1), loc='upper left', fontsize=10)
        
        # Style the table
        table.auto_set_font_size(False)
        table.set_fontsize(9)
        table.scale(1, 1.5)
        
        # Auto-adjust column widths based on content
        num_cols = len(table_headers)
        table.auto_set_column_width(col=list(range(num_cols)))
        
        # Color header row
        for i in range(len(table_headers)):
            table[(0, i)].set_facecolor('#4472C4')
            table[(0, i)].set_text_props(weight='bold', color='white')
            # Center align header text for better appearance
            table[(0, i)]._loc = 'center'
        
        # Center align numeric columns for better readability
        for i in range(1, len(table_data) + 1):
            # Test Type column - keep left aligned
            table[(i, 0)]._loc = 'left'
            # Numeric columns - center align
            for j in range(1, len(table_headers)):
                table[(i, j)]._loc = 'center'
        
        # Alternate row colors for better readability
        for i in range(1, len(table_data) + 1):
            for j in range(len(table_headers)):
                if i % 2 == 0:
                    table[(i, j)].set_facecolor('#D9E1F2')
                else:
                    table[(i, j)].set_facecolor('white')
        
        # Adjust layout to make room for wider table
        plt.subplots_adjust(right=0.45)  # More space for wider table (reduced from 0.55)
        plt.tight_layout()
        
        # Save the graph using atomic write
        output_path = os.path.join(output_dir, f'Average_TPM_Comparison_{chart_type}.png')
        save_figure_atomic(None, output_path)
        plt.close()
        print(f"Comparison graph saved: {output_path}")
        
    except Exception as e:
        print(f"Error creating comparison graph: {e}")


def create_total_tpm_comparison_graph(all_data, test_types_union, output_dir, chart_type='line', database_type=None, user_filter=None, show_values=False):
    """
    Create a comparison graph showing total TPM values (sum of all machines) across different test directories.
    
    Args:
        all_data (dict): Dictionary mapping directory names to DataFrames
        test_types_union (set): Set of all test types found across directories
        output_dir (str): Output directory for the graph
        chart_type (str): Type of chart to create ('scatter', 'line', 'bar') - default 'line' for comparison
        database_type (str): Optional database type ('MariaDB' or 'PostgreSQL') for graph titles
        user_filter (set): Optional set of test types to include (e.g., {"1_user", "20_users"})
    """
    try:
        # Apply user filter if provided
        if user_filter is not None:
            test_types_union = test_types_union.intersection(user_filter)
            if not test_types_union:
                print("No test types match user filter for total TPM comparison")
                return
        
        # Calculate total TPM (sum) for each directory and test type
        comparison_data = {}
        
        for dir_name, df in all_data.items():
            if 'Test_Type' not in df.columns:
                continue
            
            # Apply filter to dataframe
            if user_filter is not None:
                df = filter_test_types(df, user_filter)
                
            dir_totals = {}
            for test_type in test_types_union:
                test_data = df[df['Test_Type'] == test_type]
                if not test_data.empty:
                    total_tpm = test_data['TPM'].sum()  # Sum instead of mean
                    dir_totals[test_type] = total_tpm
            
            comparison_data[dir_name] = dir_totals
        
        if not comparison_data:
            print("No comparison data available for total TPM")
            return
        
        # Create the comparison graph
        plt.figure(figsize=(16, 10))
        
        # Prepare data for plotting - sort by user count, not alphabetically
        sorted_test_types = sorted(test_types_union, key=get_user_count_from_test_type)
        test_type_labels = [test_type.replace('_', ' ').title() for test_type in sorted_test_types]
        x_pos = range(len(test_type_labels))
        
        # Colors for different directories
        colors = ['steelblue', 'orange', 'green', 'red', 'purple', 'brown', 'pink', 'gray', 'olive', 'cyan']
        markers = ['o', 's', '^', 'D', 'v', 'p', '*', 'h', 'H', '+']
        
        # Calculate bar_width for bar charts (needed for x-axis positioning)
        bar_width = 0.8 / len(comparison_data) if chart_type == 'bar' else None
        
        # Calculate max value across all directories for label positioning
        max_value = 0
        if comparison_data:
            for dir_totals in comparison_data.values():
                for test_type in sorted_test_types:
                    max_value = max(max_value, dir_totals.get(test_type, 0))
        
        # Plot based on chart_type
        if chart_type == 'line':
            # Plot lines for each directory
            for i, (dir_name, dir_totals) in enumerate(comparison_data.items()):
                values = [dir_totals.get(test_type, 0) for test_type in sorted_test_types]
                plt.plot(x_pos, values, marker=markers[i % len(markers)], linewidth=2, markersize=8,
                        label=dir_name, color=colors[i % len(colors)], alpha=0.8)
                # Add value labels on top of markers if show_values is enabled
                if show_values and max_value > 0:
                    # Collect all x positions and values for this directory (filter out zeros)
                    dir_x_pos = [x_pos[j] for j, v in enumerate(values) if v > 0]
                    dir_values = [v for v in values if v > 0]
                    if dir_values:
                        add_value_labels_with_offset(dir_x_pos, dir_values, max_value, fontsize=10)
        elif chart_type == 'scatter':
            # Plot scatter points for each directory
            for i, (dir_name, dir_totals) in enumerate(comparison_data.items()):
                values = [dir_totals.get(test_type, 0) for test_type in sorted_test_types]
                plt.scatter(x_pos, values, s=100, marker=markers[i % len(markers)],
                          label=dir_name, color=colors[i % len(colors)], alpha=0.8)
        else:  # bar (default)
            # Plot bars for each directory
            max_bar_value = 0
            all_bars = []  # Store all bars for value labeling
            for i, (dir_name, dir_totals) in enumerate(comparison_data.items()):
                values = [dir_totals.get(test_type, 0) for test_type in sorted_test_types]
                bars = plt.bar([x + i * bar_width for x in x_pos], values, 
                              bar_width, label=dir_name, color=colors[i % len(colors)], alpha=0.8)
                all_bars.append((bars, values))
                max_bar_value = max(max_bar_value, max(values) if values else 0)
            
            # Add value labels on top of bars if show_values is enabled
            if show_values and max_bar_value > 0:
                for bars, values in all_bars:
                    for bar, value in zip(bars, values):
                        if value > 0:
                            plt.text(bar.get_x() + bar.get_width()/2, bar.get_height() + max_bar_value * 0.01,
                                    format_tpm_value(value), ha='center', va='bottom', 
                                    fontsize=9, fontweight='bold')
        
        # Determine database type - use provided type or auto-detect from directory names
        if database_type:
            db_name = f'{database_type} Database'
        else:
            db_name = 'Database'  # Default
            for dir_name in all_data.keys():
                if 'postgresql' in dir_name.lower():
                    db_name = 'PostgreSQL Database'
                    break
                elif 'mariadb' in dir_name.lower():
                    db_name = 'MariaDB Database'
                    break
        
        # Customize the graph
        plt.xlabel('Number of Users', fontsize=12, fontweight='bold')
        plt.ylabel('Total TPM (Transactions Per Minute)', fontsize=12, fontweight='bold')
        plt.title(f'{db_name} Total TPM Comparison Across Test Runs', fontsize=16, fontweight='bold', pad=20)
        
        # Set x-axis labels
        if chart_type == 'bar':
            plt.xticks([x + bar_width * (len(comparison_data) - 1) / 2 for x in x_pos], test_type_labels, rotation=45, ha='right')
        else:
            plt.xticks(x_pos, test_type_labels, rotation=45, ha='right')
            # Set axes to start at 0 for line/scatter charts for better visibility
            plt.xlim(left=-0.5, right=len(x_pos) - 0.5)
            plt.ylim(bottom=0)
        
        # Add grid and legend
        if chart_type == 'bar':
            plt.grid(True, alpha=0.3, axis='y')
        else:
            plt.grid(True, alpha=0.3)
        
        # Create a table with all values below the legend
        # Prepare table data: rows are test types, columns are directory names
        table_data = []
        dir_names_list = list(comparison_data.keys())
        for test_type in sorted_test_types:
            row = [test_type.replace('_', ' ').title()]
            for dir_name in dir_names_list:
                value = comparison_data[dir_name].get(test_type, 0)
                row.append(f'{value:.0f}' if value > 0 else '-')
            table_data.append(row)
        
        # Create column headers - allow longer directory names (up to 50 chars)
        table_headers = ['Test Type'] + [name[:50] + '...' if len(name) > 50 else name for name in dir_names_list]
        
        # Add table - position it to the right with more width for directory names
        ax = plt.gca()
        table = ax.table(cellText=table_data,
                        colLabels=table_headers,
                        cellLoc='left',  # Left align for better readability of directory names
                        loc='right',
                        bbox=[1.08, 0.05, 0.55, 0.92])  # Increased width from 0.42 to 0.55
        
        # Add legend after table to position it at top
        legend = plt.legend(bbox_to_anchor=(1.7, 1), loc='upper left', fontsize=10)
        
        # Style the table
        table.auto_set_font_size(False)
        table.set_fontsize(9)
        table.scale(1, 1.5)
        
        # Auto-adjust column widths based on content
        num_cols = len(table_headers)
        table.auto_set_column_width(col=list(range(num_cols)))
        
        # Color header row
        for i in range(len(table_headers)):
            table[(0, i)].set_facecolor('#4472C4')
            table[(0, i)].set_text_props(weight='bold', color='white')
            # Center align header text for better appearance
            table[(0, i)]._loc = 'center'
        
        # Center align numeric columns for better readability
        for i in range(1, len(table_data) + 1):
            # Test Type column - keep left aligned
            table[(i, 0)]._loc = 'left'
            # Numeric columns - center align
            for j in range(1, len(table_headers)):
                table[(i, j)]._loc = 'center'
        
        # Alternate row colors for better readability
        for i in range(1, len(table_data) + 1):
            for j in range(len(table_headers)):
                if i % 2 == 0:
                    table[(i, j)].set_facecolor('#D9E1F2')
                else:
                    table[(i, j)].set_facecolor('white')
        
        # Adjust layout to make room for wider table
        plt.subplots_adjust(right=0.45)  # More space for wider table (reduced from 0.55)
        plt.tight_layout()
        
        # Save the graph using atomic write
        output_path = os.path.join(output_dir, f'Total_TPM_Comparison_{chart_type}.png')
        save_figure_atomic(None, output_path)
        plt.close()
        print(f"Total TPM comparison graph saved: {output_path}")
        
    except Exception as e:
        print(f"Error creating total TPM comparison graph: {e}")


def create_detailed_comparison_graphs(all_data, test_types_union, output_dir, chart_type='scatter', database_type=None, user_filter=None, show_values=False):
    """
    Create detailed comparison graphs for each test type across directories.
    
    Args:
        all_data (dict): Dictionary mapping directory names to DataFrames
        test_types_union (set): Set of all test types found across directories
        output_dir (str): Output directory for the graphs
        chart_type (str): Type of chart to create ('scatter', 'line', 'bar')
        database_type (str): Optional database type ('MariaDB' or 'PostgreSQL') for graph titles
        user_filter (set): Optional set of test types to include (e.g., {"1_user", "20_users"})
    """
    try:
        # Apply user filter if provided
        if user_filter is not None:
            test_types_union = test_types_union.intersection(user_filter)
            if not test_types_union:
                print("No test types match user filter for detailed comparison")
                return
        
        for test_type in sorted(test_types_union, key=get_user_count_from_test_type):
            # Collect data for this test type from all directories
            test_data = {}
            
            for dir_name, df in all_data.items():
                if 'Test_Type' in df.columns:
                    type_data = df[df['Test_Type'] == test_type].copy()
                    if not type_data.empty:
                        test_data[dir_name] = type_data
            
            if not test_data:
                continue
            
            # Create graph for this test type
            plt.figure(figsize=(16, 10))
            
            # Colors for different directories
            colors = ['steelblue', 'orange', 'green', 'red', 'purple', 'brown', 'pink', 'gray', 'olive', 'cyan']
            
            # Determine the maximum number of machines in any single directory
            # This ensures we show Machine1 to MachineN where N is the max count per directory
            max_machines_per_dir = 0
            for df in test_data.values():
                unique_vms = df['VM_Number'].nunique()
                max_machines_per_dir = max(max_machines_per_dir, unique_vms)
            
            num_machines = max_machines_per_dir
            
            # Create per-directory mappings from actual VM numbers to sequential positions
            # Each directory's VMs are mapped independently to 1, 2, 3, ... N
            dir_vm_mappings = {}
            for dir_name, df in test_data.items():
                df_sorted = df.sort_values('VM_Number').dropna(subset=['VM_Number'])
                unique_vms = sorted(df_sorted['VM_Number'].unique())
                # Map each VM in this directory to sequential position
                dir_vm_mappings[dir_name] = {vm_num: idx + 1 for idx, vm_num in enumerate(unique_vms)}
            
            if chart_type == 'bar':
                # For bar charts, we need to create side-by-side bars
                # Calculate bar width and positions
                num_dirs = len(test_data)
                bar_width = 0.8 / num_dirs
                
                for i, (dir_name, df) in enumerate(test_data.items()):
                    # Sort by VM number
                    df_sorted = df.sort_values('VM_Number')
                    df_sorted = df_sorted.dropna(subset=['VM_Number', 'TPM'])
                    
                    if not df_sorted.empty:
                        # Create positions for this directory's bars using sequential positions (1 to N)
                        x_positions = []
                        y_values = []
                        vm_mapping = dir_vm_mappings[dir_name]
                        
                        # Iterate through sequential positions (1 to num_machines)
                        for seq_pos in range(1, num_machines + 1):
                            # Find the actual VM number that maps to this sequential position
                            actual_vm_num = None
                            for vm_num, pos in vm_mapping.items():
                                if pos == seq_pos:
                                    actual_vm_num = vm_num
                                    break
                            
                            if actual_vm_num is not None:
                                vm_data = df_sorted[df_sorted['VM_Number'] == actual_vm_num]
                                if not vm_data.empty:
                                    x_positions.append(seq_pos + (i - num_dirs/2 + 0.5) * bar_width)
                                    y_values.append(vm_data['TPM'].iloc[0])
                                else:
                                    x_positions.append(seq_pos + (i - num_dirs/2 + 0.5) * bar_width)
                                    y_values.append(0)
                            else:
                                # This directory doesn't have data for this sequential position
                                    x_positions.append(seq_pos + (i - num_dirs/2 + 0.5) * bar_width)
                                    y_values.append(0)
                        
                        bars = plt.bar(x_positions, y_values, 
                               width=bar_width, alpha=0.7,
                               color=colors[i % len(colors)], label=dir_name)
                        # Add value labels on top of bars if show_values is enabled
                        if show_values and y_values:
                            max_bar_val = max([v for v in y_values if v > 0] or [0])
                            if max_bar_val > 0:
                                for bar, value in zip(bars, y_values):
                                    if value > 0:
                                        plt.text(bar.get_x() + bar.get_width()/2, bar.get_height() + max_bar_val * 0.01,
                                                format_tpm_value(value), ha='center', va='bottom', 
                                                fontsize=9, fontweight='bold')
            else:
                # For line and scatter charts, use sequential positions (1 to N per directory)
                for i, (dir_name, df) in enumerate(test_data.items()):
                    # Sort by VM number
                    df_sorted = df.sort_values('VM_Number')
                    df_sorted = df_sorted.dropna(subset=['VM_Number', 'TPM'])
                    
                    if not df_sorted.empty:
                        # Create complete data series using sequential positions (1 to num_machines)
                        sequential_positions = []
                        complete_tpm_values = []
                        vm_mapping = dir_vm_mappings[dir_name]
                        
                        # Iterate through sequential positions (1 to num_machines)
                        for seq_pos in range(1, num_machines + 1):
                            # Find the actual VM number that maps to this sequential position
                            actual_vm_num = None
                            for vm_num, pos in vm_mapping.items():
                                if pos == seq_pos:
                                    actual_vm_num = vm_num
                                    break
                            
                            sequential_positions.append(seq_pos)
                            if actual_vm_num is not None:
                                vm_data = df_sorted[df_sorted['VM_Number'] == actual_vm_num]
                                if not vm_data.empty:
                                    complete_tpm_values.append(vm_data['TPM'].iloc[0])
                                else:
                                    complete_tpm_values.append(float('nan'))  # Missing data
                            else:
                                # This directory doesn't have data for this sequential position
                                complete_tpm_values.append(float('nan'))  # Missing data
                        
                        if chart_type == 'line':
                            plt.plot(sequential_positions, complete_tpm_values, 
                                    marker='o', linewidth=2, markersize=6, 
                                    color=colors[i % len(colors)], label=dir_name, alpha=0.8)
                        else:  # scatter (default)
                            plt.scatter(sequential_positions, complete_tpm_values, 
                                       s=50, alpha=0.8, 
                                       color=colors[i % len(colors)], label=dir_name)
            
            # Determine database types being compared - use provided type or auto-detect
            if database_type:
                db_name = f'{database_type} Database'
                title_prefix = f'{db_name} TPM Performance Comparison'
            else:
                db_types = set()
                for dir_name in test_data.keys():
                    if 'postgresql' in dir_name.lower():
                        db_types.add('PostgreSQL')
                    elif 'mariadb' in dir_name.lower():
                        db_types.add('MariaDB')
                    else:
                        db_types.add('Database')
                
                # Create title based on what's being compared
                if len(db_types) == 1:
                    # Single database type
                    db_name = list(db_types)[0]
                    title_prefix = f'{db_name} TPM Performance Comparison'
                elif len(db_types) == 2 and 'PostgreSQL' in db_types and 'MariaDB' in db_types:
                    # Comparing PostgreSQL vs MariaDB
                    title_prefix = 'PostgreSQL vs MariaDB TPM Performance Comparison'
                else:
                    # Mixed or unknown database types
                    db_list = ', '.join(sorted(db_types))
                    title_prefix = f'{db_list} TPM Performance Comparison'
            
            # Customize the graph
            test_type_label = test_type.replace('_', ' ').title()
            plt.xlabel('Machines', fontsize=12, fontweight='bold')
            plt.ylabel('TPM (Transactions Per Minute)', fontsize=12, fontweight='bold')
            plt.title(f'{title_prefix} - {test_type_label}', fontsize=16, fontweight='bold', pad=20)
            
            # Set X-axis labels using sequential positions (Machine1, Machine2, etc.)
            # Use sequential positions (1, 2, 3, ...) for x-axis
            sequential_positions = list(range(1, num_machines + 1))
            machine_labels = [f'Machine{i}' for i in sequential_positions]
            
            # Set x-axis limits to show all machines
            if sequential_positions:
                if chart_type == 'bar':
                    plt.xlim(0.5, num_machines + 0.5)
                else:
                    # For line and scatter charts, start at 0 for better visibility
                    plt.xlim(0, num_machines + 0.5)
                    plt.ylim(bottom=0)
            
            if num_machines <= 20:
                # Show all machine labels if 20 or fewer
                plt.xticks(sequential_positions, machine_labels, rotation=45, ha='right')
            else:
                # Show every 10th machine if more than 20 machines
                x_positions = []
                x_labels = []
                for i, pos in enumerate(sequential_positions):
                    if (i + 1) % 10 == 1 or i == 0:  # 1st, 11th, 21st, etc.
                        x_positions.append(pos)
                        x_labels.append(machine_labels[i])
                plt.xticks(x_positions, x_labels, rotation=45, ha='right')
            
            # Add grid
            plt.grid(True, alpha=0.3)
            
            # Create a summary statistics table
            # Calculate statistics for each directory
            table_data = []
            dir_names_list = list(test_data.keys())
            
            for dir_name in dir_names_list:
                df_sorted = test_data[dir_name].sort_values('VM_Number')
                df_sorted = df_sorted.dropna(subset=['VM_Number', 'TPM'])
                
                if not df_sorted.empty:
                    avg_tpm = df_sorted['TPM'].mean()
                    min_tpm = df_sorted['TPM'].min()
                    max_tpm = df_sorted['TPM'].max()
                    # Count unique VM numbers, not rows (some VMs might have multiple entries)
                    count = df_sorted['VM_Number'].nunique()
                    
                    # Use full directory name (or truncate only if extremely long)
                    display_name = dir_name[:50] + '...' if len(dir_name) > 50 else dir_name
                    table_data.append([
                        display_name,
                        f'{avg_tpm:.0f}',
                        f'{min_tpm:.0f}',
                        f'{max_tpm:.0f}',
                        f'{count}'
                    ])
            
            if table_data:
                # Create column headers
                table_headers = ['Directory', 'Avg TPM', 'Min TPM', 'Max TPM', 'VMs']
                
                # Add table to the right with more width for directory names
                ax = plt.gca()
                
                # Prepare cell text with proper alignment - Directory column left-aligned, others centered
                # We'll create the table with left alignment for readability of directory names
                table = ax.table(cellText=table_data,
                                colLabels=table_headers,
                                cellLoc='left',  # Left align for directory names readability
                                loc='right',
                                bbox=[1.08, 0.05, 0.55, 0.92])  # Increased width from 0.47 to 0.55
                
                # Add legend after table at the top
                legend = plt.legend(bbox_to_anchor=(1.7, 1), loc='upper left', fontsize=10)
                
                # Style the table
                table.auto_set_font_size(False)
                table.set_fontsize(9)
                table.scale(1, 1.8)
                
                # Adjust column widths - Directory column will naturally be wider due to content
                # Auto-adjust column widths based on content
                num_cols = len(table_headers)
                table.auto_set_column_width(col=list(range(num_cols)))
                
                # Color header row
                for i in range(len(table_headers)):
                    table[(0, i)].set_facecolor('#4472C4')
                    table[(0, i)].set_text_props(weight='bold', color='white')
                    # Center align header text for better appearance
                    table[(0, i)]._loc = 'center'
                
                # Center align numeric columns for better readability
                for i in range(1, len(table_data) + 1):
                    for j in range(1, len(table_headers)):  # Skip Directory column (j=0)
                        table[(i, j)]._loc = 'center'
                
                # Alternate row colors for better readability
                for i in range(1, len(table_data) + 1):
                    for j in range(len(table_headers)):
                        if i % 2 == 0:
                            table[(i, j)].set_facecolor('#D9E1F2')
                        else:
                            table[(i, j)].set_facecolor('white')
                
                # Adjust layout to make room for wider table
                plt.subplots_adjust(right=0.45)  # More space for wider table (reduced from 0.5)
            else:
                # No table data, use original layout and add legend
                plt.legend(bbox_to_anchor=(1.02, 1), loc='upper left', fontsize=10)
                plt.subplots_adjust(right=0.75)
            
            plt.tight_layout()
            
            # Save the graph using atomic write
            output_path = os.path.join(output_dir, f'TPM_Comparison_{test_type}_{chart_type}.png')
            save_figure_atomic(None, output_path)
            plt.close()
            print(f"Detailed comparison graph saved: {output_path}")
            
    except Exception as e:
        print(f"Error creating detailed comparison graphs: {e}")


def main():
    parser = argparse.ArgumentParser(description='Extract PostgreSQL and MariaDB benchmark results')
    parser.add_argument('args', nargs='*', default=[],
                       help='Input directories and optional output directory (deprecated: use --input-dir and --output-dir instead)')
    parser.add_argument('--input-dir', action='append', dest='input_dirs',
                       help='Input directory containing database result files (can be specified multiple times: --input-dir dir1 --input-dir dir2, or provide multiple paths: --input-dir dir1 dir2)')
    parser.add_argument('--output-dir', default='postgresql_analysis',
                       help='Output directory for CSV and PNG files (default: postgresql_analysis)')
    parser.add_argument('--compare', action='store_true',
                       help='Create comparison graphs when multiple input directories are provided')
    parser.add_argument('--chart-type', choices=['scatter', 'line', 'bar'], default='scatter',
                       help='Type of chart to create: scatter (dots only), line (with connections), or bar (default: scatter)')
    parser.add_argument('--show-values', action='store_true',
                       help='Show TPM values as labels on line graphs (default: values are not shown)')
    parser.add_argument('--database', choices=['MariaDB', 'PostgreSQL', 'mariadb', 'postgresql'], 
                       help='Specify database type for graph titles (MariaDB or PostgreSQL). If not set, auto-detected from directory names.')
    parser.add_argument('--users', type=str,
                       help='Comma-separated list of user counts to include in graphs (e.g., "1,20" or "1, 20"). Only specified user counts will be shown on graphs.')
    
    args = parser.parse_args()
    
    # Determine input directories and output directory
    if args.input_dirs:
        # Use --input-dir arguments
        input_dirs = args.input_dirs
        
        # Also check if there are positional arguments that should be treated as additional input dirs
        # This handles the case: --input-dir dir1 dir2 (where dir2 becomes a positional arg)
        if args.args:
            # If positional args are provided after --input-dir, treat them as additional input directories
            # (unless they look like they might be the output dir, but --output-dir should be used)
            input_dirs.extend(args.args)
        
        output_dir = args.output_dir
    elif args.args:
        # Fall back to positional arguments for backward compatibility
        if len(args.args) == 1:
            # One argument: treat as input directory, use default output
            input_dirs = args.args
            output_dir = args.output_dir
        else:
            # Multiple arguments: last one is output directory, rest are input directories
            input_dirs = args.args[:-1]
            output_dir = args.args[-1]
    else:
        # No arguments provided, use defaults
        input_dirs = ['postgresql-results-20250828-140741']
        output_dir = args.output_dir
    
    # Validate input directories
    valid_dirs = []
    for input_dir in input_dirs:
        if not os.path.exists(input_dir):
            print(f"Warning: Input directory '{input_dir}' does not exist, skipping")
        else:
            valid_dirs.append(input_dir)
    
    if not valid_dirs:
        print("Error: No valid input directories found")
        return 1
    
    # Parse user filter if provided
    user_filter = parse_user_filter(args.users)
    if user_filter:
        print(f"User filter: {sorted(user_filter, key=get_user_count_from_test_type)}")
    
    print(f"Input directories: {valid_dirs}")
    print(f"Output directory: {output_dir}")
    print(f"Comparison requested: {args.compare}")
    print(f"Chart type: {args.chart_type}")
    print()
    
    if len(valid_dirs) == 1:
        # Single directory - use original processing
        print(f"Processing single directory: {valid_dirs[0]}")
        process_postgresql_results(valid_dirs[0], output_dir, args.chart_type, user_filter, args.show_values)
        
        # Create comparison graphs if explicitly requested
        if args.compare:
            print("\nWarning: --compare flag set but only one input directory provided.")
            print("Comparison graphs require multiple input directories.")
    else:
        # Multiple directories - process each and create comparison graphs
        print(f"Processing {len(valid_dirs)} directories for comparison...")
        for input_dir in valid_dirs:
            dir_name = os.path.basename(input_dir.rstrip('/'))
            dir_output = os.path.join(output_dir, dir_name)
            print(f"Processing {input_dir} -> {dir_output}")
            process_postgresql_results(input_dir, dir_output, args.chart_type, user_filter, args.show_values)
        
        # Create comparison graphs if requested or if multiple directories
        if args.compare or len(valid_dirs) > 1:
            comparison_output = os.path.join(output_dir, 'comparison')
            print(f"\nCreating comparison graphs in: {comparison_output}")
            # Normalize database name (capitalize first letter)
            database_type = None
            if args.database:
                database_type = args.database.capitalize()
                if database_type.lower() == 'postgresql':
                    database_type = 'PostgreSQL'
                elif database_type.lower() == 'mariadb':
                    database_type = 'MariaDB'
            create_comparison_graphs(valid_dirs, comparison_output, args.chart_type, database_type, user_filter, args.show_values)
        else:
            print("\nSkipping comparison graphs (not requested and not automatically triggered)")
    
    return 0


if __name__ == '__main__':
    exit(main())
