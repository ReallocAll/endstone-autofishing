import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from endstone import Player
from endstone.command import Command, CommandSender
from endstone.event import (
    ActorRemoveEvent,
    ActorSpawnEvent,
    PacketReceiveEvent,
    PacketSendEvent,
    PlayerDeathEvent,
    PlayerItemHeldEvent,
    PlayerJoinEvent,
    PlayerQuitEvent,
    PlayerRespawnEvent,
    PlayerTeleportEvent,
    event_handler,
)
from endstone.plugin import Plugin

from .protocol import (
    ProtocolError,
    UseTemplate,
    inject_learned_use,
    learn_use_template_from_inventory_transaction,
    parse_actor_event,
)


PHASE_MANUAL_PENDING = "manual_pending"
PHASE_WAITING_HOOK = "waiting_hook"
PHASE_WAITING_BITE = "waiting_bite"
PHASE_BITE = "bite"
PHASE_RECAST_DELAY = "recast_delay"
PHASE_READY = "ready"


@dataclass(slots=True)
class FishingSession:
    """Ephemeral state for one concrete fishing session.

    Persistent opt-in is intentionally NOT stored here. ``_armed_players`` owns
    that setting.  When a session ends this object is removed completely, so an
    enabled-but-idle player has no per-tick/per-packet fishing state to maintain.
    """

    player_runtime_id: int
    player: Player
    token: int
    template: UseTemplate
    anchor_x: float
    anchor_y: float
    anchor_z: float
    dimension_name: str
    session_active: bool = False
    hook_runtime_id: int | None = None
    pending_use: str | None = None
    phase: str = PHASE_MANUAL_PENDING
    wait_cell: tuple[str, int, int] | None = None


@dataclass(slots=True)
class PerfCounters:
    started_at: float = field(default_factory=time.perf_counter)
    packet_receive_callbacks: int = 0
    packet_receive_target_id: int = 0
    packet_receive_unrelated_fast_return: int = 0
    packet_receive_no_session_fast_return: int = 0
    packet_send_callbacks: int = 0
    packet_send_target_id: int = 0
    packet_send_unrelated_fast_return: int = 0
    packet_send_no_session_fast_return: int = 0
    actor_event_logic_entries: int = 0
    bite_events: int = 0
    injections: int = 0
    session_starts: int = 0
    session_stops: int = 0
    scheduler_callbacks: int = 0
    scheduler_sessions_processed: int = 0


class AutoFishingPlugin(Plugin):
    api_version = "0.11"
    prefix = "AutoFishing"

    commands = {
        "autofish": {
            "description": "开启或关闭自动钓鱼",
            "usages": ["/autofish (on|off|status|reset|stats)[action: AutoFishingAction]"],
            "aliases": ["af"],
            "permissions": ["autofishing.command.use"],
        },
    }

    permissions = {
        "autofishing.command.use": {
            "description": "允许使用自动钓鱼",
            "default": True,
        },
    }

    def on_enable(self) -> None:
        self.save_default_config()

        # Persistent preference vs ephemeral session are separate by design.
        self._armed_players: set[str] = set()
        self._enabled_runtime_ids: set[int] = set()
        self._sessions: dict[int, FishingSession] = {}
        self._hook_owners: dict[int, int] = {}
        self._waiting_cells: dict[tuple[str, int, int], set[int]] = {}
        self._next_token = 1
        self._shutting_down = False

        self._load_armed_players()
        self._cache_config()
        self._perf = PerfCounters()
        self._perf_player_callbacks: dict[int, int] = {}

        # A reload can happen with players already online; seed the cheap runtime-id
        # set once here instead of consulting XUID persistence on packet hot paths.
        for player in self.server.online_players:
            if self._preference_key(player) in self._armed_players:
                self._enabled_runtime_ids.add(int(player.runtime_id))

        self.register_events(self)

        # Deliberately NO repeating scheduler.  Session timeouts/recasts/retries are
        # one-shot tasks created only for active/provisional fishing sessions.
        self.logger.info(
            "AutoFishing v0.4.0 enabled "
            f"(BDS {self.server.minecraft_version}, protocol {self.server.protocol_version}; "
            f"auth={self._pid_auth}, inventory={self._pid_inventory}, "
            f"actor_event={self._pid_actor_event}, hooktime={self._hooktime_event_id})."
        )

    def on_disable(self) -> None:
        self._shutting_down = True
        self._save_armed_players()
        self._sessions.clear()
        self._hook_owners.clear()
        self._waiting_cells.clear()
        self._enabled_runtime_ids.clear()

    # ------------------------------------------------------------------
    # Config/cache helpers. Nothing in packet hot paths calls self.config.
    # ------------------------------------------------------------------
    def _cfg(self, section: str, key: str, default: Any) -> Any:
        try:
            return self.config.get(section, {}).get(key, default)
        except Exception:
            return default

    def _cache_config(self) -> None:
        try:
            protocol_2193 = int(self.server.protocol_version) == 2193
        except Exception:
            protocol_2193 = False

        if protocol_2193:
            self._pid_auth = 144
            self._pid_inventory = 30
            self._pid_actor_event = 27
            self._item_interact_flag = 34
            self._hooktime_event_id = 13
            self._blocked_flags = frozenset((35, 36, 45))
        else:
            self._pid_auth = int(self._cfg("protocol", "player-auth-input-packet-id", 144))
            self._pid_inventory = int(self._cfg("protocol", "inventory-transaction-packet-id", 30))
            self._pid_actor_event = int(self._cfg("protocol", "actor-event-packet-id", 27))
            self._item_interact_flag = int(self._cfg("protocol", "item-interact-flag", 34))
            self._hooktime_event_id = int(self._cfg("protocol", "fish-hooktime-event-id", 13))
            self._blocked_flags = frozenset(
                (
                    int(self._cfg("protocol", "block-action-flag", 35)),
                    int(self._cfg("protocol", "item-stack-request-flag", 36)),
                    int(self._cfg("protocol", "predicted-vehicle-flag", 45)),
                )
            )

        tolerance = max(0.0, float(self._cfg("general", "movement-tolerance", 0.45)))
        self._movement_tolerance_sq = tolerance * tolerance
        self._vertical_tolerance = max(0.0, float(self._cfg("general", "vertical-movement-tolerance", 1.0)))
        self._recast_delay_ticks = max(1, int(self._cfg("general", "recast-delay-ticks", 12)))
        self._manual_timeout_ticks = max(
            1, int(round(max(0.05, float(self._cfg("general", "manual-start-timeout-seconds", 2.0))) * 20.0))
        )
        self._cast_retry_ticks = max(
            1, int(round(max(0.05, float(self._cfg("general", "cast-retry-seconds", 4.0))) * 20.0))
        )
        radius = max(1.0, float(self._cfg("general", "hook-association-radius", 12.0)))
        self._hook_radius_sq = radius * radius
        self._hook_cell_size = radius
        self._show_status = bool(self._cfg("general", "show-status", True))
        self._protocol_log = bool(self._cfg("debug", "protocol-log", False))
        self._perf_enabled = bool(self._cfg("debug", "performance-counters", False))

    # ------------------------------------------------------------------
    # Persistent player opt-in.
    # ------------------------------------------------------------------
    @property
    def _armed_file(self) -> Path:
        return Path(self.data_folder) / "armed_players.json"

    @staticmethod
    def _preference_key(player: Player) -> str:
        xuid = str(getattr(player, "xuid", "") or "").strip()
        return xuid if xuid else str(player.unique_id)

    @staticmethod
    def _runtime_id(player: Player) -> int:
        # Integer runtime IDs avoid UUID/string allocation on packet hot paths.
        return int(player.runtime_id)

    def _load_armed_players(self) -> None:
        try:
            data = json.loads(self._armed_file.read_text(encoding="utf-8"))
            values = data.get("xuids", []) if isinstance(data, dict) else []
            self._armed_players = {str(value) for value in values if str(value)}
        except FileNotFoundError:
            self._armed_players = set()
        except Exception as exc:
            self._armed_players = set()
            self.logger.warning(f"Failed to read armed_players.json: {exc}")

    def _save_armed_players(self) -> None:
        try:
            self._armed_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {"xuids": sorted(self._armed_players)}
            self._armed_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except Exception as exc:
            self.logger.warning(f"Failed to write armed_players.json: {exc}")

    def _set_armed_preference(self, player: Player, enabled: bool) -> None:
        pref = self._preference_key(player)
        runtime_id = self._runtime_id(player)
        if enabled:
            self._armed_players.add(pref)
            self._enabled_runtime_ids.add(runtime_id)
        else:
            self._armed_players.discard(pref)
            self._enabled_runtime_ids.discard(runtime_id)
        self._save_armed_players()

    def _is_enabled_player(self, player: Player, runtime_id: int) -> bool:
        if runtime_id in self._enabled_runtime_ids:
            return True
        # Self-heal after respawn/runtime-id changes. This runs only on a real
        # ItemUse packet, never on generic packet traffic.
        if self._preference_key(player) in self._armed_players:
            self._enabled_runtime_ids.add(runtime_id)
            return True
        return False

    # ------------------------------------------------------------------
    # Session/index helpers.
    # ------------------------------------------------------------------
    @staticmethod
    def _dimension_name(obj: Any) -> str:
        dimension = getattr(obj, "dimension", None)
        return str(getattr(dimension, "name", ""))

    def _cell_for(self, dimension_name: str, x: float, z: float) -> tuple[str, int, int]:
        size = self._hook_cell_size
        return dimension_name, math.floor(x / size), math.floor(z / size)

    def _index_waiting(self, session: FishingSession) -> None:
        if session.wait_cell is not None:
            return
        cell = self._cell_for(session.dimension_name, session.anchor_x, session.anchor_z)
        bucket = self._waiting_cells.get(cell)
        if bucket is None:
            bucket = set()
            self._waiting_cells[cell] = bucket
        bucket.add(session.player_runtime_id)
        session.wait_cell = cell

    def _unindex_waiting(self, session: FishingSession) -> None:
        cell = session.wait_cell
        if cell is None:
            return
        bucket = self._waiting_cells.get(cell)
        if bucket is not None:
            bucket.discard(session.player_runtime_id)
            if not bucket:
                self._waiting_cells.pop(cell, None)
        session.wait_cell = None

    def _remove_session(self, runtime_id: int, reason: str | None = None) -> None:
        session = self._sessions.pop(runtime_id, None)
        if session is None:
            return
        self._unindex_waiting(session)
        if session.hook_runtime_id is not None:
            owner = self._hook_owners.get(session.hook_runtime_id)
            if owner == runtime_id:
                self._hook_owners.pop(session.hook_runtime_id, None)
        if self._perf_enabled:
            self._perf.session_stops += 1
        if reason and runtime_id in self._enabled_runtime_ids:
            self._tell(session.player, f"§e自动钓鱼已暂停：§f{reason}§7；再次手动抛竿即可启动。", popup=True)

    def _new_manual_session(self, player: Player, runtime_id: int, template: UseTemplate) -> FishingSession:
        # User input during an old session deliberately supersedes that session.
        self._remove_session(runtime_id)
        location = player.location
        session = FishingSession(
            player_runtime_id=runtime_id,
            player=player,
            token=self._next_token,
            template=template,
            anchor_x=location.x,
            anchor_y=location.y,
            anchor_z=location.z,
            dimension_name=self._dimension_name(location),
        )
        self._next_token += 1
        self._sessions[runtime_id] = session
        self._index_waiting(session)
        self._schedule_manual_timeout(session)
        return session

    def _bind_hook(self, session: FishingSession, hook_runtime_id: int) -> None:
        self._unindex_waiting(session)
        session.hook_runtime_id = hook_runtime_id
        session.pending_use = None
        session.phase = PHASE_WAITING_BITE
        if not session.session_active:
            session.session_active = True
            if self._perf_enabled:
                self._perf.session_starts += 1
        self._hook_owners[hook_runtime_id] = session.player_runtime_id

    def _tell(self, player: Player, message: str, *, popup: bool = False) -> None:
        if popup:
            if self._show_status:
                player.send_popup(message)
            return
        player.send_message(message)

    @staticmethod
    def _is_rod(player: Player) -> bool:
        # Called only for a real InventoryTransaction ItemUse transition.
        item = player.inventory.item_in_main_hand
        if item is None:
            return False
        return getattr(getattr(item, "type", None), "id", "") == "minecraft:fishing_rod"

    # ------------------------------------------------------------------
    # One-shot scheduler. Token checks make every stale callback a no-op.
    # ------------------------------------------------------------------
    def _schedule_manual_timeout(self, session: FishingSession) -> None:
        runtime_id = session.player_runtime_id
        token = session.token
        self.server.scheduler.run_task(
            self,
            lambda: self._manual_timeout(runtime_id, token),
            delay=self._manual_timeout_ticks,
        )

    def _manual_timeout(self, runtime_id: int, token: int) -> None:
        if self._perf_enabled:
            self._perf.scheduler_callbacks += 1
        if self._shutting_down:
            return
        session = self._sessions.get(runtime_id)
        if session is None or session.token != token or session.phase != PHASE_MANUAL_PENDING:
            return
        if self._perf_enabled:
            self._perf.scheduler_sessions_processed += 1
        self._remove_session(runtime_id)
        if self._protocol_log:
            self.logger.info(f"[protocol] manual ItemUse produced no fishing hook runtime={runtime_id}; idle")

    def _schedule_recast(self, session: FishingSession) -> None:
        runtime_id = session.player_runtime_id
        token = session.token
        self.server.scheduler.run_task(
            self,
            lambda: self._arm_recast(runtime_id, token),
            delay=self._recast_delay_ticks,
        )

    def _arm_recast(self, runtime_id: int, token: int) -> None:
        if self._perf_enabled:
            self._perf.scheduler_callbacks += 1
        if self._shutting_down:
            return
        session = self._sessions.get(runtime_id)
        if session is None or session.token != token or session.phase != PHASE_RECAST_DELAY:
            return
        if self._perf_enabled:
            self._perf.scheduler_sessions_processed += 1
        session.pending_use = "cast"
        session.phase = PHASE_READY

    def _schedule_cast_retry(self, session: FishingSession) -> None:
        runtime_id = session.player_runtime_id
        token = session.token
        self.server.scheduler.run_task(
            self,
            lambda: self._arm_cast_retry(runtime_id, token),
            delay=self._cast_retry_ticks,
        )

    def _arm_cast_retry(self, runtime_id: int, token: int) -> None:
        if self._perf_enabled:
            self._perf.scheduler_callbacks += 1
        if self._shutting_down:
            return
        session = self._sessions.get(runtime_id)
        if (
            session is None
            or session.token != token
            or not session.session_active
            or session.phase != PHASE_WAITING_HOOK
            or session.hook_runtime_id is not None
            or session.pending_use is not None
        ):
            return
        if self._perf_enabled:
            self._perf.scheduler_sessions_processed += 1
        session.pending_use = "cast"
        session.phase = PHASE_READY

    # ------------------------------------------------------------------
    # Commands.
    # ------------------------------------------------------------------
    def on_command(self, sender: CommandSender, command: Command, args: list[str]) -> bool:
        if command.name != "autofish":
            return False
        if not isinstance(sender, Player):
            sender.send_message("该命令只能由玩家使用。")
            return True

        action = args[0].lower() if args and args[0] else "toggle"
        runtime_id = self._runtime_id(sender)
        enabled = self._is_enabled_player(sender, runtime_id)

        if action in {"off", "stop", "0", "false"}:
            self._remove_session(runtime_id)
            self._set_armed_preference(sender, False)
            self._tell(sender, "§7常驻自动钓鱼已关闭。")
            return True

        if action in {"status", "s"}:
            session = self._sessions.get(runtime_id)
            phase = session.phase if session is not None else "idle"
            active = bool(session and session.session_active)
            master = "§a常驻开启" if enabled else "§7关闭"
            self._tell(sender, f"§bAutoFishing§7：{master}§7，session_active={active}，状态={phase}")
            return True

        if action == "stats":
            self._show_perf_stats(sender)
            return True

        if action in {"reset", "calibrate"}:
            self._remove_session(runtime_id)
            self._tell(sender, "§e已清除当前钓鱼会话；下次手动抛竿会重新学习。")
            return True

        if action not in {"on", "start", "1", "true", "toggle"}:
            self._tell(sender, "§c用法：/autofish [on|off|status|reset|stats]")
            return True

        if action == "toggle" and enabled:
            self._remove_session(runtime_id)
            self._set_armed_preference(sender, False)
            self._tell(sender, "§7常驻自动钓鱼已关闭。")
            return True

        if enabled:
            self._tell(sender, "§a常驻自动钓鱼已经开启。§7在任意位置手动抛竿即可自动接管。")
            return True

        self._set_armed_preference(sender, True)
        self._tell(sender, "§a常驻自动钓鱼已开启。§7以后在任意位置手动抛竿都会自动启动；移动只会暂停当前钓鱼。")
        return True

    def _show_perf_stats(self, player: Player) -> None:
        if not self._perf_enabled:
            self._tell(player, "§7性能计数器未启用；在 config.toml 设置 debug.performance-counters = true。")
            return
        p = self._perf
        elapsed = max(0.001, time.perf_counter() - p.started_at)
        runtime_id = self._runtime_id(player)
        player_callbacks = self._perf_player_callbacks.get(runtime_id, 0)
        self._tell(
            player,
            "§bAutoFishing perf§7："
            f"recv={p.packet_receive_callbacks} ({p.packet_receive_callbacks / elapsed:.1f}/s), "
            f"recv_target={p.packet_receive_target_id}, recv_no_session={p.packet_receive_no_session_fast_return}; "
            f"send={p.packet_send_callbacks} ({p.packet_send_callbacks / elapsed:.1f}/s), "
            f"send_target={p.packet_send_target_id}, send_no_session={p.packet_send_no_session_fast_return}; "
            f"bite_logic={p.actor_event_logic_entries}, bites={p.bite_events}, injections={p.injections}, "
            f"sessions={p.session_starts}/{p.session_stops}, "
            f"scheduler={p.scheduler_callbacks} ({p.scheduler_callbacks / elapsed:.2f}/s), "
            f"your_active_callbacks={player_callbacks}。",
        )

    # ------------------------------------------------------------------
    # Player lifecycle. All operations are O(1) by player runtime ID.
    # ------------------------------------------------------------------
    @event_handler
    def on_player_join(self, event: PlayerJoinEvent) -> None:
        player = event.player
        if self._preference_key(player) in self._armed_players:
            self._enabled_runtime_ids.add(self._runtime_id(player))

    @event_handler
    def on_player_quit(self, event: PlayerQuitEvent) -> None:
        player = event.player
        runtime_id = self._runtime_id(player)
        self._remove_session(runtime_id)
        self._enabled_runtime_ids.discard(runtime_id)

    @event_handler
    def on_player_death(self, event: PlayerDeathEvent) -> None:
        if not self._sessions:
            return
        self._remove_session(self._runtime_id(event.player))

    @event_handler
    def on_player_respawn(self, event: PlayerRespawnEvent) -> None:
        player = event.player
        runtime_id = self._runtime_id(player)
        self._remove_session(runtime_id)
        if self._preference_key(player) in self._armed_players:
            self._enabled_runtime_ids.add(runtime_id)

    @event_handler
    def on_player_teleport(self, event: PlayerTeleportEvent) -> None:
        if not self._sessions:
            return
        self._remove_session(self._runtime_id(event.player), "位置发生变化")

    @event_handler
    def on_player_item_held(self, event: PlayerItemHeldEvent) -> None:
        if not self._sessions:
            return
        # Do not inspect inventory. Slot-change itself is sufficient to make the
        # captured ItemUse transaction unsafe to replay. Persistent opt-in stays on.
        self._remove_session(self._runtime_id(event.player), "切换了快捷栏槽位")

    # ------------------------------------------------------------------
    # Hook lifecycle. No world/entity scans; runtime IDs are direct maps.
    # ------------------------------------------------------------------
    @event_handler
    def on_actor_spawn(self, event: ActorSpawnEvent) -> None:
        if not self._waiting_cells:
            return
        actor = event.actor
        if actor.type != "minecraft:fishing_hook":
            return

        location = actor.location
        dimension_name = self._dimension_name(location)
        _, cx, cz = self._cell_for(dimension_name, location.x, location.z)

        best_distance_sq = self._hook_radius_sq + 1.0
        best_session: FishingSession | None = None
        # Search only the 3x3 local spatial buckets of sessions explicitly waiting
        # for a hook. This is independent of total online players/world entities.
        for dx in (-1, 0, 1):
            for dz in (-1, 0, 1):
                bucket = self._waiting_cells.get((dimension_name, cx + dx, cz + dz))
                if not bucket:
                    continue
                for runtime_id in bucket:
                    session = self._sessions.get(runtime_id)
                    if session is None or session.phase not in {PHASE_MANUAL_PENDING, PHASE_WAITING_HOOK, PHASE_READY}:
                        continue
                    ddx = session.anchor_x - location.x
                    ddy = session.anchor_y - location.y
                    ddz = session.anchor_z - location.z
                    distance_sq = ddx * ddx + ddy * ddy + ddz * ddz
                    if distance_sq <= self._hook_radius_sq and distance_sq < best_distance_sq:
                        best_distance_sq = distance_sq
                        best_session = session

        if best_session is None:
            return

        was_manual = best_session.phase == PHASE_MANUAL_PENDING
        self._bind_hook(best_session, int(actor.runtime_id))
        if self._protocol_log:
            self.logger.info(
                f"[protocol] spawn-bound hook={actor.runtime_id} player_runtime={best_session.player_runtime_id}"
            )
        if was_manual:
            self._tell(best_session.player, "§a自动钓鱼已启动：等待上钩…", popup=True)

    @event_handler
    def on_actor_remove(self, event: ActorRemoveEvent) -> None:
        if not self._hook_owners:
            return
        actor = event.actor
        if actor.type != "minecraft:fishing_hook":
            return
        hook_runtime_id = int(actor.runtime_id)
        player_runtime_id = self._hook_owners.pop(hook_runtime_id, None)
        if player_runtime_id is None:
            return
        session = self._sessions.get(player_runtime_id)
        if session is None or session.hook_runtime_id != hook_runtime_id:
            return
        # If this was our synthetic reel, the mapping is removed before BDS removes
        # the hook. Reaching here therefore means the hook disappeared unexpectedly.
        self._remove_session(player_runtime_id)
        if self._protocol_log:
            self.logger.info(
                f"[protocol] hook removed unexpectedly hook={hook_runtime_id} player_runtime={player_runtime_id}"
            )

    # ------------------------------------------------------------------
    # Network receive hot path.
    # ------------------------------------------------------------------
    @event_handler
    def on_packet_receive(self, event: PacketReceiveEvent) -> None:
        packet_id = event.packet_id
        if self._perf_enabled:
            self._perf.packet_receive_callbacks += 1

        if packet_id != self._pid_inventory and packet_id != self._pid_auth:
            if self._perf_enabled:
                self._perf.packet_receive_unrelated_fast_return += 1
            return

        if self._perf_enabled:
            self._perf.packet_receive_target_id += 1

        # PlayerAuthInput is ~20 Hz/player. If nobody even has a provisional/active
        # fishing session, return BEFORE event.player/payload/UUID/inventory access.
        if packet_id == self._pid_auth and not self._sessions:
            if self._perf_enabled:
                self._perf.packet_receive_no_session_fast_return += 1
            return

        player = event.player
        if player is None:
            return
        runtime_id = self._runtime_id(player)

        if packet_id == self._pid_inventory:
            if not self._is_enabled_player(player, runtime_id):
                return
            # A real ItemUse packet is a rare transition, so checking the held item
            # here is acceptable. It is never done per tick/per generic packet.
            if not self._is_rod(player):
                return
            raw = event.payload
            try:
                template = learn_use_template_from_inventory_transaction(
                    raw,
                    self._item_interact_flag,
                    encoding="zigzag",
                )
            except ProtocolError as exc:
                if self._protocol_log:
                    self.logger.info(
                        f"[protocol] packet30 ignored runtime={runtime_id}: {exc}; head={raw[:24].hex()}"
                    )
                return
            session = self._new_manual_session(player, runtime_id, template)
            if self._protocol_log:
                self.logger.info(
                    f"[protocol] manual rod use runtime={runtime_id} packed={len(template.packed_transaction)}"
                )
            return

        # From here on this is PlayerAuthInput. Do not inspect idle enabled players.
        session = self._sessions.get(runtime_id)
        if session is None:
            if self._perf_enabled:
                self._perf.packet_receive_no_session_fast_return += 1
            return
        if self._perf_enabled:
            self._perf_player_callbacks[runtime_id] = self._perf_player_callbacks.get(runtime_id, 0) + 1

        if not self._validate_motion(player, session):
            return
        if session.pending_use is None:
            return

        action = session.pending_use
        try:
            event.payload = inject_learned_use(
                event.payload,
                session.template,
                blocked_flags=self._blocked_flags,
            )
        except ProtocolError as exc:
            if self._protocol_log:
                self.logger.info(f"[protocol] use injection skipped runtime={runtime_id}: {exc}")
            return

        session.pending_use = None
        if self._perf_enabled:
            self._perf.injections += 1
        if self._protocol_log:
            self.logger.info(f"[protocol] injected action={action} runtime={runtime_id} bytes={len(event.payload)}")

        if action == "reel":
            if session.hook_runtime_id is not None:
                self._hook_owners.pop(session.hook_runtime_id, None)
            session.hook_runtime_id = None
            session.phase = PHASE_RECAST_DELAY
            self._unindex_waiting(session)
            self._schedule_recast(session)
        else:
            session.phase = PHASE_WAITING_HOOK
            self._index_waiting(session)
            self._schedule_cast_retry(session)

    # ------------------------------------------------------------------
    # Network send hot path.
    # ------------------------------------------------------------------
    @event_handler
    def on_packet_send(self, event: PacketSendEvent) -> None:
        packet_id = event.packet_id
        if self._perf_enabled:
            self._perf.packet_send_callbacks += 1

        # Endstone 0.11 currently registers packet events only by Event type, not
        # packet ID, so every send still crosses C++ -> Python. Keep the Python body
        # to one integer comparison + dict truthiness for the overwhelmingly common
        # unrelated/idle cases. A native packet-id filter is the remaining long-term
        # optimization outside this plugin's public Python API.
        if packet_id != self._pid_actor_event:
            if self._perf_enabled:
                self._perf.packet_send_unrelated_fast_return += 1
            return
        if self._perf_enabled:
            self._perf.packet_send_target_id += 1
        if not self._sessions:
            if self._perf_enabled:
                self._perf.packet_send_no_session_fast_return += 1
            return

        player = event.player
        if player is None:
            return
        runtime_id = self._runtime_id(player)
        session = self._sessions.get(runtime_id)
        if session is None or not session.session_active or session.hook_runtime_id is None:
            return
        if self._perf_enabled:
            self._perf.actor_event_logic_entries += 1
            self._perf_player_callbacks[runtime_id] = self._perf_player_callbacks.get(runtime_id, 0) + 1

        try:
            hook_runtime_id, actor_event = parse_actor_event(event.payload)
        except ProtocolError:
            return
        if hook_runtime_id != session.hook_runtime_id or actor_event != self._hooktime_event_id:
            return
        if session.pending_use is not None:
            return

        session.pending_use = "reel"
        session.phase = PHASE_BITE
        if self._perf_enabled:
            self._perf.bite_events += 1
        if self._protocol_log:
            self.logger.info(
                f"[protocol] hooktime runtime={runtime_id} hook={hook_runtime_id} event={actor_event}"
            )

    # ------------------------------------------------------------------
    # Safety/movement. Reuse active PlayerAuthInput callbacks instead of adding a
    # global PlayerMoveEvent Python listener, which would create another hot bridge.
    # ------------------------------------------------------------------
    def _validate_motion(self, player: Player, session: FishingSession) -> bool:
        location = player.location
        if self._dimension_name(location) != session.dimension_name:
            self._remove_session(session.player_runtime_id, "离开了当前钓鱼维度")
            return False
        dx = location.x - session.anchor_x
        dz = location.z - session.anchor_z
        if dx * dx + dz * dz > self._movement_tolerance_sq:
            self._remove_session(session.player_runtime_id, "移动出了挂机位置")
            return False
        if abs(location.y - session.anchor_y) > self._vertical_tolerance:
            self._remove_session(session.player_runtime_id, "移动出了挂机位置")
            return False
        return True
