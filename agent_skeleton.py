import sys
import time
import threading
import argparse
import asyncio
import socket
import logging
from typing import Optional, List

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn
import requests

from python_a2a import (
    AgentCard,
    AgentSkill,
    A2AServer,
    run_server,
    Message,
    TextContent,
    MessageRole,
    TaskStatus,
    TaskState,
)
from python_a2a.discovery import AgentRegistry, enable_discovery

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Find an available port
def find_free_port() -> int:
    """Find an available port to use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


class SampleAgent(A2AServer):
    """A sample agent that registers with a remote registry."""

    def __init__(self, name: str, description: str, url: str, registry_url: str):
        """Initialize the sample agent and attempt to register."""

        skill = AgentSkill(
            id="add_two_numbers",
            name="Add Two Numbers",
            description="Returns the sum of two numbers a and b.",
            # inputModes=["application/json"],
            # outputModes=["application/json"],
            tags=["math"],
            examples=[{"input": {"a": 2, "b": 3}, "output": {"sum": 5}}],
        )

        agent_card = AgentCard(
            name=name,
            description=description,
            url=url,
            version="1.0.0",
            skills=[skill],
            capabilities={
                "streaming": True,
                "pushNotifications": False,
                "stateTransitionHistory": False,
                "google_a2a_compatible": True,
                "parts_array_format": True,
            },
        )
        super().__init__(agent_card=agent_card)
        self.registry_url = registry_url
        self._registration_retries = 3
        self._heartbeat_interval = 30  # seconds
        self._discovery_client = None

    async def setup(self):
        """Registers the agent with the registry."""
        if self.registry_url:
            for attempt in range(self._registration_retries):
                try:
                    # Register with discovery
                    self._discovery_client = enable_discovery(
                        self,
                        registry_url=self.registry_url,
                        heartbeat_interval=self._heartbeat_interval,
                    )

                    # Add heartbeat logging
                    def heartbeat_callback(results):
                        for result in results:
                            if result.get("success"):
                                logger.info(
                                    f"Heartbeat successful with registry {result['registry']}"
                                )
                            else:
                                logger.warning(
                                    f"Heartbeat failed with registry {result['registry']}: {result.get('message', 'Unknown error')}"
                                )

                    # Set the callback
                    self._discovery_client.heartbeat_callback = heartbeat_callback

                    # Verify registration
                    response = requests.get(f"{self.registry_url}/registry/agents")
                    if response.status_code == 200:
                        agents = response.json()
                        # if any(agent["url"] == self.url for agent in agents):
                        if any(agent["url"] == self.agent_card.url for agent in agents):
                            logger.info(
                                f"Agent '{self.agent_card.name}' registered successfully with registry: {self.registry_url}"
                            )
                            return  # Success, exit the retry loop
                        else:
                            logger.warning(
                                f"Registration verification failed (attempt {attempt + 1}/{self._registration_retries})"
                            )
                    else:
                        logger.warning(
                            f"Failed to verify registration: {response.status_code} (attempt {attempt + 1}/{self._registration_retries})"
                        )

                    # Wait before retrying
                    time.sleep(2)
                except Exception as e:
                    logger.error(
                        f"Error during registration attempt {attempt + 1}: {e}"
                    )
                    if attempt < self._registration_retries - 1:
                        time.sleep(2)

            logger.error(
                f"Failed to register agent after {self._registration_retries} attempts"
            )

    def handle_message(self, message: Message) -> Message:
        # response = client.send_message("What is the meaning of life?")
        return Message(
            content=TextContent(
                text=f"Hello from {self.agent_card.name}! I received: {message.content.text}"
            ),
            role=MessageRole.AGENT,
            parent_message_id=message.message_id,
            conversation_id=message.conversation_id,
        )

    def process_message(self, message: Message) -> Message:
        print(message)
        return "dupa"


def run_agent(name: str, port: int, registry_url: str):
    """Runs a sample agent that registers with the specified registry."""
    agent = SampleAgent(
        name=name,
        description=f"Sample agent '{name}' demonstrating remote registration.",
        url=f"http://localhost:{port}",
        registry_url=registry_url,
    )
    asyncio.run(agent.setup())
    run_server(agent, host="0.0.0.0", port=port)
    logger.info(f"Agent '{name}' started on http://localhost:{port}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="A2A Sample Agent")
    parser.add_argument(
        "--name",
        type=str,
        default="....",
        help="...",
    )
    parser.add_argument(
        "--port", type=int, default=None, help="Port for the agent to run on"
    )
    parser.add_argument(
        "--registry-url",
        type=str,
        required=True,
        help="URL of the agent registry server",
    )
    args = parser.parse_args()

    agent_port = args.port or find_free_port()
    run_agent(args.name, agent_port, args.registry_url)

    # uv run agent_skeleton.py --registry-url "http://localhost:8000"
