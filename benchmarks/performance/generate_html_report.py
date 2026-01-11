#!/usr/bin/env python3
"""Generate HTML report from benchmark CSV results."""

import csv
import sys
from datetime import datetime
from pathlib import Path


def format_rate(rate):
    """Format rate as ops/sec with appropriate units."""
    if rate >= 1000:
        return f"{rate:,.0f}"
    elif rate >= 100:
        return f"{rate:.0f}"
    elif rate >= 10:
        return f"{rate:.1f}"
    else:
        return f"{rate:.2f}"


def format_duration(duration):
    """Format duration in seconds."""
    if duration >= 60:
        return f"{duration / 60:.1f}m"
    elif duration >= 1:
        return f"{duration:.2f}s"
    else:
        return f"{duration * 1000:.0f}ms"


def get_class_for_value(value, best_value, worst_value, lower_is_better=False):
    """Determine CSS class based on value comparison."""
    if value == best_value:
        return "good"
    elif value == worst_value:
        return "bad"
    return ""


def generate_flat_nested_table(csv_path, test_name):
    """Generate HTML table for flat or nested directory tests."""
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        return "<p>No data available</p>"

    # Group by scale
    scales = {}
    for row in rows:
        scale = row["scale"]
        if scale not in scales:
            scales[scale] = []
        scales[scale].append(row)

    html = f"<h2>{test_name}</h2>\n"

    for scale in sorted(scales.keys()):
        scale_rows = scales[scale]
        html += f"<h3>{scale} Files</h3>\n"
        html += "<table>\n"
        html += "<thead>\n"
        html += "<tr><th>Method</th><th>List (files/s)</th><th>Read (files/s)</th><th>Stat (files/s)</th><th>Upload (files/s)</th></tr>\n"
        html += "</thead>\n<tbody>\n"

        # Calculate best/worst for each metric
        list_rates = [float(r["list_rate"]) for r in scale_rows]
        read_rates = [float(r["read_rate"]) for r in scale_rows]
        stat_rates = [float(r["stat_rate"]) for r in scale_rows]

        for row in scale_rows:
            method = row["method"].replace("_", " ").title()
            list_rate = float(row["list_rate"])
            read_rate = float(row["read_rate"])
            stat_rate = float(row["stat_rate"])
            upload_rate = row["upload_rate"]

            list_class = get_class_for_value(list_rate, max(list_rates), min(list_rates))
            read_class = get_class_for_value(read_rate, max(read_rates), min(read_rates))
            stat_class = get_class_for_value(stat_rate, max(stat_rates), min(stat_rates))

            html += "<tr>"
            html += f"<td>{method}</td>"
            html += f"<td class='{list_class}'>{format_rate(list_rate)}</td>"
            html += f"<td class='{read_class}'>{format_rate(read_rate)}</td>"
            html += f"<td class='{stat_class}'>{format_rate(stat_rate)}</td>"
            html += f"<td>{format_rate(float(upload_rate)) if upload_rate else '-'}</td>"
            html += "</tr>\n"

        html += "</tbody>\n</table>\n"

    return html


def generate_grep_table(csv_path):
    """Generate HTML table for grep tests."""
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        return "<p>No data available</p>"

    # Group by scale
    scales = {}
    for row in rows:
        scale = row["scale"]
        if scale not in scales:
            scales[scale] = []
        scales[scale].append(row)

    html = "<h2>Grep Search Performance</h2>\n"

    for scale in sorted(scales.keys()):
        scale_rows = scales[scale]
        html += f"<h3>{scale} Files</h3>\n"
        html += "<table>\n"
        html += "<thead>\n"
        html += "<tr><th>Method</th><th>Search Rate (files/s)</th><th>Matches Found</th><th>Total Bytes</th><th>Upload (files/s)</th></tr>\n"
        html += "</thead>\n<tbody>\n"

        # Calculate best/worst
        grep_rates = [float(r["grep_rate"]) for r in scale_rows]

        for row in scale_rows:
            method = row["method"].replace("_", " ").title()
            grep_rate = float(row["grep_rate"])
            match_count = row["match_count"]
            total_bytes = int(row["total_bytes"])
            upload_rate = row["upload_rate"]

            rate_class = get_class_for_value(grep_rate, max(grep_rates), min(grep_rates))

            # Highlight if match count is 0 (bug)
            match_class = "bad" if match_count == "0" else ""

            html += "<tr>"
            html += f"<td>{method}</td>"
            html += f"<td class='{rate_class}'>{format_rate(grep_rate)}</td>"
            html += f"<td class='{match_class}'>{match_count}</td>"
            html += f"<td>{total_bytes:,}</td>"
            html += f"<td>{format_rate(float(upload_rate)) if upload_rate else '-'}</td>"
            html += "</tr>\n"

        html += "</tbody>\n</table>\n"

    return html


def generate_html_report(result_dir):
    """Generate complete HTML report."""
    result_dir = Path(result_dir)

    flat_csv = result_dir / "flat_comparison_results.csv"
    grep_csv = result_dir / "grep_comparison_results.csv"
    nested_csv = result_dir / "nested_comparison_results.csv"

    html = (
        """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Nexus Performance Test Results</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
            margin: 20px;
            max-width: 1200px;
            margin-left: auto;
            margin-right: auto;
        }
        h1 {
            color: #333;
            border-bottom: 3px solid #4CAF50;
            padding-bottom: 10px;
        }
        h2 {
            color: #555;
            margin-top: 40px;
            border-bottom: 2px solid #ddd;
            padding-bottom: 5px;
        }
        h3 {
            color: #666;
            margin-top: 30px;
        }
        table {
            border-collapse: collapse;
            width: 100%;
            margin-bottom: 30px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        th, td {
            border: 1px solid #ddd;
            padding: 12px 8px;
            text-align: right;
        }
        th {
            background-color: #f5f5f5;
            font-weight: bold;
            color: #333;
        }
        td:first-child, th:first-child {
            text-align: left;
            font-weight: 500;
        }
        .good {
            background-color: #c8e6c9;
            font-weight: bold;
        }
        .bad {
            background-color: #ffcdd2;
        }
        .summary {
            background-color: #e3f2fd;
            padding: 20px;
            border-radius: 5px;
            margin-bottom: 30px;
            border-left: 4px solid #2196F3;
        }
        .summary h2 {
            margin-top: 0;
            border: none;
        }
        .summary ul {
            margin: 10px 0;
        }
        .warning {
            background-color: #fff3cd;
            padding: 15px;
            border-radius: 5px;
            margin-bottom: 20px;
            border-left: 4px solid #ffc107;
        }
        .info {
            color: #666;
            font-size: 0.9em;
            margin-top: 10px;
        }
        footer {
            margin-top: 60px;
            padding-top: 20px;
            border-top: 1px solid #ddd;
            color: #666;
            text-align: center;
            font-size: 0.9em;
        }
    </style>
</head>
<body>
    <h1>Nexus Performance Benchmark Results</h1>
    <p><strong>Date:</strong> """
        + datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        + """</p>
    <p><strong>Platform:</strong> macOS + Docker PostgreSQL (perf_benchmark database)</p>
    <p><strong>Test Methods:</strong> Native Bash, Native Python, Nexus (network filesystem)</p>
    <p class="info"><strong>Rate Limiting:</strong> Disabled (NEXUS_RATE_LIMIT_DISABLED=true)</p>

    <div class="summary">
        <h2>Summary</h2>
        <p>This benchmark compares three methods of file operations:</p>
        <ul>
            <li><strong>Native Bash:</strong> Direct filesystem operations using bash commands (ls, cat, grep)</li>
            <li><strong>Native Python:</strong> Direct filesystem operations using Python pathlib</li>
            <li><strong>Nexus:</strong> Network filesystem with PostgreSQL backend</li>
        </ul>
        <p><strong>Test Scales:</strong> 1,000 and 10,000 files</p>
    </div>
"""
    )

    # Add warning if grep shows 0 matches for Nexus
    if grep_csv.exists():
        with open(grep_csv) as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row["method"] == "nexus" and row["match_count"] == "0":
                    html += """
    <div class="warning">
        <strong>⚠️ Known Issue:</strong> Nexus grep returns 0 matches while native methods find 250K+ matches.
        This appears to be a bug in the grep implementation that needs investigation.
    </div>
"""
                    break

    # Generate tables
    if flat_csv.exists():
        html += generate_flat_nested_table(flat_csv, "Flat Directory Performance")

    if grep_csv.exists():
        html += generate_grep_table(grep_csv)

    if nested_csv.exists():
        html += generate_flat_nested_table(nested_csv, "Nested Directory Performance")

    html += (
        """
    <h2>Raw Data Files</h2>
    <ul>
        <li><a href="flat_comparison_results.csv">flat_comparison_results.csv</a> - Flat directory test data</li>
        <li><a href="grep_comparison_results.csv">grep_comparison_results.csv</a> - Grep search test data</li>
        <li><a href="nested_comparison_results.csv">nested_comparison_results.csv</a> - Nested directory test data</li>
    </ul>

    <h2>Test Logs</h2>
    <ul>
        <li><a href="flat_test.log">flat_test.log</a> - Flat directory test output</li>
        <li><a href="grep_test.log">grep_test.log</a> - Grep test output</li>
        <li><a href="nested_test.log">nested_test.log</a> - Nested directory test output</li>
    </ul>

    <footer>
        <p>Generated by: benchmarks/performance/generate_html_report.py</p>
        <p>Source: """
        + str(result_dir)
        + """</p>
    </footer>
</body>
</html>
"""
    )

    return html


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: generate_html_report.py <result_directory>")
        sys.exit(1)

    result_dir = sys.argv[1]
    html = generate_html_report(result_dir)

    output_file = Path(result_dir) / "performance_summary.html"
    with open(output_file, "w") as f:
        f.write(html)

    print(f"✓ Generated HTML report: {output_file}")
