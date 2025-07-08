# Choosing an A2A pattern for ~10 minute jobs

**Scenario** – A *requester* agent delegates a task that takes **about ten minutes** to a *worker* agent and wants to know (nothing more, nothing less) when the job is done.

Three coordination patterns cover almost every situation:

| # | Pattern                       | Short description                                  | Best when…                                     |
|---|------------------------------|----------------------------------------------------|------------------------------------------------|
| 1 | **Poll + Get**               | Requester polls `/tasks/get` until state = DONE    | Any network; simplest synchronous code         |
| 2 | **Subscribe / Server-push**  | Requester opens one stream; worker pushes updates  | Long-lived TCP streams survive ~10 min         |
| 3 | **Callback**                 | Worker calls requester’s own `/tasks/send`         | You already run A2A on **both** sides          |

### Trade-offs at a glance

| Aspect                | Poll + Get                                    | Subscribe / Server-push                    | Callback                                  |
|-----------------------|-----------------------------------------------|--------------------------------------------|-------------------------------------------|
| **Network chatter**   | `ceil(job_time / interval)` round-trips       | One persistent stream                      | Zero until final callback                 |
| **Notification lag**  | ≤ poll interval (e.g. 30 s)                   | Milliseconds                               | Milliseconds                              |
| **Code complexity**   | Works with plain blocking HTTP clients        | Needs async client & keep-alive pings      | Requester must expose an authenticated API|
| **Robustness**        | Survives proxies & firewalls                  | Can be killed by strict proxies            | Requires mutual trust & inbound firewall  |

### Quick recommendation

* **Subscribe** – if both agents can keep a single stream open for ten minutes.  
* **Polling** – if you’re on flaky corporate networks or a CLI requester.  
* **Callback** – if every agent is already an A2A server and you want full decoupling.

---

## Method-specific introductions

### 1 · Poll + Get — “Are we there yet?”

The requester fires off the task (`/tasks/send`) and stores the returned `task_id`.  
A simple loop then sleeps for *N* seconds and calls `/tasks/get task_id`.  
Because each HTTP call is short-lived and re-authenticates, this pattern works everywhere—laptops on coffee-shop Wi-Fi, corporate proxies, even cron jobs—at the cost of extra traffic and up-to-N-seconds delay in noticing completion.


#### Worker

```python
    def _finish(self, task_id: str, secs: int):
        time.sleep(secs)
        task = self.tasks.get(task_id)  # ✅ built‑in in‑memory store
        if task:
            task.artifacts = [{"parts": [{"type": "text", "text": f"done in {secs}s"}]}]
            task.status = TaskStatus(state=TaskState.COMPLETED)

    def handle_task(self, task):
        secs = 5
        if task.status.state == TaskState.SUBMITTED:  # first hit
            threading.Thread(
                target=self._finish, args=(task.id, secs), daemon=True
            ).start()
        task.status = TaskStatus(state=TaskState.WAITING)  # client can poll
        return task
```


#### Requester
```python
import time
from python_a2a import (
    A2AClient,
    Message,
    MessageRole,
    TextContent,
    Task,
    TaskStatus,
    TaskState,
)

# Create a client
client = A2AClient("http://172.22.172.105:58829")

task = client._create_task(message={"content": "hi"})
task = client._send_task(task)

while task.status.state != TaskState.COMPLETED:
    time.sleep(2)
    task = client.get_task(task.id)
    print("polled", task.status.state)
print(task.artifacts[0]["parts"][0]["text"])
```

---

### 2 · Subscribe / Server-push — “Just ping me”

When the network allows an HTTP/2, WebSocket, or SSE stream to stay up for the whole ten-minute job, the requester can simply **subscribe** once and wait.  
The worker emits zero bytes until something changes, then streams one or more task updates ending with the *COMPLETED* state—so the requester hears “done” almost instantly and never wastes a poll.

#### Worker

```python
import asyncio, copy, json
from fastapi import FastAPI, HTTPException
from flask import Response, stream_with_context, request

...
agent_card = AgentCard(
    ...
    capabilities={
        "streaming": True,
        "tasks": True,  # has /a2a/tasks
        "task_streaming": True,  # advertises tasks_send_subscribe
    },
)
...


async def _do_long_task(self, task: Task, total_secs: int):
    """
    Yield a fresh Task update every second to show progress.
    Replace the sleep+yield block with your real workload.
    """
    for i in range(total_secs):
        await asyncio.sleep(1)  # ← your real work here
        task.status.message = {"progress": f"{i+1}/{total_secs}"}
        yield copy.deepcopy(task)

# ────────────────────────────────────────────────────────────────

async def tasks_send_subscribe(self, task: Task):
    secs = 5
    task.status = TaskStatus(state=TaskState.SUBMITTED)
    yield copy.deepcopy(task)

    task.status = TaskStatus(state=TaskState.WAITING)
    yield copy.deepcopy(task)

    # stream progress from the helper coroutine
    async for upd in self._do_long_task(task, secs):
        yield upd

    task.artifacts = [{"parts": [{"type": "text", "text": f"done in {secs}s"}]}]
    task.status = TaskStatus(state=TaskState.COMPLETED)
    yield copy.deepcopy(task)

# ------------------------------------------

# ---------- one extra Flask route ----------
def setup_routes(self, app):
    @app.route("/a2a/tasks/stream", methods=["POST"])
    def task_stream():
        raw = request.get_json(force=True)
        task = Task.from_dict(raw)

        async def agen():
            async for update in self.tasks_send_subscribe(task):
                yield f"data: {json.dumps(update.to_dict())}\n\n"

        # turn async generator into plain iterator for Flask
        def sse_wrapper():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                agen_iter = agen().__aiter__()
                while True:
                    chunk = loop.run_until_complete(agen_iter.__anext__())
                    yield chunk
            except StopAsyncIteration:
                pass
            finally:
                loop.close()

        return Response(
            stream_with_context(sse_wrapper()),
            content_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )



```


#### Requester

```python
import asyncio, uuid
from python_a2a.client.streaming import StreamingClient
from python_a2a import Message, TextContent, MessageRole, Task, TaskState


async def main():
    client = StreamingClient("http://172.22.172.105:57691")

    # build a brand-new task object
    task = Task(
        id=str(uuid.uuid4()),
        message=Message(
            content=TextContent(text="please sleep"), role=MessageRole.USER
        ).to_dict(),
    )

    async for update in client.tasks_send_subscribe(task):
        state = update.status.state
        print("state:", state.value, "msg:", update.status.message, flush=True)

        if state == TaskState.COMPLETED:
            text = update.artifacts[0]["parts"][0]["text"]
            print("result:", text)
            break


if __name__ == "__main__":
    asyncio.run(main())
```


#### 3 · Callback

```mermaid
sequenceDiagram
    participant Client
    participant Registry
    participant Worker
    participant Requester

    Client->>+Registry: Find best agent
    Registry-->>-Client: Return worker agent URL

    Client->>+Worker: Send Task(Message) with callback URL
    Worker-->>-Client: Acknowledge Task (status: WAITING)

    Note over Worker: Process long-running task...

    Worker->>+Requester: Send completed Task(Message, Artifacts) to callback URL
    Reques
```


#### Client




```python
import os
import uuid
import requests
from python_a2a import Message, TextContent, MessageRole, Task
from dotenv import load_dotenv

load_dotenv(".env", override=True)
REGISTRY_URL = os.getenv("REGISTRY_URL", "http://localhost:8000")


def find_best_agent(registry_url, query):
    """Finds the best agent for a given query."""
    response = requests.get(f"{registry_url}/registry/agents")
    response.raise_for_status()
    agents = response.json()

    # This is a simplified stand-in for the LLM-based routing logic.
    # In a real scenario, you would call the routing logic from the original script.
    # For this example, we'll just find an agent that doesn't have the name "requester".
    for agent in agents:
        if "requester" not in agent.get("name", "").lower():
            return agent["name"], agent["url"]
    raise RuntimeError("Could not find a suitable worker agent.")


def main():
    # --- Requester Setup ---
    # This would be the URL of the running requester agent.
    # Since we are running it locally, we can hardcode it.
    requester_port = 8001  # Assuming the requester runs on this port
    REQUESTER_ENDPOINT = f"http://localhost:{requester_port}"

    # --- 1. Discover the worker agent ---
    task_description = "Please write a poem about butterflies."
    try:
        worker_name, worker_url = find_best_agent(REGISTRY_URL, task_description)
        print(f"Found worker agent '{worker_name}' at {worker_url}")
    except (requests.RequestException, RuntimeError) as e:
        print(f"Error finding worker agent: {e}")
        return

    # --- 2. Create the initial task for the worker ---
    correlation_id = str(uuid.uuid4())

    callback_info = {
        "endpoint": REQUESTER_ENDPOINT + "/a2a/tasks/send",
        "data": {
            "message": {
                "role": "system",
                "content": {
                    "type": "text",
                    "text": f"Final result for task {correlation_id}",
                },
            },
        },
    }

    initial_task = Task(
        message=Message(
            role=MessageRole.USER,
            content=TextContent(text=task_description),
        ).to_dict(),
        metadata={"callback_task": callback_info},  # Embed callback info here
    )

    # --- 3. Send the task to the worker ---
    try:
        print(f"Sending task to worker at {worker_url}")
        worker_endpoint = f"{worker_url}/a2a/tasks/send"

        response = requests.post(worker_endpoint, json=initial_task.to_dict())
        response.raise_for_status()
        sent_task_data = response.json()
        print(f"Task {sent_task_data.get('id')} sent to worker. Awaiting callback.")
    except requests.RequestException as e:
        print(f"Error sending task to worker: {e}")


if __name__ == "__main__":
    main()

```



#### Worker

```python
def _finish_and_callback(self, task: Task):
    # Simulate a long-running job
    long_task_duration_secs = 10
    time.sleep(long_task_duration_secs)

    # --- Job is done, prepare the callback task ---
    callback_info = task.metadata.get("callback_task", {})
    callback_endpoint = callback_info.get("endpoint")
    callback_headers = callback_info.get("headers", {})
    callback_data = callback_info.get("data", {})

    if not callback_endpoint:
        logger.error("Callback endpoint not found in task metadata.")
        return

    # Create a new A2A client to communicate with the requester
    requester_client = A2AClient(
        callback_endpoint.split("/a2a/")[0],
        headers=callback_headers,
    )

    # Create the task to send back with the final result
    result_task = Task(**callback_data)
    result_task.artifacts = [
        {
            "parts": [
                {
                    "type": "text",
                    "text": f"Completed the analysis in {long_task_duration_secs} seconds.",
                }
            ]
        }
    ]

    # Send the final result back to the requester
    requester_client._send_task(result_task)
    print("Callback sent to the requester.")

def handle_task(self, task: Task):
    print(task)
    if task.metadata and "callback_task" in task.metadata:
        print("Received a task with a callback. Starting background job.")
        # Start the long-running job in a separate thread
        threading.Thread(
            target=self._finish_and_callback, args=(task,), daemon=True
        ).start()

        # Immediately confirm that the task is being processed
        task.status = TaskStatus(state=TaskState.WAITING)
        return task
    else:
        # Handle a regular task without a callback
        print("Received a regular task.")
        task.status = TaskStatus(state=TaskState.COMPLETED)
        task.artifacts = [
            {
                "parts": [
                    {"type": "text", "text": "Task processed without callback."}
                ]
            }
        ]
        return task
```

#### Requester

```python
def handle_message(self, message: Message) -> Message:
    """
    if method handle_task exists, it will be called instead of handle_message
    """
    print(message)
    return None
```