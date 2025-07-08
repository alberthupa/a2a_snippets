import os
import json
import sys
import time
import threading
import argparse
import asyncio
import socket
import logging
from openai import AzureOpenAI
from typing import List, Dict, Tuple, Optional

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


from dotenv import load_dotenv

load_dotenv(".env", override=True)
REGISTRY_URL = os.getenv("REGISTRY_URL", "http://localhost:8000")
AZURE_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_VERSION = os.getenv("AZURE_OPENAI_API_VERSION")


# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


"""
class AgentCard(BaseModel):
    name: str
    description: str
    url: str
    version: str
    capabilities: Dict
    skills: List[Dict]
"""


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
            id="poetry_skill",
            name="writing poetry",
            description="Returns a poetry.",
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
        self.llm_client = None

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

    def from_network_get_agents(self):
        """
        Retrieves all registered agents from the registry.
        """
        try:
            response = requests.get(f"{self.registry_url}/registry/agents")
            response.raise_for_status()  # Raises an HTTPError for bad responses (4xx or 5xx)
            agents_data = response.json()
            # print(agents_data)
            # return [(**data) for data in agents_data]
            return [dict(data) for data in agents_data]
        except requests.exceptions.RequestException as e:
            print(f"Error connecting to the agent registry: {e}")
            return []
        except json.JSONDecodeError:
            print("Error: Failed to decode JSON from registry response.")
            return []

    def from_network_get_agent_url(self, agent_name: str) -> str:
        """
        Retrieves a specific agent from the registry by name.
        """
        agents = self.get_all_agents()
        agent = next((a for a in agents if a.name == agent_name), None)
        if not agent:
            print(f"Agent {agent_name} not found.")
            return None
        try:
            return agent.url
        except requests.exceptions.RequestException as e:
            print(f"Error connecting to agent {agent_name}: {e}")
            return None
        except json.JSONDecodeError:
            print("Error decoding JSON from agent response.")
            return None

    def from_network_find_best_agent(self, query) -> Tuple[str, float]:
        """
        Routes a given query to the most appropriate agent.

        Args:
            query: The user's query string.

        Returns:
            A tuple containing the name of the best agent and a confidence score.
        """
        agents = self.from_network_get_agents()
        if not agents:
            raise RuntimeError("No agents are available in the network.")

        agent_descriptions = []
        for agent in agents:
            # print(agent)
            description = (
                f"Agent Name: {agent.name}\\n"
                f"Description: {agent.description}\\n"
                f"Capabilities: {json.dumps(agent.capabilities)}"
            )
            agent_descriptions.append(description)

        prompt = f"""
        You are an intelligent router responsible for routing user queries to the correct agent.
        Based on the user's query and the available agents' capabilities, determine the best agent to handle the query.

        User Query: "{query}"

        Available Agents:
        ---
        {" --- ".join(agent_descriptions)}
        ---

        Please respond with a JSON object containing the 'agent_name' of the most suitable agent and a 'confidence' score (from 0.0 to 1.0).
        """

        try:
            # print("helo")
            if self.llm_client is None:
                self.llm_client = AzureOpenAI(
                    api_key=os.getenv("AZURE_OPENAI_API_KEY"),
                    api_version=os.environ.get("AZURE_OPENAI_API_VERSION"),
                    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
                )

            response = self.llm_client.chat.completions.create(
                model="gpt-4o",  # Or any other suitable model
                messages=[
                    {
                        "role": "system",
                        "content": "You are a helpful assistant that responds in JSON.",
                    },
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            choice = response.choices[0].message.content
            data = json.loads(choice)
            return data["agent_name"], float(data["confidence"])
        except Exception as e:
            raise RuntimeError(
                f"Failed to get a routing decision from the LLM: {e}"
            ) from e

    def handle_message(self, message: Message) -> Message:
        """
        if method handle_task exists, it will be called instead of handle_message
        """
        return Message(
            content=TextContent(
                text=f"Hello from {self.agent_card.name}! I received: {message.content.text}"
            ),
            role=MessageRole.AGENT,
            parent_message_id=message.message_id,
            conversation_id=message.conversation_id,
        )

    def process_message(self, message: Message) -> Message:
        """
        make sure not to use this method, it is deprecated
        """
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
        default="Sample agent",
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
