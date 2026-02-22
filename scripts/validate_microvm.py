#!/usr/bin/env python3
"""
Validate a running MicroVM environment.

This script tests that a MicroVM is functioning correctly by:
1. Checking health endpoint
2. Testing reset
3. Testing basic tool calls or step actions
4. Validating response types

Usage:
    python scripts/validate_microvm.py --env echo_env --url http://172.16.0.2:8000
    python scripts/validate_microvm.py --env connect4_env --url http://172.16.0.2:8000
"""

import argparse
import sys
import time
import requests
from typing import Optional


def wait_for_health(url: str, timeout: int = 30) -> bool:
    """Wait for the health endpoint to respond."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = requests.get(f"{url}/health", timeout=5)
            if resp.status_code == 200:
                print(f"✓ Health check passed: {resp.json()}")
                return True
        except requests.exceptions.RequestException:
            pass
        time.sleep(1)
    return False


def test_reset(url: str) -> bool:
    """Test the reset endpoint."""
    try:
        resp = requests.post(f"{url}/reset", json={}, timeout=10)
        if resp.status_code == 200:
            print(f"✓ Reset successful: {resp.json()}")
            return True
        else:
            print(f"✗ Reset failed with status {resp.status_code}: {resp.text}")
            return False
    except requests.exceptions.RequestException as e:
        print(f"✗ Reset failed with error: {e}")
        return False


def test_list_tools(url: str) -> Optional[list]:
    """Test listing MCP tools."""
    try:
        resp = requests.get(f"{url}/tools", timeout=10)
        if resp.status_code == 200:
            tools = resp.json()
            print(f"✓ List tools successful: {len(tools)} tools found")
            for tool in tools:
                name = tool.get("name", "unknown")
                desc = tool.get("description", "")[:50]
                print(f"  - {name}: {desc}...")
            return tools
        else:
            print(f"✗ List tools failed with status {resp.status_code}")
            return None
    except requests.exceptions.RequestException as e:
        print(f"✗ List tools failed with error: {e}")
        return None


def test_call_tool(url: str, tool_name: str, arguments: dict) -> bool:
    """Test calling an MCP tool."""
    try:
        resp = requests.post(
            f"{url}/call_tool",
            json={"name": tool_name, "arguments": arguments},
            timeout=30
        )
        if resp.status_code == 200:
            result = resp.json()
            print(f"✓ Call tool '{tool_name}' successful")
            print(f"  Result: {str(result)[:100]}...")
            return True
        else:
            print(f"✗ Call tool failed with status {resp.status_code}: {resp.text}")
            return False
    except requests.exceptions.RequestException as e:
        print(f"✗ Call tool failed with error: {e}")
        return False


def test_step(url: str, action: dict) -> bool:
    """Test the step endpoint."""
    try:
        resp = requests.post(f"{url}/step", json=action, timeout=30)
        if resp.status_code == 200:
            result = resp.json()
            print(f"✓ Step successful")
            # Check for expected fields
            if "observation" in result:
                print(f"  Observation: {str(result['observation'])[:100]}...")
            if "reward" in result:
                print(f"  Reward: {result['reward']}")
            if "done" in result:
                print(f"  Done: {result['done']}")
            return True
        else:
            print(f"✗ Step failed with status {resp.status_code}: {resp.text}")
            return False
    except requests.exceptions.RequestException as e:
        print(f"✗ Step failed with error: {e}")
        return False


# Environment-specific validation functions
def validate_echo_env(url: str) -> bool:
    """Validate echo_env MicroVM."""
    print("\n=== Validating echo_env ===\n")

    if not wait_for_health(url):
        return False

    if not test_reset(url):
        return False

    tools = test_list_tools(url)
    if tools is None:
        return False

    # Test echo_message tool
    if not test_call_tool(url, "echo_message", {"message": "Hello from MicroVM!"}):
        return False

    # Test echo_with_length tool
    if not test_call_tool(url, "echo_with_length", {"message": "Test message"}):
        return False

    print("\n✓ echo_env validation PASSED\n")
    return True


def validate_chat_env(url: str) -> bool:
    """Validate chat_env MicroVM."""
    print("\n=== Validating chat_env ===\n")

    if not wait_for_health(url):
        return False

    if not test_reset(url):
        return False

    tools = test_list_tools(url)
    if tools is None:
        return False

    # Test send_message tool if available
    tool_names = [t.get("name") for t in tools]
    if "send_message" in tool_names:
        if not test_call_tool(url, "send_message", {"message": "Hello!"}):
            return False

    print("\n✓ chat_env validation PASSED\n")
    return True


def validate_connect4_env(url: str) -> bool:
    """Validate connect4_env MicroVM."""
    print("\n=== Validating connect4_env ===\n")

    if not wait_for_health(url):
        return False

    if not test_reset(url):
        return False

    # Connect4 uses step with column action
    if not test_step(url, {"column": 3}):
        return False

    print("\n✓ connect4_env validation PASSED\n")
    return True


def validate_grid_world_env(url: str) -> bool:
    """Validate grid_world_env MicroVM."""
    print("\n=== Validating grid_world_env ===\n")

    if not wait_for_health(url):
        return False

    if not test_reset(url):
        return False

    # Grid world uses step with action
    if not test_step(url, {"action": "UP"}):
        return False

    print("\n✓ grid_world_env validation PASSED\n")
    return True


def validate_maze_env(url: str) -> bool:
    """Validate maze_env MicroVM."""
    print("\n=== Validating maze_env ===\n")

    if not wait_for_health(url):
        return False

    if not test_reset(url):
        return False

    # Maze uses step with direction
    if not test_step(url, {"direction": "north"}):
        return False

    print("\n✓ maze_env validation PASSED\n")
    return True


def validate_snake_env(url: str) -> bool:
    """Validate snake_env MicroVM."""
    print("\n=== Validating snake_env ===\n")

    if not wait_for_health(url):
        return False

    if not test_reset(url):
        return False

    # Snake uses step with direction
    if not test_step(url, {"direction": "UP"}):
        return False

    print("\n✓ snake_env validation PASSED\n")
    return True


def validate_generic_env(url: str, env_name: str) -> bool:
    """Generic validation for environments without specific tests."""
    print(f"\n=== Validating {env_name} (generic) ===\n")

    if not wait_for_health(url):
        return False

    if not test_reset(url):
        return False

    # Try to list tools (MCP environments)
    test_list_tools(url)

    print(f"\n✓ {env_name} basic validation PASSED\n")
    return True


# Map environment names to validation functions
VALIDATORS = {
    "echo_env": validate_echo_env,
    "chat_env": validate_chat_env,
    "connect4_env": validate_connect4_env,
    "grid_world_env": validate_grid_world_env,
    "maze_env": validate_maze_env,
    "snake_env": validate_snake_env,
}


def main():
    parser = argparse.ArgumentParser(description="Validate a MicroVM environment")
    parser.add_argument("--env", required=True, help="Environment name (e.g., echo_env)")
    parser.add_argument("--url", required=True, help="MicroVM URL (e.g., http://172.16.0.2:8000)")
    parser.add_argument("--timeout", type=int, default=30, help="Health check timeout in seconds")
    args = parser.parse_args()

    print(f"Validating {args.env} at {args.url}")
    print("=" * 60)

    # Get the validator function
    validator = VALIDATORS.get(args.env, lambda url: validate_generic_env(url, args.env))

    try:
        success = validator(args.url)
        if success:
            print("=" * 60)
            print(f"VALIDATION PASSED: {args.env}")
            print("=" * 60)
            sys.exit(0)
        else:
            print("=" * 60)
            print(f"VALIDATION FAILED: {args.env}")
            print("=" * 60)
            sys.exit(1)
    except Exception as e:
        print(f"VALIDATION ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
