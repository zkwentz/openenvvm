#!/usr/bin/env python3
"""Update README.md with benchmark results from sandbox-bench."""

import json
import os
import re
import sys
from datetime import datetime


def main():
    results_file = sys.argv[1] if len(sys.argv) > 1 else "benchmark-results.json"

    if not os.path.exists(results_file):
        print(f"No results file found: {results_file}")
        return

    with open(results_file) as f:
        data = json.load(f)

    results = data.get("results", [])
    if not results:
        print("No results in file")
        return

    # Generate markdown table
    md = []
    md.append("| Provider | Time | Tool Calls | Errors | Score | Grade |")
    md.append("|----------|------|------------|--------|-------|-------|")

    for r in sorted(results, key=lambda x: x.get("score", 0), reverse=True):
        provider = r.get("provider", "unknown")
        time_s = r.get("total_time_seconds", 0)
        if time_s >= 60:
            time_str = f"{int(time_s // 60)}m {int(time_s % 60)}s"
        else:
            time_str = f"{time_s:.1f}s"
        calls = r.get("tool_calls", 0)
        errors = r.get("errors", 0)
        score = r.get("score", 0)
        grade = r.get("grade", "?")

        md.append(f"| {provider} | {time_str} | {calls} | {errors} | {score:.1f} | {grade} |")

    table = "\n".join(md)

    # Find docker-image and microvm results for comparison
    docker_result = next((r for r in results if r["provider"] == "docker-image"), None)
    microvm_result = next((r for r in results if r["provider"] == "microvm"), None)

    comparison = ""
    if docker_result and microvm_result:
        docker_time = docker_result.get("total_time_seconds", 1)
        microvm_time = microvm_result.get("total_time_seconds", 1)
        if microvm_time > 0:
            speedup = docker_time / microvm_time
            comparison = f"\n**MicroVM is {speedup:.1f}x faster than Docker** ({docker_time:.1f}s vs {microvm_time:.1f}s)"

    # Update README
    date = datetime.now().strftime('%Y-%m-%d %H:%M UTC')

    # Build the new section (using concatenation to avoid YAML parsing issues)
    new_section = "## Latest Benchmark Results\n\n"
    new_section += f"*Last updated: {date}*\n\n"
    new_section += table + "\n"
    new_section += comparison + "\n\n"
    new_section += "> Benchmarks run automatically in CI using [sandbox-bench](https://github.com/zkwentz/sandbox-bench).\n"
    new_section += "> See [workflow runs](https://github.com/zkwentz/openenvvm/actions/workflows/benchmark.yml) for details.\n\n"
    new_section += "## Performance"

    with open("README.md", "r") as f:
        readme = f.read()

    # Replace the section between "## Latest Benchmark Results" and "## Performance"
    pattern = r'## Latest Benchmark Results.*?(?=## Performance\b|## Quick Start)'
    if re.search(pattern, readme, re.DOTALL):
        readme = re.sub(pattern, new_section + "\n\n", readme, flags=re.DOTALL)
    else:
        # Try to find "Performance Results" section
        pattern = r'## Performance Results.*?(?=## Quick Start)'
        if re.search(pattern, readme, re.DOTALL):
            readme = re.sub(pattern, new_section + "\n\n## Quick Start", readme, flags=re.DOTALL)
        else:
            # Insert after badges
            pattern = r'(# OpenEnvVM\n\n(?:\[.*?\]\n\n)+)'
            if re.search(pattern, readme, re.DOTALL):
                readme = re.sub(pattern, r'\1' + new_section + "\n\n", readme, flags=re.DOTALL)

    with open("README.md", "w") as f:
        f.write(readme)

    print("README updated successfully")
    print(f"\nResults table:\n{table}")
    print(f"\nComparison: {comparison}")

    # Write outputs for GitHub Actions
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"table<<EOF\n{table}\nEOF\n")
            f.write(f"comparison<<EOF\n{comparison}\nEOF\n")
            f.write(f"date={date}\n")


if __name__ == "__main__":
    main()
