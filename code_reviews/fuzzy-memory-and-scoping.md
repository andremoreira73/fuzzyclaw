# Fuzzy Memory & Scoping — Implementation Plan

Three related features that all touch the memory/identity layer. Must be planned together — implementation order matters.

## Current State

- **Fuzzy conversational memory:** None. Each invocation gets only the triggering message. "Tell me more about the first one" has zero context.
- **Agent memory namespace:** `(owner_id, agent_name)` — shared across all briefings. Shenlong running for Briefing A and Briefing B shares the same memories.
- **Fuzzy user scoping:** `OWNER_ID=1` hardcoded in docker-compose. Single-user only.

---

## Feature 1: Fuzzy Conversational Memory

### Problem

`handle_message()` in `fuzzy_runner.py` passes only the single triggering message to the agent:

```python
result = agent.invoke({"messages": [HumanMessage(content=message_content)]})
```

No conversation history. Follow-up questions break completely.

### Approach: Board-history-as-context

The board stream IS the canonical conversation log. Read recent messages and build a HumanMessage/AIMessage list before invoking.

**Why not LangGraph checkpointing?** The board stream already exists. Duplicating it into a checkpoint creates a second source of truth. Board-as-context maintains the stateless-agent model (fresh agent per invocation, persistent memory for long-term facts).

### Implementation

#### `fuzzy_runner.py` — new function

```python
def build_conversation_history(board_redis, board_stream: str, self_id: str,
                                trigger_id: str, max_messages: int = 50,
                                user_id: str = '') -> list:
    """Read recent board messages and build LangChain message history.

    Reads from the board stream up to (but not including) the trigger message.
    Maps from=human -> HumanMessage, from=fuzzy -> AIMessage.
    Skips messages between other agents and messages from other users.
    """
```

Logic:

1. `xrevrange(stream_key, count=max_messages * 3)` — overfetch to account for filtered-out messages
2. Reverse to chronological order
3. Filter: include only `(from=human AND to=self_id)` or `(from=self_id)`. Skip inter-agent messages.
4. Stop before the trigger_id entry
5. If `user_id` is set (phase 3), also filter by `user_id` field
6. Map: `from=human` -> `HumanMessage`, `from=self_id` -> `AIMessage`
7. Take last `max_messages` entries
8. Return list

#### `fuzzy_runner.py` — modify `handle_message()`

Change the invoke call:

```python
history = build_conversation_history(
    board_redis, board_stream, self_id, trigger_id,
    max_messages=int(os.environ.get('FUZZY_HISTORY_DEPTH', '50')),
)
result = agent.invoke(
    {"messages": history + [HumanMessage(content=message_content)]},
)
```

#### `docker-compose.yml`

Add `FUZZY_HISTORY_DEPTH=50` to fuzzy service environment.

<comment> make this shorter, I think depth = 15 plus a "living summary" (meaning: when the count hits 15, makes a summary and clean the conversation up to the last 3 interactions
so the API call has the summary plus the last 3 entries; then grows until hits 15 and do the thing again)

### Token budget

50 messages at ~200 tokens average = ~10,000 tokens. GPT-5.4 handles this easily. Env var allows tuning without code changes.

### Open Questions

1. **Message-count window vs time window?** Start with count. Add time cap later if needed.

---

## Feature 2: Agent Memories Scoped by Briefing

### Problem

Namespace `(owner_id, agent_name)` means all briefings share memory. A "remember" from a job-matching briefing leaks into a market-research briefing.

### Implementation

#### `agent_tools/memory.py` — add `briefing_id` parameter

```python
def build_memory_tools(store, agent_name: str, owner_id: str, briefing_id: str = ''):
    if briefing_id:
        namespace = (owner_id, agent_name, briefing_id)
    else:
        namespace = (owner_id, agent_name)
```

Backward-compatible: callers that don't pass `briefing_id` get the old namespace. Fuzzy stays at `(owner_id, "fuzzy")`.

<comment> no need for backward compatibility here. this is still dev... we just cut with the past; we will reset all memories after this update.

#### `core/containers.py` — add `BRIEFING_ID` to container env

After the existing `env['OWNER_ID'] = str(owner_id)` line (inside `_start_agent_container_inner`), add:

```python
env['BRIEFING_ID'] = str(agent_run.run.briefing_id)
```

The ORM chain is already resolved at that point (`agent_run.run.briefing.owner_id`).

#### `agent_runner.py` — read and pass `BRIEFING_ID`

In `run_agent()`, after reading `OWNER_ID`:

```python
briefing_id = os.environ.get('BRIEFING_ID', '')
memory_tools = build_memory_tools(store, agent_def['name'], owner_id, briefing_id)
```

#### Migration

Existing memories in `(owner_id, agent_name)` become orphaned. **Accept this** — platform is in early development, existing memories are minimal, and there's no way to guess which briefing they belonged to.

<comment> we can reset/delet the exiting memories, no problem.

### Open Questions

1. **Global + briefing-scoped memory?** An agent might want to remember "user prefers markdown tables" across all briefings. This would require two namespaces per invocation and two sets of tools. **Defer** — fuzzy's global memory covers this use case better.

<comment> no need for this. Too complicated for edge cases, really.

2. **Coordinator memory?** Coordinators don't use memory currently. If they ever do, they already have access to `run.briefing_id` via the ORM.

<comment> yeah, the coordinator will probably never need a memory, but if so, it is also scoped into its own briefing world, so to speak.

---

## Feature 3: Fuzzy Per-User Scoping

### Problem

`OWNER_ID=1` hardcoded. Single-user only. For multi-user, fuzzy needs to derive the owner from each message's sender.

### Approach: Single stream with `user_id` metadata

Simpler than per-user streams — one XREAD target, no stream management. Privacy is handled by filtering in the Django views (users only see their own messages). Fuzzy is a trusted service that sees all messages.

### Implementation

#### Phase 3A: Add `user_id` to board messages

**`core/views.py` — `fuzzy_board_reply()`**

Add `user_id` to the `xadd` payload:

```python
r.xadd(FUZZY_STREAM_KEY, {
    'from': 'human',
    'to': recipient,
    'content': content,
    'ts': ts,
    'user_id': str(request.user.id),
})
```

**`core/views.py` — remove `FUZZY_OWNER_ID` hardcode**

Replace `_check_fuzzy_access()` with a simple auth check:

```python
def _check_fuzzy_access(request):
    return request.user.is_authenticated
```

**`core/views.py` — `fuzzy_board_messages()`**

Filter stream entries by user:

```python
msg_user_id = data.get('user_id', '')
if msg_user_id and msg_user_id != str(request.user.id):
    continue
```

#### Phase 3B: Fuzzy reads `user_id` from messages

**`fuzzy_runner.py` — `main()` loop**

Remove the hard requirement on `OWNER_ID`. Make it a fallback:

```python
default_owner_id = os.environ.get('OWNER_ID', '')
```

In the message processing loop, extract `user_id`:

```python
user_id = data.get('user_id', '')
owner_id = user_id or default_owner_id
```

Pass `owner_id` to `handle_message()` as before.

**`fuzzy_runner.py` — response messages**

Add `user_id` to fuzzy's board responses so the Django view can filter them:

```python
board_redis.xadd(stream_key, {
    'from': self_id,
    'to': sender,
    'content': response,
    'ts': datetime.now(timezone.utc).isoformat(),
    'user_id': owner_id,
})
```

**`fuzzy_runner.py` — dynamic system prompt**

`build_system_prompt()` currently reads `AGENT_VOLUMES` from env (static). For multi-user, the data path is `/app/data/users/{owner_id}`. Make the volumes section dynamic per invocation.

#### Phase 3C: API scoping for service account

**`core/api_views.py` — all three ViewSets**

Add `?owner=` support for staff users:

```python
def get_queryset(self):
    if self.request.user.is_staff:
        owner_filter = self.request.query_params.get('owner')
        if owner_filter:
            return Briefing.objects.filter(owner_id=owner_filter)
        return Briefing.objects.all()
    return Briefing.objects.filter(owner=self.request.user)
```

Same pattern for `RunViewSet` (`briefing__owner_id`) and `AgentRunViewSet` (`run__briefing__owner_id`).

**`agent_tools/platform_query.py` — pass `?owner=` to API calls**

New helper:

```python
def _owner_params(extra: dict | None = None) -> dict:
    params = dict(extra) if extra else {}
    owner_id = os.environ.get('OWNER_ID', '')
    if owner_id:
        params['owner'] = owner_id
    return params
```

Update all tool functions to use `_owner_params()`.

#### Phase 3D: Docker Compose changes

- Change `OWNER_ID=1` to `OWNER_ID=${FUZZYCLAW_FUZZY_OWNER_ID:-}` (empty by default)
- Change volume from `./data/users/1:/app/data:rw` to `./data:/app/data:rw` (fuzzy needs all user dirs)

### Open Questions

1. **Concurrent message processing?** Currently sequential (one at a time). If user A's request takes 30s, user B waits. **Defer** — fine for small user count. Switch to threading or asyncio if needed later.

<comment> good catch. Agree with defer, but put this in our notes as we must have this solved properly.

2. **`is_staff` vs custom permission for service account?** `is_staff` is simple but grants admin-panel access. A custom `IsServiceAccount` permission is more precise. **Recommendation:** use `is_staff` for now. Document that the service account should NOT be `is_superuser`.

<comment> ok, set to is_staff, not a superuser

3. **Single stream vs per-user streams?** Current plan: single stream, filter by `user_id`. Per-user streams (`fuzzyclaw:board:fuzzy:{user_id}`) are more private but need multi-XREAD. **Decision needed before implementation.** Recommendation: start with single stream, migrate to per-user if privacy requirements emerge.

<comment> agree! single stream for now. Note this for later, just as the async part

---

## Implementation Order

### Phase 1: Briefing-scoped memories (Feature 2)

No dependencies. Simplest change, lowest risk.

1. `agent_tools/memory.py` — add `briefing_id` parameter
2. `core/containers.py` — add `BRIEFING_ID` to container env
3. `agent_runner.py` — read and pass `BRIEFING_ID`
4. Test: specialist memory isolation across briefings, fuzzy unchanged

### Phase 2: Fuzzy conversational memory (Feature 1)

Independent of Phase 1.

1. `fuzzy_runner.py` — add `build_conversation_history()`, modify `handle_message()`
2. `docker-compose.yml` — add `FUZZY_HISTORY_DEPTH`
3. Test: multi-turn conversation with follow-up questions

### Phase 3: Per-user scoping (Feature 3)

Depends on Phase 2 (history function needs `user_id` filtering).

1. 3A: `core/views.py` — `user_id` in messages, remove hardcoded owner, scope views
2. 3B: `fuzzy_runner.py` — extract `user_id`, dynamic owner, `user_id` in responses
3. 3C: `core/api_views.py` + `agent_tools/platform_query.py` — `?owner=` filter
4. 3D: `docker-compose.yml` — remove hardcoded `OWNER_ID`, change volume mount
5. Test: two-user isolation (memory, API, board messages)

---

## Files Changed (summary)

| File                            | Phase | Change                                                    |
| ------------------------------- | ----- | --------------------------------------------------------- |
| `agent_tools/memory.py`         | 1     | Add `briefing_id` param, conditional namespace            |
| `core/containers.py`            | 1     | Add `BRIEFING_ID` to container env                        |
| `agent_runner.py`               | 1     | Read `BRIEFING_ID`, pass to memory tools                  |
| `fuzzy_runner.py`               | 2, 3  | Conversation history, `user_id` extraction, dynamic owner |
| `docker-compose.yml`            | 2, 3  | `FUZZY_HISTORY_DEPTH`, dynamic `OWNER_ID`, volume mount   |
| `core/views.py`                 | 3     | `user_id` in messages, remove hardcode, scope views       |
| `core/api_views.py`             | 3     | `?owner=` filter for staff users                          |
| `agent_tools/platform_query.py` | 3     | `_owner_params()` helper, pass `?owner=`                  |

---

## Testing Strategy

### Unit Tests

- `build_conversation_history()`: mock Redis, verify message mapping, filtering, windowing, user scoping
- `build_memory_tools()`: verify namespace tuple with and without `briefing_id`
- API ViewSet scoping: regular user vs staff user vs staff with `?owner=`

### Integration Tests

- Fuzzy multi-turn: send 3 messages, verify third invocation includes history from first two
- Briefing memory isolation: same agent, two briefings, separate `recall()` results
- Multi-user fuzzy isolation: two users, separate history, separate memory, separate API results

### Manual Testing

- Board panel works for multiple users
- Typing indicator still works
- Backward compat: specialists without `BRIEFING_ID` env var still work
