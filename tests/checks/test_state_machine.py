from __future__ import annotations

import struct
import sys
import tempfile
import types
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Minimal Endstone API stubs.
# ---------------------------------------------------------------------------
endstone = types.ModuleType("endstone")
command = types.ModuleType("endstone.command")
event = types.ModuleType("endstone.event")
plugin_mod = types.ModuleType("endstone.plugin")

class Player: pass
class CommandSender: pass
class Command:
    def __init__(self, name="autofish"): self.name = name
class Plugin: pass

_event_names = [
    "ActorRemoveEvent", "ActorSpawnEvent", "PacketReceiveEvent", "PacketSendEvent",
    "PlayerDeathEvent", "PlayerItemHeldEvent", "PlayerJoinEvent", "PlayerQuitEvent",
    "PlayerRespawnEvent", "PlayerTeleportEvent",
]
for clsname in _event_names:
    setattr(event, clsname, type(clsname, (), {}))
def event_handler(fn): return fn
setattr(event, "event_handler", event_handler)
endstone.Player = Player
command.Command = Command
command.CommandSender = CommandSender
plugin_mod.Plugin = Plugin
sys.modules.update({
    "endstone": endstone,
    "endstone.command": command,
    "endstone.event": event,
    "endstone.plugin": plugin_mod,
})

root = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(root))
from endstone_autofishing.plugin import (
    AutoFishingPlugin,
    PHASE_BITE,
    PHASE_MANUAL_PENDING,
    PHASE_READY,
    PHASE_RECAST_DELAY,
    PHASE_WAITING_BITE,
    PHASE_WAITING_HOOK,
    PerfCounters,
)
from endstone_autofishing.protocol import write_uvarint, zigzag_encode_32


@dataclass
class Dimension:
    name: str = "overworld"

@dataclass
class Loc:
    dimension: Dimension
    x: float = 0.0
    y: float = 64.0
    z: float = 0.0

class ItemType:
    def __init__(self, id): self.id = id
class Item:
    def __init__(self, id): self.type = ItemType(id)
class Inventory:
    def __init__(self):
        self.item_in_main_hand = Item("minecraft:fishing_rod")
        self.held_item_slot = 2

class FakePlayer(Player):
    def __init__(self, name, runtime_id, xuid, x=0.0):
        self.unique_id = f"uuid-{name}"
        self.runtime_id = runtime_id
        self.xuid = xuid
        self.name = name
        self.inventory = Inventory()
        self._dimension = Dimension()
        self.location = Loc(self._dimension, x=x)
        self.messages = []
    @property
    def dimension(self): return self._dimension
    def send_message(self, m): self.messages.append(m)
    def send_popup(self, m): self.messages.append(m)

class Task:
    def __init__(self, fn, delay, period):
        self.fn = fn; self.delay = delay; self.period = period

class Scheduler:
    def __init__(self): self.tasks=[]
    def run_task(self, plugin, fn, delay=0, period=None):
        self.tasks.append(Task(fn, delay, period))
        return self.tasks[-1]
    def run_all(self):
        tasks, self.tasks = self.tasks, []
        for task in tasks:
            task.fn()

class Server:
    protocol_version = 2193
    minecraft_version = "1.26.51.1"
    def __init__(self, players):
        self.online_players = list(players)
        self.scheduler = Scheduler()

class Logger:
    def __init__(self): self.lines=[]
    def info(self, m): self.lines.append(m)
    def warning(self, m): self.lines.append(m)

class PacketEvent:
    def __init__(self, player, packet_id, payload):
        self.player = player
        self.packet_id = packet_id
        self.payload = payload

class Actor:
    type = "minecraft:fishing_hook"
    def __init__(self, player, runtime_id, x=None):
        self.dimension = player.dimension
        self.location = Loc(player.dimension, player.location.x if x is None else x, player.location.y, player.location.z)
        self.runtime_id = runtime_id
class SpawnEvent:
    def __init__(self, actor): self.actor = actor
class RemoveEvent:
    def __init__(self, actor): self.actor = actor
class PlayerEvent:
    def __init__(self, player): self.player = player


def auth_packet(flags=(6, 20), tick=1234):
    tail = b"\x00\x00\x00\x00\x00" + bytes(range(28))
    out = bytearray(struct.pack("<8f", 1, 2, 3, 4, 0, 0, 5, 6))
    out += write_uvarint(len(flags))
    for flag in flags:
        out += write_uvarint(zigzag_encode_32(flag))
    out += write_uvarint(1) + write_uvarint(0) + write_uvarint(0)
    out += struct.pack("<2f", 7, 8)
    out += write_uvarint(tick)
    out += struct.pack("<3f", 9, 10, 11)
    out += tail
    return bytes(out)

packet30 = b"\x00\x00" + write_uvarint(2) + b"ITEM_USE_BODY_2193"

def actor_event(runtime_id, event_id):
    return write_uvarint(runtime_id) + bytes((event_id, 0))


def make_plugin(players):
    p = AutoFishingPlugin()
    p.config = {
        "general": {
            "movement-tolerance": 0.45,
            "vertical-movement-tolerance": 1.0,
            "recast-delay-ticks": 12,
            "hook-association-radius": 12.0,
            "show-status": False,
            "cast-retry-seconds": 4.0,
            "manual-start-timeout-seconds": 2.0,
        },
        "debug": {"protocol-log": False, "performance-counters": False},
    }
    p.logger = Logger()
    p.server = Server(players)
    p._armed_players = set()
    p._enabled_runtime_ids = set()
    p._sessions = {}
    p._hook_owners = {}
    p._waiting_cells = {}
    p._next_token = 1
    p._shutting_down = False
    p._cache_config()
    p._perf = PerfCounters()
    p._perf_player_callbacks = {}
    return p


def enable(p, player):
    assert p.on_command(player, Command(), ["on"]) is True
    assert player.runtime_id in p._enabled_runtime_ids
    assert player.xuid in p._armed_players


def manual_start(p, player, hook_id):
    p.on_packet_receive(PacketEvent(player, 30, packet30))
    s = p._sessions[player.runtime_id]
    assert s.phase == PHASE_MANUAL_PENDING and not s.session_active
    p.on_actor_spawn(SpawnEvent(Actor(player, hook_id)))
    assert s.phase == PHASE_WAITING_BITE and s.session_active
    assert s.hook_runtime_id == hook_id
    assert p._hook_owners[hook_id] == player.runtime_id
    return s


def bite_reel_recast(p, player, hook_id, new_hook_id):
    s = p._sessions[player.runtime_id]
    send = PacketEvent(player, 27, actor_event(hook_id, 13))
    p.on_packet_send(send)
    assert s.phase == PHASE_BITE and s.pending_use == "reel"

    recv = PacketEvent(player, 144, auth_packet(tick=200))
    before = recv.payload
    p.on_packet_receive(recv)
    assert recv.payload != before
    assert s.phase == PHASE_RECAST_DELAY and s.pending_use is None and s.hook_runtime_id is None

    # Includes stale manual-start timeout + the real recast one-shot. The stale
    # callback must not disturb the active session.
    p.server.scheduler.run_all()
    assert s.phase == PHASE_READY and s.pending_use == "cast"

    recv2 = PacketEvent(player, 144, auth_packet(tick=201))
    p.on_packet_receive(recv2)
    assert s.phase == PHASE_WAITING_HOOK and s.pending_use is None

    p.on_actor_spawn(SpawnEvent(Actor(player, new_hook_id)))
    assert s.phase == PHASE_WAITING_BITE and s.hook_runtime_id == new_hook_id
    # Retry callback for the now-successful cast must be stale/no-op.
    p.server.scheduler.run_all()
    assert s.phase == PHASE_WAITING_BITE and s.hook_runtime_id == new_hook_id
    return s


# ---------------------------------------------------------------------------
# Lifecycle regression: first session, movement stop, second and third sessions.
# ---------------------------------------------------------------------------
player = FakePlayer("P1", 101, "xuid-1")
p = make_plugin([player])
tmp = tempfile.TemporaryDirectory()
p.data_folder = tmp.name

enable(p, player)
assert not p._sessions, "enabled idle player must not allocate FishingSession"

s = manual_start(p, player, 1001)
s = bite_reel_recast(p, player, 1001, 1002)

# Moving only terminates the ephemeral session; persistent enabled remains true.
player.location.x = 1.0
p.on_packet_receive(PacketEvent(player, 144, auth_packet(tick=202)))
assert player.runtime_id not in p._sessions
assert player.runtime_id in p._enabled_runtime_ids
assert player.xuid in p._armed_players

# Second start must perform real bite->reel->cast again, not just report text state.
player.location.x = 10.0
s2 = manual_start(p, player, 2001)
s2 = bite_reel_recast(p, player, 2001, 2002)
assert s2.session_active and s2.hook_runtime_id == 2002

# Repeat once more to catch stale scheduler/session token regressions.
player.location.x = 11.0
p.on_packet_receive(PacketEvent(player, 144, auth_packet(tick=203)))
assert player.runtime_id not in p._sessions
player.location.x = 20.0
s3 = manual_start(p, player, 3001)
s3 = bite_reel_recast(p, player, 3001, 3002)
assert s3.session_active and s3.hook_runtime_id == 3002

# Slot changes stop only the session, never persistent enabled.
p.on_player_item_held(PlayerEvent(player))
assert player.runtime_id not in p._sessions
assert player.runtime_id in p._enabled_runtime_ids

# /off is the only user action that removes persistence.
p.on_command(player, Command(), ["off"])
assert player.runtime_id not in p._enabled_runtime_ids
assert player.xuid not in p._armed_players

# ---------------------------------------------------------------------------
# Multi-player isolation and local hook association.
# ---------------------------------------------------------------------------
a = FakePlayer("A", 201, "xuid-a", x=0.0)
b = FakePlayer("B", 202, "xuid-b", x=100.0)
p2 = make_plugin([a, b])
p2.data_folder = tmp.name
enable(p2, a); enable(p2, b)
p2.on_packet_receive(PacketEvent(a, 30, packet30))
p2.on_packet_receive(PacketEvent(b, 30, packet30))
assert set(p2._sessions) == {201, 202}

p2.on_actor_spawn(SpawnEvent(Actor(a, 4001, x=1.0)))
p2.on_actor_spawn(SpawnEvent(Actor(b, 4002, x=99.0)))
assert p2._sessions[201].hook_runtime_id == 4001
assert p2._sessions[202].hook_runtime_id == 4002
assert p2._hook_owners == {4001: 201, 4002: 202}

# A packet delivered to B that names A's hook must never trigger B.
p2.on_packet_send(PacketEvent(b, 27, actor_event(4001, 13)))
assert p2._sessions[202].pending_use is None
# Correct recipient/hook does trigger A only.
p2.on_packet_send(PacketEvent(a, 27, actor_event(4001, 13)))
assert p2._sessions[201].pending_use == "reel"
assert p2._sessions[202].pending_use is None

# O(1) hook removal lookup clears only owner session.
p2.on_actor_remove(RemoveEvent(Actor(a, 4001)))
assert 201 not in p2._sessions
assert 202 in p2._sessions

# ---------------------------------------------------------------------------
# Persistence survives reload; idle restored player still has no session.
# ---------------------------------------------------------------------------
p3 = make_plugin([b])
p3.data_folder = tmp.name
p3._armed_players = set(p2._armed_players)
p3._enabled_runtime_ids = set()
if p3._preference_key(b) in p3._armed_players:
    p3._enabled_runtime_ids.add(b.runtime_id)
assert b.runtime_id in p3._enabled_runtime_ids
assert not p3._sessions

tmp.cleanup()
print("state-machine + lifecycle + multiplayer tests: OK")

# ---------------------------------------------------------------------------
# Late-hook race: a hook arriving after retry is armed but before the next auth
# injection must cancel the pending retry rather than causing a double use.
# ---------------------------------------------------------------------------
c = FakePlayer("C", 303, "xuid-c", x=200.0)
p4 = make_plugin([c]); p4.data_folder = tempfile.mkdtemp(); enable(p4, c)
sc = manual_start(p4, c, 5001)
# Simulate a successful reel then recast injection that produced no immediate hook.
p4.on_packet_send(PacketEvent(c, 27, actor_event(5001, 13)))
p4.on_packet_receive(PacketEvent(c, 144, auth_packet(tick=300)))
p4.server.scheduler.run_all()  # recast -> ready
p4.on_packet_receive(PacketEvent(c, 144, auth_packet(tick=301)))  # cast -> waiting_hook
assert sc.phase == PHASE_WAITING_HOOK
p4.server.scheduler.run_all()  # retry -> ready/pending cast
assert sc.phase == PHASE_READY and sc.pending_use == "cast"
p4.on_actor_spawn(SpawnEvent(Actor(c, 5002)))  # late hook from previous cast
assert sc.phase == PHASE_WAITING_BITE and sc.hook_runtime_id == 5002
assert sc.pending_use is None
print("late-hook retry race test: OK")
