from __future__ import annotations
import inspect, json, sys, tempfile, types
from pathlib import Path

endstone=types.ModuleType('endstone'); command=types.ModuleType('endstone.command'); event=types.ModuleType('endstone.event'); plugin_mod=types.ModuleType('endstone.plugin')
class Event: pass
class Player: pass
class CommandSender: pass
class Command: pass
class Plugin: pass
endstone.Player=Player; command.Command=Command; command.CommandSender=CommandSender; plugin_mod.Plugin=Plugin
for n in ['ActorRemoveEvent','ActorSpawnEvent','PacketReceiveEvent','PacketSendEvent','PlayerDeathEvent','PlayerItemHeldEvent','PlayerJoinEvent','PlayerQuitEvent','PlayerRespawnEvent','PlayerTeleportEvent']:
    c=type(n,(Event,),{'NAME':n}); setattr(event,n,c)
def event_handler(fn):
    fn._is_event_handler=True; fn._priority=0; fn._ignore_cancelled=False; return fn
event.event_handler=event_handler
sys.modules.update({'endstone':endstone,'endstone.command':command,'endstone.event':event,'endstone.plugin':plugin_mod})
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'src'))
from endstone_autofishing.plugin import AutoFishingPlugin

# Every event annotation must be a concrete Event subclass, not a string.
handlers=[]
for name in dir(AutoFishingPlugin):
    fn=getattr(AutoFishingPlugin,name)
    if callable(fn) and getattr(fn,'_is_event_handler',False):
        sig=inspect.signature(fn)
        params=list(sig.parameters.values())
        assert len(params)==2, (name,sig)  # self + event on unbound method
        ann=params[1].annotation
        assert inspect.isclass(ann) and issubclass(ann,Event), (name,sig,ann)
        handlers.append(name)
assert 'on_packet_send' in handlers and 'on_packet_receive' in handlers
print('event registration annotations: OK', len(handlers))
