#!/usr/bin/env python3
"""
Validate a running MicroVM environment.

This script tests that a MicroVM is functioning correctly by:
1. Checking health endpoint
2. Testing reset and capturing initial observation
3. Testing step actions with correct StepRequest format
4. Testing MCP tool listing/calling for MCP environments
5. Validating response structure

The OpenEnv API uses:
- POST /reset  -> ResetResponse {observation, reward, done}
- POST /step   -> StepRequest {action: {...}} -> StepResponse {observation, reward, done}
- MCP tools are accessed via /step with ListToolsAction/CallToolAction

Usage:
    python scripts/validate_microvm.py --env echo_env --url http://172.16.0.2:8000
    python scripts/validate_microvm.py --env connect4_env --url http://172.16.0.2:8000
"""

import argparse
import sys
import time
import requests
from typing import Optional, Dict, Any, List


def wait_for_health(url: str, timeout: int = 30) -> bool:
    """Wait for the health endpoint to respond."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = requests.get(f"{url}/health", timeout=5)
            if resp.status_code == 200:
                print(f"  Health check passed: {resp.json()}")
                return True
        except requests.exceptions.RequestException:
            pass
        time.sleep(1)
    print("  Health check failed: timeout")
    return False


def test_reset(url: str) -> Optional[Dict[str, Any]]:
    """Test the reset endpoint. Returns the observation dict or None on failure.

    OpenEnv ResetResponse format: {observation: {...}, reward: null, done: false}
    """
    try:
        resp = requests.post(f"{url}/reset", json={}, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            # ResetResponse wraps observation in an "observation" field
            observation = data.get("observation", data)
            print(f"  Reset successful")
            if isinstance(observation, dict):
                for key in ["legal_actions", "legal_moves", "board", "fen", "grid"]:
                    if key in observation:
                        val = observation[key]
                        print(f"    {key}: {str(val)[:100]}...")
            return observation
        else:
            print(f"  Reset failed with status {resp.status_code}: {resp.text[:200]}")
            return None
    except requests.exceptions.RequestException as e:
        print(f"  Reset failed with error: {e}")
        return None


def test_step(url: str, action: dict) -> Optional[Dict[str, Any]]:
    """Test the step endpoint with proper StepRequest format.

    OpenEnv StepRequest format: {action: {...}, timeout_s: null, request_id: null}
    The action dict is unpacked into the environment's Action Pydantic model.
    """
    try:
        # Wrap action in StepRequest format
        step_request = {"action": action}
        resp = requests.post(f"{url}/step", json=step_request, timeout=30)
        if resp.status_code == 200:
            result = resp.json()
            observation = result.get("observation", result)
            print(f"  Step successful (action: {action})")
            if isinstance(observation, dict):
                for key in ["legal_actions", "legal_moves", "board", "fen", "grid"]:
                    if key in observation:
                        val = observation[key]
                        print(f"    {key}: {str(val)[:100]}...")
            if "reward" in result:
                print(f"    Reward: {result['reward']}")
            if "done" in result:
                print(f"    Done: {result['done']}")
            return observation
        elif resp.status_code == 404:
            print("  /step endpoint not available")
            return None
        elif resp.status_code == 422:
            print(f"  Step validation error (422): {resp.text[:300]}")
            return None
        else:
            print(f"  Step failed with status {resp.status_code}: {resp.text[:200]}")
            return None
    except requests.exceptions.RequestException as e:
        print(f"  Step failed with error: {e}")
        return None


def test_mcp_list_tools(url: str) -> Optional[List[Dict]]:
    """List MCP tools via /step with ListToolsAction.

    MCP environments handle {type: "list_tools"} as a special action
    that returns an observation with a "tools" field.
    """
    try:
        step_request = {"action": {"type": "list_tools"}}
        resp = requests.post(f"{url}/step", json=step_request, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            observation = data.get("observation", data)
            tools = observation.get("tools", [])
            print(f"  List tools successful: {len(tools)} tools found")
            for tool in tools:
                name = tool.get("name", "unknown")
                desc = tool.get("description", "")[:60]
                print(f"    - {name}: {desc}")
            return tools
        elif resp.status_code == 422:
            print("  MCP list_tools not supported (not an MCP environment)")
            return None
        else:
            print(f"  List tools failed with status {resp.status_code}: {resp.text[:200]}")
            return None
    except requests.exceptions.RequestException as e:
        print(f"  List tools failed with error: {e}")
        return None


def test_mcp_call_tool(url: str, tool_name: str, arguments: dict) -> bool:
    """Call an MCP tool via /step with CallToolAction.

    MCP environments handle {type: "call_tool", tool_name: ..., arguments: {...}}
    as a special action that invokes the named tool.
    """
    try:
        step_request = {
            "action": {
                "type": "call_tool",
                "tool_name": tool_name,
                "arguments": arguments,
            }
        }
        resp = requests.post(f"{url}/step", json=step_request, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            observation = data.get("observation", data)
            print(f"  Call tool '{tool_name}' successful")
            print(f"    Result: {str(observation)[:200]}...")
            return True
        elif resp.status_code == 422:
            print(f"  MCP call_tool not supported for '{tool_name}' (422)")
            return False
        else:
            print(f"  Call tool failed with status {resp.status_code}: {resp.text[:200]}")
            return False
    except requests.exceptions.RequestException as e:
        print(f"  Call tool failed with error: {e}")
        return False


# ---------------------------------------------------------------------------
# Environment-specific validation functions
# ---------------------------------------------------------------------------

def validate_echo_env(url: str) -> bool:
    """Validate echo_env MicroVM (MCP environment).

    Echo env is a pure MCP environment. Actions are ListToolsAction and
    CallToolAction, routed through the /step endpoint.
    Tools: echo_message, echo_with_length
    """
    print("\n=== Validating echo_env ===\n")

    if not wait_for_health(url):
        return False

    obs = test_reset(url)
    if obs is None:
        return False

    # Echo env is an MCP environment - list and call tools via /step
    tools = test_mcp_list_tools(url)
    if tools is not None:
        tool_names = [t.get("name") for t in tools]
        if "echo_message" in tool_names:
            if not test_mcp_call_tool(url, "echo_message", {"message": "Hello from MicroVM!"}):
                return False
        if "echo_with_length" in tool_names:
            test_mcp_call_tool(url, "echo_with_length", {"message": "Test message"})

    print("\n=== echo_env validation PASSED ===\n")
    return True


def validate_chat_env(url: str) -> bool:
    """Validate chat_env MicroVM.

    ChatAction requires tokens (torch.Tensor) which cannot be sent via JSON.
    Validation is limited to health + reset.
    """
    print("\n=== Validating chat_env ===\n")

    if not wait_for_health(url):
        return False

    obs = test_reset(url)
    if obs is None:
        return False

    print("  (Skipping step test: ChatAction requires tensor data)")
    print("\n=== chat_env validation PASSED ===\n")
    return True


def validate_connect4_env(url: str) -> bool:
    """Validate connect4_env MicroVM.

    Connect4Action: {column: int}
    """
    print("\n=== Validating connect4_env ===\n")

    if not wait_for_health(url):
        return False

    obs = test_reset(url)
    if obs is None:
        return False

    # Connect4Action has column: int (0-6)
    result = test_step(url, {"column": 3})
    if result is None:
        return False

    print("\n=== connect4_env validation PASSED ===\n")
    return True


def validate_grid_world_env(url: str) -> bool:
    """Validate grid_world_env MicroVM.

    GridWorldAction: {action: MoveAction} where MoveAction is enum "UP"/"DOWN"/"LEFT"/"RIGHT"
    """
    print("\n=== Validating grid_world_env ===\n")

    if not wait_for_health(url):
        return False

    obs = test_reset(url)
    if obs is None:
        return False

    # GridWorldAction has action: MoveAction (enum: UP, DOWN, LEFT, RIGHT)
    result = test_step(url, {"action": "UP"})
    if result is None:
        return False

    print("\n=== grid_world_env validation PASSED ===\n")
    return True


def validate_maze_env(url: str) -> bool:
    """Validate maze_env MicroVM.

    MazeAction: {action: int} - action ID from legal_actions list
    """
    print("\n=== Validating maze_env ===\n")

    if not wait_for_health(url):
        return False

    obs = test_reset(url)
    if obs is None:
        return False

    # MazeAction has action: int - pick from legal_actions in observation
    legal_actions = obs.get("legal_actions", [0])
    action_id = legal_actions[0] if legal_actions else 0
    print(f"  Using action {action_id} from legal_actions: {legal_actions}")
    result = test_step(url, {"action": action_id})
    if result is None:
        return False

    print("\n=== maze_env validation PASSED ===\n")
    return True


def validate_snake_env(url: str) -> bool:
    """Validate snake_env MicroVM.

    SnakeAction: {action: int} - direction encoded as integer
    """
    print("\n=== Validating snake_env ===\n")

    if not wait_for_health(url):
        return False

    obs = test_reset(url)
    if obs is None:
        return False

    # SnakeAction has action: int (direction encoded as integer)
    result = test_step(url, {"action": 0})
    if result is None:
        return False

    print("\n=== snake_env validation PASSED ===\n")
    return True


def validate_chess_env(url: str) -> bool:
    """Validate chess_env MicroVM.

    ChessAction: {move: str} - UCI format (e.g., "e2e4")
    Reset observation includes legal_moves list.
    """
    print("\n=== Validating chess_env ===\n")

    if not wait_for_health(url):
        return False

    obs = test_reset(url)
    if obs is None:
        return False

    # ChessAction has move: str (UCI format)
    # Pick first legal move from observation, fallback to standard opening
    legal_moves = obs.get("legal_moves", ["e2e4"])
    move = legal_moves[0] if legal_moves else "e2e4"
    print(f"  Using move '{move}' from {len(legal_moves)} legal moves")
    result = test_step(url, {"move": move})
    if result is None:
        return False

    print("\n=== chess_env validation PASSED ===\n")
    return True


def validate_openspiel_env(url: str) -> bool:
    """Validate openspiel_env MicroVM.

    OpenSpielAction: {action_id: int, game_name: str, game_params: dict}
    Default game is "catch" with random opponent.
    Reset observation includes legal_actions and info_state.
    """
    print("\n=== Validating openspiel_env ===\n")

    if not wait_for_health(url):
        return False

    obs = test_reset(url)
    if obs is None:
        return False

    # OpenSpielAction has action_id: int - pick from legal_actions
    legal_actions = obs.get("legal_actions", [0])
    action_id = legal_actions[0] if legal_actions else 0
    print(f"  Using action_id {action_id} from legal_actions: {legal_actions}")
    result = test_step(url, {"action_id": action_id})
    if result is None:
        return False

    # Play a few more steps to validate game flow
    for i in range(3):
        if result is None or result.get("done", False):
            print(f"  Game ended after step {i + 1}")
            break
        legal = result.get("legal_actions", [0])
        aid = legal[0] if legal else 0
        result = test_step(url, {"action_id": aid})

    print("\n=== openspiel_env validation PASSED ===\n")
    return True


def validate_atari_env(url: str) -> bool:
    """Validate atari_env MicroVM.

    AtariAction: {action_id: int, game_name: str, obs_type: str, full_action_space: bool}
    Default game is "pong".
    """
    print("\n=== Validating atari_env ===\n")

    if not wait_for_health(url):
        return False

    obs = test_reset(url)
    if obs is None:
        return False

    # AtariAction has action_id: int
    legal_actions = obs.get("legal_actions", [0])
    action_id = legal_actions[0] if legal_actions else 0
    print(f"  Using action_id {action_id} from legal_actions: {legal_actions}")
    result = test_step(url, {"action_id": action_id})
    if result is None:
        return False

    print("\n=== atari_env validation PASSED ===\n")
    return True


def validate_dm_control_env(url: str) -> bool:
    """Validate dm_control_env MicroVM.

    DMControlAction: {values: List[float]} - continuous action values
    Default domain is cartpole/balance (1D action space).
    """
    print("\n=== Validating dm_control_env ===\n")

    if not wait_for_health(url):
        return False

    obs = test_reset(url)
    if obs is None:
        return False

    # DMControlAction has values: List[float]
    # Default cartpole balance has 1D action space
    result = test_step(url, {"values": [0.0]})
    if result is None:
        return False

    print("\n=== dm_control_env validation PASSED ===\n")
    return True


def validate_generic_env(url: str, env_name: str) -> bool:
    """Generic validation for environments without specific validators.

    Tests health, reset, and attempts MCP tool discovery.
    """
    print(f"\n=== Validating {env_name} (generic) ===\n")

    if not wait_for_health(url):
        return False

    obs = test_reset(url)
    if obs is None:
        return False

    # Try MCP tool listing (works for MCP environments)
    tools = test_mcp_list_tools(url)
    if tools:
        print(f"  Environment exposes {len(tools)} MCP tools")

    # Try to discover action format from observation
    if obs and isinstance(obs, dict):
        legal_actions = obs.get("legal_actions")
        legal_moves = obs.get("legal_moves")
        if legal_actions:
            action_id = legal_actions[0]
            print(f"  Trying step with action_id from legal_actions: {action_id}")
            test_step(url, {"action_id": action_id})
        elif legal_moves:
            move = legal_moves[0]
            print(f"  Trying step with move from legal_moves: {move}")
            test_step(url, {"move": move})

    print(f"\n=== {env_name} basic validation PASSED ===\n")
    return True


# Map environment names to validation functions
VALIDATORS = {
    "echo_env": validate_echo_env,
    "chat_env": validate_chat_env,
    "connect4_env": validate_connect4_env,
    "grid_world_env": validate_grid_world_env,
    "maze_env": validate_maze_env,
    "snake_env": validate_snake_env,
    "chess_env": validate_chess_env,
    "openspiel_env": validate_openspiel_env,
    "atari_env": validate_atari_env,
    "dm_control_env": validate_dm_control_env,
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
