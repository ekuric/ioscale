# Database Results Extractor and Graph Generator

A comprehensive Python tool for extracting and visualizing database benchmark results from PostgreSQL and MariaDB performance tests. This script processes `.out` files containing TPM (Transactions Per Minute) data and generates detailed CSV reports and professional PNG graphs.

## Features

- **Multi-Database Support**: Handles both PostgreSQL and MariaDB benchmark results
- **Flexible Input**: Processes multiple test result directories simultaneously
- **Multiple Chart Types**: Choose between scatter plots, line charts, or bar charts
- **Comparison Analysis**: Generate comparison graphs across different test runs
- **Automated Sorting**: User counts are properly sorted (1 User, 10 Users, 20 Users, etc.)
- **Professional Output**: High-quality graphs with legends positioned outside the plot area
- **Comprehensive Reports**: Detailed CSV files with summary statistics

## Supported Database Types

### PostgreSQL
- Files containing `test_postgresql_pg` and `PostgreSQL TPM` results
- Extracts TPM values from "System achieved" lines

### MariaDB
- Files containing `test_mariadb` and `MySQL TPM` results
- Extracts TPM values from "System achieved" lines

## Installation

### Prerequisites
- Python 3.6 or higher
- Required Python packages:
  ```bash
  pip install matplotlib pandas
  ```

### Setup
1. Clone or download the script
2. Ensure the script is executable:
   ```bash
   chmod +x extract_db_results.py
   ```

## Usage

### Basic Usage

#### Single Directory Processing
```bash
python3 extract_db_results.py [input_directory] [output_directory]
```

#### Multiple Directories with Comparison
```bash
python3 extract_db_results.py dir1 dir2 dir3 [output_directory]
```

### Command Line Options

| Option | Description | Default |
|--------|-------------|---------|
| `input_dirs` | One or more input directories containing `.out` files | `postgresql-results-20250828-140741` |
| `output_dir` | Output directory for CSV and PNG files | `postgresql_analysis` |
| `--compare` | Force creation of comparison graphs | `False` |
| `--chart-type` | Chart type: `scatter`, `line`, or `bar` | `scatter` |

### Chart Types

#### Scatter Plots (Default)
- Shows individual data points as dots
- No connecting lines between points
- Best for: Individual machine performance analysis
```bash
python3 extract_db_results.py --chart-type scatter input_dir output_dir
```

#### Line Charts
- Shows data points connected by lines
- Good for: Trend analysis and performance patterns
```bash
python3 extract_db_results.py --chart-type line input_dir output_dir
```

#### Bar Charts
- Shows data as vertical bars
- Best for: Comparing discrete values and highlighting differences
```bash
python3 extract_db_results.py --chart-type bar input_dir output_dir
```

## Examples

### Example 1: Basic Single Directory Processing
```bash
python3 extract_db_results.py postgresql-results-20250828-140741 my_analysis
```

### Example 2: Multiple Directories with Bar Charts
```bash
python3 extract_db_results.py --chart-type bar test1 test2 test3 comparison_results
```

### Example 3: Force Comparison Graphs
```bash
python3 extract_db_results.py --compare --chart-type line single_test results
```

### Example 4: Complex Multi-Directory Analysis
```bash
python3 extract_db_results.py --chart-type scatter \
    postgresql-results-20250828-140741 \
    mariadb-results-20250829-140741 \
    postgresql-results-20250830-140741 \
    comprehensive_analysis
```

## Output Structure

### Single Directory Output
```
output_directory/
├── postgresql_detailed_results.csv
├── postgresql_summary_1_user.csv
├── postgresql_summary_10_users.csv
├── postgresql_summary_20_users.csv
├── postgresql_overall_summary.csv
├── postgresql_detailed_results_1_user_tpm_graph.png
├── postgresql_detailed_results_10_users_tpm_graph.png
├── postgresql_detailed_results_20_users_tpm_graph.png
└── Average_tpm_1_user_10_users_20_users.png
```

### Multiple Directory Output
```
output_directory/
├── directory1/
│   ├── postgresql_detailed_results.csv
│   ├── postgresql_summary_*.csv
│   └── *.png (individual graphs)
├── directory2/
│   ├── postgresql_detailed_results.csv
│   ├── postgresql_summary_*.csv
│   └── *.png (individual graphs)
└── comparison/
    ├── Average_TPM_Comparison.png
    └── TPM_Comparison_*.png
```

## Output Files

### CSV Files

#### Detailed Results (`{database}_detailed_results.csv`)
Contains all individual test results with columns:
- `VM_Number`: Virtual machine identifier
- `VM_Name`: Directory name of the VM
- `Test_Type`: Type of test (e.g., `1_user`, `10_users`)
- `TPM`: Transactions Per Minute
- `NOPM`: New Orders Per Minute

#### Summary Files (`{database}_summary_{test_type}.csv`)
Individual test type summaries with columns:
- `VM_Number`: Virtual machine identifier
- `VM_Name`: Directory name of the VM
- `TPM`: Transactions Per Minute
- `NOPM`: New Orders Per Minute

#### Overall Summary (`{database}_overall_summary.csv`)
Aggregate statistics with columns:
- `Test_Type`: Type of test
- `VM_Count`: Number of VMs tested
- `Total_TPM`: Sum of all TPM values
- `Total_NOPM`: Sum of all NOPM values
- `Avg_TPM`: Average TPM across all VMs
- `Avg_NOPM`: Average NOPM across all VMs

### PNG Graphs

#### Individual Test Type Graphs
- `{database}_detailed_results_{test_type}_tpm_graph.png`
- Shows TPM performance for each VM in a specific test type
- X-axis: VM Number, Y-axis: TPM

#### Average TPM Graph
- `Average_tpm_{test_types}.png`
- Bar chart showing average TPM for each test type
- User counts properly sorted (1 User, 10 Users, 20 Users, etc.)

#### Comparison Graphs (Multiple Directories)
- `Average_TPM_Comparison.png`: Bar chart comparing average TPM across test runs
- `TPM_Comparison_{test_type}.png`: Line/scatter/bar graphs comparing performance for each test type

## Input File Structure

The script expects input directories with the following structure:
```
input_directory/
├── vm-1/
│   ├── test_postgresql_pg_1.out
│   ├── test_postgresql_pg_10.out
│   └── test_postgresql_pg_20.out
├── vm-2/
│   ├── test_postgresql_pg_1.out
│   ├── test_postgresql_pg_10.out
│   └── test_postgresql_pg_20.out
└── vm-3/
    ├── test_mariadb_1.out
    ├── test_mariadb_10.out
    └── test_mariadb_20.out
```

## Graph Features

- **High Resolution**: All graphs are saved at 300 DPI for publication quality
- **Professional Styling**: Clean, modern appearance with proper fonts and colors
- **External Legends**: All legends are positioned outside the graph area for better readability
- **Proper Sorting**: User counts are sorted numerically (1, 10, 20, 30, etc.) not alphabetically
- **Grid Lines**: Subtle grid lines for easier value reading
- **Statistics Box**: Individual graphs include average, max, and min TPM values

## Error Handling

The script includes comprehensive error handling:
- Validates input directories exist
- Skips invalid or missing files gracefully
- Provides informative error messages
- Continues processing even if individual files fail

## Performance

- Processes large numbers of files efficiently
- Memory-efficient CSV generation
- Optimized graph rendering
- Parallel processing capabilities for multiple directories

## Troubleshooting

### Common Issues

#### "No valid input directories found"
- Ensure input directories exist and contain `.out` files
- Check directory permissions

#### "No Test_Type column found"
- Verify input files contain proper test result format
- Check that files contain "System achieved" TPM lines

#### Empty graphs
- Ensure input files contain valid TPM data
- Check file encoding (script handles UTF-8 with error tolerance)

### Debug Mode
For verbose output, you can modify the script to add debug prints or run with Python's verbose mode:
```bash
python3 -v extract_db_results.py input_dir output_dir
```

## Contributing

To contribute to this project:
1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test thoroughly with sample data
5. Submit a pull request

## License

This project is open source. Please check the license file for details.

## Support

For issues, questions, or feature requests, please:
1. Check this README for common solutions
2. Review the script's built-in help: `python3 extract_db_results.py --help`
3. Create an issue in the project repository

## Version History

- **v1.0**: Initial release with basic PostgreSQL support
- **v2.0**: Added MariaDB support and multiple directory processing
- **v3.0**: Added chart type selection and improved sorting
- **v4.0**: Enhanced legend positioning and comparison graphs
