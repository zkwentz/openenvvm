"""CLI for openenv-microvm."""

import argparse
import sys


def main():
    """Main CLI entrypoint."""
    parser = argparse.ArgumentParser(
        prog="openenv-microvm",
        description="Convert OpenEnv environments to Firecracker microVMs",
    )
    
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # Convert command
    convert_parser = subparsers.add_parser(
        "convert",
        help="Convert an OpenEnv environment to a microVM package",
    )
    convert_parser.add_argument(
        "env_path",
        help="Path to OpenEnv environment or hf:org/repo for HuggingFace",
    )
    convert_parser.add_argument(
        "-o", "--output",
        required=True,
        help="Output path for the .microvm package",
    )
    convert_parser.add_argument(
        "--memory",
        type=int,
        default=256,
        help="Memory allocation in MB (default: 256)",
    )
    convert_parser.add_argument(
        "--vcpus",
        type=int,
        default=1,
        help="Number of vCPUs (default: 1)",
    )
    convert_parser.add_argument(
        "--kernel",
        help="Path to custom vmlinux kernel",
    )
    
    # Run command
    run_parser = subparsers.add_parser(
        "run",
        help="Run a microVM package",
    )
    run_parser.add_argument(
        "package_path",
        help="Path to the .microvm package",
    )
    run_parser.add_argument(
        "-p", "--port",
        type=int,
        default=8000,
        help="Port to expose (default: 8000)",
    )
    run_parser.add_argument(
        "--ip",
        default="172.16.0.2",
        help="IP address for the VM (default: 172.16.0.2)",
    )
    
    # Pool command
    pool_parser = subparsers.add_parser(
        "pool",
        help="Run a pool of microVMs",
    )
    pool_parser.add_argument(
        "package_path",
        help="Path to the .microvm package",
    )
    pool_parser.add_argument(
        "-n", "--size",
        type=int,
        default=8,
        help="Pool size (default: 8)",
    )
    pool_parser.add_argument(
        "-p", "--port",
        type=int,
        default=8000,
        help="Base port (default: 8000)",
    )
    
    args = parser.parse_args()
    
    if args.command == "convert":
        from .convert import convert_env_to_microvm
        
        print(f"Converting {args.env_path} to microVM...")
        output = convert_env_to_microvm(
            args.env_path,
            args.output,
            kernel_path=args.kernel,
            memory_mb=args.memory,
            vcpu_count=args.vcpus,
        )
        print(f"Created microVM package at {output}")
        
    elif args.command == "run":
        from .runtime import start_microvm
        
        print(f"Starting microVM from {args.package_path}...")
        vm = start_microvm(args.package_path, port=args.port, ip_address=args.ip)
        print(f"MicroVM running at {vm.url}")
        print("Press Ctrl+C to stop")
        
        try:
            vm.process.wait()
        except KeyboardInterrupt:
            print("\nStopping VM...")
            vm.stop()
            
    elif args.command == "pool":
        from .pool import MicroVMPool
        
        print(f"Starting pool of {args.size} microVMs...")
        
        with MicroVMPool(args.package_path, size=args.size, port=args.port) as pool:
            print(f"Pool ready. Stats: {pool.stats()}")
            print("Press Ctrl+C to stop")
            
            try:
                while True:
                    import time
                    time.sleep(5)
                    print(f"Stats: {pool.stats()}")
            except KeyboardInterrupt:
                print("\nShutting down pool...")


if __name__ == "__main__":
    main()
