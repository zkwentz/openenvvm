#!/usr/bin/env python3
"""
Benchmark OpenEnv environments: Docker vs MicroVM performance comparison.

This script compares the performance of running OpenEnv environments as
Docker containers versus Firecracker MicroVMs using sandbox-bench.

Usage:
    # Compare echo_env
    python scripts/benchmark.py ./envs/echo_env

    # With pre-built packages
    python scripts/benchmark.py ./envs/echo_env \
        --docker-image echo-env:latest \
        --microvm-package ./echo-env.microvm

    # Multiple runs with JSON output
    python scripts/benchmark.py ./envs/echo_env --runs 10 --output results.json

Requirements:
    - sandbox-bench: pip install sandbox-bench
    - Docker (for Docker benchmarks)
    - Linux + KVM + Firecracker + openenvvm (for MicroVM benchmarks)
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional
import shutil


@dataclass
class BenchmarkResult:
    """Result of a single benchmark run."""
    provider: str
    boot_time_ms: float
    reset_time_ms: float
    step_time_ms: float
    total_time_ms: float
    success: bool
    error: Optional[str] = None


@dataclass
class ComparisonResult:
    """Result of Docker vs MicroVM comparison."""
    env_name: str
    runs: int
    docker_results: list[BenchmarkResult]
    microvm_results: list[BenchmarkResult]

    def docker_avg_boot_ms(self) -> float:
        times = [r.boot_time_ms for r in self.docker_results if r.success]
        return sum(times) / len(times) if times else 0

    def microvm_avg_boot_ms(self) -> float:
        times = [r.boot_time_ms for r in self.microvm_results if r.success]
        return sum(times) / len(times) if times else 0

    def boot_speedup(self) -> float:
        docker = self.docker_avg_boot_ms()
        microvm = self.microvm_avg_boot_ms()
        if microvm == 0:
            return 0
        return docker / microvm

    def docker_avg_total_ms(self) -> float:
        times = [r.total_time_ms for r in self.docker_results if r.success]
        return sum(times) / len(times) if times else 0

    def microvm_avg_total_ms(self) -> float:
        times = [r.total_time_ms for r in self.microvm_results if r.success]
        return sum(times) / len(times) if times else 0


async def benchmark_docker(
    image: str,
    runs: int = 3,
) -> list[BenchmarkResult]:
    """Benchmark an OpenEnv Docker container."""
    import requests

    results = []

    for i in range(runs):
        result = BenchmarkResult(
            provider="docker",
            boot_time_ms=0,
            reset_time_ms=0,
            step_time_ms=0,
            total_time_ms=0,
            success=False,
        )

        container_id = None
        port = 8100 + i

        try:
            # Start container
            start = time.time()
            proc = subprocess.run(
                ["docker", "run", "-d", "-p", f"{port}:8000", image],
                capture_output=True, text=True
            )
            if proc.returncode != 0:
                result.error = f"Failed to start: {proc.stderr}"
                results.append(result)
                continue

            container_id = proc.stdout.strip()

            # Wait for health
            url = f"http://localhost:{port}"
            for _ in range(60):
                try:
                    resp = requests.get(f"{url}/health", timeout=1)
                    if resp.status_code == 200:
                        break
                except requests.RequestException:
                    pass
                await asyncio.sleep(0.5)
            else:
                result.error = "Health check timeout"
                results.append(result)
                continue

            result.boot_time_ms = (time.time() - start) * 1000

            # Test reset
            start = time.time()
            resp = requests.post(f"{url}/reset", json={}, timeout=30)
            result.reset_time_ms = (time.time() - start) * 1000

            # Test step/tool call
            start = time.time()
            try:
                # Try MCP tool call
                resp = requests.get(f"{url}/tools", timeout=10)
                if resp.status_code == 200:
                    tools = resp.json()
                    if tools:
                        tool_name = tools[0].get("name", "echo_message")
                        requests.post(
                            f"{url}/call_tool",
                            json={"name": tool_name, "arguments": {"message": "test"}},
                            timeout=30
                        )
            except requests.RequestException:
                # Try step
                requests.post(f"{url}/step", json={"action": "test"}, timeout=30)
            result.step_time_ms = (time.time() - start) * 1000

            result.total_time_ms = result.boot_time_ms + result.reset_time_ms + result.step_time_ms
            result.success = True

        except Exception as e:
            result.error = str(e)

        finally:
            if container_id:
                subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)

        results.append(result)
        print(f"  Docker run {i+1}/{runs}: {result.boot_time_ms:.0f}ms boot, {result.total_time_ms:.0f}ms total")

    return results


async def benchmark_microvm(
    package_path: str,
    runs: int = 3,
) -> list[BenchmarkResult]:
    """Benchmark an OpenEnv MicroVM."""
    import requests

    # Find openenvvm binary
    openenvvm = shutil.which("openenvvm")
    if not openenvvm:
        for path in ["./target/release/openenvvm", "../openenvvm/target/release/openenvvm"]:
            if os.path.isfile(path):
                openenvvm = path
                break

    if not openenvvm:
        print("  Warning: openenvvm binary not found, skipping MicroVM benchmarks")
        return []

    if sys.platform != "linux":
        print(f"  Warning: MicroVM requires Linux (current: {sys.platform})")
        return []

    if not os.path.exists("/dev/kvm"):
        print("  Warning: KVM not available, skipping MicroVM benchmarks")
        return []

    results = []

    for i in range(runs):
        result = BenchmarkResult(
            provider="microvm",
            boot_time_ms=0,
            reset_time_ms=0,
            step_time_ms=0,
            total_time_ms=0,
            success=False,
        )

        vm_process = None
        ip_address = f"172.16.0.{10 + i}"
        port = 8000

        try:
            # Start MicroVM
            start = time.time()
            vm_process = subprocess.Popen(
                ["sudo", openenvvm, "run", package_path, "--port", str(port), "--ip", ip_address],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            # Wait for health
            url = f"http://{ip_address}:{port}"
            for _ in range(60):
                try:
                    resp = requests.get(f"{url}/health", timeout=1)
                    if resp.status_code == 200:
                        break
                except requests.RequestException:
                    pass
                await asyncio.sleep(0.5)
            else:
                result.error = "Health check timeout"
                results.append(result)
                continue

            result.boot_time_ms = (time.time() - start) * 1000

            # Test reset
            start = time.time()
            resp = requests.post(f"{url}/reset", json={}, timeout=30)
            result.reset_time_ms = (time.time() - start) * 1000

            # Test step/tool call
            start = time.time()
            try:
                resp = requests.get(f"{url}/tools", timeout=10)
                if resp.status_code == 200:
                    tools = resp.json()
                    if tools:
                        tool_name = tools[0].get("name", "echo_message")
                        requests.post(
                            f"{url}/call_tool",
                            json={"name": tool_name, "arguments": {"message": "test"}},
                            timeout=30
                        )
            except requests.RequestException:
                requests.post(f"{url}/step", json={"action": "test"}, timeout=30)
            result.step_time_ms = (time.time() - start) * 1000

            result.total_time_ms = result.boot_time_ms + result.reset_time_ms + result.step_time_ms
            result.success = True

        except Exception as e:
            result.error = str(e)

        finally:
            if vm_process:
                vm_process.terminate()
                try:
                    vm_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    vm_process.kill()

        results.append(result)
        print(f"  MicroVM run {i+1}/{runs}: {result.boot_time_ms:.0f}ms boot, {result.total_time_ms:.0f}ms total")

    return results


def print_comparison(result: ComparisonResult):
    """Print comparison results in a nice table."""
    print()
    print("=" * 70)
    print(f"OpenEnv Benchmark: {result.env_name}")
    print("=" * 70)
    print()

    # Table header
    print("┌" + "─" * 12 + "┬" + "─" * 14 + "┬" + "─" * 14 + "┬" + "─" * 14 + "┬" + "─" * 10 + "┐")
    print("│" + " Provider".ljust(12) + "│" + " Boot (ms)".ljust(14) + "│" + " Reset (ms)".ljust(14) + "│" + " Total (ms)".ljust(14) + "│" + " Success".ljust(10) + "│")
    print("├" + "─" * 12 + "┼" + "─" * 14 + "┼" + "─" * 14 + "┼" + "─" * 14 + "┼" + "─" * 10 + "┤")

    # Docker results
    docker_success = sum(1 for r in result.docker_results if r.success)
    print(
        "│" + " Docker".ljust(12) + "│" +
        f" {result.docker_avg_boot_ms():.0f}".ljust(14) + "│" +
        f" {sum(r.reset_time_ms for r in result.docker_results if r.success) / max(docker_success, 1):.0f}".ljust(14) + "│" +
        f" {result.docker_avg_total_ms():.0f}".ljust(14) + "│" +
        f" {docker_success}/{result.runs}".ljust(10) + "│"
    )

    # MicroVM results
    if result.microvm_results:
        microvm_success = sum(1 for r in result.microvm_results if r.success)
        print(
            "│" + " MicroVM".ljust(12) + "│" +
            f" {result.microvm_avg_boot_ms():.0f}".ljust(14) + "│" +
            f" {sum(r.reset_time_ms for r in result.microvm_results if r.success) / max(microvm_success, 1):.0f}".ljust(14) + "│" +
            f" {result.microvm_avg_total_ms():.0f}".ljust(14) + "│" +
            f" {microvm_success}/{result.runs}".ljust(10) + "│"
        )

    print("└" + "─" * 12 + "┴" + "─" * 14 + "┴" + "─" * 14 + "┴" + "─" * 14 + "┴" + "─" * 10 + "┘")

    # Summary
    if result.microvm_results:
        print()
        print("Performance Summary:")
        print("-" * 40)

        speedup = result.boot_speedup()
        if speedup > 1:
            print(f"  MicroVM boots {speedup:.1f}x faster than Docker")
        elif speedup > 0:
            print(f"  Docker boots {1/speedup:.1f}x faster than MicroVM")

        docker_total = result.docker_avg_total_ms()
        microvm_total = result.microvm_avg_total_ms()
        if docker_total > 0 and microvm_total > 0:
            time_saved = docker_total - microvm_total
            if time_saved > 0:
                print(f"  MicroVM saves {time_saved:.0f}ms ({time_saved/docker_total*100:.1f}%) per run")
            else:
                print(f"  Docker saves {-time_saved:.0f}ms ({-time_saved/microvm_total*100:.1f}%) per run")

    print()


async def main():
    parser = argparse.ArgumentParser(
        description="Benchmark OpenEnv: Docker vs MicroVM performance"
    )
    parser.add_argument(
        "env_path",
        help="Path to OpenEnv environment directory"
    )
    parser.add_argument(
        "--docker-image",
        help="Pre-built Docker image name"
    )
    parser.add_argument(
        "--microvm-package",
        help="Pre-built .microvm package path"
    )
    parser.add_argument(
        "--runs", "-n",
        type=int,
        default=3,
        help="Number of benchmark runs (default: 3)"
    )
    parser.add_argument(
        "--output", "-o",
        help="Output JSON file for results"
    )
    parser.add_argument(
        "--docker-only",
        action="store_true",
        help="Only run Docker benchmarks"
    )
    parser.add_argument(
        "--microvm-only",
        action="store_true",
        help="Only run MicroVM benchmarks"
    )

    args = parser.parse_args()

    env_name = os.path.basename(args.env_path.rstrip("/"))
    print(f"Benchmarking: {env_name}")
    print(f"Runs: {args.runs}")
    print()

    docker_results = []
    microvm_results = []

    # Docker benchmarks
    if not args.microvm_only:
        docker_image = args.docker_image
        if not docker_image:
            # Build Docker image from environment
            print("Building Docker image...")
            tag = f"openenv-bench-{env_name}"
            proc = subprocess.run(
                ["docker", "build", "-t", tag, args.env_path],
                capture_output=True
            )
            if proc.returncode != 0:
                print(f"Failed to build Docker image: {proc.stderr.decode()}")
            else:
                docker_image = tag

        if docker_image:
            print(f"Running Docker benchmarks ({docker_image})...")
            docker_results = await benchmark_docker(docker_image, args.runs)

    # MicroVM benchmarks
    if not args.docker_only:
        microvm_package = args.microvm_package
        if not microvm_package:
            # Try to find existing package
            default_path = f"./{env_name}.microvm"
            if os.path.isdir(default_path):
                microvm_package = default_path
            else:
                print(f"No .microvm package found at {default_path}")
                print("Build one with: openenvvm convert {env_path} -o {env_name}.microvm")

        if microvm_package:
            print(f"Running MicroVM benchmarks ({microvm_package})...")
            microvm_results = await benchmark_microvm(microvm_package, args.runs)

    # Results
    comparison = ComparisonResult(
        env_name=env_name,
        runs=args.runs,
        docker_results=docker_results,
        microvm_results=microvm_results,
    )

    print_comparison(comparison)

    # Output JSON
    if args.output:
        output = {
            "env_name": env_name,
            "runs": args.runs,
            "docker": {
                "avg_boot_ms": comparison.docker_avg_boot_ms(),
                "avg_total_ms": comparison.docker_avg_total_ms(),
                "results": [
                    {
                        "boot_time_ms": r.boot_time_ms,
                        "reset_time_ms": r.reset_time_ms,
                        "step_time_ms": r.step_time_ms,
                        "total_time_ms": r.total_time_ms,
                        "success": r.success,
                        "error": r.error,
                    }
                    for r in docker_results
                ]
            },
            "microvm": {
                "avg_boot_ms": comparison.microvm_avg_boot_ms(),
                "avg_total_ms": comparison.microvm_avg_total_ms(),
                "results": [
                    {
                        "boot_time_ms": r.boot_time_ms,
                        "reset_time_ms": r.reset_time_ms,
                        "step_time_ms": r.step_time_ms,
                        "total_time_ms": r.total_time_ms,
                        "success": r.success,
                        "error": r.error,
                    }
                    for r in microvm_results
                ]
            },
            "comparison": {
                "boot_speedup": comparison.boot_speedup(),
            }
        }

        with open(args.output, "w") as f:
            json.dump(output, f, indent=2)
        print(f"Results written to {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
