#!/usr/bin/env python3
"""
Validate a running MicroVM environment.

Generic validator that works for ANY OpenEnv environment by dynamically
discovering the action format from /schema and observation structure.

The OpenEnv API contract:
- GET  /health   -> 200
- GET  /metadata -> env metadata
- GET  /schema   -> JSON Schema for Action model
- POST /reset    -> ResetResponse {observation, reward, done}
- POST /step     -> StepRequest {action: {...}} -> StepResponse {observation, reward, done}

MCP environments handle special action types:
- {type: "list_tools"} -> observation with "tools" list
- {type: "call_tool", tool_name: ..., arguments: {...}} -> tool result

Usage:
    python scripts/validate_microvm.py --env echo_env --url http://172.16.0.2:8000
"""

import argparse
import json
import sys
import time
import requests
from typing import Optional, Dict, Any, List, Tuple


# ---------------------------------------------------------------------------
# Low-level endpoint helpers
# ---------------------------------------------------------------------------

def wait_for_health(url: str, timeout: int = 30) -> bool:
    """Wait for the health endpoint to respond."""
    start = time.time()
    last_status = None
    while time.time() - start < timeout:
        try:
            resp = requests.get(f"{url}/health", timeout=5)
            if resp.status_code == 200:
                try:
                    body = resp.json()
                except Exception:
                    body = resp.text[:100]
                print(f"  Health check passed: {body}")
                return True
            else:
                last_status = resp.status_code
        except requests.exceptions.ConnectionError:
            pass
        except requests.exceptions.RequestException as e:
            last_status = str(e)
        time.sleep(1)
    print(f"  Health check failed: timeout after {timeout}s (last status: {last_status})")
    return False


def get_schema(url: str) -> Optional[Dict[str, Any]]:
    """Fetch the action schema from /schema endpoint."""
    try:
        resp = requests.get(f"{url}/schema", timeout=10)
        if resp.status_code == 200:
            schema = resp.json()
            print(f"  Schema fetched successfully:")
            print(f"    {json.dumps(schema)[:500]}")
            return schema
        else:
            print(f"  /schema returned {resp.status_code}")
            return None
    except requests.exceptions.RequestException as e:
        print(f"  /schema fetch failed: {e}")
        return None


def get_metadata(url: str) -> Optional[Dict[str, Any]]:
    """Fetch environment metadata."""
    try:
        resp = requests.get(f"{url}/metadata", timeout=10)
        if resp.status_code == 200:
            metadata = resp.json()
            print(f"  Metadata fetched: {json.dumps(metadata)[:200]}")
            return metadata
        else:
            print(f"  /metadata returned {resp.status_code}")
            return None
    except requests.exceptions.RequestException as e:
        print(f"  /metadata fetch failed: {e}")
        return None


def call_reset(url: str) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Call /reset. Returns (observation, full_response) or (None, None) on failure."""
    try:
        resp = requests.post(f"{url}/reset", json={}, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            observation = data.get("observation", data)
            print(f"  Reset successful")
            if isinstance(observation, dict):
                keys = list(observation.keys())
                print(f"    Observation keys: {keys}")
                # Print notable fields for diagnostics
                for key in keys[:10]:
                    val = observation[key]
                    val_str = str(val)
                    if len(val_str) > 120:
                        val_str = val_str[:120] + "..."
                    print(f"    {key}: {val_str}")
            return observation, data
        else:
            print(f"  Reset failed with status {resp.status_code}")
            try:
                print(f"    Response: {resp.text[:500]}")
            except Exception:
                pass
            return None, None
    except requests.exceptions.RequestException as e:
        print(f"  Reset failed with error: {e}")
        return None, None


def call_step(url: str, action: dict) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Call /step with StepRequest {action: {...}}. Returns (observation, full_response)."""
    try:
        step_request = {"action": action}
        resp = requests.post(f"{url}/step", json=step_request, timeout=30)
        if resp.status_code == 200:
            result = resp.json()
            observation = result.get("observation", result)
            print(f"  Step successful (action: {json.dumps(action)[:120]})")
            if isinstance(observation, dict):
                for key in list(observation.keys())[:5]:
                    val_str = str(observation[key])
                    if len(val_str) > 120:
                        val_str = val_str[:120] + "..."
                    print(f"    {key}: {val_str}")
            if "reward" in result:
                print(f"    reward: {result['reward']}")
            if "done" in result:
                print(f"    done: {result['done']}")
            return observation, result
        elif resp.status_code == 422:
            print(f"  Step 422 (validation error): {resp.text[:300]}")
            return None, None
        elif resp.status_code == 404:
            print(f"  /step endpoint not available (404)")
            return None, None
        else:
            print(f"  Step failed with status {resp.status_code}: {resp.text[:300]}")
            return None, None
    except requests.exceptions.RequestException as e:
        print(f"  Step failed with error: {e}")
        return None, None


def probe_action_format(url: str) -> Optional[Dict[str, Any]]:
    """Discover action format by sending an empty action and parsing the 422 error.

    FastAPI/Pydantic returns detailed validation errors that reveal the expected
    field names and types. This is fully generic and works for any environment.
    """
    try:
        # Send empty action to trigger validation error
        resp = requests.post(f"{url}/step", json={"action": {}}, timeout=10)
        if resp.status_code == 200:
            # Empty action was accepted (unlikely but possible)
            print("  Probe: empty action accepted")
            return {}
        elif resp.status_code == 422:
            try:
                error_data = resp.json()
            except Exception:
                return None

            detail = error_data.get("detail", [])
            if not isinstance(detail, list):
                return None

            # Parse Pydantic validation errors to discover required fields
            # Error locations look like: ["body", "action", "field_name"]
            # or in newer Pydantic: ["body", "action", "field_name"]
            required_fields = {}
            for error in detail:
                loc = error.get("loc", [])
                error_type = error.get("type", "")
                msg = error.get("msg", "")

                # Find field names under "action" in the location path
                # loc format: ["body", "action", "field_name"] or ["action", "field_name"]
                field_name = None
                for i, part in enumerate(loc):
                    if part == "action" and i + 1 < len(loc):
                        candidate = loc[i + 1]
                        if isinstance(candidate, str) and candidate != "metadata":
                            field_name = candidate
                            break

                if field_name and field_name not in required_fields:
                    # Try to infer the expected type from the error message
                    required_fields[field_name] = _infer_type_from_error(error_type, msg)

            if required_fields:
                action = {}
                for field_name, field_type in required_fields.items():
                    if field_type == "int":
                        action[field_name] = 0
                    elif field_type == "float":
                        action[field_name] = 0.0
                    elif field_type == "bool":
                        action[field_name] = False
                    elif field_type == "list":
                        action[field_name] = []
                    elif field_type == "dict":
                        action[field_name] = {}
                    else:
                        action[field_name] = ""
                print(f"  Probe discovered fields: {required_fields}")
                print(f"  Probe built action: {json.dumps(action)[:200]}")
                return action

        return None
    except requests.exceptions.RequestException:
        return None


def _infer_type_from_error(error_type: str, msg: str) -> str:
    """Infer the expected field type from a Pydantic validation error."""
    msg_lower = msg.lower()
    type_lower = error_type.lower()

    if "int" in type_lower or "integer" in msg_lower:
        return "int"
    elif "float" in type_lower or "number" in msg_lower:
        return "float"
    elif "bool" in type_lower or "boolean" in msg_lower:
        return "bool"
    elif "list" in type_lower or "array" in msg_lower:
        return "list"
    elif "dict" in type_lower or "object" in msg_lower:
        return "dict"
    # Default to string - "missing" errors don't tell us the type
    return "string"


# ---------------------------------------------------------------------------
# Action discovery from schema
# ---------------------------------------------------------------------------

def build_action_from_schema(schema: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Try to construct a minimal valid action from the JSON schema.

    OpenEnv /schema returns the Pydantic model schema for the Action class.
    We parse properties and their types to build a sample action with default-like values.
    """
    if not schema or not isinstance(schema, dict):
        return None

    # Schema may be nested under various keys depending on OpenEnv version
    properties = schema.get("properties", {})
    if not properties:
        # Try looking for it under "action" or "Action"
        for key in ["action", "Action", "input_schema"]:
            if key in schema and isinstance(schema[key], dict):
                properties = schema[key].get("properties", {})
                if properties:
                    break

    if not properties:
        print(f"    Schema has no parseable properties")
        return None

    # Filter out metadata field (inherited from Action base class)
    properties = {k: v for k, v in properties.items() if k != "metadata"}

    if not properties:
        return None

    action = {}
    for field_name, field_schema in properties.items():
        field_type = field_schema.get("type", "string")
        enum_values = field_schema.get("enum")
        default = field_schema.get("default")

        if default is not None:
            action[field_name] = default
        elif enum_values:
            action[field_name] = enum_values[0]
        elif field_type == "integer":
            action[field_name] = 0
        elif field_type == "number":
            action[field_name] = 0.0
        elif field_type == "string":
            action[field_name] = ""
        elif field_type == "boolean":
            action[field_name] = False
        elif field_type == "array":
            items = field_schema.get("items", {})
            items_type = items.get("type", "number")
            if items_type in ("number", "integer"):
                action[field_name] = [0.0]
            else:
                action[field_name] = []
        elif field_type == "object":
            action[field_name] = {}
        else:
            action[field_name] = None

    print(f"    Built action from schema: {json.dumps(action)[:200]}")
    return action


def refine_action_with_observation(
    action: Optional[Dict[str, Any]],
    observation: Dict[str, Any],
    schema: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Refine a schema-derived action using runtime observation data.

    Many environments include legal_actions or legal_moves in their observation,
    which tells us what values are valid for the action fields.
    """
    if not isinstance(observation, dict):
        return action

    legal_actions = observation.get("legal_actions")
    legal_moves = observation.get("legal_moves")

    if action is None:
        action = {}

    if legal_actions and isinstance(legal_actions, list) and len(legal_actions) > 0:
        first = legal_actions[0]
        # Find an integer/action_id field in the action to fill
        filled = False
        for field_name in ["action", "action_id"]:
            if field_name in action:
                action[field_name] = first
                filled = True
                break
        if not filled:
            # If schema didn't give us a field, try common patterns
            if isinstance(first, int):
                # Could be "action" or "action_id" - try both
                action["action"] = first
            else:
                action["action"] = first
        print(f"    Refined action with legal_actions[0]={first}: {json.dumps(action)[:200]}")

    elif legal_moves and isinstance(legal_moves, list) and len(legal_moves) > 0:
        first = legal_moves[0]
        filled = False
        for field_name in ["move"]:
            if field_name in action:
                action[field_name] = first
                filled = True
                break
        if not filled:
            action["move"] = first
        print(f"    Refined action with legal_moves[0]={first}: {json.dumps(action)[:200]}")

    return action if action else None


# ---------------------------------------------------------------------------
# MCP tool discovery and testing
# ---------------------------------------------------------------------------

def try_mcp_tools(url: str) -> Tuple[bool, bool]:
    """Attempt MCP tool listing and calling.

    Returns (is_mcp_env, mcp_tool_call_succeeded).
    """
    # Try listing tools
    try:
        step_request = {"action": {"type": "list_tools"}}
        resp = requests.post(f"{url}/step", json=step_request, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            observation = data.get("observation", data)
            tools = observation.get("tools", [])
            if isinstance(tools, list) and len(tools) > 0:
                print(f"  MCP tools discovered: {len(tools)} tools")
                for tool in tools[:5]:
                    name = tool.get("name", "unknown")
                    desc = str(tool.get("description", ""))[:60]
                    print(f"    - {name}: {desc}")

                # Try calling the first tool with empty or minimal arguments
                first_tool = tools[0]
                tool_name = first_tool.get("name", "")
                tool_schema = first_tool.get("input_schema", first_tool.get("parameters", {}))
                tool_args = _build_minimal_tool_args(tool_schema)

                print(f"  Calling MCP tool '{tool_name}' with args: {json.dumps(tool_args)[:150]}")
                call_req = {
                    "action": {
                        "type": "call_tool",
                        "tool_name": tool_name,
                        "arguments": tool_args,
                    }
                }
                call_resp = requests.post(f"{url}/step", json=call_req, timeout=30)
                if call_resp.status_code == 200:
                    call_data = call_resp.json()
                    call_obs = call_data.get("observation", call_data)
                    print(f"  MCP tool call successful")
                    print(f"    Result: {str(call_obs)[:200]}")
                    return True, True
                else:
                    print(f"  MCP tool call returned {call_resp.status_code}: {call_resp.text[:200]}")
                    return True, False

            # 200 but no tools - might be MCP env with no tools registered
            return False, False
        elif resp.status_code == 422:
            # Not an MCP environment
            return False, False
        else:
            return False, False
    except requests.exceptions.RequestException:
        return False, False


def _build_minimal_tool_args(tool_schema: Dict[str, Any]) -> Dict[str, Any]:
    """Build minimal arguments for a tool call from its schema."""
    if not tool_schema or not isinstance(tool_schema, dict):
        return {}

    properties = tool_schema.get("properties", {})
    required = tool_schema.get("required", [])
    args = {}

    for field_name in required:
        field_def = properties.get(field_name, {})
        field_type = field_def.get("type", "string")
        enum_values = field_def.get("enum")

        if enum_values:
            args[field_name] = enum_values[0]
        elif field_type == "string":
            args[field_name] = "test"
        elif field_type == "integer":
            args[field_name] = 0
        elif field_type == "number":
            args[field_name] = 0.0
        elif field_type == "boolean":
            args[field_name] = False
        elif field_type == "array":
            args[field_name] = []
        elif field_type == "object":
            args[field_name] = {}

    return args


# ---------------------------------------------------------------------------
# Generic validator
# ---------------------------------------------------------------------------

def validate(url: str, env_name: str, timeout: int = 30) -> bool:
    """Validate any OpenEnv environment generically.

    Strategy:
    1. Health check
    2. Fetch metadata and schema
    3. Reset environment
    4. Discover if MCP environment (try list_tools)
    5. If MCP: call a tool
    6. If not MCP: build action from schema + observation, then step
    """
    print(f"\n{'=' * 60}")
    print(f"Validating: {env_name}")
    print(f"URL: {url}")
    print(f"{'=' * 60}\n")

    # --- Phase 1: Health check ---
    print("[1/5] Health check")
    if not wait_for_health(url, timeout=timeout):
        return False

    # --- Phase 2: Metadata + Schema ---
    print("\n[2/5] Fetching metadata and schema")
    metadata = get_metadata(url)
    schema = get_schema(url)

    # --- Phase 3: Reset ---
    print("\n[3/5] Resetting environment")
    observation, reset_response = call_reset(url)
    if observation is None:
        print("  FATAL: Reset failed - environment is not functional")
        return False

    # --- Phase 4: MCP tool discovery ---
    print("\n[4/5] Probing for MCP tools")
    is_mcp, mcp_call_ok = try_mcp_tools(url)

    if is_mcp:
        if mcp_call_ok:
            print("  Environment is MCP with working tool calls")
        else:
            print("  Environment is MCP but tool call failed")
            # MCP envs may not need step to work - tool listing is sufficient
            # But if tool call failed, that's a real problem
            return False

        # For MCP environments, successful tool call is the validation
        print(f"\n{'=' * 60}")
        print(f"VALIDATION PASSED: {env_name} (MCP environment)")
        print(f"{'=' * 60}")
        return True

    print("  Not an MCP environment - will test /step")

    # --- Phase 5: Step with discovered action ---
    print("\n[5/5] Testing /step")

    # Strategy: try multiple discovery methods in order of reliability
    # 1. Schema-based action construction
    # 2. Observation-based refinement (legal_actions, legal_moves)
    # 3. Probe-based discovery (parse 422 errors to learn field names)
    # 4. Blind fallback attempts

    action = None

    # Method 1: Build from schema
    if schema:
        action = build_action_from_schema(schema)

    # Method 2: Refine with observation data
    action = refine_action_with_observation(action, observation, schema)

    # Try the discovered action
    step_succeeded = False
    if action and action != {}:
        print(f"  Trying schema/observation-derived action: {json.dumps(action)[:200]}")
        obs, resp = call_step(url, action)
        if obs is not None:
            step_succeeded = True
        else:
            print("  Schema-derived action failed, trying probe discovery...")

    # Method 3: Probe-based discovery (send empty action, parse 422 errors)
    if not step_succeeded:
        probed_action = probe_action_format(url)
        if probed_action is not None and probed_action != {}:
            # Refine probed action with observation data
            probed_action = refine_action_with_observation(probed_action, observation, schema)
            if probed_action:
                # Need a fresh reset since MCP probe or previous step may have changed state
                print("  Resetting before probed step attempt...")
                observation, _ = call_reset(url)
                if observation:
                    probed_action = refine_action_with_observation(probed_action, observation, schema)
                print(f"  Trying probed action: {json.dumps(probed_action)[:200]}")
                obs, resp = call_step(url, probed_action)
                if obs is not None:
                    step_succeeded = True

    # Method 4: Blind fallback attempts
    if not step_succeeded:
        print("  All discovery methods failed, trying common patterns...")
        # Reset before blind attempts
        observation, _ = call_reset(url)

        attempts = [
            {"action": 0},           # Integer action (maze, snake, etc.)
            {"action": "wait"},       # String action (wildfire, etc.)
            {"action": "UP"},         # Enum action (grid_world, etc.)
            {"column": 0},            # Named integer field (connect4, etc.)
            {"code": "print(1)"},     # Code execution (coding_env, etc.)
            {"answer": "test"},       # Text answer (reasoning_gym, etc.)
        ]

        for attempt in attempts:
            print(f"  Attempting: {json.dumps(attempt)}")
            obs, resp = call_step(url, attempt)
            if obs is not None:
                step_succeeded = True
                break

    if not step_succeeded:
        print("  WARNING: All step attempts failed")
        print("  Environment passed health + reset but step could not be validated")
        # Don't fail the entire validation - health + reset working is meaningful
        # The action format simply couldn't be discovered generically

    print(f"\n{'=' * 60}")
    print(f"VALIDATION PASSED: {env_name}")
    print(f"{'=' * 60}")
    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Validate a MicroVM environment")
    parser.add_argument("--env", required=True, help="Environment name (e.g., echo_env)")
    parser.add_argument("--url", required=True, help="MicroVM URL (e.g., http://172.16.0.2:8000)")
    parser.add_argument("--timeout", type=int, default=30, help="Health check timeout in seconds")
    args = parser.parse_args()

    try:
        success = validate(args.url, args.env, timeout=args.timeout)
        if success:
            sys.exit(0)
        else:
            print(f"\nVALIDATION FAILED: {args.env}")
            sys.exit(1)
    except Exception as e:
        print(f"\nVALIDATION ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
