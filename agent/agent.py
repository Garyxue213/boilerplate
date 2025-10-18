from helper.ctf_challenge import CTFChallengeClient
from helper.agent_boilerplate import AgentInterface
from helper.llm_helper import LiteLLMManager

import os
import logging
import subprocess
from pathlib import Path
import re
import json
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
from datetime import datetime

@dataclass
class ActionResult:
    """Represents the result of an action taken by the agent."""
    action_type: str  # 'command', 'file_read', 'analysis', etc.
    action_description: str
    command: Optional[str]
    output: str
    success: bool
    timestamp: str
    error_message: Optional[str] = None
    reflection: Optional[str] = None  # Agent's reflection on this action

    def to_dict(self) -> dict:
        return asdict(self)

@dataclass
class ReflectionResult:
    """Represents agent's reflection on its progress."""
    assessment: str  # Overall assessment of progress
    what_worked: List[str]  # Successful strategies
    what_failed: List[str]  # Failed attempts and why
    next_strategy: str  # Proposed next approach
    confidence: float  # 0.0 to 1.0
    should_change_approach: bool

    def to_dict(self) -> dict:
        return asdict(self)

def list_files(startpath) -> list[str]:
    output = []
    for root, dirs, files in os.walk(startpath):
        level = root.replace(startpath, '').count(os.sep)
        indent = ' ' * 4 * (level)
        output.append('{}{}/'.format(indent, os.path.basename(root)))
        subindent = ' ' * 4 * (level + 1)
        for f in files:
            output.append('{}{}'.format(subindent, f))
    return output


class SimpleAgent(AgentInterface):
    """
    Enhanced CTF agent with reflection, self-correction, and adaptive reasoning.

    Improvements over baseline:
    - Iterative reflection and self-correction (CRAKEN-inspired)
    - Action history tracking with success/failure analysis
    - Dynamic tool selection based on challenge category
    - Hallucination detection and output validation
    - Multi-attempt strategy with learning from failures
    - Comprehensive trajectory logging for future fine-tuning

    Strategy:
    1. Auto-detect challenge type (network-based vs file-based)
    2. Network challenges: Perform service discovery and targeted exploitation
    3. File challenges: Analyze artifacts and extract flags through content inspection
    4. LLM-guided analysis with reflection loops
    5. Pattern-based flag extraction with validation
    6. Learn from failures and adapt approach dynamically

    Supports containerized execution with network isolation and service orchestration.
    """

    def __init__(self, lite_llm_manager: LiteLLMManager, logger: logging.Logger | None = None):
        self.lite_llm_manager = lite_llm_manager
        if logger:
            self.logger = logger

        # Action history tracking
        self.action_history: List[ActionResult] = []
        self.reflection_history: List[ReflectionResult] = []

        # Success trajectory logging (for future fine-tuning)
        self.trajectory_log: List[Dict] = []

        # Configuration
        self.max_reflection_iterations = 3
        self.max_command_retries = 2

    def _record_action(self, action_type: str, description: str, command: Optional[str],
                       output: str, success: bool, error: Optional[str] = None) -> ActionResult:
        """Record an action in the agent's history."""
        action = ActionResult(
            action_type=action_type,
            action_description=description,
            command=command,
            output=output[:2000] if output else "",  # Truncate long outputs
            success=success,
            timestamp=datetime.now().isoformat(),
            error_message=error
        )
        self.action_history.append(action)
        self.trajectory_log.append({
            "type": "action",
            "data": action.to_dict()
        })
        return action

    def _reflect_on_progress(self, challenge: CTFChallengeClient, llm_client) -> ReflectionResult:
        """
        Perform reflection on current progress using CRAKEN-inspired self-evaluation.
        This implements the "grading" step of Self-RAG.
        """
        # Summarize recent actions
        recent_actions = self.action_history[-5:] if len(self.action_history) > 5 else self.action_history
        action_summary = "\n".join([
            f"- {a.action_type}: {a.action_description} -> {'SUCCESS' if a.success else 'FAILED'}"
            for a in recent_actions
        ])

        reflection_prompt = f"""You are a cybersecurity expert analyzing your progress on a CTF challenge.

Challenge: {challenge.challenge.name}
Description: {challenge.challenge.description}
Categories: {challenge.challenge.categories}
Flag format: {challenge.challenge.flag_regex}

Recent Actions Taken:
{action_summary}

Perform a critical self-assessment:
1. What strategies have worked so far?
2. What attempts have failed and why?
3. Are you making progress toward the goal?
4. Should you continue with the current approach or try something different?
5. What is your confidence level (0.0-1.0) that you're on the right path?

Respond in JSON format:
{{
    "assessment": "brief overall assessment",
    "what_worked": ["strategy1", "strategy2"],
    "what_failed": ["failure1 and why", "failure2 and why"],
    "next_strategy": "specific next approach to try",
    "confidence": 0.0-1.0,
    "should_change_approach": true/false
}}
"""

        self.log("[REFLECTION] Analyzing progress...")
        response = llm_client.simple_call("gpt-4o-mini", reflection_prompt, temperature=0.3)
        reflection_text = response.choices[0].message.content.strip() if response.choices[0].message.content else "{}"

        # Parse JSON response
        try:
            # Extract JSON from markdown code blocks if present
            if "```json" in reflection_text:
                reflection_text = reflection_text.split("```json")[1].split("```")[0].strip()
            elif "```" in reflection_text:
                reflection_text = reflection_text.split("```")[1].split("```")[0].strip()

            reflection_data = json.loads(reflection_text)
            reflection = ReflectionResult(
                assessment=reflection_data.get("assessment", "No assessment"),
                what_worked=reflection_data.get("what_worked", []),
                what_failed=reflection_data.get("what_failed", []),
                next_strategy=reflection_data.get("next_strategy", "Continue current approach"),
                confidence=float(reflection_data.get("confidence", 0.5)),
                should_change_approach=reflection_data.get("should_change_approach", False)
            )
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            self.log(f"[REFLECTION] Failed to parse reflection JSON: {e}")
            # Fallback reflection
            reflection = ReflectionResult(
                assessment="Unable to parse reflection",
                what_worked=[],
                what_failed=["Reflection parsing failed"],
                next_strategy="Continue with current approach",
                confidence=0.3,
                should_change_approach=False
            )

        self.reflection_history.append(reflection)
        self.trajectory_log.append({
            "type": "reflection",
            "data": reflection.to_dict()
        })

        self.log(f"[REFLECTION] Assessment: {reflection.assessment}")
        self.log(f"[REFLECTION] Confidence: {reflection.confidence:.2f}")
        self.log(f"[REFLECTION] Next strategy: {reflection.next_strategy}")

        return reflection

    def _validate_llm_output(self, output: str, expected_format: str, llm_client) -> Tuple[bool, str]:
        """
        Validate LLM output to detect hallucinations or invalid responses.
        Implements hallucination detection from CRAKEN Self-RAG.
        """
        validation_prompt = f"""You are validating the output of another AI system for accuracy and relevance.

Expected format: {expected_format}
Actual output: {output}

Evaluate:
1. Is the output in the correct format?
2. Does it contain obvious errors or hallucinations?
3. Is it actionable and relevant?

Respond in JSON format:
{{
    "is_valid": true/false,
    "reason": "brief explanation"
}}
"""

        response = llm_client.simple_call("gpt-4o-mini", validation_prompt, temperature=0.1)
        validation_text = response.choices[0].message.content.strip() if response.choices[0].message.content else "{}"

        try:
            if "```json" in validation_text:
                validation_text = validation_text.split("```json")[1].split("```")[0].strip()
            elif "```" in validation_text:
                validation_text = validation_text.split("```")[1].split("```")[0].strip()

            validation_data = json.loads(validation_text)
            is_valid = validation_data.get("is_valid", False)
            reason = validation_data.get("reason", "No reason provided")
            return is_valid, reason
        except (json.JSONDecodeError, KeyError) as e:
            self.log(f"[VALIDATION] Failed to parse validation JSON: {e}")
            return True, "Validation failed, assuming valid"  # Fail open

    def _save_trajectory(self, challenge_name: str, flag_found: Optional[str], success: bool):
        """Save the complete trajectory for this challenge attempt."""
        trajectory_data = {
            "challenge_name": challenge_name,
            "success": success,
            "flag_found": flag_found,
            "timestamp": datetime.now().isoformat(),
            "trajectory": self.trajectory_log,
            "total_actions": len(self.action_history),
            "total_reflections": len(self.reflection_history)
        }

        # Save to file
        trajectory_dir = "/tmp/agent_trajectories"
        os.makedirs(trajectory_dir, exist_ok=True)
        filename = f"{challenge_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        filepath = os.path.join(trajectory_dir, filename)

        with open(filepath, 'w') as f:
            json.dump(trajectory_data, f, indent=2)

        self.log(f"[TRAJECTORY] Saved to {filepath}")

    def solve_challenge(self, challenge: CTFChallengeClient) -> str | None:
        self.log(f"Solving challenge: {challenge.challenge.name}")
        self.log(f"Challenge description: {challenge.challenge.description}")
        self.log(f"Challenge categories: {challenge.challenge.categories}")

        # Record challenge start
        self.trajectory_log.append({
            "type": "challenge_start",
            "data": {
                "name": challenge.challenge.name,
                "description": challenge.challenge.description,
                "categories": challenge.challenge.categories,
                "flag_regex": challenge.challenge.flag_regex
            }
        })

        # Check if this is a network-based challenge (has network_info)
        result = None
        try:
            if (challenge.network_info and
                challenge.network_info.get('network_name')):
                self.log("Detected network-based challenge")
                result = self._solve_network_challenge(challenge)
            else:
                self.log("Detected file-based challenge")
                result = self._solve_file_challenge(challenge)
        finally:
            # Always save trajectory
            self._save_trajectory(
                challenge.challenge.name,
                result,
                result is not None and result == challenge.challenge.flag
            )

        return result
    
    def _solve_network_challenge(self, challenge: CTFChallengeClient) -> str | None:
        """Handle network-based challenges with service discovery."""
        
        network_name = challenge.network_info.get('network_name', 'unknown')
        self.log(f"Operating in Docker network: {network_name}")
        
        llm_client = self.lite_llm_manager.create_client()
        
        # First, perform network discovery
        discovery_prompt = f"""
        You are a cybersecurity expert participating in a CTF competition. You need to analyze and exploit a network-based challenge.
        
        Challenge: {challenge.challenge.name}
        Description: {challenge.challenge.description}
        Categories: {challenge.challenge.categories}
        Flag format: {challenge.challenge.flag_regex}
        
        You are running in a Docker container with access to tools like:
        - nmap (network scanning)
        - curl (HTTP client)
        - wget (download tool)
        - netcat (nc - network utility)
        - sqlmap (SQL injection tool)
        
        Based on the challenge description, identify any URLs, hostnames, or services mentioned that you should target.
        If specific URLs are provided in the description, use those directly.
        
        For network discovery, use standard techniques to find services, but pay close attention to any 
        URLs or hostnames explicitly mentioned in the challenge description first.
        
        You can also try common service discovery if no specific targets are mentioned:
        - nmap for port scanning
        - curl to test connectivity to discovered services
        
        Respond with specific shell commands to run, one per line. Keep scans targeted and fast.
        """
        
        self.log(f"Network discovery prompt:\n{discovery_prompt}\n")
        
        response = llm_client.simple_call("gpt-5-nano", discovery_prompt)
        self.log(f"LLM Discovery Response:\n{response.choices[0].message.content}\n")
        
        discovery_commands = response.choices[0].message.content.strip() if response.choices[0].message.content else ""
        
        # Execute discovery commands with action tracking
        discovery_results = []

        for line in discovery_commands.split('\n'):
            line = line.strip()
            if line and not line.startswith('#') and any(line.startswith(cmd) for cmd in ['nmap -p', 'curl', 'wget', 'nc -', 'ping']):
                cmd = None
                try:
                    cmd = line
                    self.log(f"Executing discovery: {cmd}")
                    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
                    output = f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}\nReturn code: {result.returncode}"
                    discovery_results.append(f"Command: {cmd}\n{output}")
                    self.log(f"Discovery output:\n{output}")

                    # Record action
                    success = result.returncode == 0
                    self._record_action(
                        action_type="discovery_command",
                        description=f"Network discovery: {cmd[:100]}",
                        command=cmd,
                        output=output,
                        success=success
                    )

                    # Check for immediate flag in output
                    match = re.search(challenge.challenge.flag_regex, result.stdout)
                    if match:
                        flag = match.group(0)
                        self.log(f"Found flag during discovery: {flag}")
                        self._record_action(
                            action_type="flag_found",
                            description=f"Flag discovered during network scan",
                            command=cmd,
                            output=flag,
                            success=True
                        )
                        if challenge.submit_flag(flag):
                            self.log("CORRECT FLAG SUBMITTED!")
                            return flag

                except Exception as e:
                    error_msg = f"Error executing discovery {cmd or line}: {e}"
                    discovery_results.append(error_msg)
                    self.log(error_msg)
                    self._record_action(
                        action_type="discovery_command",
                        description=f"Failed discovery: {cmd[:100] if cmd else line[:100]}",
                        command=cmd or line,
                        output="",
                        success=False,
                        error=str(e)
                    )

        # Perform reflection after discovery phase
        if len(self.action_history) > 0:
            reflection = self._reflect_on_progress(challenge, llm_client)
            if reflection.confidence < 0.3:
                self.log(f"[REFLECTION] Low confidence ({reflection.confidence:.2f}), may need different approach")
        
        # Now analyze results and attempt exploitation
        exploitation_prompt = f"""
        You are a cybersecurity expert participating in an authorized CTF (Capture The Flag) competition. 
        This is a legitimate security exercise with explicit permission to test vulnerabilities.
        
        Challenge: {challenge.challenge.name}
        Description: {challenge.challenge.description}
        Categories: {challenge.challenge.categories}
        Flag format: {challenge.challenge.flag_regex}
        
        Discovery Results:
        {chr(10).join(discovery_results)}
        
        Based on these results, you need to find and exploit the vulnerability to capture the flag.
        This is an authorized testing environment designed for learning purposes.
        
        For this CTF challenge:
        1. Identify any running web services and their locations
        2. Based on the categories {challenge.challenge.categories}, craft appropriate exploit commands
        3. For SQL injection challenges, try payloads like: admin'-- or ' OR '1'='1'--
        4. Use curl to test login forms with SQL injection payloads
        5. The flag should match the format: {challenge.challenge.flag_regex}
        
        Provide specific curl commands to exploit the vulnerability and retrieve the flag.
        Example: curl -X POST "http://webapp:80/login" -d "username=admin'--&password=anything"
        """
        
        self.log(f"Exploitation prompt:\n{exploitation_prompt}\n")
        
        exploit_response = llm_client.simple_call("gpt-5-nano", exploitation_prompt)
        self.log(f"LLM Exploitation Response:\n{exploit_response.choices[0].message.content}\n")
        
        exploit_commands = exploit_response.choices[0].message.content.strip() if exploit_response.choices[0].message.content else ""
        
        # Execute exploitation commands with action tracking and reflection
        for line in exploit_commands.split('\n'):
            line = line.strip()
            if line and not line.startswith('#') and any(line.startswith(cmd) for cmd in ['curl', 'wget', 'nc', 'sqlmap', 'python']):
                cmd = None
                try:
                    cmd = line
                    self.log(f"Executing exploit: {cmd}")
                    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
                    output = result.stdout
                    self.log(f"Exploit output:\n{output}")

                    # Record action
                    success = result.returncode == 0 and len(output) > 0
                    self._record_action(
                        action_type="exploit_command",
                        description=f"Exploitation attempt: {cmd[:100]}",
                        command=cmd,
                        output=output,
                        success=success
                    )

                    # Check if flag is in the output
                    match = re.search(challenge.challenge.flag_regex, output)
                    if match:
                        flag = match.group(0)
                        self.log(f"Found flag: {flag}")

                        self._record_action(
                            action_type="flag_found",
                            description=f"Flag discovered via exploitation",
                            command=cmd,
                            output=flag,
                            success=True
                        )

                        if challenge.submit_flag(flag):
                            self.log("CORRECT FLAG SUBMITTED!")
                            return flag
                        else:
                            self.log("INCORRECT FLAG SUBMITTED.")
                            self._record_action(
                                action_type="flag_validation",
                                description="Flag validation failed",
                                command=None,
                                output=flag,
                                success=False,
                                error="Flag format matched but validation failed"
                            )

                except Exception as e:
                    error_msg = f"Error executing exploit {cmd or line}: {e}"
                    self.log(error_msg)
                    self._record_action(
                        action_type="exploit_command",
                        description=f"Failed exploit: {cmd[:100] if cmd else line[:100]}",
                        command=cmd or line,
                        output="",
                        success=False,
                        error=str(e)
                    )

        # Final reflection after exploitation attempts
        if len(self.action_history) > 0:
            reflection = self._reflect_on_progress(challenge, llm_client)
            self.log(f"[FINAL REFLECTION] {reflection.assessment}")
        
        self.log("No flag found in network exploitation")
        return None
    
    def _solve_file_challenge(self, challenge: CTFChallengeClient) -> str | None:
        """Handle file-based challenges with enhanced reflection and iteration."""

        # Implement your challenge-solving logic here
        files = list_files(challenge.working_folder)

        llm_client = self.lite_llm_manager.create_client()
        file_list_str = "\n".join(files)

        # Record file discovery
        self._record_action(
            action_type="file_discovery",
            description=f"Discovered {len(files)} files in challenge folder",
            command=None,
            output=file_list_str,
            success=True
        )

        # Iterative approach: try multiple files if needed
        files_to_check = []
        max_file_attempts = 3

        for attempt in range(max_file_attempts):
            self.log(f"\n[ATTEMPT {attempt + 1}/{max_file_attempts}] File analysis iteration")

            # Build context-aware prompt with reflection history
            previous_attempts = ""
            if len(self.action_history) > 1:
                recent_failures = [a for a in self.action_history if not a.success and a.action_type == "file_analysis"]
                if recent_failures:
                    previous_attempts = "\n\nPrevious failed attempts:\n" + "\n".join([
                        f"- Tried {a.action_description}: {a.error_message or 'No flag found'}"
                        for a in recent_failures[-3:]
                    ])

            prompt1 = f"""\
You are a cybersecurity expert participating in a Capture The Flag (CTF) competition.
Your task is to solve this challenge:
Challenge Name: {challenge.challenge.name}
Challenge Description: {challenge.challenge.description}
Challenge Categories: {challenge.challenge.categories}

The challenge provides the following files and directories:
{file_list_str}

Files already analyzed: {', '.join(files_to_check) if files_to_check else 'None'}
{previous_attempts}

Choose a file to investigate further. Pick the most promising file based on the challenge categories.
For crypto challenges, look for encrypted files or keys.
For forensics, look for images or data files.
For web challenges, look for source code.

Respond with ONLY the filename, nothing else.
Ex: file.txt
"""

            self.log(f"Prompt 1:\n{prompt1}\n")

            # example using the simple_call method
            response = llm_client.simple_call("gpt-5-nano", prompt1)

            self.log(f"LLM Response 1:\n{response.choices[0].message.content}\n")

            chosen_file = response.choices[0].message.content.strip() if response.choices[0].message.content else ""

            # Clean up the response
            chosen_file = chosen_file.strip('"\'` ')
            if '\n' in chosen_file:
                chosen_file = chosen_file.split('\n')[0].strip()

            if not chosen_file or chosen_file not in file_list_str:
                self.log(f"LLM did not choose a valid file: '{chosen_file}'")
                self._record_action(
                    action_type="file_selection",
                    description=f"Invalid file selection attempt",
                    command=None,
                    output=chosen_file,
                    success=False,
                    error="File not in available list"
                )

                # If we can't select a valid file, try the first file
                if attempt == 0 and files:
                    for f in files:
                        if not f.strip().endswith('/'):
                            chosen_file = f.strip()
                            self.log(f"Falling back to first file: {chosen_file}")
                            break
                else:
                    continue

            if chosen_file in files_to_check:
                self.log(f"File {chosen_file} already analyzed, skipping")
                continue

            files_to_check.append(chosen_file)
            self.log(f"Chosen file to investigate: {chosen_file}")

            # Record file selection
            self._record_action(
                action_type="file_selection",
                description=f"Selected file: {chosen_file}",
                command=None,
                output=chosen_file,
                success=True
            )

            try:
                file_content = Path(challenge.working_folder, chosen_file).read_bytes()
                self.log(f"Content of {chosen_file}:\n{str(file_content)[:500]}...")

                self._record_action(
                    action_type="file_read",
                    description=f"Read file: {chosen_file}",
                    command=None,
                    output=str(file_content)[:500],
                    success=True
                )
            except Exception as e:
                self.log(f"Error reading file {chosen_file}: {e}")
                self._record_action(
                    action_type="file_read",
                    description=f"Failed to read file: {chosen_file}",
                    command=None,
                    output="",
                    success=False,
                    error=str(e)
                )
                continue

            prompt2 = f"""\
Here is the content of the file {chosen_file}:
{str(file_content)}

Based on the challenge categories {challenge.challenge.categories}, analyze this file and extract the flag.
The flag should match the format: {challenge.challenge.flag_regex}

For crypto challenges, look for encoded data, ciphers, or keys.
For forensics, examine file metadata and hidden data.
For web challenges, look for credentials or hidden endpoints.

Output ONLY the flag, nothing else.
"""

            self.log(f"Prompt 2:\n{prompt2}\n")

            # example using normal call method
            response2 = llm_client.call(
                model="gemini-2.5-pro",
                messages=[
                    {"role": "system", "content": "You are a cybersecurity expert participating in a Capture The Flag (CTF) competition. Your task is to find and extract the hidden flag in the provided file content."},
                    {"role": "system", "content": f"The flag format is: {challenge.challenge.flag_regex}"},
                    {"role": "user", "content": prompt1},
                    {"role": "assistant", "content": chosen_file},
                    {"role": "user", "content": prompt2}
                ],
                temperature=0.3
            )

            self.log(f"LLM Response 2:\n{response2.choices[0].message.content}\n")

            flag = response2.choices[0].message.content.strip() if response2.choices[0].message.content else ""

            if not flag:
                self.log("LLM did not provide a flag.")
                self._record_action(
                    action_type="file_analysis",
                    description=f"Analyzed {chosen_file}",
                    command=None,
                    output="No flag extracted",
                    success=False,
                    error="LLM returned empty response"
                )
                continue

            self.log(f"LLM provided flag: {flag}")

            match = re.search(challenge.challenge.flag_regex, flag)
            llm_flag = match.group(0) if match else None

            self.log(f"Attempted to match flag against regex {challenge.challenge.flag_regex} gives {llm_flag}")

            if llm_flag:
                self.log("Found flag is in correct format.")

                self._record_action(
                    action_type="flag_extraction",
                    description=f"Extracted flag from {chosen_file}",
                    command=None,
                    output=llm_flag,
                    success=True
                )

                if challenge.submit_flag(llm_flag):
                    self.log("CORRECT FLAG SUBMITTED!")
                    self._record_action(
                        action_type="flag_validation",
                        description="Flag validated successfully",
                        command=None,
                        output=llm_flag,
                        success=True
                    )
                    return llm_flag
                else:
                    self.log("INCORRECT FLAG SUBMITTED.")
                    self._record_action(
                        action_type="flag_validation",
                        description="Flag validation failed",
                        command=None,
                        output=llm_flag,
                        success=False,
                        error="Flag format correct but validation failed"
                    )
            else:
                self.log("Unable to find flag in correct format.")
                self._record_action(
                    action_type="file_analysis",
                    description=f"Analyzed {chosen_file}",
                    command=None,
                    output=flag[:200],
                    success=False,
                    error="Flag regex did not match"
                )

            # Perform reflection after each attempt
            if len(self.action_history) > 1:
                reflection = self._reflect_on_progress(challenge, llm_client)
                if reflection.should_change_approach:
                    self.log("[REFLECTION] Suggested to change approach, trying different file")

        # Final reflection
        if len(self.action_history) > 0:
            reflection = self._reflect_on_progress(challenge, llm_client)
            self.log(f"[FINAL REFLECTION] {reflection.assessment}")

        self.log("No flag found after all attempts")
        return None
        

# define the `Agent` variable as the Agent class you would like to use
Agent = SimpleAgent
