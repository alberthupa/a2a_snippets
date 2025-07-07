# Choosing an A2A pattern for 10‑minute jobs

**Scenario**: a *requester* agent kicks off a job it knows takes **≈10 minutes** on a *worker* agent and wants to be notified when done.


https://chatgpt.com/c/6867c5e1-77bc-800a-9b8f-4912dd041ce3

---

## 1. Poll + Get

| Aspect              | Polling (`/tasks/send` + periodic `/tasks/get`)                                                 |
| ------------------- | ----------------------------------------------------------------------------------------------- |
| **Network**         | `N = ceil(job_time / interval)` round‑trips. For a 10‑minute job at 30 s cadence → 20 requests. |
| **Latency to know** | *At most* the poll interval (e.g. 30 s).                                                        |
| **Simplicity**      | Works with plain synchronous code. No server‑side push needed.                                  |
| **Robustness**      | Resilient if firewalls/proxies block long‑lived connections; each request re‑authenticates.     |
| **Downsides**       | Wastes traffic & CPU when thousands of concurrent tasks; longer interval ⇒ slower notice.       |

### Code sketch

*Worker*: background thread updates `self.tasks` when done.
*Requester*: loop `while state != COMPLETED: sleep(interval); get_task()`.

---

## 2. Subscribe / Server‑push

| Aspect              | Subscribe (`/tasks/subscribe`)                                                 |
| ------------------- | ------------------------------------------------------------------------------ |
| **Network**         | One long‑lived HTTP/2 or WS stream. Practically zero chatter until completion. |
| **Latency to know** | Near‑instant (milliseconds) once the worker publishes.                         |
| **Simplicity**      | Requires async IO on requester; worker must call `publisher.publish(task)`.    |
| **Robustness**      | May be blocked by strict proxies; must keep connection alive \~10 min.         |
| **Downsides**       | Each open stream ties up server memory; need keep‑alive pings or SSE.          |

### Code sketch

*Worker*: `asyncio.create_task(_finish())`, then `await publisher.publish(task)` after sleep.
*Requester*: `async for update in client.subscribe_task(task_id): ...`.

---

## 3. Callback (Worker → Requester)

* Worker, once done, issues **a new A2A request back to the requester’s own endpoint**.
* Pros: requester needn’t stay connected; natural fit for chained workflows.
* Cons: requester must expose an endpoint & auth token; cycles of mutual trust.

---

## Recommendation

| Environment                                                                 | Pick                                                       |
| --------------------------------------------------------------------------- | ---------------------------------------------------------- |
| **Both agents can keep a 10‑min stream open** (same LAN, no proxy timeouts) | **Subscribe** — immediate notification, least chatter.     |
| **Uncertain network / simple client**                                       | **Polling** at a moderate interval (e.g., 30–60 s).        |
| **Requester is itself an A2A server** and you want full decoupling          | **Callback** chain: worker `tasks/send` back to requester. |

For most cloud or container setups where idle TCP streams survive 10 minutes, *subscribe* gives the best user experience. If you’re on flaky corporate networks or CLI environments, stick to *polling*. If you’re orchestrating multi‑step agent pipelines, consider the *callback* pattern.


---

*As i understand, if a requester sends a task, he waits for response from worker, what may take some time. And what if I have a requester who must send tasks to various workers And not wait for immediate response*


You don’t have to wait synchronously for each worker—you can “fire and forget” and collect the answers later.  Three common ways to structure it:

| Pattern                         | How the requester behaves                                                                                                                                                                  | When it shines                                                                                                                                                                                                 |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Fan-out + polling**           | Loop over all worker URLs<br>`_send_task()` → store each `task_id`<br>Start a timer (or background thread/async task) that polls every N seconds for every ID.                             | Very simple code; works anywhere HTTP is allowed.  Good when you have a few dozen jobs or can tolerate a 30--60 s delay.                                                                                       |
| **Fan-out + subscribe streams** | For each worker, open an async `subscribe_task()` stream and add it to an `asyncio.gather()` list.  Your main coroutine just awaits all streams; each one pushes when the worker finishes. | Instant notification and no wasted polling traffic.  Ideal if you already run an asyncio loop and the network keeps long-lived connections alive.                                                              |
| **Callback chain**              | Each worker, when done, calls **your** requester’s own A2A endpoint (`/tasks/send`) with the result.  Your requester is itself an A2A server.                                              | Best for high fan-out (hundreds/thousands).  The requester doesn’t need to keep connections open or poll; everything is event-driven.  Requires mutual auth/trust and that the requester exposes an HTTP port. |

**Typical recipe**

```text
for worker in workers:                 # fan-out
    task = client._create_task(...)    # describe work
    task = client._send_task(task)     # returns immediately
    store[worker] = task.id            # remember where to look later

# later – either:
#  1) poll each store[worker] in a loop
#  2) await subscribe_task() streams
#  3) accept callbacks on your own /tasks/send
```

**Rule of thumb**

* *Do you control both sides and can keep a 10-minute TCP stream?* → **subscribe** (lowest latency, no polling).
* *Behind proxies or very simple CLI requester?* → **polling** every 30-60 s.
* *Running a pipeline where every agent is already an A2A server?* → **callback** so workers push results back.

All three integrate cleanly with the minimal skeletons in the canvas—you just duplicate the send logic across the list of worker URLs and choose one collection strategy.


## Scenario poll and get for long tasks:

### Worker

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


### Requester
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

## Scenario Subscribe / Server‑push

### Worker



### Requester

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


### Requester

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