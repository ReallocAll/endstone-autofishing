from __future__ import annotations

# Reuse stubs/helpers by importing the state-machine test module. It executes its
# regression suite first, which is intentional for this lightweight source bundle.
import test_state_machine as sm

p = sm.make_plugin([])

class GuardEvent:
    def __init__(self, packet_id):
        self.packet_id = packet_id
        self.player_reads = 0
        self.payload_reads = 0
    @property
    def player(self):
        self.player_reads += 1
        raise AssertionError("hot fast-path touched event.player")
    @property
    def payload(self):
        self.payload_reads += 1
        raise AssertionError("hot fast-path touched event.payload")

# Unrelated receive/send packets must do exactly the ID branch before returning.
r = GuardEvent(999)
p.on_packet_receive(r)
assert r.player_reads == 0 and r.payload_reads == 0
s = GuardEvent(999)
p.on_packet_send(s)
assert s.player_reads == 0 and s.payload_reads == 0

# Even target high-frequency packet IDs must not touch player/payload if there is
# no provisional/active fishing session.
r144 = GuardEvent(p._pid_auth)
p.on_packet_receive(r144)
assert r144.player_reads == 0 and r144.payload_reads == 0
s27 = GuardEvent(p._pid_actor_event)
p.on_packet_send(s27)
assert s27.player_reads == 0 and s27.payload_reads == 0

# No global repeating scheduler exists. The plugin only schedules one-shot tasks
# from a concrete session transition.
assert not p.server.scheduler.tasks

# Instrumentation is opt-in and can quantify bridge callbacks vs relevant logic.
p._perf_enabled = True
p._perf = sm.PerfCounters()
p.on_packet_receive(GuardEvent(999))
p.on_packet_send(GuardEvent(999))
assert p._perf.packet_receive_callbacks == 1
assert p._perf.packet_receive_unrelated_fast_return == 1
assert p._perf.packet_send_callbacks == 1
assert p._perf.packet_send_unrelated_fast_return == 1

print("packet hot-path guard tests: OK")
