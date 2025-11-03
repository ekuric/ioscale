#!/usr/bin/env python3
"""
Database Results Extractor and Graph Generator

This script extracts "System achieved" TPM (Transactions Per Minute) values
from PostgreSQL and MariaDB benchmark result files. It processes all .out files in any 
subdirectories (directory name agnostic), creates CSV reports with the extracted data, 
and generates PNG graphs showing VM numbers vs TPM performance.

Supports both:
- PostgreSQL: Files containing "test_postgresql_pg" and "PostgreSQL TPM" results
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
    
    # Legacy syntax (still supported for backward compatibility)
    python3 extract_db_results.py [input_directory] [output_directory]
    python3 extract_db_results.py dir1 dir2 dir3 output_directory

Arguments:
    --input-dir: Input directory containing subdirectories with .out files (can be specified multiple times)
    --output-dir: Directory to save CSV and PNG output files (default: postgresql_analysis)
    --compare: Force creation of comparison graphs
    --chart-type: Type of chart to create - scatter (dots only), line (with connections), or bar (default: scatter)
    
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
from pathlib import Path
from collections import defaultdict
import matplotlib.pyplot as plt
import pandas as pd


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


def create_tpm_graphs(csv_file_path, output_dir, chart_type='scatter'):
    """
    Create PNG graphs from database CSV files showing VM numbers vs TPM values.
    
    Args:
        csv_file_path (str): Path to the CSV file
        output_dir (str): Output directory for PNG files
        chart_type (str): Type of chart to create ('scatter', 'line', 'bar')
    """
    try:
        # Read the CSV file
        df = pd.read_csv(csv_file_path)
        
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
                filename = f"{base_name}_{test_type}_tpm_graph.png"
            else:
                # Use all data if no Test_Type column
                test_data = df.copy()
                graph_title = f"{db_name} TPM Performance - {base_name.replace('_', ' ').title()}"
                filename = f"{base_name}_tpm_graph.png"
            
            # Sort by VM number and remove any rows with NaN values
            test_data = test_data.sort_values('VM_Number')
            test_data = test_data.dropna(subset=['VM_Number', 'TPM'])
            
            if test_data.empty:
                print(f"No valid data found for {test_type}, skipping graph")
                continue
            
            # Create the graph
            plt.figure(figsize=(15, 8))
            
            if chart_type == 'bar':
                plt.bar(test_data['VM_Number'], test_data['TPM'], alpha=0.7, width=0.8)
            elif chart_type == 'line':
                plt.plot(test_data['VM_Number'], test_data['TPM'], marker='o', linewidth=2, markersize=6)
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
            
            # Save the graph
            output_path = os.path.join(output_dir, filename)
            plt.savefig(output_path, dpi=300, bbox_inches='tight')
            plt.close()
            
            print(f"Graph saved: {output_path}")
            
    except Exception as e:
        print(f"Error creating graph from {csv_file_path}: {e}")


def create_average_tpm_graph(csv_file_path, output_dir):
    """
    Create a graph showing average TPM values for all tested user counts.
    
    Args:
        csv_file_path (str): Path to the detailed CSV file
        output_dir (str): Output directory for PNG files
    """
    try:
        # Read the CSV file
        df = pd.read_csv(csv_file_path)
        
        if 'Test_Type' not in df.columns:
            print("No Test_Type column found, skipping average TPM graph")
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
        
        # Create bar chart with labels for legend
        colors = ['steelblue', 'orange', 'green', 'red', 'purple'][:len(test_types)]
        bars = plt.bar(test_type_labels, avg_tpm_values, color=colors, alpha=0.8, label=[f'{label}: {value:.0f} TPM' for label, value in zip(test_type_labels, avg_tpm_values)])
        
        # Add value labels on top of bars
        for bar, value in zip(bars, avg_tpm_values):
            plt.text(bar.get_x() + bar.get_width()/2, bar.get_height() + max(avg_tpm_values) * 0.01,
                    f'{value:.0f}', ha='center', va='bottom', fontsize=12, fontweight='bold')
        
        # Customize the graph (title will be updated later with machine count)
        plt.xlabel('Number of Users', fontsize=12, fontweight='bold')
        plt.ylabel('Average TPM (Transactions Per Minute)', fontsize=12, fontweight='bold')
        
        # Rotate x-axis labels if needed
        plt.xticks(rotation=45, ha='right')
        
        # Add grid for better readability
        plt.grid(True, alpha=0.3, axis='y')
        
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
        
        # Add legend outside the plot area at top right
        plt.legend(bbox_to_anchor=(1.02, 1), loc='upper left', fontsize=10)
        
        # Adjust layout to make room for legend on the right
        plt.subplots_adjust(right=0.75)
        plt.tight_layout()
        
        # Save the graph
        output_path = os.path.join(output_dir, f'Average_tpm_{"_".join(test_types)}.png')
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        plt.close()
        
        print(f"Average TPM graph saved: {output_path}")
        
    except Exception as e:
        print(f"Error creating average TPM graph from {csv_file_path}: {e}")


def create_combined_tpm_graph(csv_file_path, output_dir, chart_type='scatter'):
    """
    Create a combined graph showing both test types on the same plot.
    
    Args:
        csv_file_path (str): Path to the detailed CSV file
        output_dir (str): Output directory for PNG files
        chart_type (str): Type of chart to create ('scatter', 'line', 'bar')
    """
    try:
        # Read the CSV file
        df = pd.read_csv(csv_file_path)
        
        if 'Test_Type' not in df.columns:
            print("No Test_Type column found, skipping combined graph")
            return
        
        # Create the combined graph
        plt.figure(figsize=(15, 8))
        
        # Plot each test type with different colors and markers
        # Use distinct colors that are easy to differentiate
        colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', 
                 '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf',
                 '#aec7e8', '#ffbb78', '#98df8a', '#ff9896', '#c5b0d5',
                 '#c49c94', '#f7b6d3', '#c7c7c7', '#dbdb8d', '#9edae5']
        markers = ['o', 's', '^', 'D', 'v', 'p', '*', 'h', 'H', '+', 'x', 'X', 'd', '|', '_']
        
        # Get sorted test types for consistent ordering
        sorted_test_types = sorted(df['Test_Type'].unique(), key=get_user_count_from_test_type)
        print(f"Creating combined graph with test types: {sorted_test_types}")
        
        for i, test_type in enumerate(sorted_test_types):
            test_data = df[df['Test_Type'] == test_type].sort_values('VM_Number')
            color = colors[i % len(colors)]
            marker = markers[i % len(markers)]
            print(f"  {test_type}: color={color}, marker={marker}")
            
            if chart_type == 'bar':
                plt.bar(test_data['VM_Number'], test_data['TPM'], 
                       alpha=0.7, width=0.8, 
                       label=test_type.replace('_', ' ').title(), color=color)
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
        
        # Save the graph
        output_path = os.path.join(output_dir, f'{db_name.lower()}_combined_tpm_comparison.png')
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        plt.close()
        
        print(f"Combined graph saved: {output_path}")
        
    except Exception as e:
        print(f"Error creating combined graph from {csv_file_path}: {e}")


def process_postgresql_results(input_dir, output_dir, chart_type='scatter'):
    """
    Process all database result files (PostgreSQL and MariaDB) and extract TPM values.
    
    Args:
        input_dir (str): Input directory containing subdirectories with .out files
        output_dir (str): Output directory for CSV files
        chart_type (str): Type of chart to create ('scatter', 'line', 'bar')
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
            out_files = [f for f in os.listdir(item_path) if f.endswith('.out') and ('test_postgresql_pg' in f or 'test_mariadb' in f)]
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
        out_files = [f for f in os.listdir(vm_path) if f.endswith('.out') and ('test_postgresql_pg' in f or 'test_mariadb' in f)]
        
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
    
    # Create graphs from detailed results
    create_tpm_graphs(detailed_csv, output_dir, chart_type)
    
    # Create combined comparison graph (disabled - confusing)
    # create_combined_tpm_graph(detailed_csv, output_dir, chart_type)
    
    # Create average TPM graph
    create_average_tpm_graph(detailed_csv, output_dir)
    
    # Create graphs from summary files
    for test_type in sorted(test_types, key=get_user_count_from_test_type):
        summary_csv = os.path.join(output_dir, f'{db_type}_summary_{test_type}.csv')
        if os.path.exists(summary_csv):
            create_tpm_graphs(summary_csv, output_dir, chart_type)
    
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


def create_comparison_graphs(input_dirs, output_dir, chart_type='scatter'):
    """
    Create comparison graphs across multiple test result directories.
    
    Args:
        input_dirs (list): List of input directories to compare
        output_dir (str): Output directory for comparison graphs
        chart_type (str): Type of chart to create ('scatter', 'line', 'bar')
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
        process_postgresql_results(input_dir, temp_output, chart_type)
        
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
    
    # Create comparison graphs
    create_average_tpm_comparison_graph(all_data, test_types_union, output_dir)
    create_detailed_comparison_graphs(all_data, test_types_union, output_dir, chart_type)


def create_average_tpm_comparison_graph(all_data, test_types_union, output_dir):
    """
    Create a comparison graph showing average TPM values across different test directories.
    
    Args:
        all_data (dict): Dictionary mapping directory names to DataFrames
        test_types_union (set): Set of all test types found across directories
        output_dir (str): Output directory for the graph
    """
    try:
        # Calculate average TPM for each directory and test type
        comparison_data = {}
        
        for dir_name, df in all_data.items():
            if 'Test_Type' not in df.columns:
                continue
                
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
        
        # Plot bars for each directory
        bar_width = 0.8 / len(comparison_data)
        for i, (dir_name, dir_averages) in enumerate(comparison_data.items()):
            values = [dir_averages.get(test_type, 0) for test_type in sorted_test_types]
            bars = plt.bar([x + i * bar_width for x in x_pos], values, 
                          bar_width, label=dir_name, color=colors[i % len(colors)], alpha=0.8)
            
            # Add value labels on top of bars
            for bar, value in zip(bars, values):
                if value > 0:
                    plt.text(bar.get_x() + bar.get_width()/2, bar.get_height() + max(values) * 0.01,
                            f'{value:.0f}', ha='center', va='bottom', fontsize=10, fontweight='bold')
        
        # Determine database type from directory names
        db_name = 'Database'  # Default
        for dir_name in all_data.keys():
            if 'postgresql' in dir_name.lower():
                db_name = 'PostgreSQL'
                break
            elif 'mariadb' in dir_name.lower():
                db_name = 'MariaDB'
                break
        
        # Customize the graph
        plt.xlabel('Number of Users', fontsize=12, fontweight='bold')
        plt.ylabel('Average TPM (Transactions Per Minute)', fontsize=12, fontweight='bold')
        plt.title(f'{db_name} Average TPM Comparison Across Test Runs', fontsize=16, fontweight='bold', pad=20)
        
        # Set x-axis labels
        plt.xticks([x + bar_width * (len(comparison_data) - 1) / 2 for x in x_pos], test_type_labels, rotation=45, ha='right')
        
        # Add grid and legend
        plt.grid(True, alpha=0.3, axis='y')
        plt.legend(bbox_to_anchor=(1.02, 1), loc='upper left', fontsize=10)
        
        # Adjust layout
        plt.subplots_adjust(right=0.75)
        plt.tight_layout()
        
        # Save the graph
        output_path = os.path.join(output_dir, 'Average_TPM_Comparison.png')
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        plt.close()
        
        print(f"Comparison graph saved: {output_path}")
        
    except Exception as e:
        print(f"Error creating comparison graph: {e}")


def create_detailed_comparison_graphs(all_data, test_types_union, output_dir, chart_type='scatter'):
    """
    Create detailed comparison graphs for each test type across directories.
    
    Args:
        all_data (dict): Dictionary mapping directory names to DataFrames
        test_types_union (set): Set of all test types found across directories
        output_dir (str): Output directory for the graphs
        chart_type (str): Type of chart to create ('scatter', 'line', 'bar')
    """
    try:
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
            
            if chart_type == 'bar':
                # For bar charts, we need to create side-by-side bars
                # First, collect all VM numbers and create a mapping
                all_vm_numbers = set()
                for df in test_data.values():
                    all_vm_numbers.update(df['VM_Number'].unique())
                all_vm_numbers = sorted(all_vm_numbers)
                
                # Calculate bar width and positions
                num_dirs = len(test_data)
                bar_width = 0.8 / num_dirs
                
                for i, (dir_name, df) in enumerate(test_data.items()):
                    # Sort by VM number
                    df_sorted = df.sort_values('VM_Number')
                    df_sorted = df_sorted.dropna(subset=['VM_Number', 'TPM'])
                    
                    if not df_sorted.empty:
                        # Create positions for this directory's bars
                        x_positions = []
                        y_values = []
                        
                        for vm_num in all_vm_numbers:
                            vm_data = df_sorted[df_sorted['VM_Number'] == vm_num]
                            if not vm_data.empty:
                                x_positions.append(vm_num + (i - num_dirs/2 + 0.5) * bar_width)
                                y_values.append(vm_data['TPM'].iloc[0])
                            else:
                                x_positions.append(vm_num + (i - num_dirs/2 + 0.5) * bar_width)
                                y_values.append(0)
                        
                        plt.bar(x_positions, y_values, 
                               width=bar_width, alpha=0.7,
                               color=colors[i % len(colors)], label=dir_name)
            else:
                # For line and scatter charts, use the original logic
                for i, (dir_name, df) in enumerate(test_data.items()):
                    # Sort by VM number
                    df_sorted = df.sort_values('VM_Number')
                    df_sorted = df_sorted.dropna(subset=['VM_Number', 'TPM'])
                    
                    if not df_sorted.empty:
                        if chart_type == 'line':
                            plt.plot(df_sorted['VM_Number'], df_sorted['TPM'], 
                                    marker='o', linewidth=2, markersize=6, 
                                    color=colors[i % len(colors)], label=dir_name, alpha=0.8)
                        else:  # scatter (default)
                            plt.scatter(df_sorted['VM_Number'], df_sorted['TPM'], 
                                       s=50, alpha=0.8, 
                                       color=colors[i % len(colors)], label=dir_name)
            
            # Determine database types being compared
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
            
            # Set X-axis labels based on number of machines
            all_vm_numbers = set()
            for df in test_data.values():
                all_vm_numbers.update(df['VM_Number'].unique())
            all_vm_numbers = sorted(all_vm_numbers)
            num_machines = len(all_vm_numbers)
            
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
            
            # Add grid and legend
            plt.grid(True, alpha=0.3)
            plt.legend(bbox_to_anchor=(1.02, 1), loc='upper left', fontsize=10)
            
            # Adjust layout
            plt.subplots_adjust(right=0.75)
            plt.tight_layout()
            
            # Save the graph
            output_path = os.path.join(output_dir, f'TPM_Comparison_{test_type}.png')
            plt.savefig(output_path, dpi=300, bbox_inches='tight')
            plt.close()
            
            print(f"Detailed comparison graph saved: {output_path}")
            
    except Exception as e:
        print(f"Error creating detailed comparison graphs: {e}")


def main():
    parser = argparse.ArgumentParser(description='Extract PostgreSQL and MariaDB benchmark results')
    parser.add_argument('args', nargs='*', default=[],
                       help='Input directories and optional output directory (deprecated: use --input-dir and --output-dir instead)')
    parser.add_argument('--input-dir', action='append', dest='input_dirs',
                       help='Input directory containing database result files (can be specified multiple times)')
    parser.add_argument('--output-dir', default='postgresql_analysis',
                       help='Output directory for CSV and PNG files (default: postgresql_analysis)')
    parser.add_argument('--compare', action='store_true',
                       help='Create comparison graphs when multiple input directories are provided')
    parser.add_argument('--chart-type', choices=['scatter', 'line', 'bar'], default='scatter',
                       help='Type of chart to create: scatter (dots only), line (with connections), or bar (default: scatter)')
    
    args = parser.parse_args()
    
    # Determine input directories and output directory
    if args.input_dirs:
        # Use --input-dir arguments
        input_dirs = args.input_dirs
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
    
    print(f"Input directories: {valid_dirs}")
    print(f"Output directory: {output_dir}")
    print()
    
    if len(valid_dirs) == 1:
        # Single directory - use original processing
        process_postgresql_results(valid_dirs[0], output_dir, args.chart_type)
    else:
        # Multiple directories - process each and create comparison graphs
        for input_dir in valid_dirs:
            dir_name = os.path.basename(input_dir.rstrip('/'))
            dir_output = os.path.join(output_dir, dir_name)
            print(f"Processing {input_dir} -> {dir_output}")
            process_postgresql_results(input_dir, dir_output, args.chart_type)
        
        # Create comparison graphs if requested or if multiple directories
        if args.compare or len(valid_dirs) > 1:
            comparison_output = os.path.join(output_dir, 'comparison')
            create_comparison_graphs(valid_dirs, comparison_output, args.chart_type)
    
    return 0


if __name__ == '__main__':
    exit(main())
