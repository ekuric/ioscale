# FIO BW and IOPS Analysis Tools

A comprehensive suite of Python tools for analyzing FIO (Flexible I/O Tester) performance results, with support for IOPS and bandwidth analysis, graph generation, and test comparison.

## Table of Contents

- [Overview](#overview)
- [Installation](#installation)
- [Tools](#tools)
  - [analyze_bw_mean_with_line_graphs.py](#analyze_bw_mean_with_line_graphspy)
  - [test_comparison_analyzer.py](#test_comparison_analyzerpy)
- [Usage Examples](#usage-examples)
- [Output Files](#output-files)
- [Requirements](#requirements)
- [File Structure](#file-structure)

## Overview

This toolkit provides comprehensive analysis of FIO test results with the following capabilities:

- **IOPS Analysis**: Extract and analyze IOPS ( iops_mean ) metrics from FIO JSON files
- **Bandwidth Analysis**: Extract and analyze bandwidth (bw_mean) metrics
- **Graph Generation**: Create bar charts and line graphs for visualization
- **Test Comparison**: Compare results between different test runs
- **Flexible Output**: Save results to custom directories
- **Multiple Operations**: Support for read, write, randread, randwrite operations
- **Block Size Analysis**: Automatic detection and analysis of different block sizes

## Installation

### Prerequisites

- Python 3.6 or higher
- FIO test results in JSON format

### Install Dependencies

```bash
pip install -r requirements.txt
```

Or install manually:

```bash
pip install pandas matplotlib numpy seaborn 
```

## Tools

### analyze_bw_mean_with_graphs.py 

**Main IOPS and BW analysis tool** that processes FIO JSON files and generates comprehensive analysis reports.

#### Features

- Extracts IOPS and BW data from FIO JSON files in in structure `test-fio/*` directories - this means if we put fio results inside `test-fio` directories saved in json format it will analyze these files and created graphs.
run `python3 analyze_bw_mean_with_graphs.py --help` for comprehensive help output. 

- Generates CSV files with integer IOPS values
- Creates PNG graphs (bar charts, line graphs, or both)
- Supports all operation types: read, write, randread, randwrite
- Automatic block size detection and separate files per operation/block size
- Operation summary files combining all block sizes
- Custom results directory support

#### Usage

```bash
$ python3 analyze_bw_mean_with_graphs.py [-h] [--iops] [--bw] [--input-dir INPUT_DIR] [--output-dir OUTPUT_DIR] [--graph-type {bar,line,both}] [--block-sizes BLOCK_SIZES] [--operation-summary] [--summary-only]
```

#### Options

- `--graph-type {bar,line,both}`: Type of graphs to generate
- `--operation-summary`: Generate operation summary files (all block sizes combined)
- `--block-sizes BLOCK_SIZES`: Comma-separated list of block sizes to include in operation summary (e.g., "4k,8k,128k")
- `--input-dir`: INPUT_DIR Directory containing FIO JSON files in subdirectories (any name).
- `--summary-only`: Generate only summary graphs (skip per-VM comparison graphs)
- `--output-dir`: Directory to save output files (CSV and PNG) 
- `--help`: Show help message

#### Examples

We need to run single command to generate results

```bash
python3 analyze_bw_mean_with_graphs.py --input-dir fio-results --output-dir iops-bw-output --graph-type line  --operation-summary --summary-only --iops –bw 
```
After executing above command results will be saved to `--output-dir` for further analysis. 


### test_comparison_analyzer.py

**Test comparison tool** that compares IOPS and bandwidth results between different test runs.

#### Features

- Compares results from different test directories
- Generates comparison graphs (bar and line charts)
- Creates comprehensive comparison reports
- Supports both IOPS and bandwidth metrics
- Block size filtering for focused analysis
- Rate IOPS information in legends
- Average per VM and total metrics

#### Usage

```bash
test_comparison_analyzer.py [-h] [--graphs {bar,line,both,none}] [--output-dir OUTPUT_DIR] [--block-sizes [BLOCK_SIZES ...]] [--summary-only] [--iops | --bw] directories [directories ...]
```

#### Options

  - `-h --help`: show this help message and exit
  - `--graphs {bar,line,both,none}`: Type of graphs to generate
  - `--output-dir`: OUTPUT_DIR Output directory for results (default: current directory)
  `--block-sizes:` [BLOCK_SIZES ...] Specify block sizes to analyze (e.g., --block-sizes 4k 8k 128k). If not specified, all available block sizes will be used.
  `--summary-only`: Generate only summary graphs (skip per-VM comparison graphs)
  `--iops`: Analyze IOPS performance (default)
  `--bw:`:  Analyze bandwidth performance

#### Examples

```bash
# Compare two test directories and generate iops summary 
python3 test_comparison_analyzer.py fio-results-default/ fio-results-rate_iops_100/ --graphs bar  --output-dir test1vstest2 --iops

# Compare two test directories and generate bandwidth  summary 
python3 test_comparison_analyzer.py fio-results-default/ fio-results-rate_iops_100/ --graphs bar  --output-dir test1vstest2 --bw
```
#### Examples

```bash
# Basic comparison
./compare_tests.sh test1/ test2/

# Compare with specific options
./compare_tests.sh -g bar -o results/ test1/ test2/

# Compare bandwidth with specific block sizes
./compare_tests.sh -b -s 4k 8k 128k test1/ test2/
```


## Output Files

### IOPS Analyzer Output

- **CSV Files**: `summary-{operation}-{block_size}.csv`
- **Bar Charts**: `summary-{operation}-{block_size}_bar.png`
- **Line Charts**: `summary-{operation}-{block_size}_line-chart.png`
- **Simple Graphs**: `summary-{operation}-{block_size}.png`
- **Operation Summary**: `summary-{operation}-all-blocks.csv`
- **Operation Summary Graphs**: `summary-{operation}-all-blocks_comparison-{block_sizes}_{graph_type}.png`

### Bandwidth Analyzer Output

- **CSV Files**: `bw_mean_{operation}_{block_size}.csv`
- **Bar Charts**: `bw_mean_{operation}_{block_size}_bar.png`
- **Line Charts**: `bw_mean_{operation}_{block_size}_line.png`

### Comparison Analyzer Output

- **Comparison CSV**: `comparison_{operation}_{metric}.csv`
- **Comparison Graphs**: `comparison_{operation}_{block_size}_per_vm_{graph_type}_{metric}.png`
- **Summary Graphs**: `comparison_{operation}_summary_{graph_type}_{metric}.png`
- **Comparison Report**: `comparison_report.txt`

## File Structure

### Expected Input Structure

```
project_directory/
├── vm-1/
│   ├── fio-test-read-bs-4k.json
│   ├── fio-test-write-bs-4k.json
│   ├── fio-test-randread-bs-8k.json
│   └── ...
├── vm-2/
│   ├── fio-test-read-bs-4k.json
│   └── ...
└── vm-N/
    └── ...
```

### Output Structure

```
results_directory/
├── summary-read-4k.csv
├── summary-read-4k_bar.png
├── summary-read-4k_line-chart.png
├── summary-write-8k.csv
├── summary-write-8k_bar.png
├── comparison_report.txt
└── ...
```

## Tips and Best Practices

### 1. Organize Your Results

```bash
# Create organized directory structure
mkdir -p results/{baseline,optimized,comparison}
python3 test_comparison_analyzer.py results/baseline/ results/optimized/ --output-dir results/comparison/
```

### 2. Focus on Specific Block Sizes

```bash
# Analyze only relevant block sizes
python3 test_comparison_analyzer.py test1/ test2/ --block-sizes 4k 8k 128k
```

### 3. Generate Both Graph Types

```bash
# Generate both bar and line charts for comprehensive analysis
python3 test_comparison_analyzer.py test1/ test2/ --graphs both
```

### 4. Use Descriptive Directory Names

```bash
# Use descriptive names for different test runs
python3 analyze_bw_mean_with_graphs.py  --input-dir  results/2024-01-15_baseline_100vm
```

## Troubleshooting

### Common Issues

1. **No vm-* directories found**
   - Ensure FIO test results are in the correct directory structure
   - Check that machine-* directories contain JSON files

2. **Missing dependencies**
   - Install required packages: `pip install -r requirements.txt`
   - Check Python version: `python3 --version`

3. **Permission errors**
   - Ensure write permissions for output directories
   - Use absolute paths if relative paths cause issues

4. **Empty or zero results**
   - Check FIO JSON files for valid data
   - Verify that tests completed successfully
   - Check for rate limiting or other FIO configuration issues


### Getting Help

- Use `--help` flag with any tool for detailed usage information
- Check the script docstrings for additional details
- Verify input file format and structure

## Contributing

When adding new features or fixing issues:

1. Maintain backward compatibility
2. Update this README.md with new features
3. Add appropriate help text and examples
4. Test with various input configurations
5. Follow the existing code style and structure



## Directory Structure Expected for test_comparison_anaylzer.py 

```
baseline/ (or any directory name)
├── vm-1/
│   ├── fio-test-read-bs-4k.json
│   ├── fio-test-write-bs-4k.json
│   ├── fio-test-randread-bs-4k.json
│   ├── fio-test-randwrite-bs-4k.json
│   └── ... (other block sizes)
└── vm-2/
    ├── fio-test-read-bs-4k.json
    ├── fio-test-write-bs-4k.json
    └── ... (other block sizes)

optimized/ (or any directory name)
├── vm-1/
│   ├── fio-test-read-bs-4k.json
│   ├── fio-test-write-bs-4k.json
│   └── ... (other block sizes)
└── vm-2/
    ├── fio-test-read-bs-4k.json
    ├── fio-test-write-bs-4k.json
    └── ... (other block sizes)
```

This tool expect below

- fio output json files must have fio `operation` name and `block size` in its name, for example `fio-read-4k.json` or `write-fio-1024k.json` are valid names
- it expect fio output to be in `json` format so ensure your fio results are save in json format
- directory structure as stated above

In future releases we will make it to work without these limitations. 


## Output Files

The tool generates several types of output files:

### CSV Files
- `test_comparison_summary.csv` - Complete comparison data
- `comparison_{operation}_read_{metric}.csv` - Read metric comparison by operation (iops or bw)
- `comparison_{operation}_write_{metric}.csv` - Write metric comparison by operation (iops or bw)
- `{vm_name}_detailed_metrics.csv` - Individual machine detailed metrics (one file per VM)

### PNG Graphs
- `comparison_{operation}_summary_bar_{metric}.png` - Summary bar chart comparison with total metric values (sum across all VMs) displayed on bars
- `comparison_{operation}_summary_line_{metric}.png` - Summary line chart comparison with total metric values (sum across all VMs) displayed on data points
- `comparison_{operation}_{block_size}_per_vm_bar_{metric}.png` - Per-VM bar chart showing individual VM performance for specific block size (test1 vs test2 vs test3) with total sum annotations
- `comparison_{operation}_{block_size}_per_vm_line_{metric}.png` - Per-VM line chart showing individual VM performance for specific block size (test1 vs test2 vs test3) with total sum annotations

**Note**: `{metric}` is either `iops` or `bw` depending on the analysis type (--iops or --bw option)

### Text Report
- `comparison_report.txt` - Human-readable comparison report with improvement percentages

## Features

1. **Automatic Data Extraction**: Extracts IOPS, bandwidth, and latency data from FIO JSON files
   - **Multi-Job Support**: Automatically sums metrics across multiple FIO jobs running on the same machine
2. **Multiple Directory Support**: Compare 2 or more test directories in a single analysis
3. **Metric Selection**: Choose between IOPS analysis (default) or bandwidth analysis (--bw option)
4. **Sum Analysis**: Shows the total sum of all VMs for each FIO operation (e.g., sum of read IOPS/bandwidth from vm-1 + vm-2 + vm-3 + ...)
5. **Summary Comparison Graphs**: Creates single graphs comparing all test directories for each operation (sum across all VMs)
6. **Per-VM Comparison Graphs**: Creates detailed graphs showing individual VM performance across different test directories (like iops_analyzer.py)
   - **One graph per block size**: Each graph shows test1 vs test2 vs test3 for a specific block size only
   - **Clean comparison**: Easier to read and compare test results for each block size separately
   - **Total sum annotations**: Shows "Total across all VMs" values under the legend on the right side for organized display
7. **Per-Machine CSV Files**: Creates individual CSV files for each machine with detailed metrics
   - **One CSV per machine**: Each VM gets its own detailed metrics file (e.g., `vm-1_detailed_metrics.csv`)
   - **Complete metrics**: Includes IOPS, bandwidth, latency, and file information for all operations and block sizes
   - **No graphs needed**: Use `--graphs none` to generate only CSV files without any graphs
8. **FIO Configuration Subtitles**: Displays FIO job configuration on all graphs
   - **Complete configuration**: Shows size, block size, runtime, direct, numjobs, iodepth, and rate_iops (if used)
   - **Automatic extraction**: Extracts configuration from FIO JSON files automatically
   - **All graph types**: Subtitles appear on summary graphs, per-VM graphs, bar charts, and line charts
9. **Data Points Display**: Shows metric values directly on graphs (on bars for bar charts, on data points for line charts)
10. **Multiple Graph Types**: Supports bar charts, line charts, or both
11. **Performance Metrics**: Calculates improvement percentages between first and last test directories
12. **Comprehensive Reports**: Generates both CSV data and human-readable reports
13. **Flexible Output**: Supports custom output directories
14. **Flexible Directory Naming**: Works with any directory names (baseline/optimized, before/after, etc.)

## Example Output

The tool will show:
- Overall performance comparison between all test directories
- **Multi-job aggregation**: Automatically sums IOPS and bandwidth across multiple FIO jobs per machine
- IOPS or bandwidth improvements/degradations by operation and block size
- **Sum of all VMs** for each FIO operation (total performance across all machines)
- **Individual VM performance** across different test directories and block sizes
- Summary comparison graphs with data points showing exact metric values
- Per-VM comparison graphs showing detailed individual machine performance
- **Total sum annotations**: "Total across all VMs" values displayed under the legend on the right side of each per-block-size graph
- **Individual machine CSV files**: Detailed metrics for each VM in separate CSV files
- Performance differences across all block sizes for each operation
- Visual comparison with precise numerical values displayed on graphs
- Improvement percentages from first to last test directory

